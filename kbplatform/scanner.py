"""存储源扫描编排。

职责边界：
  * 路径索引（fs_entries/fs_fts）由 `fsindex` 做；
  * 本模块只负责「什么时候扫哪个源」以及「新/变更的可解析文件如何进
    catalog + jobs」；
  * 真正的解析仍由现有 pipeline 消费 jobs 完成，扫描线程不阻塞在解析上。

NAS 侧直接删除的文件只清索引，不进平台回收站（平台回收站只记录网页删除）。
"""
from __future__ import annotations

import threading
import time

from kbplatform import auth, fsindex, indexer
from kbplatform.catalog import FileRecord
from kbplatform.config import Config
from kbplatform.db import upsert_catalog
from kbplatform.jobs import JobQueue
from kbplatform.paths import norm_unc, split_share
from kbplatform.storage import adapter_for, source_from_row
from kbplatform.vectors import VectorStore


def index_scope_for(cfg: Config, source) -> tuple[bool, str]:
    """这个存储源的**索引/解析范围**（相对 source.root 的子目录）。

    浏览范围是整个存储源（管理员能看到共享里的全部文件夹），
    但**只解析配置里监控的那部分**（[monitor].roots）——
    否则一个共享里十几万文件会被全量解析，把队列和算力吃干。

    返回 (是否要索引, 相对子目录)；不在监控范围 → (False, "")。
    """
    root = norm_unc(source.root).rstrip("\\")
    roots = [m for m in (cfg.monitor_roots or []) if m]
    if not roots:
        # 没配监控根 = 这是个纯 2.0 部署，整个源都索引
        return True, ""
    best = None
    for m in roots:
        m = norm_unc(m).rstrip("\\")
        if not m:
            continue
        if m.lower() == root.lower():
            return True, ""                       # 整个源就是监控根
        if m.lower().startswith(root.lower() + "\\"):
            sub = m[len(root) + 1:]
            if best is None or len(sub) < len(best):
                best = sub
    if best is None:
        return False, ""
    return True, best


class SourceScanner:
    def __init__(self, conn, cfg: Config, interval: int | None = None):
        self.conn = conn
        self.cfg = cfg
        self.interval = cfg.source_scan_interval_sec if interval is None else interval
        self.queue = JobQueue(conn)
        self.vs = VectorStore(cfg.vector_dir, dim=cfg.embed_dim)
        self._requested: set[int] = set()
        self._req_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._progress: dict[int, int] = {}

    # ------------------------------------------------------------ 单源扫描
    def scan_now(self, source_id: int) -> dict:
        sid = int(source_id)
        row = self.conn.execute("SELECT * FROM storage_sources WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError(f"存储源不存在: {sid}")
        source = source_from_row(row)
        if not source.enabled:
            return {"skipped": "disabled"}
        with self._scan_lock:
            self._progress[sid] = 0
            self.conn.execute(
                "UPDATE storage_sources SET scan_state='scanning', scan_error=NULL WHERE id=?",
                (sid,))
            self.conn.commit()
            try:
                password = auth.decrypt_secret(row["secret_enc"]) if row["secret_enc"] else None
                adapter = adapter_for(source, password)
                want, scope = index_scope_for(self.cfg, source)
                if not want:
                    # 这个共享不在监控范围里：只浏览，不索引不解析
                    self.conn.execute(
                        "UPDATE storage_sources SET scan_state='idle', scan_error=? WHERE id=?",
                        ("该共享不在配置的监控范围内（[monitor].roots），仅浏览不解析", sid))
                    self.conn.commit()
                    return {"skipped": "不在监控范围"}
                stats = fsindex.scan_source(self.conn, source, adapter,
                                            qps=self.cfg.scan_qps, scope=scope)
                self._progress[sid] = stats["files"] + stats["dirs"]
                enqueued = self._sync_catalog(source, stats)
                self.conn.execute(
                    "UPDATE storage_sources SET scan_state='idle', last_scan_at=? WHERE id=?",
                    (time.time(), sid))
                self.conn.commit()
                stats["catalog_enqueued"] = enqueued
                return stats
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.conn.execute(
                    "UPDATE storage_sources SET scan_state='error', scan_error=?, last_scan_at=? "
                    "WHERE id=?", (msg, time.time(), sid))
                self.conn.commit()
                return {"error": msg}
            finally:
                self._progress.pop(sid, None)

    def _sync_catalog(self, source, stats: dict) -> int:
        share = split_share(source.root)[0] or source.name
        n = 0
        for rel in stats.get("changed_parseable", []):
            row = self.conn.execute(
                "SELECT * FROM fs_entries WHERE source_id=? AND rel_key=?",
                (source.id, fsindex.rel_key(rel))).fetchone()
            if row is None or row["is_dir"]:
                continue
            path = fsindex.entry_path(source, row["rel_path"])
            old = self.conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()
            fr = FileRecord(path=path, share=share, rel_path=row["rel_path"],
                            name=row["name"], ext=row["ext"] or "",
                            size=row["size"] or 0, mtime=row["mtime"] or 0.0)
            cid = upsert_catalog(self.conn, fr)
            if old:
                indexer.clear_artifacts(self.conn, self.cfg.fts_path, self.vs, path)
                self.conn.execute(
                    "UPDATE catalog SET status='pending', status_detail=NULL, "
                    "tier1_done=0, tier2_done=0 WHERE id=?", (cid,))
                self.conn.commit()
            self.conn.execute("UPDATE fs_entries SET catalog_id=? WHERE id=?",
                              (cid, row["id"]))
            self.conn.commit()
            self.queue.enqueue("index", cid, force=bool(old))
            n += 1
        for rel in stats.get("removed_rels", []):
            path = fsindex.entry_path(source, rel)
            row = self.conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()
            if row:
                indexer.remove_file(self.conn, self.cfg.fts_path, self.vs, int(row[0]))
        return n

    # ------------------------------------------------------------ 后台线程
    def trigger(self, source_id: int | None = None) -> None:
        with self._req_lock:
            if source_id is None:
                self._requested.add(-1)
            else:
                self._requested.add(int(source_id))
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kb-source-scanner",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _enabled_ids(self) -> list[int]:
        return [int(r[0]) for r in self.conn.execute(
            "SELECT id FROM storage_sources WHERE enabled=1 ORDER BY id")]

    def _loop(self) -> None:
        # 启动先全量扫一遍，随后按间隔轮询；trigger 可提前唤醒。
        self._scan_ids(self._enabled_ids())
        while not self._stop.is_set():
            self._wake.wait(timeout=max(1, self.interval))
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._req_lock:
                requested = set(self._requested)
                self._requested.clear()
            if -1 in requested:
                ids = self._enabled_ids()
            else:
                ids = sorted(requested) or self._enabled_ids()
            self._scan_ids(ids)

    def _scan_ids(self, ids: list[int]) -> None:
        for sid in ids:
            if self._stop.is_set():
                return
            try:
                self.scan_now(sid)
            except Exception:
                # scan_now 已把错误写进 scan_state；这里只保证线程不退出
                pass

    def status(self, source_id: int | None = None):
        rows = self.conn.execute(
            "SELECT id,name,scan_state,scan_error,last_scan_at FROM storage_sources "
            "ORDER BY id").fetchall()
        items = [{"id": r["id"], "name": r["name"], "state": r["scan_state"],
                  "error": r["scan_error"], "last_scan_at": r["last_scan_at"],
                  "progress": self._progress.get(int(r["id"]), 0)} for r in rows]
        if source_id is not None:
            for it in items:
                if it["id"] == int(source_id):
                    return it
            return None
        return items
