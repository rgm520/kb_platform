"""可见范围（scope）与涉密（restricted）判定。

两个正交的概念（spec D10 / D13）：
  * **可见范围** user_scopes  是「谁能看到什么」——管理员为每个用户勾选
  * **涉密标记** dir_scopes   是「这个目录本身敏不敏感」——与授权无关

因此同一个目录对所有人都是涉密的，不会因为「谁被授权」而改变。

路径语义：Windows 语义，**不区分大小写**；前缀匹配必须按目录边界
（`AI知识库` 不应匹配 `AI知识库2`）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    share: str
    subpath: str = ""

    def covers(self, share: str, rel_path: str) -> bool:
        if not self.share or self.share.lower() != (share or "").lower():
            return False
        if not self.subpath:
            return True
        r = (rel_path or "").lower()
        s = self.subpath.lower().strip("\\")
        if not s:
            return True
        return r == s or r.startswith(s + "\\")


def load_scopes(conn: sqlite3.Connection, user_id: int) -> list[Scope]:
    rows = conn.execute(
        "SELECT share, subpath FROM user_scopes WHERE user_id=?", (user_id,)).fetchall()
    return [Scope(r[0], r[1] or "") for r in rows]


def load_restricted(conn: sqlite3.Connection) -> list[Scope]:
    rows = conn.execute(
        "SELECT share, subpath FROM dir_scopes WHERE restricted=1").fetchall()
    return [Scope(r[0], r[1] or "") for r in rows]


def is_visible(scopes: list[Scope], share: str, rel_path: str) -> bool:
    return any(s.covers(share, rel_path) for s in scopes)


def is_restricted(restricted: list[Scope], share: str, rel_path: str) -> bool:
    return any(s.covers(share, rel_path) for s in restricted)


def _like_escape(s: str) -> str:
    """转义 LIKE 模式里的特殊字符。

    注意：配合 `ESCAPE '\\'` 使用时，模式末尾的 `%` 才是通配符，
    而被转义的 `\\%` 表示**字面百分号**。所以前缀匹配的模式是
    `escape(subpath) + '%'`，不是 `escape(subpath) + '\\%'`。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def visible_predicate(user_id: int, alias: str = "c",
                      scopes: list[Scope] | None = None) -> tuple[str, list]:
    """生成 chunks+catalog 联表上的可见范围 SQL 谓词。

    必须**在 SQL 层生效**（spec Global Constraint / §7.4.1）：先召回再过滤
    会漏召回，也会泄露被拒文档的存在性。
    """
    if scopes is None:
        return "1=1", []
    if not scopes:
        return "1=0", []
    clauses = []
    params: list = []
    for s in scopes:
        sub = (s.subpath or "").strip("\\")
        if not sub:
            clauses.append(f"LOWER({alias}.share) = LOWER(?)")
            params.append(s.share)
        else:
            clauses.append(
                f"(LOWER({alias}.share) = LOWER(?) AND "
                f"(LOWER({alias}.rel_path) = ? OR "
                f"LOWER({alias}.rel_path) LIKE ? ESCAPE '\\'))")
            params.extend([s.share, sub.lower(),
                            _like_escape(sub.lower()) + "%"])
    return "(" + " OR ".join(clauses) + ")", params


def restricted_predicate(restricted: list[Scope], alias: str = "c") -> tuple[str, list]:
    """生成「命中涉密内容」的 SQL 谓词（用于查询时标注 restricted）。"""
    if not restricted:
        return "1=0", []
    clauses = []
    params: list = []
    for s in restricted:
        sub = (s.subpath or "").strip("\\")
        if not sub:
            clauses.append(f"LOWER({alias}.share) = LOWER(?)")
            params.append(s.share)
        else:
            clauses.append(
                f"(LOWER({alias}.share) = LOWER(?) AND "
                f"(LOWER({alias}.rel_path) = ? OR "
                f"LOWER({alias}.rel_path) LIKE ? ESCAPE '\\'))")
            params.extend([s.share, sub.lower(),
                            _like_escape(sub.lower()) + "%"])
    return "(" + " OR ".join(clauses) + ")", params


def scope_from_catalog_row(row) -> tuple[str, str]:
    return (row["share"], row["rel_path"])


def grants_predicate(conn, user_id: int, alias: str = "c") -> tuple[str, list]:
    """按 2.0 的目录授权（grants）生成 SQL 谓词。

    文件页和文档看板用**同一套授权**：grants 命中 → 文件可见，
    这里就让对应路径下的文档也可见（用 catalog.path 前缀匹配）。
    """
    rows = conn.execute(
        "SELECT s.root, g.subpath FROM grants g JOIN storage_sources s ON s.id=g.source_id "
        "WHERE g.user_id=?", (int(user_id),)).fetchall()
    if not rows:
        return "1=0", []
    clauses: list[str] = []
    params: list = []
    for root, sub in rows:
        prefix = str(root or "").rstrip("\\")
        sub = (sub or "").strip("\\")
        if sub:
            prefix = prefix + "\\" + sub
        low = prefix.lower()
        clauses.append(
            f"(LOWER({alias}.path) = ? OR "
            f"LOWER({alias}.path) LIKE ? ESCAPE '\\')")
        # 目录边界：前缀 + 一个分隔符（转义），再用 % 通配
        params.extend([low, _like_escape(low + "\\") + "%"])
    return "(" + " OR ".join(clauses) + ")", params


def describe_scopes(scopes: list[Scope]) -> list[str]:
    out = []
    for s in scopes:
        out.append(s.share if not s.subpath else f"{s.share}\\{s.subpath}")
    return out
