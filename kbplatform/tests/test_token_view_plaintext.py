"""「查看密钥」接口：普通用户查自己的密钥会 500。

背景（2026-10-28）：普通用户在「API 密钥」页点「查看密钥」，
页面弹 "取不到明文: Internal Server Error"。

根因：这个端点从 `require_user` 改成了 `actor`（为了让**带 Bearer 密钥的
AI 智能体**也能取回自己的密钥），但审计那一行没跟着改，还在读旧形状的
`ctx["id"]`。`actor` 返回的是嵌套 dict（`{"user": {...}}`），没有顶层 `id`
→ KeyError → 500。

修的是「读错 ctx 形状」这一处，不是加 try/except 把异常吞掉：
审计写不进去应该不影响「把密钥给用户」这件事（参考 audit() 的容错风格）。
"""
import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user, issue_token
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _login(app, name, role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_normal_user_can_view_own_token_plaintext(app):
    """普通用户必须能取回自己那枚密钥的完整明文。"""
    conn = app.state.conn
    c = _login(app, "alice")
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    raw, tid = issue_token(conn, uid, "alice-token")

    r = c.get(f"/api/v1/admin/tokens/{tid}/plaintext")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] == raw, "必须返回完整密钥，不是前缀"
    assert body["prefix"] == raw[:12]
    assert body["revoked"] is False


def test_admin_can_view_other_users_token_plaintext(app):
    """管理员能取回别人的密钥（交给新客户端用）。"""
    conn = app.state.conn
    _login(app, "root", role="admin")
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0] \
        if conn.execute("SELECT count(*) FROM users WHERE username='alice'").fetchone()[0] \
        else create_user(conn, "alice", "pw123456")
    raw, tid = issue_token(conn, uid, "alice-token")

    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "root", "password": "pw123456"})
    r = c.get(f"/api/v1/admin/tokens/{tid}/plaintext")
    assert r.status_code == 200, r.text
    assert r.json()["token"] == raw


def test_cannot_view_other_users_token_plaintext(app):
    """普通用户不能查别人的密钥。"""
    conn = app.state.conn
    _login(app, "alice")
    bob = create_user(conn, "bob", "pw123456")
    _raw, tid = issue_token(conn, bob, "bob-token")

    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "alice", "password": "pw123456"})
    r = c.get(f"/api/v1/admin/tokens/{tid}/plaintext")
    assert r.status_code == 403, r.text


def test_viewing_own_token_with_bearer_works(app):
    """带 Bearer 密钥的智能体也能取回自己的密钥（端点用 actor 就是为这个）。"""
    conn = app.state.conn
    c = _login(app, "alice")
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    raw, tid = issue_token(conn, uid, "alice-token")

    r = c.get(f"/api/v1/admin/tokens/{tid}/plaintext",
              headers={"Authorization": "Bearer " + raw})
    assert r.status_code == 200, r.text
    assert r.json()["token"] == raw


def test_token_view_is_audited(app):
    """取看密钥要留痕。"""
    conn = app.state.conn
    c = _login(app, "alice")
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    _raw, tid = issue_token(conn, uid, "alice-token")

    assert c.get(f"/api/v1/admin/tokens/{tid}/plaintext").status_code == 200
    row = conn.execute("SELECT user_id, action FROM audit_log WHERE action='token_view' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "查看密钥必须落审计"
    assert row["user_id"] == uid


def test_audit_failure_does_not_block_key_recovery(app):
    """审计写库失败也不能挡住「把密钥还给用户」。

    这个接口存在的意义就是：密钥丢了能在原地找回，不用换密钥（换密钥会让
    所有正在用旧密钥的智能体立刻 401）。所以它不该因为**旁路**的审计写不进去
    而 500——那正好把它废掉了。
    """
    conn = app.state.conn
    c = _login(app, "alice")
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    raw, tid = issue_token(conn, uid, "alice-token")

    # 模拟审计表坏掉（写入报错而不是返回 False）
    conn.execute("DROP TABLE audit_log")
    conn.commit()

    r = c.get(f"/api/v1/admin/tokens/{tid}/plaintext")
    assert r.status_code == 200, r.text
    assert r.json()["token"] == raw
