import json

import pytest

from kbplatform.db import init_db


FAKE = {
    "summary": "测试文档摘要",
    "key_points": [{"title": "数据机房建设管控", "detail": "三级以下不允许自建",
                    "entities": ["数据机房", "三级及以下子企业"], "category": "标准"}],
    "relations": [{"from": "三级及以下子企业", "to": "数据机房", "relation": "原则上不允许"}],
}


def _seed(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status)"
                 " VALUES ('\\\\a.pdf','s','a.pdf','a.pdf','.pdf',1,1.0,'done')")
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES ('\\\\a.pdf',0,'内容')")
    conn.commit()
    cid = conn.execute("SELECT id FROM chunks").fetchone()[0]
    cat = conn.execute("SELECT id FROM catalog").fetchone()[0]
    return conn, cid, cat


def test_extract_writes_knowledge_and_graph(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid, cat = _seed(tmp_path)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    res = ex.extract_file(conn, cat, "\\\\a.pdf", [cid])
    assert res.ok is True
    assert res.key_points == 1
    assert res.relations == 1
    assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
    nodes = conn.execute("SELECT name, type FROM nodes").fetchall()
    assert any(r[0] == "数据机房" and r[1] == "entity" for r in nodes)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] >= 1


def test_extract_marks_tier2_done(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid, cat = _seed(tmp_path)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    ex.extract_file(conn, cat, "\\\\a.pdf", [cid])
    assert conn.execute("SELECT tier2_done FROM catalog WHERE id=?", (cat,)).fetchone()[0] == 1


def test_extract_passes_think_false_and_num_predict_2500(tmp_path, monkeypatch):
    """Global Constraint #5: 结构化生成必须 think=false + num_predict>=2500。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid, cat = _seed(tmp_path)
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return FAKE, {"wall": 0.1}
    monkeypatch.setattr(llm, "chat_json", spy)
    ex.extract_file(conn, cat, "\\\\a.pdf", [cid])
    assert seen.get("think") is False
    assert seen.get("num_predict", 0) >= 2500


def test_extract_uses_64k_model_by_default(tmp_path, monkeypatch):
    """默认模型必须是 64k 变体，默认的 qwen3.5:9b 上下文只有 4096 会静默截断。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid, cat = _seed(tmp_path)
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return FAKE, {"wall": 0.1}
    monkeypatch.setattr(llm, "chat_json", spy)
    ex.extract_file(conn, cat, "\\\\a.pdf", [cid])
    assert seen.get("model", "").endswith("-64k") or seen.get("model", "").endswith("-32k")


def test_extract_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid, cat = _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("结构化输出失败")
    monkeypatch.setattr(llm, "chat_json", boom)
    res = ex.extract_file(conn, cat, "\\\\a.pdf", [cid])
    assert res.ok is False
    assert "结构化输出" in res.error


def test_extract_records_model_usage(tmp_path, monkeypatch):
    """每块提炼都要记到 model_usage，否则「是否充分调动」在平台上看不见。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    from kbplatform import models as M
    conn, cid, cat = _seed(tmp_path)
    mid = M.add_model(conn, "远端", "ollama", "http://192.0.2.77:11434",
                      "qwen3.5:9b", in_parse_pool=True, extra={"concurrency": 2})
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})
    monkeypatch.setattr(llm, "chat_json",
                        lambda *a, **k: (FAKE, {"wall": 0.1, "prompt_tokens": 111,
                                                "eval_tokens": 22}))
    pool = M.ParsePool(conn)
    res = ex.extract_file(conn, cat, "\\\\a.pdf", [cid], model="", pool=pool)
    assert res.ok is True
    row = conn.execute("SELECT user_id, model_id, calls, prompt_tokens, output_tokens"
                       " FROM model_usage").fetchone()
    assert row is not None, "没有写入任何用量"
    assert row[0] is None and row[1] == mid
    assert row[2] == 1 and row[3] == 111 and row[4] == 22


def test_extract_records_usage_for_single_model(tmp_path, monkeypatch):
    """没有算力池时用配置里的模型名，也要能对上 models 表里的 id。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    from kbplatform import models as M
    conn, cid, cat = _seed(tmp_path)
    mid = M.add_model(conn, "本地", "ollama", "http://localhost:11434",
                      "qwen3.5-9b-64k")
    monkeypatch.setattr(llm, "chat_json",
                        lambda *a, **k: (FAKE, {"wall": 0.1, "prompt_tokens": 7,
                                                "eval_tokens": 3}))
    ex.extract_file(conn, cat, "\\\\a.pdf", [cid], model="qwen3.5-9b-64k")
    row = conn.execute("SELECT model_id, calls FROM model_usage").fetchone()
    assert row is not None and row[0] == mid and row[1] == 1


def test_extract_uses_prepicked_member_and_releases_it(tmp_path, monkeypatch):
    """并发执行器会先占好池成员再派活，extract_file 必须复用它并在结束时归还。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    from kbplatform import models as M
    conn, cid, cat = _seed(tmp_path)
    M.add_model(conn, "远端", "ollama", "http://192.0.2.77:11434",
                "qwen3.5:9b", in_parse_pool=True, extra={"concurrency": 1})
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})
    seen = {}
    monkeypatch.setattr(llm, "chat_json",
                        lambda *a, **k: (seen.update(k), (FAKE, {"wall": 0.1}))[1])
    pool = M.ParsePool(conn)
    member = pool.pick()
    assert member is not None
    ex.extract_file(conn, cat, "\\\\a.pdf", [cid], model="", pool=pool, member=member)
    assert seen.get("model") == "qwen3.5:9b"
    assert pool.pick() is not None, "预占的成员没有被归还，容量会永久少一个"


def test_canonicalize_pending_handles_empty(tmp_path):
    from kbplatform.extract import canonicalize_pending
    conn = init_db(str(tmp_path / "kb.db"))
    assert canonicalize_pending(conn) == 0


def test_canonicalize_entities_is_batched(tmp_path, monkeypatch):
    """Global Constraint #7: 实体归一必须分批，不得全量稠密矩阵。"""
    import kb.graph as g
    conn = init_db(str(tmp_path / "kb.db"))
    for i in range(30):
        conn.execute("INSERT INTO nodes(name,norm,type,mentions) VALUES (?,?,'entity',1)",
                     (f"实体{i}", f"实体{i}"))
    conn.commit()
    seen = {}

    def fake_embed(names, *a, **k):
        seen["n"] = len(names)
        return [[1.0, 0.0] for _ in names]
    monkeypatch.setattr(g.llm, "embed", fake_embed)
    res = g.canonicalize_entities(conn, batch=10, use_llm=False)
    assert seen["n"] <= 10
    assert res["processed"] == 10


def test_merge_edges_preserves_chunk_id(tmp_path):
    """图谱扩展靠 chunk_id 关联块与节点，置空会让扩展完全失效。"""
    import kb.graph as g
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO nodes(name,norm,type) VALUES ('a','a','entity')")
    conn.execute("INSERT INTO nodes(name,norm,type) VALUES ('b','b','entity')")
    conn.execute("INSERT INTO chunks(file_path,chunk_idx,text) VALUES ('f',0,'t')")
    cid = conn.execute("SELECT id FROM chunks").fetchone()[0]
    a = conn.execute("SELECT id FROM nodes WHERE name='a'").fetchone()[0]
    b = conn.execute("SELECT id FROM nodes WHERE name='b'").fetchone()[0]
    conn.execute("INSERT INTO edges(src,dst,rel,file_path,chunk_id,weight) VALUES (?,?,'r','f',?,1)",
                 (a, b, cid))
    conn.execute("INSERT INTO edges(src,dst,rel,file_path,chunk_id,weight) VALUES (?,?,'r','f',?,1)",
                 (a, b, cid))
    conn.commit()
    g.merge_edges(conn)
    row = conn.execute("SELECT chunk_id, weight FROM edges").fetchone()
    assert row[0] == cid
    assert row[1] == 2
