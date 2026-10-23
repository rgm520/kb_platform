import time

from kbplatform.config import Config
from kbplatform.scanner import SourceScanner


def _seed_source(conn, root):
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('测试共享','local',?,1,1,?)", (str(root), time.time()))
    conn.commit()
    return conn.execute("SELECT id FROM storage_sources").fetchone()[0]


def test_scan_enqueues_parseable_and_indexes_paths(tmp_path, conn):
    share = tmp_path / "share"
    (share / "制度").mkdir(parents=True)
    (share / "制度" / "考勤办法.docx").write_text("x", encoding="utf-8")
    (share / "制度" / "logo.png").write_bytes(b"png")
    sid = _seed_source(conn, share)
    sc = SourceScanner(conn, Config(data_dir=str(tmp_path / "data")), interval=0)
    r = sc.scan_now(sid)
    assert r["files"] == 2
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM catalog WHERE name='考勤办法.docx'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index'").fetchone()[0] == 1
    sc.scan_now(sid)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index'").fetchone()[0] == 1


def test_modify_requeues_and_resets_status(tmp_path, conn):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text("正文" * 50, encoding="utf-8")
    sid = _seed_source(conn, share)
    sc = SourceScanner(conn, Config(data_dir=str(tmp_path / "data")), interval=0)
    sc.scan_now(sid)
    cid = conn.execute("SELECT id FROM catalog WHERE name='a.md'").fetchone()[0]
    conn.execute("UPDATE jobs SET state='done'")
    conn.execute("UPDATE catalog SET status='done', tier1_done=1 WHERE id=?", (cid,))
    conn.commit()
    time.sleep(0.02)
    (share / "a.md").write_text("改过的正文" * 60, encoding="utf-8")
    sc.scan_now(sid)
    row = conn.execute("SELECT status, tier1_done FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "pending" and row["tier1_done"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0] == 1


def test_nas_side_delete_clears_index_but_keeps_no_trash(tmp_path, conn):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text("正文" * 50, encoding="utf-8")
    sid = _seed_source(conn, share)
    sc = SourceScanner(conn, Config(data_dir=str(tmp_path / "data")), interval=0)
    sc.scan_now(sid)
    (share / "a.md").unlink()
    r = sc.scan_now(sid)
    assert r["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM catalog WHERE name='a.md'").fetchone()[0] == "skipped"
