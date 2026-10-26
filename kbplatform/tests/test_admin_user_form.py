"""管理台「新建用户」的回归测试。

需求（2026-10-26，用户报障）：点「新建用户」提示"用户名不能为空"。
根因有四层，本文件逐条钉住：

1. 前端 `createUser()` 把 scopes 写死成 [] → 表单里的「共享/子目录」两个输入框
   形同摆设（后端其实一直支持 scopes）。
2. 「为所选用户授权目录」按钮调用了一个**从未定义**的 `addScope()` →
   点了毫无反应（只有控制台报错，用户看不到）。
3. 任何管理台动作都会重新渲染整个面板 → 管理员填了一半的表单被清空 →
   再点「新建用户」就报"用户名不能为空"。
4. 建号时给的初始可见范围只写进老表 user_scopes，**授权（grants）要等下次
   平台重启才同步** → 新建的用户在文件页要等重启才看得到目录。

另外：建号接口此前不校验密码（空密码也能建），与"改密码至少 8 位"的平台
策略不一致 —— 一并钉住。
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
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return c


def _add_source(conn, root, name="共享"):
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,writable,created_at) "
        "VALUES (?,?,?,1,1,0)", (name, "local", root))
    conn.commit()
    return conn.execute("SELECT id FROM storage_sources WHERE root=?", (root,)).fetchone()[0]


def test_create_user_with_scope_writes_grants_immediately(app):
    """建号时给的初始可见范围，授权（grants）要**立刻**生效，不能等平台重启。"""
    c = _login(app)
    conn = app.state.conn
    sid = _add_source(conn, r"\\nas\share")

    r = c.post("/api/v1/admin/users", json={
        "username": "newbie", "password": "pw123456", "role": "user",
        "display_name": "新人", "scopes": [["share", "sub"]]})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]

    rows = conn.execute("SELECT source_id, subpath FROM grants WHERE user_id=?", (uid,)).fetchall()
    assert len(rows) == 1, f"授权没有立刻写入 grants，实际：{rows}"
    assert tuple(rows[0]) == (sid, "sub")


def test_create_user_without_scope_writes_no_grants(app):
    c = _login(app)
    conn = app.state.conn
    _add_source(conn, r"\\nas\share")
    r = c.post("/api/v1/admin/users", json={
        "username": "nobody", "password": "pw123456", "role": "user"})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    assert conn.execute("SELECT COUNT(*) FROM grants WHERE user_id=?", (uid,)).fetchone()[0] == 0


def test_admin_user_list_shows_grant_scope(app):
    """列表里的「可见范围」要显示真正生效的授权（grants），不能只显示老范围。

    建号时给的初始范围写进了 grants，但老表是空的 —— 列表若只看老表，
    管理员会看到"（未授权）"，以为没给权限。
    """
    c = _login(app)
    conn = app.state.conn
    _add_source(conn, r"\\nas\share", name="共享")
    r = c.post("/api/v1/admin/users", json={
        "username": "scoped", "password": "pw123456", "role": "user",
        "scopes": [["共享", "子目录"]]})
    assert r.status_code == 200, r.text

    users = c.get("/api/v1/admin/users").json()["users"]
    row = next(u for u in users if u["username"] == "scoped")
    assert row["scopes"], "列表没有显示该用户的可见范围"
    assert any("子目录" in s for s in row["scopes"]), row["scopes"]


def test_create_user_rejects_empty_password(app):
    """空密码也要挡 —— 与"改密码至少 8 位"的平台策略一致。"""
    c = _login(app)
    r = c.post("/api/v1/admin/users", json={
        "username": "nopass", "password": "", "role": "user"})
    assert r.status_code == 400, r.text
    assert "密码" in r.json()["detail"]


def test_create_user_rejects_short_password(app):
    c = _login(app)
    r = c.post("/api/v1/admin/users", json={
        "username": "shortpw", "password": "123", "role": "user"})
    assert r.status_code == 400, r.text
    assert "密码" in r.json()["detail"]


def test_create_user_rejects_username_with_space(app):
    c = _login(app)
    r = c.post("/api/v1/admin/users", json={
        "username": "bad name", "password": "pw123456", "role": "user"})
    assert r.status_code == 400, r.text


def test_frontend_create_form_uses_scope_inputs():
    """前端必须把「共享/子目录」当成建号时的初始可见范围提交上去。"""
    from pathlib import Path
    js = (Path("web/static/app.js")).read_text(encoding="utf-8")
    assert "ns-share" in js, "建号表单没读 #ns-share"
    assert "ns-sub" in js, "建号表单没读 #ns-sub"
    assert "scopes:" in js, "createUser 没提交 scopes"


def test_frontend_has_no_dead_addscope_call():
    """「为所选用户授权目录」调用的 addScope() 从未定义 —— 点了毫无反应。"""
    from pathlib import Path
    js = (Path("web/static/app.js")).read_text(encoding="utf-8")
    assert "addScope(" not in js, "仍在调用不存在的 addScope()（死按钮）"


def test_frontend_validates_before_request():
    """用户名/密码为空时要就地提示并聚焦，而不是发一个必然失败的请求。"""
    from pathlib import Path
    js = (Path("web/static/app.js")).read_text(encoding="utf-8")
    assert "请填写用户名" in js, "缺少用户名为空的前端提示"
    assert "请填写初始密码" in js, "缺少初始密码为空的前端提示"


def test_frontend_keeps_form_draft_across_rerender():
    """面板重渲染不能把管理员填了一半的表单清空。"""
    from pathlib import Path
    js = (Path("web/static/app.js")).read_text(encoding="utf-8")
    assert "newUserDraft" in js, "建号表单没有草稿保存，任意管理台动作都会清空输入"
