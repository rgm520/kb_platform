"""路径索引与文件名搜索。

一张 `fs_entries` 存目录与文件的元数据（全量，不区分是否可解析），
一张 `fs_fts` 用 trigram 分词器做中文子串秒搜。内容索引仍走
`catalog + chunks + vectors`，两者靠 `catalog_id` 关联。
"""
from __future__ import annotations

import os
import time

from kbplatform.catalog import PARSEABLE_EXT
from kbplatform.storage import Entry, Source, normalize_rel

SKIP_DIR_NAMES = {"_kb_trash", "#recycle", "#snapshot", "@eaDir",
                  "$RECYCLE.BIN", "System Volume Information"}


def rel_key(rel: str) -> str:
    return normalize_rel(rel).lower()


def entry_path(source: Source, rel: str) -> str:
    rel = normalize_rel(rel)
    return source.root.rstrip("\\") + ("\\" + rel if rel else "")


def _parent_rel(rel: str) -> str:
    rel = normalize_rel(rel)
    return rel.rsplit("\\", 1)[0] if "\\" in rel else ""


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _is_parseable(entry: Entry) -> bool:
    return (not entry.is_dir) and _ext(entry.name) in PARSEABLE_EXT


def _set_fts(conn, rowid: int, name: str, rel: str) -> None:
    conn.execute("DELETE FROM fs_fts WHERE rowid=?", (rowid,))
    conn.execute("INSERT INTO fs_fts(rowid,name,rel_path) VALUES (?,?,?)",
                 (rowid, name, rel))


def upsert_entry(conn, source, entry: Entry) -> int:
    """写入/更新一条路径记录，并同步 FTS。返回 fs_entries.id。"""
    sid = source.id if hasattr(source, "id") else int(source)
    rel = normalize_rel(entry.rel)
    conn.execute(
        """INSERT INTO fs_entries(source_id, rel_path, rel_key, parent_rel, name,
                                  is_dir, size, mtime, ext, parseable, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_id, rel_key) DO UPDATE SET
             rel_path=excluded.rel_path, parent_rel=excluded.parent_rel,
             name=excluded.name, is_dir=excluded.is_dir, size=excluded.size,
             mtime=excluded.mtime, ext=excluded.ext,
             parseable=excluded.parseable, updated_at=excluded.updated_at""",
        (sid, rel, rel_key(rel), _parent_rel(rel), entry.name,
         1 if entry.is_dir else 0, 0 if entry.is_dir else entry.size,
         entry.mtime, _ext(entry.name), 1 if _is_parseable(entry) else 0,
         time.time()))
    row = conn.execute("SELECT id FROM fs_entries WHERE source_id=? AND rel_key=?",
                       (sid, rel_key(rel))).fetchone()
    _set_fts(conn, int(row[0]), entry.name, rel)
    return int(row[0])


def delete_subtree(conn, source_id: int, rel: str) -> list[str]:
    """删除相对路径（含子目录）下的全部索引行，返回被删的 rel_path。"""
    key = rel_key(rel)
    if key:
        rows = conn.execute(
            "SELECT id, rel_path FROM fs_entries WHERE source_id=? AND "
            "(rel_key=? OR rel_key LIKE ?)",
            (source_id, key, key + "\\%")).fetchall()
    else:
        rows = conn.execute("SELECT id, rel_path FROM fs_entries WHERE source_id=?",
                            (source_id,)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM fs_fts WHERE rowid=?", (r["id"],))
        conn.execute("DELETE FROM fs_entries WHERE id=?", (r["id"],))
    conn.commit()
    return [r["rel_path"] for r in rows]


def scan_source(conn, source: Source, adapter, qps: int = 50) -> dict:
    """全量遍历一个存储源，增量写入路径索引。

    单个目录读取失败只记录错误并跳过其删除判断，不能把整棵子树误删。
    """
    sid = source.id
    existing = {r["rel_key"]: r for r in conn.execute(
        "SELECT id, rel_path, rel_key, is_dir, size, mtime FROM fs_entries "
        "WHERE source_id=?", (sid,))}
    seen: set[str] = set()
    failed: list[str] = []
    stats = {"files": 0, "dirs": 0, "added": 0, "updated": 0, "removed": 0,
             "removed_rels": [], "changed_parseable": [], "errors": []}
    interval = (1.0 / qps) if qps and qps > 0 else 0.0

    def walk(rel: str) -> None:
        try:
            entries = adapter.listdir(rel)
        except Exception as e:
            failed.append(rel_key(rel))
            stats["errors"].append(f"{rel or '/'}: {type(e).__name__}: {e}")
            return
        for e in entries:
            if e.name.startswith("~$"):
                continue
            if e.is_dir and e.name in SKIP_DIR_NAMES:
                continue
            key = rel_key(e.rel)
            seen.add(key)
            if e.is_dir:
                stats["dirs"] += 1
            else:
                stats["files"] += 1
            old = existing.get(key)
            changed = old is None
            if old is not None and not changed:
                if bool(old["is_dir"]) != e.is_dir:
                    changed = True
                elif not e.is_dir and (old["size"] != e.size or
                                       abs((old["mtime"] or 0) - e.mtime) > 1e-6):
                    changed = True
            if changed:
                upsert_entry(conn, source, e)
                stats["added" if old is None else "updated"] += 1
                if _is_parseable(e):
                    stats["changed_parseable"].append(e.rel)
            if e.is_dir:
                walk(e.rel)
            if interval:
                time.sleep(interval)
        conn.commit()

    walk("")

    for key, row in existing.items():
        if key in seen:
            continue
        if any(key == p or key.startswith(p + "\\") for p in failed):
            continue
        conn.execute("DELETE FROM fs_fts WHERE rowid=?", (row["id"],))
        conn.execute("DELETE FROM fs_entries WHERE id=?", (row["id"],))
        stats["removed"] += 1
        stats["removed_rels"].append(row["rel_path"])
    conn.commit()
    return stats


def _scope_sql(allowed: list[tuple[int, str]] | None) -> tuple[str, list]:
    if allowed is None:
        return "", []
    if not allowed:
        return "AND 0", []
    parts, params = [], []
    for source_id, subpath in allowed:
        key = rel_key(subpath)
        if key:
            parts.append("(f.source_id=? AND (f.rel_key=? OR f.rel_key LIKE ?))")
            params.extend([int(source_id), key, key + "\\%"])
        else:
            parts.append("(f.source_id=?)")
            params.append(int(source_id))
    return "AND (" + " OR ".join(parts) + ")", params


def _rows(conn, q: str, allowed, limit: int, use_fts: bool) -> list:
    scope, scope_params = _scope_sql(allowed)
    base = ("SELECT f.id, f.source_id, COALESCE(s.name, '') AS source_name, f.rel_path, "
            "f.name, f.is_dir, f.size, f.mtime, f.ext "
            "FROM fs_entries f LEFT JOIN storage_sources s ON s.id=f.source_id "
            "WHERE ")
    if use_fts:
        where = "f.id IN (SELECT rowid FROM fs_fts WHERE fs_fts MATCH ?) "
        params = ['"' + q.replace('"', '""') + '"']
    else:
        where = "(f.name LIKE ? OR f.rel_path LIKE ?) "
        params = [f"%{q}%", f"%{q}%"]
    sql = (base + where + scope +
           " ORDER BY CASE WHEN f.name = ? THEN 0 WHEN f.name LIKE ? THEN 1 ELSE 2 END,"
           " f.mtime DESC LIMIT ?")
    params = params + scope_params + [q, f"%{q}%", limit]
    return conn.execute(sql, params).fetchall()


def search(conn, q: str, allowed: list[tuple[int, str]] | None,
           limit: int = 20) -> list[dict]:
    """文件名搜索。allowed=None 表示管理员不限；[] 表示没有任何授权。

    trigram 对 1~2 个字符无法命中，直接走 LIKE；3 个字符以上先用 FTS，
    没有结果再兜底 LIKE（文件名里的标点会打散 trigram）。
    """
    q = (q or "").strip()
    if not q:
        return []
    rows = []
    if len(q) >= 3:
        try:
            rows = _rows(conn, q, allowed, limit, True)
        except Exception:
            rows = []
    if not rows:
        rows = _rows(conn, q, allowed, limit, False)
    return [{"id": r["id"], "source_id": r["source_id"],
             "source_name": r["source_name"], "rel": r["rel_path"],
             "name": r["name"], "is_dir": bool(r["is_dir"]),
             "size": r["size"] or 0, "mtime": r["mtime"] or 0.0,
             "ext": r["ext"] or ""} for r in rows]
