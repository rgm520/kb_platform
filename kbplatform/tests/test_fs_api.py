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


def _login(app, name="alice", role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200
    return c, uid


def _source(conn, root, writable=True):
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('共享','local',?,?,1,0)", (str(root), 1 if writable else 0))
    conn.commit()
    return conn.execute("SELECT id FROM storage_sources").fetchone()[0]


def _grant(conn, uid, sid, subpath="", can_write=False):
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) "
                 "VALUES (?,?,?,?,0)", (uid, sid, subpath, 1 if can_write else 0))
    conn.commit()


def test_list_only_granted_subtree(app, tmp_path):
    share = tmp_path / "share"
    (share / "A" / "B" / "C").mkdir(parents=True)
    (share / "A" / "X").mkdir()
    (share / "A" / "B" / "C" / "c.txt").write_text("hi", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    c, uid = _login(app)
    _grant(conn, uid, sid, r"A\B\C")
    body = c.get(f"/api/v1/fs/list?source_id={sid}&rel=").json()
    assert [e["name"] for e in body["entries"]] == ["A"]
    body = c.get(f"/api/v1/fs/list?source_id={sid}&rel=A").json()
    assert [e["name"] for e in body["entries"]] == ["B"]
    assert c.get(f"/api/v1/fs/list?source_id={sid}&rel=A/X").status_code == 404
    assert c.get(f"/api/v1/fs/list?source_id={sid}&rel=../../").status_code == 400
    assert c.get(f"/api/v1/fs/list?source_id={sid}&rel=A/B/C").json()["entries"][0]["name"] == "c.txt"


def test_admin_list_sees_all_entries(app, tmp_path):
    share = tmp_path / "share_admin"
    (share / "A").mkdir(parents=True)
    (share / "B").mkdir()
    (share / "root.txt").write_text("x", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    c, _ = _login(app, "root", role="admin")
    body = c.get(f"/api/v1/fs/list?source_id={sid}&rel=").json()
    assert [e["name"] for e in body["entries"]] == ["A", "B", "root.txt"]


def test_sources_only_returns_granted_or_admin(app, tmp_path):
    conn = app.state.conn
    sid = _source(conn, tmp_path / "share")
    (tmp_path / "share").mkdir()
    c, uid = _login(app)
    assert c.get("/api/v1/fs/sources").json()["sources"] == []
    _grant(conn, uid, sid)
    body = c.get("/api/v1/fs/sources").json()
    assert body["sources"][0]["id"] == sid
    ca, _ = _login(app, "root", role="admin")
    assert len(ca.get("/api/v1/fs/sources").json()["sources"]) == 1


def test_fs_api_preview_text_and_download(app, tmp_path):
    share = tmp_path / "s"
    (share / "d").mkdir(parents=True)
    (share / "d" / "a.txt").write_text("你好，世界", encoding="utf-8")
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('s','local',?,1,1,0)", (str(share),))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c, uid = _login(app)
    _grant(conn, uid, sid, "", can_write=True)
    r = c.get(f"/api/v1/fs/preview?source_id={sid}&rel=d/a.txt")
    assert r.status_code == 200 and "你好" in r.text
    r = c.get(f"/api/v1/fs/download?source_id={sid}&rel=d/a.txt")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "a.txt" in r.headers["content-disposition"]
    assert c.get(f"/api/v1/fs/preview?source_id={sid}&rel=d/none.txt").status_code == 404
    assert c.get(f"/api/v1/fs/preview?source_id={sid}&rel=../../x").status_code == 400


def test_search_does_not_leak_other_branch(app, tmp_path):
    conn = app.state.conn
    share = tmp_path / "share2"
    (share / "甲").mkdir(parents=True)
    (share / "乙").mkdir()
    (share / "甲" / "预算说明.docx").write_text("x", encoding="utf-8")
    (share / "乙" / "预算底稿.docx").write_text("x", encoding="utf-8")
    sid = _source(conn, share)
    from kbplatform.fsindex import scan_source
    from kbplatform.storage import LocalAdapter, source_from_row
    scan_source(conn, source_from_row(conn.execute(
        "SELECT * FROM storage_sources WHERE id=?", (sid,)).fetchone()), LocalAdapter(str(share)))
    c, uid = _login(app)
    _grant(conn, uid, sid, "甲")
    body = c.get("/api/v1/fs/search?q=预算").json()
    assert [r["rel"] for r in body["results"]] == ["甲\\预算说明.docx"]
    assert c.get("/api/v1/fs/search?q=").json()["results"] == []
    assert c.get("/api/v1/fs/search?q=预算&limit=999").status_code == 422


def test_chunked_upload_creates_file_and_enqueues_parse(app, tmp_path):
    share = tmp_path / "s"
    share.mkdir()
    conn = app.state.conn
    sid = _source(conn, share)
    c, uid = _login(app)
    _grant(conn, uid, sid, "", can_write=True)
    data = "制度正文".encode("utf-8") * 1000
    init = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "制度.md", "size": len(data)}).json()
    up = init["upload_id"]
    for off in range(0, len(data), 4096):
        r = c.put(f"/api/v1/fs/upload/{up}?offset={off}", content=data[off:off + 4096])
        assert r.status_code == 200, r.text
    done = c.post(f"/api/v1/fs/upload/{up}/complete").json()
    assert (share / "制度.md").read_bytes() == data
    assert done["queued"] is True
    assert conn.execute("SELECT status FROM catalog WHERE name='制度.md'").fetchone()[0] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM fs_entries WHERE name='制度.md'").fetchone()[0] == 1


def test_upload_existing_without_overwrite_409_and_bad_name_400(app, tmp_path):
    share = tmp_path / "s"
    share.mkdir()
    (share / "已有.txt").write_text("x", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    c, uid = _login(app)
    _grant(conn, uid, sid, "", can_write=True)
    r = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "已有.txt", "size": 1})
    assert r.status_code == 409
    r = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "../x.txt", "size": 1})
    assert r.status_code == 400
    r = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "新.txt", "size": 10 ** 12})
    assert r.status_code == 413


def test_overwrite_upload_resets_parse_state_and_requeues(app, tmp_path):
    share = tmp_path / "s"
    share.mkdir()
    (share / "a.md").write_text("旧内容", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    c, uid = _login(app)
    _grant(conn, uid, sid, "", can_write=True)
    data = "新内容".encode("utf-8")
    init = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "a.md", "size": len(data),
        "overwrite": True}).json()
    up = init["upload_id"]
    assert c.put(f"/api/v1/fs/upload/{up}?offset=0", content=data).status_code == 200
    assert c.post(f"/api/v1/fs/upload/{up}/complete").status_code == 200
    cid = conn.execute("SELECT id FROM catalog WHERE name='a.md'").fetchone()[0]
    conn.execute("UPDATE catalog SET status='done', tier1_done=1 WHERE id=?", (cid,))
    conn.execute("UPDATE jobs SET state='done'")
    conn.commit()
    data2 = "再改一次".encode("utf-8")
    init = c.post("/api/v1/fs/upload/init", json={
        "source_id": sid, "rel": "", "name": "a.md", "size": len(data2),
        "overwrite": True}).json()
    up = init["upload_id"]
    assert c.put(f"/api/v1/fs/upload/{up}?offset=0", content=data2).status_code == 200
    assert c.post(f"/api/v1/fs/upload/{up}/complete").status_code == 200
    row = conn.execute("SELECT status, tier1_done FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "pending" and row["tier1_done"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0] == 1


def test_delete_moves_to_trash_restore_and_search_cleanup(app, tmp_path):
    share = tmp_path / "s"
    (share / "d").mkdir(parents=True)
    (share / "d" / "a.md").write_text("正文" * 20, encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    from kbplatform.fsindex import scan_source, search
    from kbplatform.storage import LocalAdapter, source_from_row
    scan_source(conn, source_from_row(conn.execute(
        "SELECT * FROM storage_sources WHERE id=?", (sid,)).fetchone()), LocalAdapter(str(share)))
    c, uid = _login(app)
    _grant(conn, uid, sid, "", can_write=True)
    r = c.post("/api/v1/fs/delete", json={"source_id": sid, "rels": ["d/a.md"]})
    assert r.status_code == 200, r.text
    assert len(r.json()["deleted"]) == 1
    assert not (share / "d" / "a.md").exists()
    assert search(conn, "a.md", None) == []
    items = c.get("/api/v1/fs/trash").json()["items"]
    assert len(items) == 1 and items[0]["original_rel"] == "d\\a.md"
    tid = r.json()["deleted"][0]
    assert c.post(f"/api/v1/fs/trash/{tid}/restore").status_code == 200
    assert (share / "d" / "a.md").exists()
    assert c.get("/api/v1/fs/trash").json()["items"] == []


def test_delete_grant_root_is_skipped(app, tmp_path):
    share = tmp_path / "s"
    (share / "A" / "B").mkdir(parents=True)
    (share / "A" / "B" / "x.txt").write_text("x", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    c, uid = _login(app)
    _grant(conn, uid, sid, "A", can_write=True)
    r = c.post("/api/v1/fs/delete", json={"source_id": sid, "rels": ["A"]})
    assert r.status_code == 200
    assert r.json()["deleted"] == []
    assert r.json()["skipped"][0]["rel"] == "A"
    assert (share / "A" / "B" / "x.txt").exists()
    r = c.post("/api/v1/fs/delete", json={"source_id": sid, "rels": ["A/B/x.txt"]})
    assert len(r.json()["deleted"]) == 1


def test_admin_purge_trash(app, tmp_path):
    share = tmp_path / "s"
    share.mkdir()
    (share / "a.txt").write_text("x", encoding="utf-8")
    conn = app.state.conn
    sid = _source(conn, share)
    ca, _ = _login(app, "root", role="admin")
    r = ca.post("/api/v1/fs/delete", json={"source_id": sid, "rels": ["a.txt"]})
    tid = r.json()["deleted"][0]
    assert ca.delete(f"/api/v1/fs/trash/{tid}").status_code == 200
    assert ca.get("/api/v1/fs/trash").json()["items"] == []



