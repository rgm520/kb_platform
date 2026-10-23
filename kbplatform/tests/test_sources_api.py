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


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c, uid


def test_create_source_encrypts_password_and_never_returns_it(app, tmp_path):
    c, _ = _login(app)
    share = tmp_path / "share"
    share.mkdir()
    r = c.post("/api/v1/admin/sources", json={
        "name": "信息化管理", "kind": "local", "root": str(share),
        "username": "", "password": "p@ss", "writable": True, "enabled": True})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    body = c.get("/api/v1/admin/sources").json()
    assert body["sources"][0]["has_password"] is True
    assert "p@ss" not in r.text
    assert "p@ss" not in c.get("/api/v1/admin/sources").text
    assert c.post(f"/api/v1/admin/sources/{sid}/test").json()["ok"] is True
    assert c.post(f"/api/v1/admin/sources/{sid}/scan").status_code == 200
    st = c.get(f"/api/v1/admin/sources/{sid}/scan/status").json()
    assert st["id"] == sid


def test_update_source_keeps_password_when_blank(app, tmp_path):
    c, _ = _login(app)
    share = tmp_path / "share"
    share.mkdir()
    sid = c.post("/api/v1/admin/sources", json={
        "name": "共享", "kind": "local", "root": str(share),
        "password": "old-pass"}).json()["id"]
    r = c.put(f"/api/v1/admin/sources/{sid}", json={"name": "共享2", "password": ""})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "共享2" and r.json()["has_password"] is True
    assert "old-pass" not in r.text
    r = c.put(f"/api/v1/admin/sources/{sid}", json={"password": "new-pass"})
    assert r.status_code == 200 and r.json()["has_password"] is True


def test_set_and_read_user_grants(app, tmp_path):
    c, uid = _login(app)
    r = c.put(f"/api/v1/admin/users/{uid}/grants", json={"grants": [
        {"source_id": 1, "subpath": "A\\B", "can_write": True}]})
    assert r.status_code == 200, r.text
    body = c.get(f"/api/v1/admin/users/{uid}/grants").json()
    assert body["grants"] == [{"source_id": 1, "subpath": "A\\B", "can_write": True}]
    assert c.put("/api/v1/admin/users/99999/grants",
                 json={"grants": []}).status_code == 404


def test_tree_only_returns_dirs(app, tmp_path):
    c, _ = _login(app)
    share = tmp_path / "share"
    (share / "A" / "B").mkdir(parents=True)
    (share / "file.txt").write_text("x", encoding="utf-8")
    sid = c.post("/api/v1/admin/sources", json={
        "name": "共享", "kind": "local", "root": str(share)}).json()["id"]
    body = c.get(f"/api/v1/admin/sources/{sid}/tree?rel=").json()
    assert [d["name"] for d in body["dirs"]] == ["A"]
    body = c.get(f"/api/v1/admin/sources/{sid}/tree?rel=A").json()
    assert [d["name"] for d in body["dirs"]] == ["B"]
    assert c.get(f"/api/v1/admin/sources/{sid}/tree?rel=../").status_code == 400


def test_delete_source_requires_force_when_grants_exist(app, tmp_path):
    c, uid = _login(app)
    share = tmp_path / "share"
    share.mkdir()
    sid = c.post("/api/v1/admin/sources", json={
        "name": "共享", "kind": "local", "root": str(share)}).json()["id"]
    c.put(f"/api/v1/admin/users/{uid}/grants", json={"grants": [
        {"source_id": sid, "subpath": "", "can_write": False}]})
    assert c.delete(f"/api/v1/admin/sources/{sid}").status_code == 409
    assert c.delete(f"/api/v1/admin/sources/{sid}?force=1").status_code == 200
    assert c.get("/api/v1/admin/sources").json()["sources"] == []
    assert c.get(f"/api/v1/admin/users/{uid}/grants").json()["grants"] == []
