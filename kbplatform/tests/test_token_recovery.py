"""密钥（token）的可恢复性与失效诊断。

背景：2026-10-22 出了两次"密钥又失效"的事故——一次是管理员把前缀
当密钥发出去，一次是清理脚本把正在用的密钥删了。根因是密钥**只存哈希**，
明文只在签发那一刻出现一次，一旦丢失就只能换密钥，而换密钥会让所有
正在用旧密钥的智能体立刻 401。

这里锁住三件事：
  1. 密钥同时存可恢复密文，管理员能随时取回完整明文
  2. 失效原因能被区分（前缀 / 吊销 / 过期 / 不存在）
  3. 已知明文的密钥能被原样恢复（客户端不用改配置）
"""
import pytest

from kbplatform.db import init_db
from kbplatform.auth import (create_user, diagnose_token, get_encryption_key,
                             issue_token, resolve_token, restore_token,
                             revoke_token, set_encryption_key, token_plaintext)


@pytest.fixture
def conn(tmp_path):
    """建库并注入测试主密钥；用完恢复原来的全局状态（别影响别的测试）。"""
    saved = get_encryption_key()
    set_encryption_key("test-secret-abc", True)
    c = init_db(str(tmp_path / "kb.db"))
    yield c
    set_encryption_key(saved[0], saved[1])


# ---------------------------------------------------------------- 可恢复
def test_issued_token_can_be_recovered(conn):
    """签发的密钥必须能再取回完整明文——否则丢失就只能换密钥。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    got = token_plaintext(conn, tid)
    assert got == raw
    assert len(got) == 35


def test_recovered_token_still_authenticates(conn):
    """取回的明文必须仍然能通过校验。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    assert resolve_token(conn, token_plaintext(conn, tid)) is not None


def test_token_hash_is_not_the_plaintext(conn):
    """库里存的是哈希 + 密文，明文本身不落库。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    row = conn.execute("SELECT token_hash, token_enc FROM api_tokens WHERE id=?",
                        (tid,)).fetchone()
    assert raw not in row["token_hash"]
    assert raw not in row["token_enc"]
    assert row["token_enc"].startswith("v1.")


def test_different_master_key_cannot_decrypt(conn):
    """换了主密钥就解不开——密文不是明文编码。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    set_encryption_key("另一个主密钥", True)
    assert token_plaintext(conn, tid) is None
    set_encryption_key("test-secret-abc", True)
    assert token_plaintext(conn, tid) == raw


def test_recoverable_can_be_disabled(tmp_path):
    """关掉可恢复时就不存密文（回到纯哈希模式）。"""
    set_encryption_key("test-secret-abc", False)
    c = init_db(str(tmp_path / "kb.db"))
    uid = create_user(c, "alice", "pw123456")
    raw, tid = issue_token(c, uid, "test", 30)
    row = c.execute("SELECT token_enc FROM api_tokens WHERE id=?", (tid,)).fetchone()
    assert row["token_enc"] is None
    assert token_plaintext(c, tid) is None
    assert resolve_token(c, raw) is not None      # 仍然能用
    set_encryption_key(None, False)


# ---------------------------------------------------------------- 诊断
def test_diagnose_prefix(conn):
    """拿到前缀而不是密钥——必须能认出来。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    assert diagnose_token(conn, raw[:12]) == "prefix"


def test_diagnose_revoked(conn):
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    revoke_token(conn, tid)
    assert diagnose_token(conn, raw) == "revoked"


def test_diagnose_expired(conn):
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    conn.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", (1, tid))
    conn.commit()
    assert diagnose_token(conn, raw) == "expired"


def test_diagnose_unknown(conn):
    assert diagnose_token(conn, "kb_zzzzzzzzzzzzzzzzzzzzzzzzzzz") == "unknown"
    assert diagnose_token(conn, "") == "unknown"


# ---------------------------------------------------------------- 恢复
def test_restore_token_is_idempotent(conn):
    """把已知明文的密钥写回库：重复调用不产生重复行。"""
    uid = create_user(conn, "alice", "pw123456")
    raw = "kb_IeQbzdTDvN75_U5N1BbzejLGWzUCmLgO"
    tid1 = restore_token(conn, raw, uid, "agent", 365)
    tid2 = restore_token(conn, raw, uid, "agent", 365)
    assert tid1 == tid2
    n = conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
    assert n == 1
    assert resolve_token(conn, raw) is not None


def test_restore_token_reactivates_revoked(conn):
    """恢复一枚被吊销的密钥时，它会重新可用。"""
    uid = create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "test", 30)
    revoke_token(conn, tid)
    restore_token(conn, raw, uid, "test", 30)
    assert resolve_token(conn, raw) is not None


def test_restore_token_requires_kb_prefix(conn):
    uid = create_user(conn, "alice", "pw123456")
    with pytest.raises(ValueError):
        restore_token(conn, "not-a-token", uid)
