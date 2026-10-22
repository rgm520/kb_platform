"""知识库凭证（TokenStore）与 agent 侧查询 helper。"""
import os
import subprocess
import sys

import pytest

from kbplatform.auth import create_user, issue_token, resolve_token
from kbplatform.db import init_db
from kbplatform.feishu.kbcred import TokenStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KBQ = os.path.join(ROOT, "tools", "kbq.py")


# ---------------------------------------------------------------- TokenStore
def test_token_is_issued_once_per_user(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    a = store.token_for(uid)
    b = store.token_for(uid)
    assert a == b
    n = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (uid,)).fetchone()[0]
    assert n == 1


def test_token_is_stored_hashed_not_plaintext(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    raw = TokenStore(conn).token_for(uid)
    row = conn.execute("SELECT token_hash FROM api_tokens WHERE user_id=?", (uid,)).fetchone()
    assert raw not in row[0]


def test_rotate_revokes_old_token(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    old = store.token_for(uid)
    new = store.rotate(uid)
    assert new != old
    assert resolve_token(conn, old) is None
    assert resolve_token(conn, new) is not None


def test_rotate_keeps_at_most_one_active_token(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    store.token_for(uid)
    store.rotate(uid)
    n = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=0",
                     (uid,)).fetchone()[0]
    assert n == 1


def test_prune_removes_revoked_bot_tokens(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    store.token_for(uid)
    store.rotate(uid)
    # 用户自己签的 token 不能被误删
    other, _ = issue_token(conn, uid, "手工签发", ttl_days=30)
    conn.execute("UPDATE api_tokens SET revoked=1 WHERE label='手工签发'")
    conn.commit()
    removed = store.prune_revoked()
    assert removed >= 1
    rows = conn.execute("SELECT label FROM api_tokens WHERE user_id=?", (uid,)).fetchall()
    labels = [r[0] for r in rows]
    assert "手工签发" in labels
    assert labels.count("feishu-bot") == 1


def test_write_session_token_creates_file(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    path = store.write_session_token("ses_1", uid, str(tmp_path))
    assert os.path.isfile(path)
    assert open(path, encoding="utf-8").read().strip() == store.token_for(uid)
    assert path.replace("\\", "/").endswith(".feishu/tokens/ses_1.token")


def test_write_session_token_overwrites(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    store.write_session_token("ses_1", uid, str(tmp_path))
    store.rotate(uid)
    path = store.write_session_token("ses_1", uid, str(tmp_path))
    assert open(path, encoding="utf-8").read().strip() == store.token_for(uid)


# ---------------------------------------------------------------- tools/kbq.py
def _run(args, token_file=None, base="http://127.0.0.1:1"):
    env = dict(os.environ)
    env["KB_API_BASE"] = base
    if token_file is None:
        env.pop("KB_TOKEN_FILE", None)
    else:
        env["KB_TOKEN_FILE"] = str(token_file)
    return subprocess.run([sys.executable, KBQ] + args,
                          capture_output=True, text=True, env=env, cwd=ROOT)


def test_missing_token_file_exits_3():
    r = _run(["search", "x"], token_file=os.devnull)
    assert r.returncode == 3
    assert "KB_AUTH_EXPIRED" in (r.stdout + r.stderr)


def test_unreachable_service_exits_4(tmp_path):
    f = tmp_path / "t.token"
    f.write_text("kb_fake", encoding="utf-8")
    r = _run(["query", "问题"], token_file=f)
    assert r.returncode == 4
    assert "KB_UNREACHABLE" in (r.stdout + r.stderr)


def test_help_works():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "query" in (r.stdout + r.stderr)
