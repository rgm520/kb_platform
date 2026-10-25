"""重复文件识别：按 **MD5 内容哈希** 分组（不按文件名）。

为什么要 MD5：同名不同内容、两份一样的扫描件在 NAS 上很常见，
只有内容哈希才能判断"这两份文件其实是同一份"。

成本：MD5 要读一遍文件（SMB 上更慢）。用**大小筛子**先过一遍：
大小唯一的文件不可能重复，不用算哈希；只有"大小撞车"的候选才读文件。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time

CHUNK = 1024 * 1024


def file_md5(path: str) -> str | None:
    """算文件 MD5；读不到返回 None。"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def hash_pending(conn: sqlite3.Connection, limit: int = 10) -> int:
    """给"大小撞车、还没算过哈希"的文档补算 MD5。返回本次算的数量。

    只算候选：大小在库里出现 ≥2 次的文档。唯一大小的文件不可能重复。
    """
    rows = conn.execute(
        """SELECT c.id, c.path FROM catalog c
           WHERE c.md5 IS NULL AND c.size IS NOT NULL AND c.size > 0
             AND c.size IN (SELECT size FROM catalog
                            WHERE md5 IS NULL AND size IS NOT NULL AND size > 0
                            GROUP BY size HAVING COUNT(*) > 1)
           ORDER BY c.id LIMIT ?""", (int(limit),)).fetchall()
    n = 0
    for r in rows:
        md5 = file_md5(r["path"])
        conn.execute("UPDATE catalog SET md5=?, md5_at=? WHERE id=?",
                     (md5, time.time(), int(r["id"])))
        n += 1
    conn.commit()
    return n


def duplicate_groups(conn: sqlite3.Connection, where: str = "1=1",
                     params: list | None = None) -> list[dict]:
    """按 MD5 找出重复分组（≥2 份），并带上每一份的明细。

    where/params 是可见范围过滤（普通用户只看得到自己授权目录里的）。
    """
    params = params or []
    groups = []
    md5s = conn.execute(
        f"SELECT c.md5 FROM catalog c WHERE c.md5 IS NOT NULL AND {where} "
        f"GROUP BY c.md5 HAVING COUNT(*) > 1", params).fetchall()
    for (md5,) in md5s:
        items = conn.execute(
            f"""SELECT c.id, c.name, c.path, c.share, c.rel_path, c.size, c.mtime,
                       c.status, c.chunk_count, c.tier1_done
                FROM catalog c WHERE c.md5=? AND {where}
                ORDER BY c.path""", [md5] + params).fetchall()
        if len(items) < 2:
            continue
        groups.append({
            "md5": md5,
            "size": items[0]["size"],
            "count": len(items),
            "items": [dict(i) for i in items],
        })
    groups.sort(key=lambda g: (-g["count"], -(g["size"] or 0)))
    return groups


def duplicate_summary(conn: sqlite3.Connection, where: str = "1=1",
                       params: list | None = None) -> dict:
    """看板卡片用：多少组重复、共多少个文件、可省下多少字节。"""
    groups = duplicate_groups(conn, where, params)
    files = sum(g["count"] for g in groups)
    wasted = sum(g["size"] * (g["count"] - 1) for g in groups if g["size"])
    return {"group_count": len(groups), "file_count": files,
            "wasted_bytes": wasted, "groups": groups}
