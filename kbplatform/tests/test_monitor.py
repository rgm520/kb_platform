import time

import pytest

from kbplatform.db import init_db
from kbplatform.monitor import Monitor
from kbplatform.jobs import JobQueue


def _mk(d, name, content=b"x"):
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_added_file_detected_on_next_poll(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["added"]
    assert changes[0].path.endswith("a.pdf")


def test_unchanged_file_produces_no_change(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    m.poll_once()
    assert m.poll_once() == []


def test_modified_file_detected_by_size_or_mtime(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    p = _mk(tmp_path, "a.pdf", b"a")
    m.poll_once()
    time.sleep(1.1)
    p.write_bytes(b"bbbb")
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["modified"]


def test_removed_file_detected(tmp_path):
    """Review Focus #3: 删除必须被发现，后续由 indexer 清理索引。"""
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    p = _mk(tmp_path, "a.pdf")
    m.poll_once()
    p.unlink()
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["removed"]


def test_multiple_changes_reported_together(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    m.poll_once()
    _mk(tmp_path, "b.pdf")
    _mk(tmp_path, "c.pdf")
    kinds = sorted(c.kind for c in m.poll_once())
    assert kinds == ["added", "added"]


def test_change_paths_follow_drive_map(tmp_path):
    """监控器上报的路径必须与扫描器一致，且带 drive_map 映射时映射生效。

    UNC 前缀的转换本身由 test_paths.py 覆盖；这里验证映射表被真正使用。
    """
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    p = m.poll_once()[0].path
    assert p.endswith("a.pdf")
    assert p.startswith(str(tmp_path))

    # 用一个自定义盘符作为根，drive_map 把它映射到真实目录
    conn2 = init_db(str(tmp_path / "kb2.db"))
    m2 = Monitor(conn2, ["Z:"], {"Z:": str(tmp_path)}, qps=1000)
    p2 = m2.poll_once()[0].path
    assert p2.endswith("a.pdf")
    assert p2.startswith(str(tmp_path))
    assert "Z:" not in p2


def test_job_queue_claim_and_finish(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    jid = q.enqueue("index", 42)
    job = q.claim()
    assert job.id == jid and job.kind == "index" and job.catalog_id == 42
    q.finish(jid)
    assert q.claim() is None
    assert q.pending_count() == 0


def test_job_queue_marks_failure(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    jid = q.enqueue("index", 7)
    q.claim()
    q.finish(jid, error="文件被独占")
    row = conn.execute("SELECT state, error FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert "独占" in row[1]


def test_job_queue_claims_in_fifo_order(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    a = q.enqueue("index", 1)
    b = q.enqueue("index", 2)
    assert q.claim().id == a
    assert q.claim().id == b


def test_requeue_stale_returns_running_jobs(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    q.enqueue("index", 3)
    q.claim()
    assert q.requeue_stale() == 1
    assert q.claim() is not None
