"""任务队列。

claim() 用一条 UPDATE 原子取任务，避免多 worker 抢同一个任务。
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Job:
    id: int
    kind: str          # index | extract
    catalog_id: int
    state: str
    error: str | None


class JobQueue:
    def __init__(self, conn):
        self.conn = conn

    def enqueue(self, kind: str, catalog_id: int, force: bool = False) -> int:
        """入队；同一 (kind, catalog_id) 已有在途任务时复用，不重复插入。

        扫描器每个周期都可能把同一文件再报一次（重启后快照丢失尤其明显），
        若不去重，队列会随扫描轮次无限膨胀。

        force=True 用于覆盖上传/恢复后强制再解析：已有 queued 仍复用；
        已有 running 时再排一个 queued，保证在跑的旧任务结束后还有一次新解析。
        """
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE kind=? AND catalog_id=? "
            "AND state='queued' ORDER BY id LIMIT 1",
            (kind, catalog_id)).fetchone()
        if row:
            return int(row[0])
        if not force:
            row = self.conn.execute(
                "SELECT id FROM jobs WHERE kind=? AND catalog_id=? "
                "AND state='running' ORDER BY id LIMIT 1",
                (kind, catalog_id)).fetchone()
            if row:
                return int(row[0])
        cur = self.conn.execute(
            "INSERT INTO jobs(kind, catalog_id, state, attempts, created_at)"
            " VALUES (?,?,'queued',0,?)",
            (kind, catalog_id, time.time()))
        self.conn.commit()
        return cur.lastrowid

    def claim(self, kind: str | None = None) -> Job | None:
        """原子取一个任务。

        kind 过滤是必需的：Tier 1 完成后会立刻入队 Tier 2 的任务，
        若不按类型过滤，Tier 2 任务会被 Tier 1 的循环领走而跳过执行。

        **必须是单条 UPDATE ... RETURNING**：Tier 2 现在是多线程消费
        （每个池成员一个线程），「先 SELECT 再 UPDATE」会让两个线程同时
        选中同一行，同一份文档被重复提炼（实测 200 个任务被领了 205 次）。
        """
        cond = "state='queued'"
        params: tuple = (time.time(),)
        if kind:
            cond += " AND kind=?"
            params += (kind,)
        cur = self.conn.execute(
            f"UPDATE jobs SET state='running', started_at=?, attempts=attempts+1 "
            f"WHERE id=(SELECT id FROM jobs WHERE {cond} ORDER BY id LIMIT 1) "
            f"RETURNING id, kind, catalog_id, state, error", params)
        row = cur.fetchone()
        self.conn.commit()
        if not row:
            return None
        return Job(row[0], row[1], row[2], row[3], row[4])

    def release(self, job_id: int) -> None:
        """把领到但还没开始做的任务原样放回队列（例如算力池暂无空闲成员）。"""
        self.conn.execute(
            "UPDATE jobs SET state='queued', started_at=NULL WHERE id=? AND state='running'",
            (job_id,))
        self.conn.commit()

    def finish(self, job_id: int, error: str | None = None) -> None:
        if error:
            self.conn.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=? WHERE id=?",
                (error, time.time(), job_id))
        else:
            self.conn.execute(
                "UPDATE jobs SET state='done', error=NULL, finished_at=? WHERE id=?",
                (time.time(), job_id))
        self.conn.commit()

    def requeue_stale(self) -> int:
        """把卡在 running 的任务（进程崩了）放回队列。"""
        cur = self.conn.execute(
            "UPDATE jobs SET state='queued' WHERE state='running'")
        self.conn.commit()
        return cur.rowcount

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
