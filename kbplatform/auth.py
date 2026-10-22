"""认证与授权：密码、token、会话。

安全约束：
  * 密码用 argon2 哈希（不可逆）
  * token **同时**存哈希与加密密文：
      - `token_hash`  校验用，永远不外泄
      - `token_enc`    AES-GCM 密文，密钥由 config.toml 的 session_secret 派生，
                      让管理员能在管理台**重新查看**完整密钥
  * token 可吊销、可过期

为什么加 `token_enc`（可恢复明文）：
    只存哈希时，密钥明文只在签发那一刻出现一次。密钥一旦丢失/被重新签发，
    所有正在使用旧密钥的智能体**立刻全线 401**，而且没有任何补救办法——
    只能再签发一枚，等于把故障从"管理员这边"转嫁给"所有客户端"。
    内部平台的��理员本身就能读全部文档、签发密钥，多存一份可恢复密文
    不构成权限提升，却让"密钥失效"这类故障**可以就地修复而不是换密钥**。
    密钥由 session_secret 派生，密文只对本平台实例可解。
    想回到纯哈希模式：config.toml 里设 [security] token_recoverable = false。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

TOKEN_PREFIX = "kb_"
TOKEN_BYTES = 24
DEFAULT_TOKEN_TTL_DAYS = 90

# 密钥密文的加解密密钥，由 set_encryption_key() 注入（进程内全局）
_ENC_KEY: bytes | None = None
_ENC_SECRET: str | None = None
# 是否允许保存可恢复密文（默认允许；config.toml 可关掉）
_ENC_ENABLED = True


def set_encryption_key(secret: str | None, enabled: bool = True) -> None:
    """注入密钥加密主密钥。secret 为空则不保存可恢复密文。"""
    global _ENC_KEY, _ENC_ENABLED, _ENC_SECRET
    _ENC_ENABLED = enabled
    _ENC_SECRET = secret or None
    if secret:
        _ENC_KEY = hashlib.sha256(
            ("kb-token-enc-v1|" + secret).encode("utf-8")).digest()
    else:
        _ENC_KEY = None


def get_encryption_key() -> tuple[str | None, bool]:
    """取当前的主密钥原文与开关（供测试保存/恢复全局状态）。"""
    return _ENC_SECRET, _ENC_ENABLED


def _encrypt(plain: str) -> str | None:
    """AES-GCM 加密。返回 'v1.<base64(nonce+密文+tag)>'；不可用时返回 None。"""
    if _ENC_KEY is None or not _ENC_ENABLED:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    nonce = os.urandom(12)
    blob = AESGCM(_ENC_KEY).encrypt(nonce, plain.encode("utf-8"), None)
    return "v1." + base64.urlsafe_b64encode(nonce + blob).decode("ascii")


def _decrypt(blob: str | None) -> str | None:
    if not blob or _ENC_KEY is None or not blob.startswith("v1."):
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(blob[3:].encode("ascii"))
        return AESGCM(_ENC_KEY).decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except Exception:
        return None



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
    """签发 token。返回 (明文 token, token_id)。

    明文除了当场返回，还会以 AES-GCM 密文存进 `token_enc`，
    管理员日后可以在管理台重新查看（见模块 docstring 的说明）。
    """
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    expires = time.time() + ttl_days * 86400 if ttl_days else None
    enc = _encrypt(raw)
    cur = conn.execute(
        "INSERT INTO api_tokens(user_id, token_hash, prefix, label, created_at, expires_at,"
        " token_enc) VALUES (?,?,?,?,?,?,?)",
        (user_id, _token_hash(raw), raw[:12], label, time.time(), expires, enc))
    conn.commit()
    return raw, cur.lastrowid


def restore_token(conn: sqlite3.Connection, raw: str, user_id: int,
                  label: str | None = None,
                  ttl_days: int = DEFAULT_TOKEN_TTL_DAYS) -> int:
    """把**已知明文**的密钥写回库里（迁移/恢复场景）。

    与 issue_token 的区别：明文是外面给进来的，不是随机生成的。
    用于把别处保存的密钥纳入平台管理，或恢复一条被误删的密钥——
    这样正在使用它的客户端不必改配置。

    幂等：同一枚密钥重复调用只会更新，不产生重复行。
    """
    if not raw.startswith(TOKEN_PREFIX):
        raise ValueError("密钥必须以 kb_ 开头")
    h = _token_hash(raw)
    enc = _encrypt(raw)
    expires = time.time() + ttl_days * 86400 if ttl_days else None
    row = conn.execute("SELECT id FROM api_tokens WHERE token_hash=?",
                       (h,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE api_tokens SET user_id=?, label=COALESCE(?,label), token_enc=?,"
            " expires_at=?, revoked=0 WHERE id=?",
            (user_id, label, enc, expires, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO api_tokens(user_id, token_hash, prefix, label, created_at,"
        " expires_at, token_enc) VALUES (?,?,?,?,?,?,?)",
        (user_id, h, raw[:12], label, time.time(), expires, enc))
    conn.commit()
    return cur.lastrowid


def token_plaintext(conn: sqlite3.Connection, token_id: int) -> str | None:
    """取回某枚密钥的完整明文。

    只有在保存了 `token_enc` 且主密钥正确时才拿得到；否则返回 None。
    """
    row = conn.execute("SELECT token_enc FROM api_tokens WHERE id=?",
                       (token_id,)).fetchone()
    if row is None:
        return None
    return _decrypt(row["token_enc"])


def diagnose_token(conn: sqlite3.Connection, presented: str) -> str:
    """判定一个无效密钥的失败原因（供 401 给出可执行的提示）。

    返回：
      "prefix"   对方拿的是密钥前缀（前 12 位），不是密钥本身
      "revoked"  密钥存在但已被吊销
      "expired"  密钥存在但已过期
      "unknown"  库里没有这枚密钥
    """
    if not presented:
        return "unknown"
    # 1) 是不是某枚密钥的前缀
    if len(presented) <= 12:
        row = conn.execute("SELECT id FROM api_tokens WHERE prefix=?",
                           (presented,)).fetchone()
        if row is not None:
            return "prefix"
    # 2) 是不是某枚密钥本身（哈希对得上，但被吊销/过期）
    row = conn.execute(
        "SELECT revoked, expires_at FROM api_tokens WHERE token_hash=?",
        (_token_hash(presented),)).fetchone()
    if row is not None:
        if row["revoked"]:
            return "revoked"
        if row["expires_at"] and row["expires_at"] < time.time():
            return "expired"
    return "unknown"


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


MIN_PASSWORD_LEN = 8


def change_password(conn: sqlite3.Connection, user_id: int, old_password: str,
                    new_password: str) -> tuple[bool, str]:
    """改密码。必须校验旧密码。

    返回 (是否成功, 错误信息)。
    """
    u = get_user_by_id(conn, user_id)
    if u is None:
        return False, "用户不存在"
    if not verify_password(u["password_hash"], old_password):
        return False, "原密码不正确"
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        return False, f"新密码至少 {MIN_PASSWORD_LEN} 位"
    if new_password == old_password:
        return False, "新密码不能与原密码相同"
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (hash_password(new_password), user_id))
    conn.commit()
    return True, ""


def set_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    """管理员直接重置密码（不校验旧密码）。"""
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (hash_password(new_password), user_id))
    conn.commit()
