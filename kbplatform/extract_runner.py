"""Tier 2 提炼的并发执行器。

为什么需要独立线程：
  * `pipeline.run_once()` 的 Tier 1 循环把 index 队列抽干才轮到 Tier 2。
    新共享一次灌进 18.5 万篇后，提炼被饿了整整两天，大模型全程空转。
  * 提炼是 LLM 长调用（一块 ~30s），串行做的话池里其它成员（尤其远端）
    永远分不到并行任务。这里按池容量开线程：本地 1 条、远端 3~4 条。

线程与 sqlite：
  每个线程必须有自己的连接（`ThreadLocalConn`）。共用一条连接时，
  一个线程的 `commit()` 会重置另一个线程正在 fetch 的游标（踩过两次）。
"""
from __future__ import annotations

import threading
import time

from kbplatform import extract as extract_mod
from kbplatform.db import ThreadLocalConn
from kbplatform.jobs import JobQueue


class ExtractRunner:
    def __init__(self, db_path: str, pool, idle_sleep: float = 2.0, log=print,
                 skip_exts: list[str] | None = None):
        self.pool = pool
        self.idle_sleep = idle_sleep
        self.log = log
        self.skip_exts = list(skip_exts or [])
        self.conn = ThreadLocalConn(db_path)
        self.queue = JobQueue(self.conn)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.stats = {"extracted": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------ 生命周期
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def start(self) -> int:
        """按算力池容量开工。返回启动的线程数。"""
        n = max(0, self.pool.capacity())
        if n <= 0:
            self.log("[extract] 算力池没有可用成员，暂不启动提炼线程")
            return 0
        for i in range(n):
            t = threading.Thread(target=self._worker, name=f"extract-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        self.log(f"[extract] 已启动 {n} 条提炼线程（池容量 {n}）")
        return n

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    def take(self) -> dict:
        """取走并清零累计统计（跑一整轮后打印用）。"""
        with self._lock:
            out = dict(self.stats)
            self.stats = {"extracted": 0, "failed": 0, "skipped": 0}
        return out

    # ------------------------------------------------------------ 工作循环
    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def _worker(self) -> None:
        while not self._stop.is_set():
            member = self.pool.pick()
            if member is None:
                self._stop.wait(self.idle_sleep)
                continue
            job = self.queue.claim("extract")
            if job is None:
                self.pool.release(member)
                self._stop.wait(self.idle_sleep)
                continue

            handed_off = False
            try:
                row = self.conn.execute("SELECT path, ext FROM catalog WHERE id=?",
                                        (job.catalog_id,)).fetchone()
                if not row:
                    self.queue.finish(job.id)
                    continue
                if extract_mod.is_skipped_ext(row[1], self.skip_exts):
                    extract_mod.mark_extract_skipped(self.conn, job.catalog_id)
                    self._bump("skipped")
                    self.queue.finish(job.id)
                    continue
                chunk_ids = [r[0] for r in self.conn.execute(
                    "SELECT id FROM chunks WHERE file_path=?", (row[0],)).fetchall()]
                handed_off = True     # extract_file 内部 finally 负责归还成员
                res = extract_mod.extract_file(
                    self.conn, job.catalog_id, row[0], chunk_ids,
                    model="", pool=self.pool, member=member)
                self._bump("extracted" if res.ok else "failed")
                self.queue.finish(job.id, None if res.ok else (res.error or "提炼失败"))
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.log(f"[extract] 任务 #{job.id} 异常: {msg}")
                # 失败时立刻回滚。否则这条线程的连接会一直停在旧事务里：
                # 它持有的读快照会让 WAL 永远无法 checkpoint（实测 WAL 涨到 125GB
                # 把磁盘写满，web 打不开库），后续写入也会落在这个"脏事务"里。
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._bump("failed")
                self.queue.finish(job.id, msg)
            finally:
                if not handed_off:
                    self.pool.release(member)
