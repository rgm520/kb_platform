"""文件页与文档看板用**同一套授权**的回归测试。

背景（2026-10-24）：普通用户在文件页看到"没有可访问的存储源"，但文档看板
有数据——因为文件页按 2.0 的 `grants` 过滤，看板还在按 1.0 的 `user_scopes`。
现在：grants 是唯一授权源，老范围启动时自动同步；看板/检索也认 grants。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.db import upsert_catalog
from kbplatform.catalog import FileRecord
from kbplatform.grants import migrate_user_scopes
from kbplatform.service import make_app

SHARE = "信息化管理"
UNC = r"\\192.0.2.19\信息化管理"


def _cfg(tmp_path, with_share=True):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    extra = ""
    if with_share:
        extra = ('[drives]\n'
                 '"W:" = "\\\\\\\\192.0.2.19\\\\信息化管理"\n'
                 '[shares]\nnames = ["信息化管理"]\n')
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n' + extra,
                   encoding="utf-8")
    return cfg


@pytest.fixture()
def app(tmp_path):
    return make_app(str(_cfg(tmp_path)))


def _login(app, name="zhangsan", role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c, uid


def _doc(conn, rel="AI知识库\\办法.pdf"):
    path = UNC + "\\" + rel
    fr = FileRecord(path=path, share=SHARE, rel_path=rel, name="办法.pdf",
                    ext=".pdf", size=10, mtime=1700000000.0)
    return upsert_catalog(conn, fr)


def test_legacy_scope_migrates_to_grants_on_startup(tmp_path):
    """老范围在启动时同步成 2.0 目录授权（幂等）。"""
    # 先造一个"只有老范围"的环境
    cfg = _cfg(tmp_path, with_share=False)
    app0 = make_app(str(cfg))
    conn = app0.state.conn
    create_user(conn, "zhangsan", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='zhangsan'").fetchone()[0]
    conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                 (uid, SHARE, "AI知识库"))
    conn.commit()
    # 重新启动（这次配置里有了共享）
    app2 = make_app(str(_cfg(tmp_path)))
    grants = app2.state.conn.execute(
        "SELECT source_id, subpath FROM grants WHERE user_id=?", (uid,)).fetchall()
    assert len(grants) == 1, f"老范围没有同步成目录授权：{grants}"
    assert grants[0][1] == "AI知识库"
    # 再跑一次不应重复
    migrate_user_scopes(app2.state.conn, app2.state.cfg)
    assert app2.state.conn.execute(
        "SELECT COUNT(*) FROM grants WHERE user_id=?", (uid,)).fetchone()[0] == 1


def test_normal_user_sees_files_and_documents_with_same_grant(app):
    """同一份授权：文件页能看到目录，看板也能看到对应文档。"""
    c, uid = _login(app)
    conn = app.state.conn
    sid = conn.execute("SELECT id FROM storage_sources WHERE name=?", (SHARE,)).fetchone()[0]
    # 只给 2.0 授权（不碰老表）
    from kbplatform.grants import set_grants
    set_grants(conn, uid, [{"source_id": sid, "subpath": "AI知识库", "can_write": False}])
    _doc(conn)

    # 文件页：能看到这个源
    srcs = c.get("/api/v1/fs/sources").json()["sources"]
    assert [s["name"] for s in srcs] == [SHARE]
    # 文件页：能进入授权目录
    r = c.get(f"/api/v1/fs/list?source_id={sid}&rel=AI知识库")
    assert r.status_code == 200, r.text
    # 看板：同一份授权下能看到对应文档
    docs = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs["total"] >= 1, "看板看不到已授权目录下的文档"
    # 越权目录：文件和文档都看不到
    conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                 (uid, SHARE, "别的目录"))
    conn.commit()
    docs2 = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs2["total"] == docs["total"], "老范围不该凭空多出可见文档"


def test_grant_without_legacy_scope_hides_documents(app):
    """没有任何授权的普通用户：文件和文档都看不到。"""
    c, uid = _login(app, "lisi")
    conn = app.state.conn
    _doc(conn)
    docs = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs["total"] == 0
    assert c.get("/api/v1/fs/sources").json()["sources"] == []


def test_legacy_scope_api_writes_grants(app):
    """老的「共享/子目录」授权接口内部改写 grants（统一授权源）。"""
    c, uid = _login(app)
    r = c.put(f"/api/v1/admin/users/{uid}/scopes", ctx=None) if False else None
    # 用管理员身份来改普通用户的范围
    c2, _ = _login(app, "admin", role="admin")
    resp = c2.put(f"/api/v1/admin/users/{uid}/scopes",
                   json={"scopes": [[SHARE, "AI知识库"]]})
    assert resp.status_code == 200, resp.text
    conn = app.state.conn
    grants = conn.execute("SELECT subpath FROM grants WHERE user_id=?", (uid,)).fetchall()
    assert [g[0] for g in grants] == ["AI知识库"]
    # 文件页立刻能用
    srcs = c.get("/api/v1/fs/sources").json()["sources"]
    assert [s["name"] for s in srcs] == [SHARE]
