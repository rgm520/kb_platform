"""平台侧：用户停用开关（管理台唯一的止损路径）。"""
import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    data_dir = str(tmp_path / "data").replace("\\", "/")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[paths]\ndata_dir = "{data_dir}"\n'
        f'[models]\nembed_dim = 1024\n', encoding="utf-8")
    return make_app(str(cfg_path))


def _admin_client(app):
    conn = app.state.conn
    create_user(conn, "admin", "pw123456", role="admin")
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw123456"})
    assert r.status_code == 200
    return c


def test_admin_can_deactivate_user(app):
    conn = app.state.conn
    uid = create_user(conn, "bob", "pw123456")
    c = _admin_client(app)
    r = c.put(f"/api/v1/admin/users/{uid}/active", json={"active": False})
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert conn.execute("SELECT active FROM users WHERE id=?", (uid,)).fetchone()[0] == 0


def test_admin_can_reactivate_user(app):
    conn = app.state.conn
    uid = create_user(conn, "bob", "pw123456")
    conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    conn.commit()
    c = _admin_client(app)
    r = c.put(f"/api/v1/admin/users/{uid}/active", json={"active": True})
    assert r.status_code == 200
    assert conn.execute("SELECT active FROM users WHERE id=?", (uid,)).fetchone()[0] == 1


def test_inactive_user_cannot_use_token(app):
    """停用后，该用户已签发的 token 立刻失效——这才是真正的止损。"""
    from kbplatform.auth import issue_token
    conn = app.state.conn
    uid = create_user(conn, "bob", "pw123456")
    raw, _ = issue_token(conn, uid, "手工", ttl_days=30)
    c = _admin_client(app)
    # 停用前，token 可用
    r = c.get("/api/v1/documents/stats", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    c.put(f"/api/v1/admin/users/{uid}/active", json={"active": False})
    r = c.get("/api/v1/documents/stats", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401


def test_non_admin_cannot_toggle(app):
    conn = app.state.conn
    uid = create_user(conn, "bob", "pw123456")
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "bob", "password": "pw123456"})
    r = c.put(f"/api/v1/admin/users/{uid}/active", json={"active": False})
    assert r.status_code == 403


def test_anonymous_cannot_toggle(app):
    conn = app.state.conn
    uid = create_user(conn, "bob", "pw123456")
    c = TestClient(app)
    r = c.put(f"/api/v1/admin/users/{uid}/active", json={"active": False})
    assert r.status_code == 401
