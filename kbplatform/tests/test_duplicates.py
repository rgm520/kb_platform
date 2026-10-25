"""重复文件识别与清理的回归测试。

需求（2026-10-25）：文档看板增加重复文档统计——**按 MD5 内容哈希**判定重复
（不按文件名），支持批量删除重复件只保留一份，并可选择在原位置留快捷方式。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kbplatform import hashing
from kbplatform.auth import create_user
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    app = make_app(str(cfg))
    # 放文件的共享目录（本地存储源），文件删除要落在某个已接入的源里
    share = tmp_path / "share"
    share.mkdir(parents=True, exist_ok=True)
    app.state.conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,writable,created_at) "
        "VALUES ('共享','local',?,1,1,0)", (str(share),))
    app.state.conn.commit()
    return app


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _add(app, tmp_path, name, content, share="共享"):
    f = tmp_path / "share" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    conn = app.state.conn
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status,size) "
        "VALUES (?,?,?,?,?,'done',?)",
        (str(f), share, name, name, os.path.splitext(name)[1], len(content)))
    conn.commit()
    return int(conn.execute("SELECT id FROM catalog WHERE path=?", (str(f),)).fetchone()[0])


def test_md5_groups_only_identical_content(app, tmp_path):
    """同名不同内容不算重复；内容相同（名字不同）算重复。"""
    c = _login(app)
    body = b"same content"
    a = _add(app, tmp_path, "a.pdf", body)
    b = _add(app, tmp_path, "b.pdf", body)
    d = _add(app, tmp_path, "c.pdf", b"different")
    # 算哈希
    n = hashing.hash_pending(app.state.conn, limit=50)
    assert n == 2, "只有大小相同的候选才需要算哈希"
    groups = hashing.duplicate_groups(app.state.conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert {i["id"] for i in groups[0]["items"]} == {a, b}
    assert d not in {i["id"] for i in groups[0]["items"]}
    # 接口也能拿到
    r = c.get("/api/v1/documents/duplicates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_count"] == 1
    assert body["file_count"] == 2


def test_duplicate_summary_counts_wasted_bytes(app, tmp_path):
    c = _login(app)
    payload = b"x" * 1000
    _add(app, tmp_path, "a.pdf", payload)
    _add(app, tmp_path, "b.pdf", payload)
    _add(app, tmp_path, "c.pdf", payload)
    hashing.hash_pending(app.state.conn, limit=50)
    body = c.get("/api/v1/documents/duplicates").json()
    assert body["group_count"] == 1
    assert body["file_count"] == 3
    assert body["wasted_bytes"] == 2000, "两份冗余各 1000 字节"


def test_resolve_keeps_one_and_deletes_others(app, tmp_path):
    c = _login(app)
    payload = b"dup"
    keep = _add(app, tmp_path, "keep.pdf", payload)
    drop1 = _add(app, tmp_path, "drop1.pdf", payload)
    drop2 = _add(app, tmp_path, "drop2.pdf", payload)
    hashing.hash_pending(app.state.conn, limit=50)
    r = c.post("/api/v1/documents/duplicates/resolve", json={
        "keep_id": keep, "delete_ids": [drop1, drop2], "make_shortcut": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["deleted"]) == 2
    assert body["kept"]["id"] == keep
    conn = app.state.conn
    # 保留的那份还在，被删的两份从 catalog 里移除
    assert conn.execute("SELECT COUNT(*) FROM catalog WHERE id=?", (keep,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM catalog WHERE id=?", (drop1,)).fetchone()[0] == 0
    # 删除进了回收站（可恢复）
    n = conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0]
    assert n == 2, "删除的重复件要进回收站，保证可恢复"


def test_resolve_rejects_keeping_a_different_file(app, tmp_path):
    c = _login(app)
    keep = _add(app, tmp_path, "keep.pdf", b"dup")
    other = _add(app, tmp_path, "other.pdf", b"duq")   # 同大小、不同内容
    hashing.hash_pending(app.state.conn, limit=50)
    r = c.post("/api/v1/documents/duplicates/resolve", json={
        "keep_id": keep, "delete_ids": [other]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"], "哈希不同的不该被删"
    assert body["deleted"] == []


def test_resolve_is_admin_only(app, tmp_path):
    _add(app, tmp_path, "a.pdf", b"same")
    _add(app, tmp_path, "b.pdf", b"same")
    hashing.hash_pending(app.state.conn, limit=50)
    c = _login(app, "lisi", role="user")
    r = c.post("/api/v1/documents/duplicates/resolve",
               json={"keep_id": 1, "delete_ids": [2]})
    assert r.status_code == 403, r.text


def test_shortcut_helper_creates_lnk(tmp_path):
    """快捷方式生成：真实写一个 .lnk 并指向目标文件（Windows 环境才有意义）。"""
    from kbplatform import shortcut
    target = tmp_path / "target.txt"
    target.write_text("hello", encoding="utf-8")
    link = tmp_path / "link.lnk"
    ok, msg = shortcut.create_shortcut(str(link), str(target))
    if not ok:
        pytest.skip(f"本机不能生成快捷方式：{msg}")
    assert link.exists()
    assert link.stat().st_size > 0
