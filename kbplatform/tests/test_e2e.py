import pytest

from kbplatform.config import Config
from kbplatform.pipeline import Pipeline


FAKE = {"summary": "s",
        "key_points": [{"title": "t", "detail": "d", "entities": ["信创"], "category": "标准"}],
        "relations": []}


def _cfg(tmp_path):
    return Config(monitor_roots=[str(tmp_path)],
                  data_dir=str(tmp_path / "data"),
                  poll_interval_sec=1, scan_qps=1000)


def test_pipeline_processes_new_file_end_to_end(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    (tmp_path / "a.md").write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))

    p = Pipeline(_cfg(tmp_path))
    stats = p.run_once()
    assert stats["indexed"] == 1
    assert stats["extracted"] == 1
    assert stats["failed"] == 0


def test_pipeline_requeues_stale_running_jobs_on_startup(tmp_path):
    """重启后，上次领了没做完的任务必须放回队列，否则那些文档永远不再处理。"""
    p = Pipeline(_cfg(tmp_path))
    p.conn.execute("INSERT INTO jobs(kind,catalog_id,state,created_at,attempts) "
                   "VALUES ('index', NULL, 'running', 0, 1)")
    p.conn.commit()

    p2 = Pipeline(_cfg(tmp_path))
    assert p2.conn.execute("SELECT state FROM jobs").fetchone()[0] == "queued"


def test_pipeline_marks_catalog_done_with_tier_flags(tmp_path, monkeypatch):
    import kb.llm as llm
    (tmp_path / "a.md").write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    p = Pipeline(_cfg(tmp_path))
    p.run_once()
    row = p.conn.execute(
        "SELECT status, tier1_done, tier2_done, chunk_count, kp_count FROM catalog").fetchone()
    assert row[0] == "done"
    assert row[1] == 1 and row[2] == 1
    assert row[3] >= 1
    assert row[4] >= 1


def test_pipeline_is_idempotent_on_second_run(tmp_path, monkeypatch):
    import kb.llm as llm
    (tmp_path / "a.md").write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    p = Pipeline(_cfg(tmp_path))
    p.run_once()
    n1 = p.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    stats = p.run_once()
    n2 = p.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n1 == n2
    assert stats["indexed"] == 0


def test_pipeline_handles_removed_file(tmp_path, monkeypatch):
    import kb.llm as llm
    f = tmp_path / "a.md"
    f.write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    p = Pipeline(_cfg(tmp_path))
    p.run_once()
    assert p.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 1
    f.unlink()
    stats = p.run_once()
    assert stats["removed"] == 1
    assert p.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_pipeline_survives_unreadable_file(tmp_path, monkeypatch):
    """Review Focus #5: 单个文件失败不能让整轮崩掉，且原因要落库。"""
    import kb.llm as llm
    (tmp_path / "a.md").write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"not a real pdf at all")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    p = Pipeline(_cfg(tmp_path))
    stats = p.run_once()
    assert stats["indexed"] == 1          # 好文件正常处理
    assert stats["failed"] == 1           # 坏文件标记失败
    bad = p.conn.execute(
        "SELECT status, status_detail FROM catalog WHERE ext='.pdf'").fetchone()
    assert bad[0] == "failed"
    assert bad[1]
