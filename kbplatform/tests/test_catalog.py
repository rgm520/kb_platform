import pytest

from kbplatform.catalog import PARSEABLE_EXT, scan_root
from kbplatform.db import init_db, upsert_catalog, set_status, get_by_path


def _mk(tmp_path, name, content=b"x"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_parsetable_whitelist_is_exact():
    assert PARSEABLE_EXT == {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx",
                             ".doc", ".xls", ".txt", ".md", ".ofd"}


def test_scan_root_returns_only_parseable_files(tmp_path):
    """Review Focus #4: 非文档文件不进 catalog。"""
    _mk(tmp_path, "a.pdf")
    _mk(tmp_path, "b.docx")
    _mk(tmp_path, "c.jpg")
    _mk(tmp_path, "d.zip")
    recs, stats = scan_root(str(tmp_path), {}, [], qps=1000)
    assert sorted(r.ext for r in recs) == [".docx", ".pdf"]
    assert stats == {".jpg": 1, ".zip": 1}


def test_scan_root_skips_recycle_hidden_and_kb_output(tmp_path):
    _mk(tmp_path, "a.pdf")
    _mk(tmp_path, "#recycle/b.pdf")
    _mk(tmp_path, ".hidden/c.pdf")
    _mk(tmp_path, "_kb/knowledge.jsonl")
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    assert [r.name for r in recs] == ["a.pdf"]


def test_scan_root_records_size_and_mtime(tmp_path):
    p = _mk(tmp_path, "a.pdf", b"hello")
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    assert recs[0].size == 5
    assert recs[0].mtime == pytest.approx(p.stat().st_mtime, abs=1.0)


def test_upsert_catalog_is_idempotent(tmp_path):
    _mk(tmp_path, "a.pdf")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    i1 = upsert_catalog(conn, recs[0])
    i2 = upsert_catalog(conn, recs[0])
    assert i1 == i2
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 1


def test_upsert_catalog_preserves_progress_flags(tmp_path):
    """重复扫描不得把 tier1/tier2 进度清掉。"""
    _mk(tmp_path, "a.pdf")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    conn.execute("UPDATE catalog SET tier1_done=1, tier2_done=1, status='done' WHERE id=?",
                 (cid,))
    conn.commit()
    upsert_catalog(conn, recs[0])
    row = conn.execute("SELECT tier1_done, tier2_done, status FROM catalog WHERE id=?",
                        (cid,)).fetchone()
    assert row[0] == 1 and row[1] == 1 and row[2] == "done"


def test_set_status_records_detail(tmp_path):
    _mk(tmp_path, "a.pdf")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    set_status(conn, cid, "failed", "文件被独占，无法打开")
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "failed"
    assert "独占" in row[1]


def test_get_by_path_finds_row(tmp_path):
    _mk(tmp_path, "a.pdf")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    upsert_catalog(conn, recs[0])
    got = get_by_path(conn, recs[0].path)
    assert got is not None
    assert got["name"] == recs[0].name
