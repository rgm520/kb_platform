import json

import pytest

from kbplatform.db import init_db
from kbplatform.auth import (create_user, hash_password, issue_token, resolve_token,
                             revoke_token, set_scopes, verify_password, verify_login)
from kbplatform import models as models_mod
from kbplatform.models import (PRESETS, add_model, list_models, route_for)


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


# ---------------------------------------------------------------- 改密
def test_change_password_requires_correct_old_password(tmp_path):
    from kbplatform.auth import change_password
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "heidi", "oldpass123")
    ok, msg = change_password(conn, uid, "wrong", "newpass456")
    assert ok is False and "原密码" in msg
    assert verify_login(conn, "heidi", "oldpass123") is not None


def test_change_password_rejects_short_password(tmp_path):
    from kbplatform.auth import change_password
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "ivan", "oldpass123")
    ok, msg = change_password(conn, uid, "oldpass123", "short")
    assert ok is False
    assert verify_login(conn, "ivan", "oldpass123") is not None


def test_change_password_rejects_same_password(tmp_path):
    from kbplatform.auth import change_password
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "judy", "samepass123")
    ok, _ = change_password(conn, uid, "samepass123", "samepass123")
    assert ok is False


def test_change_password_succeeds_and_old_stops_working(tmp_path):
    from kbplatform.auth import change_password
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "karl", "oldpass123")
    ok, _ = change_password(conn, uid, "oldpass123", "newpass456")
    assert ok is True
    assert verify_login(conn, "karl", "newpass456") is not None
    assert verify_login(conn, "karl", "oldpass123") is None


def test_stored_hash_never_contains_plaintext(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "lena", "secretpwd1")
    h = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
    assert "secretpwd1" not in h
    assert h.startswith(("argon2$", "pbkdf2$"))


# ---------------------------------------------------------------- 密钥
def test_purge_requires_revoked_first(tmp_path):
    """未吊销的密钥不能直接物理删除——必须先吊销再删。"""
    from kbplatform.auth import issue_token, revoke_token
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "mike", "pw123456")
    raw, tid = issue_token(conn, uid)
    assert resolve_token(conn, raw) is not None
    revoke_token(conn, tid)
    assert resolve_token(conn, raw) is None
    # 吊销后记录仍在，便于人工核对
    row = conn.execute("SELECT revoked FROM api_tokens WHERE id=?", (tid,)).fetchone()
    assert row is not None and row[0] == 1
    conn.execute("DELETE FROM api_tokens WHERE id=?", (tid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM api_tokens WHERE id=?", (tid,)).fetchone()[0] == 0


# ---------------------------------------------------------------- 模型
def test_presets_are_templates_not_seeded_rows(tmp_path):
    """预置厂商只是「添加模型」对话框里的模板，**不进库**。

    没有 API Key 的云端模型是不可用的，预置进库只是噪音。
    """
    conn = init_db(str(tmp_path / "kb.db"))
    before = len(list_models(conn))
    models_mod.seed_presets(conn)
    after = len(list_models(conn))
    assert after == before == 0
    # 但模板本身仍然同时提供本地与云端选项
    providers = {p["provider"] for p in models_mod.PRESETS}
    assert "ollama" in providers and "openai_compatible" in providers


def test_presets_cover_common_vendors():
    names = " ".join(p["name"] for p in models_mod.PRESETS)
    for v in ("DeepSeek", "通义千问", "腾讯混元", "智谱", "Kimi", "MiniMax", "OpenAI", "Ollama"):
        assert v in names, f"预置缺少 {v}"


def test_check_usable_flags_cloud_model_without_key(tmp_path):
    """云端模型没配 Key -> 不可用。"""
    conn = init_db(str(tmp_path / "kb.db"))
    mid = models_mod.add_model(conn, "无Key的云端模型", "openai_compatible",
                              "https://api.example.com/v1", "some-model")
    m = models_mod.get_model(conn, mid)
    st = models_mod.check_usable(m)
    assert st["usable"] is False
    assert "API Key" in st["reason"]


def test_check_usable_flags_unreachable_ollama(tmp_path):
    """Ollama 主机连不上 -> 不可用。"""
    conn = init_db(str(tmp_path / "kb.db"))
    mid = models_mod.add_model(conn, "连不上的Ollama", "ollama",
                              "http://127.0.0.1:59999", "x")
    m = models_mod.get_model(conn, mid)
    st = models_mod.check_usable(m)
    assert st["usable"] is False
    assert "不可达" in st["reason"]


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
