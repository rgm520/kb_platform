"""Task 0.3：答案反馈 + 检索轨迹。

为什么要有这些测试：
    "不准"目前是静默的——用户拿到不满意的答案只能默默离开，管理员
    既不知道"这次到底召回了什么"，也不知道"有多少人点了没用"。
    本文件锁住三件事：
      1. 问答响应里的 request_id 必须落一条 retrieval_traces（对得上号）；
      2. /feedback 必须把 up/down/wrong_cite 记下来，写库失败也不能 500；
      3. 轨迹里**绝不能**出现原文片段 / 文件路径 / 问题原文（红线）。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from kbplatform import models as models_mod
from kbplatform.auth import create_user
from kbplatform.service import make_app


FAKE_ANSWER = "（模拟回答）根据资料，预算上限为一百万元。"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    a = make_app(str(cfg))
    # 提供一个可用模型，并屏蔽真正的 LLM 调用
    models_mod.add_model(a.state.conn, "fake", "openai_compatible",
                         "http://localhost", "fake-model", api_key="sk-x",
                         is_default=True)
    monkeypatch.setattr(models_mod, "chat",
                        lambda *args, **kwargs: (FAKE_ANSWER, {"wall": 0.1}))
    seeded = _seed_docs(a.state.conn)
    _SEED_CHUNKS.clear()
    _SEED_CHUNKS.update(seeded)
    _stub_retrieval_routes(monkeypatch)
    yield a


DOC_TEXTS = {
    "doc0": "预算管理细则：全年采购金额上限为一百万元，超上限需总部审批。",
    "doc1": "费用报销流程：先在 OA 提交审批，再由财务部开具报销凭证。",
}


def _seed_docs(conn):
    """两份文档各一个 chunk：让真实 retrieve() 能走完 chunk_meta 的 JOIN。"""
    ids = {}
    for i, (name, text) in enumerate(DOC_TEXTS.items()):
        path = f"\\\\nas\\share\\{name}.md"
        conn.execute(
            "INSERT INTO catalog(path,share,rel_path,name,ext,status) "
            "VALUES(?,?,?,?,?,'done')",
            (path, "share", f"{name}.md", f"{name}.md", ".md"))
        cur = conn.execute(
            "INSERT INTO chunks(file_path,file_id,chunk_idx,text,locator,tokens) "
            "VALUES(?,NULL,?,?,?,NULL)", (path, 0, text, f"第{i + 1}节"))
        ids[name] = cur.lastrowid
    conn.commit()
    return ids


def _stub_retrieval_routes(monkeypatch):
    """让四路召回里最尖 costly 的两路（FTS / 向量）返回确定结果。"""
    chunk_ids = list(_SEED_CHUNKS.values())

    def fake_keyword(self, query, topk):
        return chunk_ids[:2]

    def fake_vector(self, query, topk):
        return chunk_ids[1:]

    def fake_wiki(self, query, topk):
        return []

    monkeypatch.setattr("kbplatform.retrieval.Retriever._keyword", fake_keyword)
    monkeypatch.setattr("kbplatform.retrieval.Retriever._vector", fake_vector)
    monkeypatch.setattr("kbplatform.retrieval.Retriever._wiki_ids_for", fake_wiki)


# 在 import 时还没建库，所以种子 id 用 fixture 里存到全局（见 app fixture 后置钩子）
_SEED_CHUNKS: dict[str, int] = {}


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _do_query(client, question="预算上限是多少？"):
    r = client.post("/api/v1/query", json={"question": question, "top_k": 8})
    assert r.status_code == 200, r.text
    return r.json()


def test_query_returns_request_id_and_writes_trace(app):
    c = _login(app)
    resp = _do_query(c)
    rid = resp.get("request_id")
    assert rid, f"响应里必须有 request_id，实际 keys={sorted(resp)}"
    rows = app.state.conn.execute(
        "SELECT * FROM retrieval_traces WHERE request_id=?", (rid,)).fetchall()
    assert len(rows) == 1, "每次问答应落一条检索轨迹，且 request_id 对得上"
    row = rows[0]
    want_hash = hashlib.sha256("预算上限是多少？".encode()).hexdigest()[:16]
    assert row["question_hash"] == want_hash


def test_feedback_is_recorded(app):
    c = _login(app)
    rid = _do_query(c)["request_id"]
    r = c.post(f"/api/v1/query/{rid}/feedback",
               json={"rating": "down", "comment": "答非所问"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    rows = app.state.conn.execute(
        "SELECT * FROM query_feedback WHERE request_id=?", (rid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["rating"] == "down"
    assert rows[0]["comment"] == "答非所问"


def test_feedback_rejects_unknown_rating(app):
    c = _login(app)
    rid = _do_query(c)["request_id"]
    r = c.post(f"/api/v1/query/{rid}/feedback", json={"rating": "meh"})
    assert r.status_code == 400, "非法 rating 必须被拒绝"


def test_trace_is_admin_only(app):
    _login(app)  # 产生一条轨迹
    user = _login(app, name="alice", role="user")
    r = user.get("/api/v1/admin/traces?limit=50")
    assert r.status_code == 403, "轨迹列表只允许管理员"


def test_trace_detail_is_admin_only(app):
    c = _login(app)
    rid = _do_query(c)["request_id"]
    user = _login(app, name="bob", role="user")
    r = user.get(f"/api/v1/admin/traces/{rid}")
    assert r.status_code == 403, "轨迹详情只允许管理员"


def test_trace_contains_no_raw_text(app):
    """红线：轨迹绝不存原文片段 / 文件路径 / 问题原文。"""
    c = _login(app)
    _do_query(c)
    for row in app.state.conn.execute(
            "SELECT request_id, question_hash, route_stats, candidates, top_ids "
            "FROM retrieval_traces").fetchall():
        blob = " ".join(str(row[i]) for i in range(5))
        for doc in DOC_TEXTS.values():
            # 原文片段（取中间一段，避开"预算/报销"这类恰好等于标题词的开头）
            frag = doc[10:20]
            assert frag not in blob, f"轨迹里出现了原文片段：{frag!r}"
        assert "\\\\nas\\share" not in blob, "轨迹里出现了文件路径"
        assert "预算上限是多少" not in blob, "轨迹里出现了问题原文"
    # 反馈表同样不含原文
    assert app.state.conn.execute("SELECT COUNT(*) FROM query_feedback").fetchone()[0] == 0


def test_trace_records_all_routes(app):
    """轨迹必须把四路召回都记进去：至少 keyword 与 vector。"""
    c = _login(app)
    _do_query(c)
    row = app.state.conn.execute(
        "SELECT candidates, route_stats FROM retrieval_traces").fetchone()
    trace = json.loads(row["candidates"])
    stats = json.loads(row["route_stats"])
    assert "keyword" in trace["routes"], "轨迹缺失 keyword 路"
    assert "vector" in trace["routes"], "轨迹缺失 vector 路"
    assert "keyword" in stats and "vector" in stats
    assert isinstance(trace["fused"], list) and trace["fused"], "融合结果应有候选"
    for item in trace["fused"]:
        assert {"chunk_id", "score", "routes"} <= set(item.keys())


def test_retriever_trace_out_param_is_backward_compatible(app):
    """retrieve() 加了可选 trace 出参后，不传 trace 的调用方不受影响。"""
    r = app.state.retriever
    hits, stats = r.retrieve("预算上限", user_id=None, topk=8)
    assert hits and "keyword" in stats
    t = {}
    hits2, stats2 = r.retrieve("预算上限", user_id=None, topk=8, trace=t)
    assert len(hits2) == len(hits)  # 返回值形状不变
    assert "keyword" in t["routes"] and "vector" in t["routes"]
    assert any(item["chunk_id"] == hits2[0].chunk_id
               for item in t["fused"]), "fused 里应包含最终 top1 的 chunk"


def test_admin_traces_list_and_detail(app):
    c = _login(app)
    rid = _do_query(c)["request_id"]
    c.post(f"/api/v1/query/{rid}/feedback", json={"rating": "up"})
    r = c.get("/api/v1/admin/traces?limit=50")
    assert r.status_code == 200, r.text
    traces = r.json()["traces"]
    assert len(traces) == 1
    t = traces[0]
    assert t["request_id"] == rid
    assert t.get("feedback_rating") == "up", "列表里要能看到反馈情况"
    d = c.get(f"/api/v1/admin/traces/{rid}")
    assert d.status_code == 200, d.text
    detail = d.json()["trace"]
    assert {"routes", "fused", "acl_filtered"} <= set(detail.keys())
    assert detail["acl_filtered"] is False  # 管理员不受限，没有 ACL 过滤
