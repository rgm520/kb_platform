"""Tier 1 索引器：单个文件 -> 文本块 -> FTS 索引 + 向量。

约束：
  * 解析失败**绝不向上抛异常**，必须把原因写进 catalog.status_detail
    （Review Focus #5：NAS 断连 / 文件被独占 / 权限不足不能让流水线卡死）
  * 重新索引同一文件必须**替换**旧块，不能残留
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass

import kb.chunker as chunker
import kb.llm as llm
import kb.parsers as parsers

from kbplatform import fts
from kbplatform.converters import LEGACY_EXT, SCAN_EXT, convert_legacy, ocr_pdf
from kbplatform.db import set_status
from kbplatform.monitor import Change


@dataclass
class IndexResult:
    catalog_id: int
    chunk_ids: list[int]
    ok: bool
    error: str | None


def _fail(conn: sqlite3.Connection, catalog_id: int, msg: str) -> IndexResult:
    set_status(conn, catalog_id, "failed", msg)
    return IndexResult(catalog_id, [], False, msg)


def _chunks_of(conn: sqlite3.Connection, file_path: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE file_path=?", (file_path,)).fetchall()]


def _delete_artifacts(conn: sqlite3.Connection, fts_path: str, vs, file_path: str) -> None:
    """删除某文件的全部派生产物：块、向量、FTS 行、knowledge、图谱边。"""
    ids = _chunks_of(conn, file_path)
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM knowledge WHERE chunk_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM edges WHERE chunk_id IN ({marks})", ids)
        try:
            vs.remove(ids)
        except Exception:
            pass
        fts.delete(fts_path, ids)
    conn.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
    conn.commit()


def _insert_chunks(conn: sqlite3.Connection, file_path: str, chunks: list[dict]) -> list[int]:
    """替换某文件的块。旧的块连同其 knowledge / edges / vectors 必须一起清掉，
    否则重跑会产生孤儿知识（knowledge 行数多于 chunks 数）与重复图谱节点。"""
    old_ids = _chunks_of(conn, file_path)
    if old_ids:
        marks = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM knowledge WHERE chunk_id IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM edges WHERE chunk_id IN ({marks})", old_ids)
    conn.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
    ids = []
    for c in chunks:
        cur = conn.execute(
            "INSERT INTO chunks(file_path, chunk_idx, text, locator, tokens)"
            " VALUES (?,?,?,?,?)",
            (file_path, c["idx"], c["text"], c.get("locator", ""), c.get("tokens", 0)))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def index_file(conn: sqlite3.Connection, fts_path: str, vs, catalog_id: int,
               unc_path: str, embed_model: str = "bge-m3",
               convert_dir: str | None = None) -> IndexResult:
    """把一个文件变成可检索的块。失败时返回 ok=False 并记录原因。

    老格式（.doc/.ppt/.xls）先经 LibreOffice 转换；扫描件 PDF 先经 OCR。
    转换器/OCR 不可用时**不报错**，而是标记为「待支持」并继续。
    """
    parse_target = unc_path
    ext = os.path.splitext(unc_path)[1].lower()
    tmpdir = convert_dir or tempfile.gettempdir()

    if ext in LEGACY_EXT:
        converted = convert_legacy(unc_path, tmpdir)
        if not converted:
            return _fail(conn, catalog_id,
                         "老格式待支持（LibreOffice 不可用或转换失败）")
        parse_target = converted
    elif ext in SCAN_EXT:
        # 先按普通 PDF 解析；若判定为扫描件再走 OCR
        try:
            _blocks, meta0 = parsers.parse_file(unc_path)
        except Exception:
            meta0 = {}
        if meta0.get("need_ocr"):
            ocr_txt = ocr_pdf(unc_path, tmpdir)
            if not ocr_txt:
                return _fail(conn, catalog_id, "扫描件待 OCR 支持")
            parse_target = ocr_txt

    try:
        blocks, meta = parsers.parse_file(parse_target)
    except Exception as e:
        return _fail(conn, catalog_id, f"解析失败: {type(e).__name__}: {e}")

    if not blocks or sum(len(b.text) for b in blocks) < 30:
        return _fail(conn, catalog_id, "文件无有效文本（可能是扫描件或空文件）")

    try:
        chunks = chunker.chunk_blocks(blocks)
        if not chunks:
            return _fail(conn, catalog_id, "分块结果为空")
        chunk_ids = _insert_chunks(conn, unc_path, chunks)
        texts = [c["text"] for c in chunks]

        fts.upsert(fts_path, list(zip(chunk_ids, texts)))

        vecs = llm.embed(texts, model=embed_model) if texts else []
        if vecs:
            vs.add(chunk_ids, vecs)

        need_ocr = 1 if meta.get("need_ocr") else 0
        conn.execute(
            """UPDATE catalog SET status='done', status_detail=?, tier1_done=1,
                 chunk_count=?, needs_ocr=?, scanned=?, updated_at=? WHERE id=?""",
            (None, len(chunk_ids), need_ocr, need_ocr, time.time(), catalog_id))
        conn.commit()
        return IndexResult(catalog_id, chunk_ids, True, None)
    except Exception as e:
        return _fail(conn, catalog_id, f"索引失败: {type(e).__name__}: {e}")


def remove_file(conn: sqlite3.Connection, fts_path: str, vs, catalog_id: int) -> None:
    """文件被删除：清掉它的全部索引与图谱产物，catalog 行标记为 skipped。"""
    row = conn.execute("SELECT path FROM catalog WHERE id=?", (catalog_id,)).fetchone()
    if row:
        _delete_artifacts(conn, fts_path, vs, row[0])
        set_status(conn, catalog_id, "skipped", "源文件已删除，索引与图谱产物已清理")


def process_changes(conn: sqlite3.Connection, fts_path: str, vs,
                    changes: list[Change], embed_model: str = "bge-m3") -> list[IndexResult]:
    """把监控变更落到索引上。"""
    results: list[IndexResult] = []
    for ch in changes:
        row = conn.execute("SELECT id FROM catalog WHERE path=?", (ch.path,)).fetchone()
        if not row:
            continue
        cid = row[0]
        if ch.kind == "removed":
            remove_file(conn, fts_path, vs, cid)
            results.append(IndexResult(cid, [], True, None))
        else:
            results.append(index_file(conn, fts_path, vs, cid, ch.path, embed_model))
    return results
