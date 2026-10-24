"""管理台「用户信息可修改」的后端回归测试。

需求：管理员能修改用户信息（显示名 / 角色 / 部门 / 启用状态），必要时重置密码；
同时要防止把最后一个管理员自己降级或停用，导致没人能进管理台。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _login(app, name="admin", role="admin", password="pw123456"):
    conn = app.state.conn
    create_user(conn, name, password, role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return c, uid


def test_admin_can_update_user_info(app):
    c, _ = _login(app)
    conn = app.state.conn
    create_user(conn, "zhangsan", "pw123456", role="user", display_name="张三", dept="信息部")
    uid = conn.execute("SELECT id FROM users WHERE username='zhangsan'").fetchone()[0]

    r = c.put(f"/api/v1/admin/users/{uid}", json={
        "display_name": "张三丰", "role": "admin", "dept": "信息化管理部"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "张三丰"
    assert body["role"] == "admin"
    assert body["dept"] == "信息化管理部"

    row = conn.execute("SELECT display_name, role, dept FROM users WHERE id=?", (uid,)).fetchone()
    assert tuple(row) == ("张三丰", "admin", "信息化管理部")


def test_admin_can_toggle_active_and_reset_password(app):
    c, _ = _login(app)
    conn = app.state.conn
    create_user(conn, "lisi", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]

    r = c.put(f"/api/v1/admin/users/{uid}", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    assert conn.execute("SELECT active FROM users WHERE id=?", (uid,)).fetchone()[0] == 0

    r = c.put(f"/api/v1/admin/users/{uid}", json={"active": True, "password": "newpw12345"})
    assert r.status_code == 200, r.text
    # 新密码能登录
    c2 = TestClient(app)
    assert c2.post("/api/v1/auth/login",
                    json={"username": "lisi", "password": "newpw12345"}).status_code == 200


def test_cannot_remove_last_admin_or_edit_self_into_lockout(app):
    c, uid = _login(app)
    conn = app.state.conn
    create_user(conn, "onlyadmin", "pw123456", role="admin")
    other = conn.execute("SELECT id FROM users WHERE username='onlyadmin'").fetchone()[0]

    # 自己降级：拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"role": "user"})
    assert r.status_code == 400, r.text
    # 停用自己：拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"active": False})
    assert r.status_code == 400, r.text
    # 降级另一个管理员：允许（自己仍是管理员，系统不会失去管理入口）
    r = c.put(f"/api/v1/admin/users/{other}", json={"role": "user"})
    assert r.status_code == 200, r.text
    # 现在自己是最后一个管理员：再降级自己 → 拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"role": "user"})
    assert r.status_code == 400, r.text
    # 停用自己同样拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"active": False})
    assert r.status_code == 400, r.text


def test_update_unknown_user_returns_404(app):
    c, _ = _login(app)
    assert c.put("/api/v1/admin/users/99999", json={"display_name": "x"}).status_code == 404


def test_create_user_rejects_empty_or_duplicate_username(app):
    c, _ = _login(app)
    assert c.post("/api/v1/admin/users",
                  json={"username": "  ", "password": "pw123456"}).status_code == 400
    assert c.post("/api/v1/admin/users",
                  json={"username": "a b", "password": "pw123456"}).status_code == 400
    assert c.post("/api/v1/admin/users",
                  json={"username": "admin", "password": "pw123456"}).status_code == 409


def test_username_can_be_changed_but_id_cannot(app):
    """用户名可改（登录标识），用户 ID 永远不可改。"""
    c, _ = _login(app)
    conn = app.state.conn
    create_user(conn, "wangwu", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='wangwu'").fetchone()[0]

    r = c.put(f"/api/v1/admin/users/{uid}", json={"username": "wangwu_new"})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "wangwu_new"
    row = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    assert row[0] == "wangwu_new"
    # ID 不变
    assert conn.execute("SELECT id FROM users WHERE username='wangwu_new'").fetchone()[0] == uid
    # 改完名仍然能用新名字登录
    c2 = TestClient(app)
    assert c2.post("/api/v1/auth/login",
                    json={"username": "wangwu_new", "password": "pw123456"}).status_code == 200


def test_username_must_be_unique_and_valid(app):
    c, _ = _login(app)
    conn = app.state.conn
    create_user(conn, "zhaoliu", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='zhaoliu'").fetchone()[0]
    # 与已有用户重名 → 拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"username": "admin"})
    assert r.status_code == 409, r.text
    # 空名 → 拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"username": "   "})
    assert r.status_code == 400, r.text
    # 含空格 → 拒绝
    r = c.put(f"/api/v1/admin/users/{uid}", json={"username": "a b"})
    assert r.status_code == 400, r.text
    # 原名不受影响
    assert conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()[0] == "zhaoliu"
