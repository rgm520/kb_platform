"""认证与授权：密码、token、会话。

安全约束：
  * 密码用 argon2 哈希（不可逆）
  * **token 只存哈希**，库里另存 prefix 便于人工识别；明文只在签发时返回一次
  * token 可吊销、可过期
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time

TOKEN_PREFIX = "kb_"
TOKEN_BYTES = 24
DEFAULT_TOKEN_TTL_DAYS = 90


# ---------------------------------------------------------------- 密码
def _get_hasher():
    try:
        import argon2
        return argon2.PasswordHasher()
    except ImportError:
        return None


def hash_password(password: str) -> str:
    """返回 `argon2$...` 或（无 argon2 时）`pbkdf2$...`。"""
    h = _get_hasher()
    if h is not None:
        return "argon2$" + h.hash(password)
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def verify_password(stored: str, password: str) -> bool:
    try:
        if stored.startswith("argon2$"):
            h = _get_hasher()
            if h is None:
                return False
            return h.verify(stored[len("argon2$"):], password)
        if stored.startswith("pbkdf2$"):
            _, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(salt_hex), 200_000)
            return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- token
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn: sqlite3.Connection, user_id: int, label: str | None = None,
                ttl_days: int = DEFAULT_TOKEN_TTL_DAYS) -> tuple[str, int]:
    """签发 token。返回 (明文 token, token_id)。明文只在这一次出现。"""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    expires = time.time() + ttl_days * 86400 if ttl_days else None
    cur = conn.execute(
        "INSERT INTO api_tokens(user_id, token_hash, prefix, label, created_at, expires_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, _token_hash(raw), raw[:12], label, time.time(), expires))
    conn.commit()
    return raw, cur.lastrowid


def resolve_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """校验 token 并返回对应的 api_tokens 行（含 user_id）。无效/吊销/过期返回 None。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE token_hash=?", (_token_hash(token),)).fetchone()
    if row is None:
        return None
    if row["revoked"]:
        return None
    if row["expires_at"] and row["expires_at"] < time.time():
        return None
    conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
    conn.commit()
    return row


def revoke_token(conn: sqlite3.Connection, token_id: int) -> bool:
    cur = conn.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------- 用户
def create_user(conn: sqlite3.Connection, username: str, password: str,
                role: str = "user", display_name: str | None = None,
                dept: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, dept, active, created_at)"
        " VALUES (?,?,?,?,?,1,?)",
        (username, display_name, hash_password(password), role, dept, time.time()))
    conn.commit()
    return cur.lastrowid


def get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    u = get_user(conn, username)
    if u is None or not u["active"]:
        return None
    if not verify_password(u["password_hash"], password):
        return None
    return u


def set_scopes(conn: sqlite3.Connection, user_id: int, scopes: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM user_scopes WHERE user_id=?", (user_id,))
    for share, subpath in scopes:
        conn.execute("INSERT OR IGNORE INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                     (user_id, share, (subpath or "").strip("\\")))
    conn.commit()
