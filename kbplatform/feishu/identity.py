"""飞书身份映射：open_id ↔ kbplatform 平台账号。

设计见 `docs/superpowers/specs/2026-10-21-feishu-bot-design.md` §5.2。

约定：
  * 平台账号 username = `fsu_` + md5(open_id) 前 10 位（唯一、不可枚举）
  * 自动建号的密码是**随机**的，没人知道 → 该账号无法通过 Web 登录，
    平台账号只作为 scope / 审计的归属主体
  * 自动建号的账号**没有 user_scopes** → 默认可见范围为空
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time

from kbplatform.auth import create_user

# opencode CLI 在桌面端的安装位置，版本目录会变
_CLI_GLOB_ROOT = os.path.join("ai.opencode.desktop", "cli")


def platform_username(open_id: str) -> str:
    """飞书 open_id -> 平台账号 username。"""
    digest = hashlib.md5((open_id or "").encode("utf-8")).hexdigest()
    return "fsu_" + digest[:10]


def find_opencode_cli(explicit: str = "") -> str:
    """找 opencode-cli.exe。显式路径优先，否则扫描桌面端版本目录取最新。"""
    if explicit and os.path.isfile(explicit):
        return explicit
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    root = os.path.join(appdata, _CLI_GLOB_ROOT)
    if not os.path.isdir(root):
        return ""
    best, best_ver = "", ""
    for name in os.listdir(root):
        candidate = os.path.join(root, name, "opencode-cli.exe")
        if os.path.isfile(candidate) and name > best_ver:
            best, best_ver = candidate, name
    return best


class Identity:
    """飞书用户 <-> 平台账号。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ 查询
    def user_id_for(self, open_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT user_id FROM feishu_users WHERE feishu_open_id=?", (open_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def is_active(self, user_id: int) -> bool:
        row = self.conn.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(row and row[0])

    def role_of(self, user_id: int) -> str:
        row = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        return (row[0] if row else "user") or "user"

    # ------------------------------------------------------------ 建号
    def get_or_create(self, open_id: str, name: str | None = None,
                      dept: str | None = None,
                      union_id: str | None = None) -> tuple[int, bool]:
        """返回 (平台账号 id, 是否本次新建)。"""
        row = self.conn.execute(
            "SELECT id, user_id FROM feishu_users WHERE feishu_open_id=?", (open_id,)).fetchone()
        if row is not None and row[1] is not None:
            self.conn.execute("UPDATE feishu_users SET last_seen_at=? WHERE id=?",
                              (time.time(), row[0]))
            self.conn.commit()
            return int(row[1]), False

        display = name or "飞书用户"
        uid = create_user(self.conn, platform_username(open_id),
                          secrets.token_urlsafe(32), role="user",
                          display_name=display, dept=dept)
        now = time.time()
        if row is None:
            self.conn.execute(
                "INSERT INTO feishu_users(feishu_open_id, feishu_union_id, feishu_name,"
                " user_id, created_at, last_seen_at) VALUES(?,?,?,?,?,?)",
                (open_id, union_id, display, uid, now, now))
        else:
            self.conn.execute(
                "UPDATE feishu_users SET feishu_union_id=COALESCE(?,feishu_union_id),"
                " feishu_name=?, user_id=?, last_seen_at=? WHERE id=?",
                (union_id, display, uid, now, row[0]))
        self.conn.commit()
        return uid, True
