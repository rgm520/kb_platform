"""普通用户能在「API 密钥」页下载 API 调用手册（与技能包）。

背景（2026-10-24）：手册/技能包接口挂在 /admin/ 且要求管理员权限，
普通用户点「📖 API 调用手册」直接 403，下载不了。
手册是**客户端说明书**（不含管理侧数据），应向所有登录用户开放，
并附上调用者自己的可见目录。
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


def _login(app, name, role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_normal_user_can_get_and_download_api_guide(app):
    c = _login(app, "zhangsan")
    r = c.get("/api/v1/admin/api-guide")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["guide"].strip()
    assert body["base_url"]
    # 手册里带自己的可见目录
    assert "scope" in body


def test_guide_contains_callers_own_scope(app):
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
                 "VALUES ('共享','smb','\\\\nas\\\\共享',1,0)")
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c = _login(app, "zhangsan")
    uid = conn.execute("SELECT id FROM users WHERE username='zhangsan'").fetchone()[0]
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) "
                 "VALUES (?,?,?,0,0)", (uid, sid, "制度"))
    conn.commit()
    body = c.get("/api/v1/admin/api-guide").json()
    assert "共享" in body["scope"]
    assert "制度" in body["scope"]
    assert "共享" in body["guide"]


def test_normal_user_can_download_skill_package(app):
    c = _login(app, "zhangsan")
    r = c.get("/api/v1/admin/skill-package")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    assert len(r.content) > 1000


def test_cannot_use_another_users_token_id_in_guide(app):
    c_admin = _login(app, "root", role="admin")
    c_user = _login(app, "lisi")
    # 管理员给自己签一枚
    admin_id = c_admin.get("/api/v1/me").json()["user"]["id"]
    tid = c_admin.post(f"/api/v1/admin/tokens?user_id={admin_id}",
                       json={"label": "admin-token", "ttl_days": 30}).json()["id"]
    # lisi 不能引用别人的密钥（会带出别人的密钥信息）
    r = c_user.get(f"/api/v1/admin/api-guide?token_id={tid}")
    assert r.status_code == 403, "普通用户不应能引用别人的密钥"
    # 用自己的可以
    own = c_user.post("/api/v1/admin/tokens", json={"label": "mine", "ttl_days": 30}).json()["id"]
    assert c_user.get(f"/api/v1/admin/api-guide?token_id={own}").status_code == 200
