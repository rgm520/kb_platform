"""管理台按角色分权的回归测试。

规则（2026-10-24 用户口径）：
  * 普通用户**能进管理台**，但只碰自己的东西：
    - API 密钥：只看得到/管得了自己账号的（可以给自己开）
    - 已接入智能体：只看得到"自己账号关联密钥所接入的"（agents.user_id = 密钥所属人）
    - 调用审计：只看得到自己账号关联智能体调用的记录
  * 管理员看全部。
  * 用户与权限 / 存储接入 / 模型配置仍然只对管理员开放。
"""
from __future__ import annotations

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


def _user(app, name, role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    return uid


def _login(app, name, role="user"):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _agent(conn, user_id, name):
    conn.execute(
        "INSERT INTO agents(user_id,name,kind,client_id,meta,created_at,last_seen_at) "
        "VALUES (?,?,'client','cid','{}',0,0)", (user_id, name))
    conn.commit()


def _audit(conn, user_id, prefix, action="query"):
    conn.execute(
        "INSERT INTO audit_log(user_id, token_prefix, action, question, hit_paths, model_used,"
        " restricted, ip, created_at) VALUES (?,?,?,?,?,?,0,'',0)",
        (user_id, prefix, action, "q", "[]", "m"))
    conn.commit()


def test_normal_user_sees_only_own_tokens(app):
    _uid_a = _user(app, "alice")
    _uid_b = _user(app, "bob")
    conn = app.state.conn
    issue_token(conn, _uid_a, "alice-token")
    issue_token(conn, _uid_b, "bob-token")
    c = _login(app, "alice")
    toks = c.get("/api/v1/admin/tokens").json()["tokens"]
    assert len(toks) == 1
    assert toks[0]["label"] == "alice-token"


def test_normal_user_can_issue_and_revoke_own_token_only(app):
    uid_a = _user(app, "alice")
    uid_b = _user(app, "bob")
    conn = app.state.conn
    _, tid_b = issue_token(conn, uid_b, "bob-token")
    c = _login(app, "alice")
    # 给自己签发
    r = c.post("/api/v1/admin/tokens", json={"label": "mine", "ttl_days": 30})
    assert r.status_code == 200, r.text
    assert r.json()["token"]
    # 不能给别人签发
    r = c.post(f"/api/v1/admin/tokens?user_id={uid_b}", json={"label": "x", "ttl_days": 30})
    assert r.status_code == 403, r.text
    # 不能吊销别人的
    assert c.delete(f"/api/v1/admin/tokens/{tid_b}").status_code == 403
    # 能吊销自己的
    toks = c.get("/api/v1/admin/tokens").json()["tokens"]
    assert c.delete(f"/api/v1/admin/tokens/{toks[0]['id']}").status_code == 200


def test_admin_sees_all_tokens(app):
    _uid_a = _user(app, "alice")
    _uid_b = _user(app, "bob")
    conn = app.state.conn
    issue_token(conn, _uid_a, "alice-token")
    issue_token(conn, _uid_b, "bob-token")
    c = _login(app, "root", role="admin") if False else None
    conn.execute("UPDATE users SET role='admin' WHERE username='alice'")
    conn.commit()
    c = _login(app, "alice")
    toks = c.get("/api/v1/admin/tokens").json()["tokens"]
    assert len(toks) == 2, "管理员应看到全部密钥"


def test_agents_and_audit_are_scoped_by_owner(app):
    uid_a = _user(app, "alice")
    uid_b = _user(app, "bob")
    conn = app.state.conn
    _agent(conn, uid_a, "alice-agent")
    _agent(conn, uid_b, "bob-agent")
    _audit(conn, uid_a, "kb_alice")
    _audit(conn, uid_b, "kb_bob")

    c = _login(app, "alice")
    agents = c.get("/api/v1/admin/agents").json()["agents"]
    assert [a["name"] for a in agents] == ["alice-agent"]
    audit = c.get("/api/v1/admin/audit").json()["audit"]
    assert [a["token_prefix"] for a in audit] == ["kb_alice"]

    # 管理员看全部
    conn.execute("UPDATE users SET role='admin' WHERE username='alice'")
    conn.commit()
    c = _login(app, "alice")
    assert len(c.get("/api/v1/admin/agents").json()["agents"]) == 2
    assert len(c.get("/api/v1/admin/audit").json()["audit"]) == 2


def test_admin_only_areas_stay_closed_to_normal_users(app):
    """用户与权限 / 存储接入 / 模型管理仍然只对管理员开放。"""
    _user(app, "alice")
    c = _login(app, "alice")
    for path in ("/api/v1/admin/users", "/api/v1/admin/sources", "/api/v1/admin/restricted",
                 "/api/v1/admin/users/1/grants"):
        assert c.get(path).status_code == 403, f"{path} 不该对普通用户开放"
    # 模型管理的写操作也要拦住（读模型列表是问答页要用的，对所有人开放）
    assert c.post("/api/v1/admin/models", json={
        "name": "x", "provider": "ollama", "base_url": "http://x", "model_id": "y"
    }).status_code == 403
