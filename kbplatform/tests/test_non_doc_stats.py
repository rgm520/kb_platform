"""非文档文件分类统计的回归测试。

需求（2026-10-24）：点看板上的「跳过的非文档文件」→ 下方切成按类型分类的数量统计。
同时要把计数改准：扫描器覆盖写（不累加），老流水线不再对同一共享重复累加。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.db import bump_stats
from kbplatform.fsindex import write_non_doc_stats
from kbplatform.service import make_app
from kbplatform.storage import Source


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_stats_expose_non_doc_breakdown(app):
    c = _login(app)
    conn = app.state.conn
    bump_stats(conn, "共享A", {".png": 100, ".jpg": 50})
    bump_stats(conn, "共享B", {".vsdx": 20})
    s = c.get("/api/v1/documents/stats").json()
    assert s["non_document_files"] == 170
    by_ext = {x["ext"]: x["count"] for x in s["non_doc_by_ext"]}
    assert by_ext == {".png": 100, ".jpg": 50, ".vsdx": 20}
    # 按数量倒序
    assert [x["ext"] for x in s["non_doc_by_ext"]] == [".png", ".jpg", ".vsdx"]


def test_non_doc_breakdown_is_scoped_to_visible_shares(app):
    conn = app.state.conn
    bump_stats(conn, "共享A", {".png": 100})
    bump_stats(conn, "共享B", {".vsdx": 20})
    # 造一个只授权"共享A"的普通用户
    conn.execute("INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
                 "VALUES ('共享A','smb','\\\\nas\\\\共享A',1,0)")
    sid = conn.execute("SELECT id FROM storage_sources WHERE name='共享A'").fetchone()[0]
    create_user(conn, "lisi", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) "
                 "VALUES (?,?,'',0,0)", (uid, sid))
    conn.commit()
    c = _login(app, "lisi")
    s = c.get("/api/v1/documents/stats").json()
    assert s["non_document_files"] == 100
    assert [x["ext"] for x in s["non_doc_by_ext"]] == [".png"]


def test_write_non_doc_stats_replaces_instead_of_accumulating(app):
    """扫描器写的是**当前数量**：同样内容再扫一次，数字不变。"""
    conn = app.state.conn
    source = Source(id=1, name="共享A", kind="smb", root="\\\\nas\\\\共享A")
    write_non_doc_stats(conn, source, {".png": 10, ".jpg": 5})
    write_non_doc_stats(conn, source, {".png": 10, ".jpg": 5})
    rows = conn.execute("SELECT ext, count FROM catalog_stats WHERE share=? "
                         "ORDER BY ext", ("共享A",)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(".jpg", 5), (".png", 10)]
    total = conn.execute("SELECT SUM(count) FROM catalog_stats").fetchone()[0]
    assert total == 15, "重复扫描不应让计数翻倍"
