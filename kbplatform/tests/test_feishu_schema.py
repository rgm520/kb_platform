"""飞书身份与会话映射表。"""
import sqlite3

import pytest

from kbplatform.db import init_db


def test_feishu_users_table_created(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feishu_users)")}
    assert {"feishu_open_id", "feishu_union_id", "feishu_name", "user_id",
            "created_at", "last_seen_at"} <= cols


def test_feishu_sessions_table_created(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feishu_sessions)")}
    assert {"feishu_user_id", "chat_id", "chat_type", "session_id", "model_id",
            "created_at", "updated_at"} <= cols


def test_feishu_open_id_is_unique(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")


def test_feishu_session_unique_per_user_and_chat(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")
    uid = conn.execute("SELECT id FROM feishu_users WHERE feishu_open_id='ou_1'").fetchone()[0]
    conn.execute("INSERT INTO feishu_sessions(feishu_user_id, chat_id, created_at, updated_at)"
                 " VALUES(?, 'oc_1', 1, 1)", (uid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feishu_sessions(feishu_user_id, chat_id, created_at, updated_at)"
                     " VALUES(?, 'oc_1', 1, 1)", (uid,))
    # 同一个用户换群是另一条
    conn.execute("INSERT INTO feishu_sessions(feishu_user_id, chat_id, created_at, updated_at)"
                 " VALUES(?, 'oc_2', 1, 1)", (uid,))


def test_init_db_is_idempotent(tmp_path):
    path = str(tmp_path / "kb.db")
    init_db(path)
    conn = init_db(path)   # 再建一次不能报错
    n = conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0]
    assert n == 0
