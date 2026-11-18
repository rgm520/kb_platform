"""摄入流水线编排：监控 -> 队列 -> Tier 1 索引 -> Tier 2 提炼。"""
from __future__ import annotations

import time

from kbplatform import catalog as cat
from kbplatform import fts
from kbplatform import indexer
from kbplatform import hashing
from kbplatform import auth, extract as extract_mod
from kbplatform import models as models_mod
from kbplatform.config import Config, DEFAULT_CONFIG_PATH
from kbplatform.db import (init_db, purge_expired_trace_data, upsert_catalog,
                           bump_stats, checkpoint_wal)
from kbplatform.jobs import JobQueue
from kbplatform.extract_runner import ExtractRunner
from kbplatform.monitor import Monitor
from kbplatform.paths import is_under_root
from kbplatform.vectors import VectorStore


class Pipeline:
    def __init__(self, config: Config):
        self.cfg = config
        # 流水线是**独立进程**（supervisor 起的 `python -m kbplatform.pipeline`），
        # 网页进程注入的主密钥不会跟过来。不在这里注入，就解不开存储源里
        # 加密保存的共享密码 → 带凭据的 SMB 共享（如 tmp_ppt）全解析失败。
        # 配置没写就先不动，别把别处已注入的密钥清掉。
        if config.session_secret:
            auth.set_encryption_key(config.session_secret, config.token_recoverable)
        self.conn = init_db(config.db_path)
        self.vs = VectorStore(config.vector_dir, dim=config.embed_dim)
        self.queue = JobQueue(self.conn)
        # 进程崩溃 / 被重启时，上一轮领到一半的任务会永远停在 running，
        # 没人回收就再也不会被处理。启动时先把它们放回队列。
        stale = self.queue.requeue_stale()
        if stale:
            print(f"[pipeline] 启动回收 {stale} 个卡在 running 的任务", flush=True)
        self.monitor = Monitor(self.conn, config.monitor_roots, config.drive_map,
                               qps=config.scan_qps,
                               poll_interval=config.poll_interval_sec)
        # 解析算力池：本地 + 远端多个大模型，按权重调度
        self.pool = models_mod.ParsePool(self.conn)
        # Tier 2 提炼的并发执行器：索引队列再长，提炼也照常出发
        # （2026-11-11 事故：Tier 1 抽干队列才轮到 Tier 2，提炼饿死两天）。
        self.runner = ExtractRunner(config.db_path, self.pool,
                                    skip_exts=config.extract_skip_exts,
                                    log=lambda msg: print(msg, flush=True))
        self._warm_snapshot()
        self._reconcile_pending()

    def _warm_snapshot(self) -> None:
        """用 catalog 里监控范围内的记录预热监控快照。

        监控快照在内存里，服务重启后会丢。必须回填**所有**监控范围内的
        已登记文件，不能只回填已完成的：否则 pending/排队中的文件每个
        扫描周期都会被当成新增重新入队，队列随轮次越积越多（实测 8 分钟
        涨 800 个）。

        同时**只预热 cfg.monitor_roots 之下的行**（UNC 归一后按目录边界
        比较，is_under_root 就是这么做的）：catalog 里还留着历史旧账——
        范围外的共享、1.0 引擎导出的 `_kb\\wiki\\…` 产物。这些行放快照里
        每次轮询都会被判成"源文件已删除"，反复生成删除变更。
        """
        roots = self.cfg.monitor_roots
        rows = self.conn.execute(
            "SELECT path, size, mtime FROM catalog").fetchall()
        for r in rows:
            if not is_under_root(r[0], roots):
                continue
            self.monitor._snapshot[r[0]] = (r[1] or 0, r[2] or 0.0)

    def _reconcile_pending(self) -> None:
        """给没有在途任务的 pending 文档补索引任务。

        任务行可能因历史 bug / 手工清理丢失，文档会永远停在 pending。
        enqueue 本身幂等，这里只补真正缺任务的。
        """
        rows = self.conn.execute(
            "SELECT c.id FROM catalog c WHERE c.tier1_done=0 "
            "AND c.status='pending' AND NOT EXISTS ("
            "  SELECT 1 FROM jobs j WHERE j.kind='index' AND j.catalog_id=c.id"
            "  AND j.state IN ('queued','running'))").fetchall()
        for (cid,) in rows:
            self.queue.enqueue("index", cid)
        if rows:
            print(f"[pipeline] 启动补队 {len(rows)} 个待索引文档", flush=True)

    # ------------------------------------------------------------ 单轮
    def run_once(self) -> dict:
        """同步跑完一轮摄入。返回统计。"""
        stats = {"indexed": 0, "extracted": 0, "removed": 0, "failed": 0,
                 "scanned": 0, "pending": 0}

        # 1) 扫描监控根，写 catalog
        for root in self.cfg.monitor_roots:
            try:
                recs, non_doc = cat.scan_root(root, self.cfg.drive_map,
                                              self.cfg.monitor_roots,
                                              qps=self.cfg.scan_qps)
            except Exception as e:
                print(f"[pipeline] 扫描 {root} 失败: {type(e).__name__}: {e}", flush=True)
                continue
            for fr in recs:
                upsert_catalog(self.conn, fr)
            if non_doc:
                share, _ = cat.split_share(root, self.cfg.monitor_roots)
                # 2.0 扫描器接管统计的共享由它写"当前数量"，这里不再累加
                # （否则两套口径叠加，数字每轮翻倍）
                owned = self.conn.execute(
                    "SELECT 1 FROM catalog_stats WHERE share=? LIMIT 1", (share,)).fetchone()
                if owned is None:
                    bump_stats(self.conn, share, non_doc)
            stats["scanned"] += len(recs)

        # 2) 变更检测
        try:
            changes = self.monitor.poll_once()
        except Exception as e:
            print(f"[pipeline] 轮询失败: {type(e).__name__}: {e}", flush=True)
            changes = []

        # 3) 变更 -> 任务
        for ch in changes:
            row = self.conn.execute("SELECT id FROM catalog WHERE path=?",
                                     (ch.path,)).fetchone()
            if not row:
                continue
            if ch.kind == "removed":
                indexer.remove_file(self.conn, self.cfg.fts_path, self.vs, row[0])
                stats["removed"] += 1
            else:
                self.queue.enqueue("index", row[0])

        # 4) 执行 Tier 1（只领 index 任务）
        #    OCR 任务（index_ocr）优先级更低：扫描件一页 136 秒，放在普通解析后面，
        #    免得一个扫描件把整条解析队列堵住。
        while True:
            job = self.queue.claim("index")
            if job is None:
                job = self.queue.claim("index_ocr")
            if job is None:
                break
            row = self.conn.execute("SELECT path, ext FROM catalog WHERE id=?",
                                     (job.catalog_id,)).fetchone()
            if row:
                res = indexer.index_file(self.conn, self.cfg.fts_path, self.vs,
                                         job.catalog_id, row[0],
                                         embed_model=self.cfg.embed_model)
                if res.ok:
                    stats["indexed"] += 1
                    # 表格类只索引不提炼（块数占全局 81%，行数据对问答价值低）
                    if not extract_mod.is_skipped_ext(row[1], self.cfg.extract_skip_exts):
                        self.queue.enqueue("extract", job.catalog_id)
                else:
                    stats["failed"] += 1
            self.queue.finish(job.id)

        # 5) 执行 Tier 2（只领 extract 任务）
        #    常驻运行时提炼由 ExtractRunner 的后台线程并发消费（见 run_forever）；
        #    只有 `--once` / 单测这种没有后台线程的场景才在这里同步做一轮。
        if not self.runner.running():
            while True:
                job = self.queue.claim("extract")
                if job is None:
                    break
                row = self.conn.execute("SELECT path, ext FROM catalog WHERE id=?",
                                         (job.catalog_id,)).fetchone()
                if row:
                    if extract_mod.is_skipped_ext(row[1], self.cfg.extract_skip_exts):
                        extract_mod.mark_extract_skipped(self.conn, job.catalog_id)
                        stats["skipped"] = stats.get("skipped", 0) + 1
                    else:
                        chunk_ids = [r[0] for r in self.conn.execute(
                            "SELECT id FROM chunks WHERE file_path=?", (row[0],)).fetchall()]
                        # 有可用池成员就走算力池（本地+远端分担）；池空则退回单模型
                        if self.pool.members():
                            res = extract_mod.extract_file(self.conn, job.catalog_id, row[0],
                                                           chunk_ids, model="", pool=self.pool)
                        else:
                            res = extract_mod.extract_file(self.conn, job.catalog_id, row[0],
                                                           chunk_ids, model=self.cfg.llm_model)
                        if res.ok:
                            stats["extracted"] += 1
                        else:
                            stats["failed"] += 1
                self.queue.finish(job.id)

        stats["pending"] = self.queue.pending_count()

        # 5.5) 补算重复文件用的 MD5（每轮少量，别抢解析资源）
        try:
            n = hashing.hash_pending(self.conn, limit=30)
            if n:
                stats["hashed"] = stats.get("hashed", 0) + n
        except Exception as e:
            print(f"[pipeline] 计算 MD5 失败: {type(e).__name__}: {e}", flush=True)

        # 6) 清理孤立节点与边，重算提及次数
        try:
            import kb.graph as pgraph
            pgraph.prune_orphans(self.conn)
            pgraph.recount_mentions(self.conn)
        except Exception as e:
            print(f"[pipeline] 图谱清理失败: {type(e).__name__}: {e}", flush=True)

        # 7) 观测数据保留期（spec §3 A1：轨迹/反馈保留 30 天）。
        #    放在**既有**周期任务里清，不依赖任何新调度器；DELETE 走
        #    created_at 索引，到期行少时可忽略开销。失败只记日志——
        #    清理不应把摄入流水线的本轮判为失败。
        try:
            n = purge_expired_trace_data(self.conn)
            if n:
                stats["purged"] = n
        except Exception as e:
            print(f"[pipeline] 过期轨迹清理失败: {type(e).__name__}: {e}", flush=True)
        return stats

    # ------------------------------------------------------------ 循环
    def run_forever(self) -> None:
        print(f"[pipeline] 开始监控 {self.cfg.monitor_roots} "
              f"（每 {self.cfg.poll_interval_sec}s 一轮）", flush=True)
        self.runner.start()
        try:
            while True:
                try:
                    stats = self.run_once()
                    done = self.runner.take()
                    stats["extracted"] += done["extracted"]
                    stats["failed"] += done["failed"]
                    if done["skipped"]:
                        stats["skipped"] = stats.get("skipped", 0) + done["skipped"]
                    if (stats["indexed"] or stats["extracted"] or stats["removed"]
                            or stats.get("skipped")):
                        print(f"[pipeline] {stats}", flush=True)
                except Exception as e:
                    print(f"[pipeline] 本轮失败: {type(e).__name__}: {e}", flush=True)
                    # 本轮异常可能把连接留在未提交事务里，读快照会钉住 WAL，
                    # 和提炼线程同样处理：先回滚，再进下一轮。
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                # WAL 兜底：超过阈值主动 checkpoint，避免 checkpoint 长期挨饿
                # 把 WAL 堆到把磁盘写满（2026-11-17 故障）。
                try:
                    checkpoint_wal(self.conn, self.cfg.db_path)
                except Exception as e:
                    print(f"[pipeline] WAL checkpoint 异常: {type(e).__name__}: {e}",
                          flush=True)
                time.sleep(self.cfg.poll_interval_sec)
        finally:
            self.runner.stop()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from kbplatform.config import load_config
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    p = Pipeline(load_config(cfg_path))
    if "--once" in sys.argv:
        print(p.run_once())
    else:
        p.run_forever()
