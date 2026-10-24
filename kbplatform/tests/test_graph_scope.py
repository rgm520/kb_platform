"""知识图谱按目录授权收敛的回归测试。

普通用户看到的图谱必须只包含其授权范围内的文档所派生的节点与关系。
"""
from __future__ import annotations

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


def _seed(conn, tag="a"):
    """造两份文档：授权目录内一份、目录外一份，各带一个节点和一条边。"""
    root = r"\\nas\信息化管理"
    inside = root + r"\AI知识库\%s.md" % tag
    outside = root + r"\其它\%s.md" % tag
    for path, rel in ((inside, r"AI知识库\%s.md" % tag), (outside, r"其它\%s.md" % tag)):
        conn.execute(
            "INSERT INTO catalog(path,share,rel_path,name,ext,status,tier1_done) "
            "VALUES (?,?,?,?,?,?,1)", (path, "信息化管理", rel, rel.split("\\")[-1], ".md", "done"))
        conn.execute("INSERT INTO chunks(file_path,text) VALUES (?,?)", (path, "x"))
    chunk_inside = conn.execute("SELECT id FROM chunks WHERE file_path=?", (inside,)).fetchone()[0]
    chunk_outside = conn.execute("SELECT id FROM chunks WHERE file_path=?", (outside,)).fetchone()[0]
    conn.execute("INSERT INTO nodes(name,norm,type,mentions) VALUES (?,?,?,?)",
                 ("范围内节点" + tag, "范围内节点" + tag, "knowledge_point", 9))
    n_in = conn.execute("SELECT id FROM nodes WHERE name=?", ("范围内节点" + tag,)).fetchone()[0]
    conn.execute("INSERT INTO nodes(name,norm,type,mentions) VALUES (?,?,?,?)",
                 ("范围外节点" + tag, "范围外节点" + tag, "knowledge_point", 9))
    n_out = conn.execute("SELECT id FROM nodes WHERE name=?", ("范围外节点" + tag,)).fetchone()[0]
    conn.execute("INSERT INTO edges(src,dst,rel,file_path,chunk_id) VALUES (?,?,?,?,?)",
                 (n_in, n_out, "包含", inside, chunk_inside))
    conn.execute("INSERT INTO edges(src,dst,rel,file_path,chunk_id) VALUES (?,?,?,?,?)",
                 (n_out, n_in, "包含", outside, chunk_outside))
    conn.commit()
    return n_in, n_out


def _login(app, name, role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_graph_overview_is_limited_to_authorized_scope(app):
    """只授权一个子目录时，图谱里不能出现该目录之外的节点关系。"""
    conn = app.state.conn
    n_in, n_out = _seed(conn)
    # 存储源 + 授权
    conn.execute("INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
                 "VALUES ('共享','smb',?,1,0)", (r"\\nas\信息化管理",))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    create_user(conn, "zhangsan", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='zhangsan'").fetchone()[0]
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) "
                 "VALUES (?,?,?,0,0)", (uid, sid, "AI知识库"))
    conn.commit()

    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "zhangsan", "password": "pw123456"})
    assert r.status_code == 200
    body = c.get("/api/v1/graph/overview?limit=60").json()
    # 范围内的节点可见，范围外的关系不可见
    assert n_in in {n["id"] for n in body["nodes"]}
    # 边只保留落在范围内的（chunk 在范围外的那条被过滤）
    edge_chunks = conn.execute(
        "SELECT chunk_id FROM edges WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
        (n_in, n_out, n_out, n_in)).fetchall()
    assert len(edge_chunks) == 2, "种子数据应当有两条方向相反的边"
    assert len(body["edges"]) == 1, "范围外的关系不应出现在普通用户的图谱里"


def test_graph_is_empty_for_user_without_any_grant(app):
    conn = app.state.conn
    _seed(conn)
    c = _login(app, "lisi")
    body = c.get("/api/v1/graph/overview?limit=60").json()
    assert body["nodes"] == [] and body["edges"] == []
    assert c.get("/api/v1/graph/search?q=节点").json()["nodes"] == []


def test_admin_still_sees_everything(app):
    conn = app.state.conn
    n_in, n_out = _seed(conn)
    c = _login(app, "root", role="admin")
    body = c.get("/api/v1/graph/overview?limit=60").json()
    assert {n_in, n_out} <= {n["id"] for n in body["nodes"]}
    assert len(body["edges"]) == 2
