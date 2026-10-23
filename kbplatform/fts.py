"""FTS5 全文索引层。

关键约束（spec Global Constraint #3）：
  **必须用 FTS5 + 预写入字符二元组，不得用 trigram。**
  实测 trigram 对「信创」「审批」这类 2 字中文词全部查不到（要求 >= 3 字符），
  而中文企业术语里 2 字词极其常见。

索引结构：contentless FTS5 表，rowid 即 chunk_id，存的是预分词后的 token 串。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable

CJK = re.compile(r"[一-鿿]")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
# 词边界：空白 + 中英文标点（保留 `_` `-` 以免切碎 chunk_id 这类标识符）
_SPLIT = re.compile(
    r"[\s\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\x21-\x2c\x2e-\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7e]+")


def tokenize(text: str) -> list[str]:
    """中文：单字 + 相邻二元组；英文/数字：按词。全部小写。

    必须**按词边界分段**再取二元组。直接在整串上取相邻二字会拼出跨词的
    假二元组：`任职资格 版本变更` 里的 `格版` 在任何文档中都不存在，
    AND 匹配必然落空——多词查询的 FTS 召回会整个变成 0
    （2026-10-23 实测：`数据机房 审批` 只有向量通道有结果）。
    """
    if not text:
        return []
    toks: list[str] = []
    for seg in _SPLIT.split(text):
        if not seg:
            continue
        toks.extend(m.group().lower() for m in WORD.finditer(seg))
        cj = CJK.findall(seg)
        toks.extend(cj)
        toks.extend(a + b for a, b in zip(cj, cj[1:]))
    # 去重（保持顺序）：索引串更短，查询里重复词也不会被算成多个 AND 条件
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _cjk_only(tokens: list[str]) -> bool:
    return bool(tokens) and all(len(t) == 1 for t in tokens)


def needs_vector_fallback(query: str) -> bool:
    """该查询无法构成有效二元组，应降级为向量检索。

    1 个汉字只能产出 1 个单字 token，AND 匹配必然落空。
    """
    toks = tokenize(query)
    if not toks:
        return True
    if _cjk_only(toks) and len(toks) < 2:
        return True
    return False


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _ensure(con: sqlite3.Connection) -> None:
    # contentless_delete=1 需要 SQLite >= 3.43（本机 3.49.1）。
    # 没有它，contentless 表无法按 rowid DELETE，增量更新与文件删除都做不了。
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    if ver < (3, 43):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} 过低，需要 >= 3.43 以支持 "
            f"contentless_delete=1（FTS5 增量删除）")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5("
        "tok, content='', contentless_delete=1, tokenize='unicode61')")


def _tokens_for_index(text: str) -> str:
    return " ".join(tokenize(text))


def build_index(path: str, rows: Iterable[tuple[int, str]]) -> None:
    """重建整个索引。rows = (chunk_id, text)。"""
    con = _connect(path)
    try:
        _ensure(con)
        con.execute("DELETE FROM ft")
        con.executemany("INSERT INTO ft(rowid, tok) VALUES (?,?)",
                        [(int(cid), _tokens_for_index(txt)) for cid, txt in rows])
        con.commit()
    finally:
        con.close()


def upsert(path: str, items: Iterable[tuple[int, str]]) -> None:
    """写入或替换若干行。"""
    items = [(int(cid), txt) for cid, txt in items]
    if not items:
        return
    con = _connect(path)
    try:
        _ensure(con)
        for cid, txt in items:
            con.execute("DELETE FROM ft WHERE rowid=?", (cid,))
            con.execute("INSERT INTO ft(rowid, tok) VALUES (?,?)",
                        (cid, _tokens_for_index(txt)))
        con.commit()
    finally:
        con.close()


def delete(path: str, chunk_ids: Iterable[int]) -> None:
    ids = [int(i) for i in chunk_ids]
    if not ids:
        return
    con = _connect(path)
    try:
        _ensure(con)
        con.executemany("DELETE FROM ft WHERE rowid=?", [(i,) for i in ids])
        con.commit()
    finally:
        con.close()


def search(path: str, query: str, limit: int = 20) -> list[tuple[int, float]]:
    """返回 [(chunk_id, bm25 分数)]，**分数越小越相关**（SQLite bm25 的语义）。

    查询无法构成二元组时返回空列表，调用方应改用向量检索。
    """
    toks = tokenize(query)
    if not toks:
        return []
    if _cjk_only(toks) and len(toks) < 2:
        return []
    expr = " AND ".join(f'"{t}"' for t in toks)
    con = _connect(path)
    try:
        _ensure(con)
        rows = con.execute(
            "SELECT rowid, bm25(ft) FROM ft WHERE ft MATCH ? ORDER BY bm25(ft) LIMIT ?",
            (expr, int(limit))).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
