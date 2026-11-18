"""失败原因（catalog.status_detail）对普通用户可见。

对应迭代任务 0.5（spec C4「只会看得见坏，看不见为什么」）：

  * `GET /api/v1/documents` 不再对普通用户剥掉 `status_detail` ——
    看板能告诉用户"为什么坏了"，不再只有一句"已跳过/失败"。
  * 涉密目录（`dir_scopes`）只作审计标记：失败原因、详情都照常展示
    （2026-11-11 放开，内网 + 本地模型）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.catalog import FileRecord
from kbplatform.db import set_status, upsert_catalog
from kbplatform.grants import set_grants
from kbplatform.service import make_app

SHARE = "审计共享"
SECRET_DETAIL = "解析失败：文件被其它进程独占，无法读取"
COMMON_DETAIL = "解析失败：HTTP Error 413 单次输入超限"


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _user(app, name, role="user") -> int:
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    return conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]


def _seed(app, user_id: int):
    """一个授权共享 + 两篇失败文档：一篇在涉密目录下，一篇在普通目录下。"""
    conn = app.state.conn
    conn.execute(
        "INSERT INTO storage_sources(id,name,kind,root) VALUES (1,?, 'local','D:\\nas')",
        (SHARE,))
    set_grants(conn, user_id, [{"source_id": 1, "subpath": "", "can_write": False}])
    conn.execute(
        "INSERT INTO dir_scopes(share,subpath,restricted,label) VALUES (?,?,1,'薪酬涉密')",
        (SHARE, "审计资料"))
    s1 = upsert_catalog(conn, FileRecord(path=r"D:\nas\审计资料\薪酬明细.xlsx",
                                         share=SHARE, rel_path=r"审计资料\薪酬明细.xlsx",
                                         name="薪酬明细.xlsx", ext=".xlsx",
                                         size=10, mtime=1700000000.0))
    set_status(conn, s1, "failed", SECRET_DETAIL)
    s2 = upsert_catalog(conn, FileRecord(path=r"D:\nas\公共资料\通讯录.pdf",
                                         share=SHARE, rel_path=r"公共资料\通讯录.pdf",
                                         name="通讯录.pdf", ext=".pdf",
                                         size=10, mtime=1700000000.0))
    set_status(conn, s2, "failed", COMMON_DETAIL)
    conn.commit()


def _login(app, name, role="user"):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_status_detail_visible_to_normal_user(app):
    """普通用户能看到自己范围内文档的失败原因（不再被剥掉）。"""
    uid = _user(app, "alice")
    _seed(app, uid)
    c = _login(app, "alice")
    items = c.get("/api/v1/documents?page_size=50").json()["items"]
    by_name = {d["name"]: d for d in items}
    assert set(by_name) == {"薪酬明细.xlsx", "通讯录.pdf"}, "授权范围内的两篇都应可见"
    assert by_name["通讯录.pdf"]["status_detail"] == COMMON_DETAIL
    assert by_name["薪酬明细.xlsx"]["status_detail"] == SECRET_DETAIL


def test_status_detail_visible_for_restricted_doc(app):
    """命中涉密目录的文档：失败原因照常展示，不再脱敏。"""
    uid = _user(app, "alice")
    _seed(app, uid)
    c = _login(app, "alice")
    items = c.get("/api/v1/documents?page_size=50").json()["items"]
    by_name = {d["name"]: d for d in items}
    secret = by_name["薪酬明细.xlsx"]
    assert secret["status_detail"] == SECRET_DETAIL
    assert by_name["通讯录.pdf"]["status_detail"] == COMMON_DETAIL


def test_admin_sees_full_status_detail(app):
    """管理员仍然看完整的失败原因，含涉密文档。"""
    uid = _user(app, "bob")
    _seed(app, uid)
    _user(app, "root", role="admin")
    c = _login(app, "root", role="admin")
    items = c.get("/api/v1/documents?page_size=50").json()["items"]
    by_name = {d["name"]: d for d in items}
    assert by_name["薪酬明细.xlsx"]["status_detail"] == SECRET_DETAIL
    assert by_name["通讯录.pdf"]["status_detail"] == COMMON_DETAIL


def test_detail_endpoint_shows_full_status_detail_for_normal_user(app):
    """详情接口同样照常展示失败原因，涉密标记不再脱敏。"""
    uid = _user(app, "alice")
    _seed(app, uid)
    c = _login(app, "alice")
    items = c.get("/api/v1/documents?page_size=50").json()["items"]
    secret = next(d for d in items if d["name"] == "薪酬明细.xlsx")
    r = c.get(f"/api/v1/documents/{secret['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["status_detail"] == SECRET_DETAIL


def test_admin_restricted_endpoints_manage_marks(app):
    """涉密目录标记接口可用：设置 → 列表可见 → 取消标记。"""
    uid = _user(app, "bob")
    _seed(app, uid)
    _user(app, "root", role="admin")
    c = _login(app, "root", role="admin")
    dirs = c.get("/api/v1/admin/restricted").json()["dirs"]
    assert len(dirs) == 1 and dirs[0]["restricted"] == 1
    # 取消标记（upsert 语义：restricted 置 0）
    r = c.put("/api/v1/admin/restricted", json={
        "share": SHARE, "subpath": "审计资料", "restricted": False,
        "label": "薪酬涉密"})
    assert r.status_code == 200, r.text
    dirs = c.get("/api/v1/admin/restricted").json()["dirs"]
    assert dirs[0]["restricted"] == 0
