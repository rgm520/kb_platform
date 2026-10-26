"""Tier 1 索引器：单个文件 -> 文本块 -> FTS 索引 + 向量。

约束：
  * 解析失败**绝不向上抛异常**，必须把原因写进 catalog.status_detail
    （Review Focus #5：NAS 断连 / 文件被独占 / 权限不足不能让流水线卡死）
  * 重新索引同一文件必须**替换**旧块，不能残留
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field

import kb.chunker as chunker
import kb.parsers as parsers

from kbplatform import auth, embedding
from kbplatform import fts
from kbplatform.converters import (LEGACY_EXT, SCAN_EXT, convert_legacy,
                                   is_ocr_backlog, ocr_pdf)
from kbplatform.db import set_status
from kbplatform.monitor import Change
from kbplatform.storage import adapter_for, source_from_row


@dataclass
class IndexResult:
    catalog_id: int
    chunk_ids: list[int]
    ok: bool
    error: str | None
    warnings: list[str] = field(default_factory=list)


def _fail(conn: sqlite3.Connection, catalog_id: int, msg: str) -> IndexResult:
    set_status(conn, catalog_id, "failed", msg)
    return IndexResult(catalog_id, [], False, msg)


def _defer_ocr(conn: sqlite3.Connection, catalog_id: int, msg: str) -> IndexResult:
    """扫描件暂不 OCR：标 skipped + needs_ocr，让出队列（spec D9）。"""
    set_status(conn, catalog_id, "skipped", msg)
    conn.execute(
        "UPDATE catalog SET needs_ocr=1, scanned=1, updated_at=? WHERE id=?",
        (time.time(), catalog_id))
    conn.commit()
    return IndexResult(catalog_id, [], False, msg)


def _ocr_requested(conn, catalog_id: int) -> bool:
    """管理员是否手动点名要重跑（允许对存量扫描件做 OCR）。"""
    try:
        row = conn.execute("SELECT ocr_requested FROM catalog WHERE id=?",
                           (catalog_id,)).fetchone()
        return bool(row and row[0])
    except sqlite3.OperationalError:
        return False


def _chunks_of(conn: sqlite3.Connection, file_path: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE file_path=?", (file_path,)).fetchall()]


def _fetch_local(conn: sqlite3.Connection, unc_path: str) -> str | None:
    """带账号密码的 SMB 源：把文件取到本地临时文件，返回路径；否则 None。

    为什么需要：解析用的是普通 `open()`，也就是**操作系统重定向器**。
    共享需要账号密码时，凭据只在平台的 smbclient 里，OS 打不开 UNC 路径
    （实测 tmp_ppt：`PackageNotFoundError` → 被报成"文件无有效文本"）。
    所以先用平台自己的适配器读一遍，落到本地临时文件再交给解析器。

    匿名共享（如现网 192.0.2.19 那几个）保持原样，不额外下载。
    """
    path = str(unc_path or "").replace("/", "\\")
    if not path:
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM storage_sources WHERE kind='smb' AND enabled=1 "
            "AND (COALESCE(username,'') <> '' OR secret_enc IS NOT NULL)").fetchall()
    except sqlite3.OperationalError:
        return None
    low = path.lower()
    for row in rows:
        root = str(row["root"] or "").replace("/", "\\").rstrip("\\")
        if not root or not low.startswith(root.lower() + "\\"):
            continue
        rel = path[len(root) + 1:]
        suffix = os.path.splitext(path)[1].lower()
        try:
            password = auth.decrypt_secret(row["secret_enc"]) if row["secret_enc"] else None
            adapter = adapter_for(source_from_row(row), password)
            fd, tmp = tempfile.mkstemp(prefix="kb-fetch-", suffix=suffix)
            os.close(fd)
            try:
                with adapter.open_read(rel) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            return tmp
        except Exception:
            # 取不下来就退回原路径解析：让下游把真实原因写进 status_detail，
            # 这里的失败不额外制造噪音。
            return None
    return None


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


def _insert_chunks(conn: sqlite3.Connection, fts_path: str, vs,
                   file_path: str, chunks: list[dict]) -> list[int]:
    """替换某文件的块。旧的块连同其 knowledge / edges / vectors 必须一起清掉，
    否则重跑会产生孤儿知识（knowledge 行数多于 chunks 数）与重复图谱节点。

    **FTS 行与向量库也必须清**：旧 chunk id 被回收后，FTS 里残留的 rowid 会在
    检索时被召回，再被 JOIN catalog 丢掉 —— 白占 top-k 名额，把真命中挤掉
    （向量库同理：旧 id 仍被当作有效向量参与相似度排序）。
    """
    old_ids = _chunks_of(conn, file_path)
    if old_ids:
        marks = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM knowledge WHERE chunk_id IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM edges WHERE chunk_id IN ({marks})", old_ids)
        try:
            vs.remove(old_ids)
        except Exception:
            pass
        fts.delete(fts_path, old_ids)
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
    tmpdir = convert_dir or tempfile.gettempdir()
    # 带凭据的 SMB 共享：OS 打不开 UNC（凭据在平台里），先取到本地临时文件。
    fetched = _fetch_local(conn, unc_path)
    parse_src = fetched or unc_path
    parse_target = parse_src
    ext = os.path.splitext(parse_src)[1].lower()

    try:
        if ext in LEGACY_EXT:
            converted = convert_legacy(parse_src, tmpdir)
            if not converted:
                return _fail(conn, catalog_id,
                             "老格式待支持（LibreOffice 不可用或转换失败）")
            parse_target = converted
        elif ext in SCAN_EXT:
            # 先按普通 PDF 解析；若判定为扫描件再走 OCR
            try:
                _blocks, meta0 = parsers.parse_file(parse_src)
            except Exception:
                meta0 = {}
            if meta0.get("need_ocr"):
                # 存量不批量 OCR：单页约 136 秒，一个大扫描件能堵死整条队列。
                # 但管理员在看板点了「重新解析」→ ocr_requested=1 → 这次照做。
                if is_ocr_backlog(parse_src) and not _ocr_requested(conn, catalog_id):
                    return _defer_ocr(conn, catalog_id,
                                      "扫描件：存量不自动 OCR（可在看板手动触发重跑）")
                ocr_txt = ocr_pdf(parse_src, tmpdir)
                if not ocr_txt:
                    return _defer_ocr(conn, catalog_id,
                                      "扫描件：OCR 失败/超时，已跳过")
                parse_target = ocr_txt

        try:
            blocks, meta = parsers.parse_file(parse_target)
        except Exception as e:
            return _fail(conn, catalog_id, f"解析失败: {type(e).__name__}: {e}")
    finally:
        if fetched:
            try:
                os.remove(fetched)
            except OSError:
                pass

    # 解析器把异常吞成空块时，如实报出原因 —— 否则"共享读不到"会被伪装成
    # "文件无有效文本"，运维会以为是空文件而白查一轮。
    if meta.get("type") == "error":
        return _fail(conn, catalog_id, f"解析失败: {meta.get('error')}")

    if not blocks or sum(len(b.text) for b in blocks) < 30:
        return _fail(conn, catalog_id, "文件无有效文本（可能是扫描件或空文件）")

    try:
        chunks = chunker.chunk_blocks(blocks)
        if not chunks:
            return _fail(conn, catalog_id, "分块结果为空")
        # _insert_chunks 负责清掉旧块的 FTS 行与向量条目，再写入新块。
        chunk_ids = _insert_chunks(conn, fts_path, vs, unc_path, chunks)
        texts = [c["text"] for c in chunks]

        fts.upsert(fts_path, list(zip(chunk_ids, texts)))

        # 带超限兜底的向量化：一个超限块不再让整篇向量作废（Task 0.2）。
        vecs, emb_warnings = embedding.embed_chunks(texts, model=embed_model)
        kept = [(cid, v) for cid, v in zip(chunk_ids, vecs) if v is not None]
        if kept:
            vs.add([cid for cid, _ in kept], [v for _, v in kept])

        # 部分块没向量时留下人能读懂的说明（覆盖上传 / 重跑会重写它，不会残留旧标记）
        detail = "；".join(emb_warnings) if emb_warnings else None
        need_ocr = 1 if meta.get("need_ocr") else 0
        conn.execute(
            """UPDATE catalog SET status='done', status_detail=?, tier1_done=1,
                 chunk_count=?, needs_ocr=?, scanned=?, updated_at=? WHERE id=?""",
            (detail, len(chunk_ids), need_ocr, need_ocr, time.time(), catalog_id))
        conn.commit()
        return IndexResult(catalog_id, chunk_ids, True, None, emb_warnings)
    except Exception as e:
        return _fail(conn, catalog_id, f"索引失败: {type(e).__name__}: {e}")


def remove_file(conn: sqlite3.Connection, fts_path: str, vs, catalog_id: int) -> None:
    """文件被删除：清掉它的全部索引与图谱产物，catalog 行标记为 skipped。"""
    row = conn.execute("SELECT path FROM catalog WHERE id=?", (catalog_id,)).fetchone()
    if row:
        _delete_artifacts(conn, fts_path, vs, row[0])
        set_status(conn, catalog_id, "skipped", "源文件已删除，索引与图谱产物已清理")


def clear_artifacts(conn: sqlite3.Connection, fts_path: str, vs, file_path: str) -> None:
    """覆盖上传前清空旧产物；catalog 行保留，稍后重新解析。"""
    _delete_artifacts(conn, fts_path, vs, file_path)


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
