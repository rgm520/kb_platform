"""模型路由与 session 权限规则。

设计见 `docs/superpowers/specs/2026-10-21-feishu-bot-design.md` §5.4。

模型路由：**用户可见范围里有涉密目录 → 整个会话走本地模型**。
理由：涉密原文现在会经知识库 API 正常返回，但 agent 有 bash，能直接从磁盘读 NAS 上
的原文（U:/V:/W:/X:/Y: 是持久映射，开机自动重连）。只要原文进过云端模型，
就等于出内网了。

权限规则：opencode 的规则是**最后匹配者生效**，所以宽泛的规则在前、具体的在后。
"""
from __future__ import annotations

import sqlite3

# 知识库服务自己盯着的凭据文件，也不给 agent
SECRET_FILES = ("secrets.toml",)


def restricted_dirs(conn: sqlite3.Connection) -> list[str]:
    """所有被标记为涉密的目录（share + subpath）。"""
    rows = conn.execute(
        "SELECT share, subpath FROM dir_scopes WHERE restricted=1").fetchall()
    out = []
    for r in rows:
        share = (r[0] or "").strip()
        sub = (r[1] or "").strip().replace("\\", "/").strip("/")
        if not share:
            continue
        path = f"{share}/{sub}" if sub else share
        out.append(path.rstrip("/"))
    return out


def user_has_restricted_access(conn: sqlite3.Connection, user_id: int) -> bool:
    """该用户的可见范围里有没有涉密目录。管理员不受范围限制。"""
    row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if row and (row[0] or "user") == "admin":
        return False

    restricted = conn.execute(
        "SELECT share, subpath FROM dir_scopes WHERE restricted=1").fetchall()
    if not restricted:
        return False
    scopes = conn.execute(
        "SELECT share, subpath FROM user_scopes WHERE user_id=?", (user_id,)).fetchall()

    def norm(share: str, subpath: str) -> tuple[str, str]:
        return ((share or "").strip().upper(),
                (subpath or "").strip().replace("\\", "/").strip("/").upper())

    for s in scopes:
        s_share, s_sub = norm(s[0], s[1])
        for d in restricted:
            d_share, d_sub = norm(d[0], d[1])
            if s_share != d_share:
                continue
            # 用户范围是受限目录本身或其子目录
            if not s_sub:
                return True
            if not d_sub:
                continue
            if s_sub == d_sub or s_sub.startswith(d_sub + "/"):
                return True
    return False


def pick_model(conn: sqlite3.Connection, user_id: int,
               cloud_ref: dict, local_ref: dict) -> tuple[dict, bool]:
    """返回 (模型 ref, 是否本地模型)。"""
    if user_has_restricted_access(conn, user_id):
        return local_ref, True
    return cloud_ref, False


def build_permissions(restricted_paths: list[str],
                      extra_denied: list[str] | None = None) -> list[dict]:
    """生成 session 级权限规则。

    顺序很重要：opencode 取**最后一条匹配**的规则，所以宽泛在前、具体在后。
    """
    perms: list[dict] = [
        # 框架级强控制：Location（D:/kb-platform）之外的路径一律不让读/改
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]

    for pattern in (extra_denied or []):
        perms.append({"action": "read", "resource": pattern, "effect": "deny"})
        perms.append({"action": "edit", "resource": pattern, "effect": "deny"})

    for path in restricted_paths or []:
        p = path.replace("\\", "/").rstrip("/")
        perms.append({"action": "read", "resource": f"{p}/*", "effect": "deny"})
        perms.append({"action": "edit", "resource": f"{p}/*", "effect": "deny"})
        # shell 的 resource 是命令文本，官方文档说目录推断是尽力而为，
        # 这里只挡字面路径（补漏，不是保证）
        perms.append({"action": "shell", "resource": f"*{p}*", "effect": "deny"})

    for name in SECRET_FILES:
        perms.append({"action": "read", "resource": name, "effect": "deny"})
        perms.append({"action": "shell", "resource": f"*{name}*", "effect": "deny"})

    return perms
