"""图谱页初始概览：打开页面就要有图，不能必须先搜索。"""
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


def _admin_client(app):
    conn = app.state.conn
    create_user(conn, "admin", "pw123456", role="admin")
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw123456"})
    assert r.status_code == 200
    return c


def _seed(conn):
    path = r"\\nas\信息化管理\AI知识库\a.md"
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status,tier1_done)"
        " VALUES (?,?,?,?,?,?,1)", (path, "信息化管理", r"AI知识库\a.md", "a.md", ".md", "done"))
    conn.execute("INSERT INTO chunks(file_path,text) VALUES (?,?)", (path, "x"))
    chunk_id = conn.execute("SELECT id FROM chunks").fetchone()[0]
    conn.execute("INSERT INTO nodes(name,norm,type,mentions) VALUES ('流程','流程','category',5)")
    n1 = conn.execute("SELECT id FROM nodes WHERE name='流程'").fetchone()[0]
    conn.execute("INSERT INTO nodes(name,norm,type,mentions) VALUES ('审批','审批','knowledge_point',2)")
    n2 = conn.execute("SELECT id FROM nodes WHERE name='审批'").fetchone()[0]
    conn.execute("INSERT INTO edges(src,dst,rel,file_path,chunk_id) VALUES (?,?,?,?,?)",
                 (n1, n2, "包含", path, chunk_id))
    conn.commit()
    return n1, n2


def test_graph_overview_returns_seeded_nodes_and_edges(app):
    n1, n2 = _seed(app.state.conn)
    c = _admin_client(app)

    r = c.get("/api/v1/graph/overview")
    assert r.status_code == 200
    body = r.json()
    assert {n["id"] for n in body["nodes"]} == {n1, n2}
    assert body["edges"] == [{"src": n1, "dst": n2, "rel": "包含"}]


def test_graph_overview_empty_without_data(app):
    c = _admin_client(app)
    r = c.get("/api/v1/graph/overview")
    assert r.status_code == 200
    assert r.json() == {"nodes": [], "edges": []}
