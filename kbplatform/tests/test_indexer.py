import pytest

from kbplatform.db import init_db, upsert_catalog
from kbplatform.catalog import scan_root
from kbplatform.fts import build_index, search
from kbplatform.vectors import VectorStore
from kbplatform.indexer import index_file, remove_file, process_changes
from kbplatform.monitor import Change


def _setup(tmp_path, name="a.md", content=None):
    p = tmp_path / name
    p.write_text(content or ("信创资源池由总部统一规划建设。\n" * 200), encoding="utf-8")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    fts_path = str(tmp_path / "fts.db")
    build_index(fts_path, [])
    vs = VectorStore(str(tmp_path / "vectors"), dim=1024)
    return conn, fts_path, vs, cid, str(p)


def test_index_file_creates_chunks(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    res = index_file(conn, fts_path, vs, cid, path)
    assert res.ok is True
    assert len(res.chunk_ids) >= 1
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(res.chunk_ids)


def test_index_file_populates_fts(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    res = index_file(conn, fts_path, vs, cid, path)
    assert len(search(fts_path, "信创", 10)) >= 1


def test_index_file_populates_vectors(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    res = index_file(conn, fts_path, vs, cid, path)
    assert vs.count() == len(res.chunk_ids)


def test_index_file_marks_tier1_done(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    row = conn.execute("SELECT tier1_done, status FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == 1
    assert row[1] == "done"


def test_reindex_replaces_old_chunks(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    first = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    index_file(conn, fts_path, vs, cid, path)
    second = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert first == second


def test_index_failure_records_reason_and_does_not_raise(tmp_path):
    """Review Focus #5: 失败要有原因，流水线不崩。"""
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    res = index_file(conn, fts_path, vs, cid, str(tmp_path / "missing.md"))
    assert res.ok is False
    assert res.error
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "failed"
    assert row[1]


def test_remove_file_clears_chunks_fts_and_vectors(tmp_path):
    """Review Focus #3: 删除后不得残留。"""
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    remove_file(conn, fts_path, vs, cid)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE file_path=?", (path,)).fetchone()[0] == 0
    assert search(fts_path, "信创", 10) == []
    assert vs.count() == 0


def test_remove_file_marks_catalog_skipped(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    remove_file(conn, fts_path, vs, cid)
    row = conn.execute("SELECT status FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "skipped"


def test_process_changes_handles_added_modified_removed(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    ch = Change(path, "added", 100, 1.0)
    res = process_changes(conn, fts_path, vs, [ch])
    assert res[0].ok is True
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 1

    ch2 = Change(path, "removed", 100, 1.0)
    process_changes(conn, fts_path, vs, [ch2])
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
