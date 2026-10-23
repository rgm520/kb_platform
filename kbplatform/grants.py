"""目录授权与可见性。

权限模型（2.0）：
  * 一个 grant = (user, source, subpath, can_write)，subpath 为空表示整个源；
  * 管理员不受限；
  * 普通用户能进入授权路径的**祖先**（否则点不到深层目录），但祖先下
    只显示通往授权路径的那一段，兄弟目录一律不出现；
  * 个人空间是合成授权（不进 grants 表）：每个启用源下的
    `<personal_root>/<username>`，读写权限。
"""
from __future__ import annotations

import sqlite3
import time

from kbplatform.paths import norm_unc, split_share
from kbplatform.storage import Source, is_safe_rel, normalize_rel


class Grant:
    __slots__ = ("source_id", "subpath", "can_write")

    def __init__(self, source_id: int, subpath: str, can_write: bool):
        self.source_id = int(source_id)
        self.subpath = normalize_rel(subpath)
        self.can_write = bool(can_write)

    def __eq__(self, other):
        return (isinstance(other, Grant) and self.source_id == other.source_id
                and self.subpath == other.subpath
                and self.can_write == other.can_write)

    def __hash__(self):
        return hash((self.source_id, self.subpath, self.can_write))

    def __repr__(self):
        return f"Grant({self.source_id}, {self.subpath!r}, {self.can_write})"


def _is_admin(user) -> bool:
    return (user or {}).get("role") == "admin"


def _under(rel: str, base: str) -> bool:
    rel, base = normalize_rel(rel), normalize_rel(base)
    return base == "" or rel == base or rel.startswith(base + "\\")


def _next_component(rel: str, subpath: str) -> str | None:
    """当前目录 rel 下，通往 subpath 的下一段完整相对路径。"""
    rel, subpath = normalize_rel(rel), normalize_rel(subpath)
    if not subpath:
        return None
    if rel == "":
        return subpath.split("\\")[0]
    prefix = rel + "\\"
    if subpath.startswith(prefix):
        return rel + "\\" + subpath[len(prefix):].split("\\")[0]
    return None


# 供文件接口补「祖先导航占位」用
next_component = _next_component


def user_grants(conn, user) -> list[Grant]:
    if _is_admin(user):
        return []
    rows = conn.execute(
        "SELECT source_id, subpath, can_write FROM grants WHERE user_id=?",
        (user["id"],)).fetchall()
    out = [Grant(r["source_id"], r["subpath"], r["can_write"]) for r in rows]
    seen = {(g.source_id, g.subpath) for g in out}
    username = (user.get("username") or "").strip()
    if username and "\\" not in username:
        for s in conn.execute(
                "SELECT id, personal_root FROM storage_sources "
                "WHERE enabled=1 AND COALESCE(personal_root,'')<>''"):
            sub = normalize_rel(f"{s['personal_root']}\\{username}")
            if (int(s["id"]), sub) not in seen:
                out.append(Grant(int(s["id"]), sub, True))
    return out


def can_read(user, grants: list[Grant], source_id: int, rel: str) -> bool:
    if _is_admin(user):
        return True
    rel = normalize_rel(rel)
    for g in grants:
        if g.source_id != int(source_id):
            continue
        if _under(rel, g.subpath) or _under(g.subpath, rel):
            return True
    return False


def can_write(user, grants: list[Grant], source_id: int, rel: str) -> bool:
    if _is_admin(user):
        return True
    rel = normalize_rel(rel)
    return any(g.source_id == int(source_id) and g.can_write and _under(rel, g.subpath)
               for g in grants)


def filter_entries(grants: list[Grant], source_id: int, rel: str, entries: list) -> list:
    """隐藏兄弟目录，只保留授权子树与通往授权的下一段目录。"""
    rel = normalize_rel(rel)
    mine = [g for g in grants if g.source_id == int(source_id)]
    if any(g.subpath == "" for g in mine):
        return list(entries)
    allowed_exact = {_next_component(rel, g.subpath) for g in mine}
    allowed_exact.discard(None)
    out, seen = [], set()
    for e in entries:
        erel = normalize_rel(e.rel)
        if erel in seen:
            continue
        if any(_under(erel, g.subpath) for g in mine) or erel in allowed_exact:
            out.append(e)
            seen.add(erel)
    return out


def set_grants(conn, user_id: int, items: list[dict]) -> int:
    now = time.time()
    conn.execute("DELETE FROM grants WHERE user_id=?", (int(user_id),))
    n = 0
    for it in items:
        sub = normalize_rel(it.get("subpath", ""))
        if not is_safe_rel(sub):
            raise ValueError(f"非法授权路径: {sub!r}")
        conn.execute(
            "INSERT OR REPLACE INTO grants(user_id,source_id,subpath,can_write,created_at) "
            "VALUES (?,?,?,?,?)",
            (int(user_id), int(it["source_id"]), sub, 1 if it.get("can_write") else 0, now))
        n += 1
    conn.commit()
    return n


def ensure_personal_space(conn, user, source: Source, adapter) -> Grant | None:
    if not source.personal_root:
        return None
    username = (user.get("username") or "").strip()
    if not username or "\\" in username:
        return None
    sub = normalize_rel(f"{source.personal_root}\\{username}")
    if not is_safe_rel(sub):
        return None
    try:
        if adapter.stat(sub) is None:
            adapter.mkdirs(sub)
    except Exception:
        # 个人空间建不出来不能阻断登录；上传时会再报错
        pass
    return Grant(source.id, sub, True)


def migrate_user_scopes(conn, cfg) -> int:
    """把旧的 (share, subpath) 授权一次性映射到新 grants。

    只有新建存储源之后调用才有意义；没有源时不写 user_version，
    留到下次启动再试，避免迁移永远被跳过。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version and version >= 1:
        return 0
    sources = conn.execute("SELECT id, root FROM storage_sources WHERE enabled=1").fetchall()
    if not sources:
        return 0
    n = 0
    for s in sources:
        source_base = normalize_rel(split_share(norm_unc(s["root"]))[1])
        share = split_share(norm_unc(s["root"]))[0]
        for r in conn.execute(
                "SELECT user_id, subpath FROM user_scopes WHERE share=?", (share,)).fetchall():
            sub = normalize_rel(r["subpath"])
            if source_base and _under(sub, source_base):
                grant_sub = sub[len(source_base):].strip("\\")
            elif source_base and _under(source_base, sub):
                grant_sub = ""
            elif not source_base:
                grant_sub = sub
            else:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO grants(user_id,source_id,subpath,can_write,created_at) "
                "VALUES (?,?,?,0,?)", (r["user_id"], s["id"], grant_sub, time.time()))
            n += 1
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    return n
