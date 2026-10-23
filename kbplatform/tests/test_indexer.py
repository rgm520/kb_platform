import os
import time

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


def test_reindex_does_not_leave_orphan_fts_rows(tmp_path):
    """重跑索引必须清掉旧块的 FTS 行。

    旧块 id 被删除后，FTS 里若还留着这些 rowid，检索会把它们召回，
    再被 JOIN catalog 丢掉 —— 白占 top-k 名额，真命中被挤掉。
    """
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    old_ids = [r[0] for r in conn.execute("SELECT id FROM chunks").fetchall()]
    assert old_ids

    index_file(conn, fts_path, vs, cid, path)

    new_ids = {r[0] for r in conn.execute("SELECT id FROM chunks").fetchall()}
    assert new_ids.isdisjoint(old_ids), "重索引应分配新的 chunk id"

    hits = search(fts_path, "信创", 50)
    assert hits, "重索引后应仍能检索到内容"
    orphan = [cid_ for cid_, _ in hits if cid_ not in new_ids]
    assert orphan == [], f"FTS 残留孤儿行: {orphan}"


def test_backlog_scan_pdf_is_skipped_without_ocr(tmp_path, monkeypatch):
    """存量扫描件不走 OCR（136 秒/页会堵死队列），标 skipped + needs_ocr。"""
    import kbplatform.indexer as ix

    conn, fts_path, vs, cid, path = _setup(tmp_path, "a.pdf")
    monkeypatch.setattr(ix.parsers, "parse_file",
                        lambda *a, **k: ([], {"need_ocr": True}))
    called = []
    monkeypatch.setattr(ix, "ocr_pdf", lambda *a, **k: called.append(1) or None)
    monkeypatch.setattr(ix, "is_ocr_backlog", lambda p: True)

    res = ix.index_file(conn, fts_path, vs, cid, path)
    assert res.ok is False
    row = conn.execute("SELECT status, needs_ocr FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "skipped" and row[1] == 1
    assert called == [], "存量扫描件不应触发 OCR"


def test_new_scan_pdf_still_goes_through_ocr(tmp_path, monkeypatch):
    """新文件仍然自动 OCR（spec D9：监控范围内新文件自动处理）。"""
    import kbplatform.indexer as ix

    conn, fts_path, vs, cid, path = _setup(tmp_path, "a.pdf")
    monkeypatch.setattr(ix.parsers, "parse_file",
                        lambda *a, **k: ([], {"need_ocr": True}))
    monkeypatch.setattr(ix, "is_ocr_backlog", lambda p: False)
    called = []
    monkeypatch.setattr(ix, "ocr_pdf",
                        lambda *a, **k: called.append(1) or path)

    ix.index_file(conn, fts_path, vs, cid, path)
    assert called == [1]


def test_reindex_does_not_leave_orphan_knowledge(tmp_path):

    """重跑索引必须清掉旧块的 knowledge / edges，否则 knowledge 行数会多于 chunks。"""
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    index_file(conn, fts_path, vs, cid, path)
    old_chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks").fetchall()]
    marks = ",".join("?" * len(old_chunk_ids))
    conn.execute(f"INSERT INTO knowledge(file_path, chunk_id, summary, key_points, relations, raw, created_at)"
                 " VALUES (?,?,'s','[]','[]','{}',0)", (path, old_chunk_ids[0]))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1

    index_file(conn, fts_path, vs, cid, path)
    n_knowledge = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n_knowledge <= n_chunks
    orphan = conn.execute(
        "SELECT COUNT(*) FROM knowledge WHERE chunk_id NOT IN (SELECT id FROM chunks)").fetchone()[0]
    assert orphan == 0


def test_prune_orphans_removes_nodes_without_edges(tmp_path):
    import kb.graph as g
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    conn.execute("INSERT INTO nodes(name,norm,type) VALUES ('孤点','孤点','knowledge_point')")
    conn.execute("INSERT INTO nodes(name,norm,type) VALUES ('保留','保留','entity')")
    conn.execute("INSERT INTO nodes(name,norm,type) VALUES ('有边','有边','entity')")
    conn.commit()
    a = conn.execute("SELECT id FROM nodes WHERE name='有边'").fetchone()[0]
    b = conn.execute("SELECT id FROM nodes WHERE name='保留'").fetchone()[0]
    conn.execute("INSERT INTO edges(src,dst,rel,weight) VALUES (?,?,'r',1)", (a, b))
    conn.commit()
    g.prune_orphans(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM nodes")}
    assert "孤点" not in names
    assert "保留" in names and "有边" in names
