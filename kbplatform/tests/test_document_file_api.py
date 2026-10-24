"""文档看板右键菜单的后端支撑：文档 → 实际文件位置。

看板要能「查看文件 / 打开所在路径 / 下载文件」，需要把 catalog 行
映射到 2.0 存储源里的 (source_id, rel)；老目录（还没进 2.0 索引）则回退到
老的 content / download 接口。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.db import upsert_catalog
from kbplatform.service import make_app
from kbplatform.catalog import FileRecord


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
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _add_doc(conn, path, name, share="信息化管理", rel="子目录"):
    fr = FileRecord(path=path, share=share, rel_path=rel, name=name, ext=".pdf",
                    size=123, mtime=1700000000.0)
    return upsert_catalog(conn, fr)


def test_document_file_resolves_to_fs_source(app):
    c = _login(app)
    conn = app.state.conn
    cid = _add_doc(conn, "\\\\192.0.2.19\\信息化管理\\AI知识库\\制度\\办法.pdf", "办法.pdf")
    conn.execute("INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
                 "VALUES ('共享','smb','\\\\192.0.2.19\\信息化管理',1,0)")
    sid = conn.execute("SELECT id FROM storage_sources WHERE name='共享'").fetchone()[0]
    conn.execute("INSERT INTO fs_entries(source_id,rel_path,rel_key,parent_rel,name,is_dir,"
                 "catalog_id) VALUES (?,?,?,?,?,0,?)",
                 (sid, "制度\\办法.pdf", "制度\\办法.pdf".lower(), "制度", "办法.pdf", cid))
    conn.commit()

    r = c.get(f"/api/v1/documents/{cid}/file")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["in_fs"] is True
    assert body["source_id"] == sid
    assert body["rel"] == "制度\\办法.pdf"
    assert "source_id=" in body["preview_url"] and "rel=" in body["preview_url"]
    assert "source_id=" in body["download_url"]


def test_document_file_falls_back_to_legacy_urls(app):
    c = _login(app)
    conn = app.state.conn
    cid = _add_doc(conn, "\\\\192.0.2.19\\共享\\老文档.pdf", "老文档.pdf", share="共享")
    r = c.get(f"/api/v1/documents/{cid}/file")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["in_fs"] is False
    assert body["source_id"] is None
    assert body["preview_url"].endswith(f"/documents/{cid}/content")
    assert body["download_url"].endswith(f"/documents/{cid}/download")


def test_document_file_matches_source_root_by_prefix(app):
    """文件还没被扫描索引时，只要路径落在已接入的共享下也能定位。"""
    c = _login(app)
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
                 "VALUES ('共享','smb','\\\\192.0.2.19\\共享',1,0)")
    sid = conn.execute("SELECT id FROM storage_sources WHERE name='共享'").fetchone()[0]
    cid = _add_doc(conn, "\\\\192.0.2.19\\共享\\子目录\\报告.pdf", "报告.pdf", share="共享")
    body = c.get(f"/api/v1/documents/{cid}/file").json()
    assert body["in_fs"] is True
    assert body["source_id"] == sid
    assert body["rel"] == "子目录\\报告.pdf"


def test_document_file_unknown_id_404(app):
    c = _login(app)
    assert c.get("/api/v1/documents/999999/file").status_code == 404
