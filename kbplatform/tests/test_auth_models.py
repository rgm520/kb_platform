import json

import pytest

from kbplatform.db import init_db
from kbplatform.auth import (create_user, hash_password, issue_token, resolve_token,
                             revoke_token, set_scopes, verify_password, verify_login)
from kbplatform.models import (PRESETS, add_model, list_models, route_for, seed_presets)


# ---------------------------------------------------------------- 密码
def test_password_hash_is_salted_and_verifiable():
    h1 = hash_password("秘密123")
    h2 = hash_password("秘密123")
    assert h1 != h2                      # 两次哈希不同（随机盐）
    assert verify_password(h1, "秘密123")
    assert not verify_password(h1, "错密码")


def test_verify_login_rejects_wrong_password(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    create_user(conn, "alice", "pw123456", role="user")
    assert verify_login(conn, "alice", "pw123456") is not None
    assert verify_login(conn, "alice", "nope") is None
    assert verify_login(conn, "ghost", "pw123456") is None


def test_inactive_user_cannot_login(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "bob", "pw123456")
    conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    conn.commit()
    assert verify_login(conn, "bob", "pw123456") is None


# ---------------------------------------------------------------- token
def test_token_is_stored_hashed_not_plaintext(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "carol", "pw123456")
    raw, tid = issue_token(conn, uid, "我的电脑")
    row = conn.execute("SELECT token_hash, prefix FROM api_tokens WHERE id=?", (tid,)).fetchone()
    assert raw not in row[0]             # 库里不能有明文
    assert row[1] == raw[:12]            # 只留可识别的前缀
    assert raw.startswith("kb_")


def test_resolve_token_returns_row_for_valid_token(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "dave", "pw123456")
    raw, _ = issue_token(conn, uid)
    row = resolve_token(conn, raw)
    assert row is not None and row["user_id"] == uid


def test_resolve_token_rejects_garbage_and_revoked(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "erin", "pw123456")
    raw, tid = issue_token(conn, uid)
    assert resolve_token(conn, "kb_garbage") is None
    assert resolve_token(conn, "") is None
    assert resolve_token(conn, "not-a-token") is None
    revoke_token(conn, tid)
    assert resolve_token(conn, raw) is None


def test_expired_token_is_rejected(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "frank", "pw123456")
    raw, _ = issue_token(conn, uid, ttl_days=0)
    # ttl_days=0 -> 无过期；改成过去的过期时间
    conn.execute("UPDATE api_tokens SET expires_at=? WHERE token_hash=?",
                 (1, __import__("hashlib").sha256(raw.encode()).hexdigest()))
    conn.commit()
    assert resolve_token(conn, raw) is None


# ---------------------------------------------------------------- scope
def test_set_scopes_replaces_previous(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "gina", "pw123456")
    set_scopes(conn, uid, [("A", "x")])
    set_scopes(conn, uid, [("B", "y")])
    rows = conn.execute("SELECT share, subpath FROM user_scopes WHERE user_id=?",
                         (uid,)).fetchall()
    assert [tuple(r) for r in rows] == [("B", "y")]


# ---------------------------------------------------------------- 模型
def test_seed_presets_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    n1 = seed_presets(conn)
    n2 = seed_presets(conn)
    assert n1 == len(PRESETS)
    assert n2 == 0
    assert len(list_models(conn)) == len(PRESETS)


def test_presets_include_local_and_cloud(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    seed_presets(conn)
    ms = list_models(conn)
    assert any(m.is_local for m in ms)
    assert any(not m.is_local for m in ms)
    assert any(m.model_id == "qwen3.5-9b-64k" for m in ms)


def test_route_for_switches_cloud_to_local_when_restricted(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    local = add_model(conn, "本地", "ollama", "http://localhost:11434",
                      "qwen3.5-9b-64k", is_local=True)
    cloud = add_model(conn, "云端", "openai_compatible", "https://x/v1", "gpt-4o")
    cloud_m = [m for m in list_models(conn) if m.id == cloud][0]
    local_m = [m for m in list_models(conn) if m.id == local][0]

    used, switched = route_for(True, cloud_m, [local_m])
    assert switched is True
    assert used.is_local is True

    used2, switched2 = route_for(False, cloud_m, [local_m])
    assert switched2 is False
    assert used2.id == cloud


def test_route_for_keeps_local_when_restricted(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    local = add_model(conn, "本地", "ollama", "http://localhost:11434",
                      "qwen3.5-9b-64k", is_local=True)
    local_m = [m for m in list_models(conn) if m.id == local][0]
    used, switched = route_for(True, local_m, [local_m])
    assert switched is False
    assert used.id == local
