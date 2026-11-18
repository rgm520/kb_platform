"""多线程并发领取任务：同一个 job 不能被两个线程同时领到。

为什么重要：Tier 2 改成多线程提炼后，claim 若仍是「先 SELECT 再 UPDATE」，
两个线程可能同时选中同一行（CPython 只在字节码之间切换），结果同一份
文档被重复提炼：算力白烧，knowledge / edges 还会重复写入。
"""
import sys
import threading

from kbplatform.db import connect, init_db
from kbplatform.jobs import JobQueue


def _seed(conn, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO jobs(kind, catalog_id, state, attempts, created_at)"
            " VALUES ('extract', ?, 'queued', 0, 0)", (i,))
    conn.commit()


def test_concurrent_claims_never_duplicate(tmp_path):
    db = str(tmp_path / "kb.db")
    init_db(db).close()
    conn = connect(db)
    _seed(conn, 200)
    conn.close()

    seen = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)
    old_interval = sys.getswitchinterval()
    # 把切换间隔压到极小，让「SELECT 之后、UPDATE 之前」被切走的概率接近必然，
    # 从而稳定复现旧实现的竞态（原子化之后这条设置不再影响结果）。
    sys.setswitchinterval(1e-6)

    def worker():
        c = connect(db)
        q = JobQueue(c)
        local = []
        barrier.wait()
        while True:
            job = q.claim("extract")
            if job is None:
                break
            local.append(job.id)
        with lock:
            seen.extend(local)
        c.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        sys.setswitchinterval(old_interval)

    assert len(seen) == 200, f"应领到全部 200 个任务，实际 {len(seen)}"
    dup = len(seen) - len(set(seen))
    assert dup == 0, f"有 {dup} 个任务被重复领取"


def test_release_puts_job_back(tmp_path):
    """取到任务后若发现没有可用模型，要能原样放回队列（不丢任务）。"""
    db = str(tmp_path / "kb.db")
    conn = init_db(db)
    _seed(conn, 1)
    q = JobQueue(conn)
    job = q.claim("extract")
    assert job is not None
    q.release(job.id)
    again = q.claim("extract")
    assert again is not None and again.id == job.id
