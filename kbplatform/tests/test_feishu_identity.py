"""飞书身份映射：open_id ↔ 平台账号，首次私聊自动建号。"""
import os

import pytest

from kbplatform.auth import create_user, verify_login
from kbplatform.db import init_db
from kbplatform.feishu.identity import (Identity, find_opencode_cli,
                                         platform_username)


def test_platform_username_is_stable_and_unique():
    a = platform_username("ou_abc")
    b = platform_username("ou_abc")
    c = platform_username("ou_xyz")
    assert a == b and a != c
    assert a.startswith("fsu_") and len(a) == 14


def test_get_or_create_creates_platform_user_once(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid1, new1 = ident.get_or_create("ou_abc", name="张三")
    uid2, new2 = ident.get_or_create("ou_abc", name="张三")
    assert new1 is True and new2 is False
    assert uid1 == uid2
    row = conn.execute("SELECT username, display_name, role, active FROM users WHERE id=?",
                       (uid1,)).fetchone()
    assert row["username"] == platform_username("ou_abc")
    assert row["display_name"] == "张三"
    assert row["role"] == "user" and row["active"] == 1


def test_new_user_has_no_scopes(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid, _ = ident.get_or_create("ou_abc")
    n = conn.execute("SELECT COUNT(*) FROM user_scopes WHERE user_id=?", (uid,)).fetchone()[0]
    assert n == 0


def test_auto_created_user_cannot_log_in(tmp_path):
    """自动建号的密码是随机的，没人知道，所以这个账号登不进来。"""
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    ident.get_or_create("ou_abc")
    assert verify_login(conn, platform_username("ou_abc"), "") is None
    assert verify_login(conn, platform_username("ou_abc"), "password") is None


def test_user_id_for_returns_none_for_unknown(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    assert Identity(conn).user_id_for("ou_nobody") is None


def test_user_id_for_returns_mapped_user(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid, _ = ident.get_or_create("ou_abc")
    assert ident.user_id_for("ou_abc") == uid


def test_is_active_reflects_users_active_column(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid, _ = ident.get_or_create("ou_abc")
    assert ident.is_active(uid) is True
    conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    conn.commit()
    assert ident.is_active(uid) is False


def test_get_or_create_updates_last_seen(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    ident.get_or_create("ou_abc")
    before = conn.execute("SELECT last_seen_at FROM feishu_users").fetchone()[0]
    assert before is not None


def test_get_or_create_records_dept_and_union_id(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    ident.get_or_create("ou_abc", name="李四", dept="od_dept1", union_id="on_union")
    row = conn.execute("SELECT feishu_name, feishu_union_id FROM feishu_users").fetchone()
    assert row["feishu_name"] == "李四"
    assert row["feishu_union_id"] == "on_union"


def test_find_opencode_cli_returns_existing_path_or_empty():
    p = find_opencode_cli()
    assert p == "" or (p.endswith("opencode-cli.exe") and os.path.isfile(p))
