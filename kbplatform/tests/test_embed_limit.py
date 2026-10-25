"""Task 0.2：embedding 单次输入上限——按块嵌入 + 显式失败。

背景（实测）：embedding 服务对**单次输入**有 2,048 token 上限；indexer 曾把一篇
文件的所有块打包成一次 llm.embed 批量调用，一个超限块让整篇向量作废，且失败被
`if vecs:` 静默跳过（4 篇文档 126 个块因此永远搜不到）。

本文件验证：
* embed_chunks：整批 → 逐条 → 超限块二分 的降级路径，失败位置显式为 None + 警告；
* index_file：部分块未向量化时 status_detail 留下人能读懂的原因，不再静默。
"""
import re

import kb.llm
import kbplatform.embedding as embedding
from kbplatform.catalog import scan_root
from kbplatform.db import init_db, upsert_catalog
from kbplatform.fts import build_index, search
from kbplatform.indexer import index_file
from kbplatform.vectors import VectorStore

DIM = 1024
MARK = "超限标记"


def _unit():
    return [1.0 / (DIM ** 0.5)] * DIM


def _patch_oversize_fails(monkeypatch, calls):
    """批量输入里含「超限标记」的文本就抛错（模拟 HTTP 413 / 超长输入），
    其余文本无论多长都返回等量单位向量；calls 记录每次调用的输入。"""

    def fake_embed(texts, model="bge-m3", keep_alive="30m"):
        calls.append(list(texts))
        if any(MARK in t for t in texts):
            raise RuntimeError('{"error":"the input length exceeds the context length"}')
        return [_unit() for _ in texts]

    monkeypatch.setattr(kb.llm, "embed", fake_embed)


def _patch_too_long_fails(monkeypatch, calls, limit):
    """任一文本超过 limit 字符才抛错——二分到不超过 limit 后应成功。"""

    def fake_embed(texts, model="bge-m3", keep_alive="30m"):
        calls.append([len(t) for t in texts])
        if any(len(t) > limit for t in texts):
            raise RuntimeError("the input length exceeds the context length")
        return [_unit() for _ in texts]

    monkeypatch.setattr(kb.llm, "embed", fake_embed)


# ---------------------------------------------------------------- embedding.py

def test_embed_chunks_empty_input():
    vectors, warnings = embedding.embed_chunks([], model="bge-m3")
    assert vectors == [] and warnings == []


def test_embed_chunks_survives_one_oversized_chunk(monkeypatch):
    """一个超限块只损失自己：正常块向量照常返回，超限块显式为 None + 警告。"""
    calls = []
    _patch_oversize_fails(monkeypatch, calls)
    texts = ["第一个正常块", MARK * 500, "第三个正常块"]

    vectors, warnings = embedding.embed_chunks(texts, model="bge-m3")

    assert calls[0] == texts, "必须先整批尝试（正常文档不应退化为逐条调用）"
    assert len(vectors) == len(texts)
    assert vectors[0] is not None and len(vectors[0]) == DIM
    assert vectors[2] is not None and len(vectors[2]) == DIM
    assert vectors[1] is None, "无法二分救回的块必须显式为 None，不能假装成功"
    assert len(warnings) == 1
    assert "1 个分块向量化失败" in warnings[0]
    assert "2048" in warnings[0], "警告里要写明单次输入上限"


def test_embed_chunks_splits_oversized_chunk(monkeypatch):
    """超限块应被二分降级：切到服务能接受的长度后嵌入成功，警告为空。"""
    calls = []
    _patch_too_long_fails(monkeypatch, calls, limit=2000)
    texts = ["正常小段", "超长" * 3000, "正常小段二"]

    vectors, warnings = embedding.embed_chunks(texts, model="bge-m3")

    assert len(vectors) == len(texts)
    assert all(v is not None and len(v) == DIM for v in vectors), \
        "二分后应恢复成与 texts 等长且无缺口的向量列表"
    assert warnings == []

    lens = [n for c in calls for n in c]
    assert len(calls[0]) == len(texts), "必须先整批尝试"
    assert max(lens) > 2000, "首次逐条重试时仍带着原始超长文本"
    assert 1500 in lens, "应有把 6000 字符块切到 1500 字符后成功嵌入的一次调用"


# ---------------------------------------------------------------- indexer.py

def _setup(tmp_path, content):
    p = tmp_path / "a.md"
    p.write_text(content, encoding="utf-8")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    fts_path = str(tmp_path / "fts.db")
    build_index(fts_path, [])
    vs = VectorStore(str(tmp_path / "vectors"), dim=DIM)
    return conn, fts_path, vs, cid, str(p)


def test_index_file_keeps_vectorless_chunks_visible(tmp_path, monkeypatch):
    """某块嵌入失败：catalog.status_detail 要有人能读懂的原因，而不是静默 None。"""
    normal = "正常段落内容，简短说明。\n" * 130
    content = normal + MARK * 100 + "\n" + normal   # 标记段 > 重叠保留阈值，只落在一个块里
    calls = []
    _patch_oversize_fails(monkeypatch, calls)

    conn, fts_path, vs, cid, path = _setup(tmp_path, content)
    res = index_file(conn, fts_path, vs, cid, path)

    assert res.ok is True, "部分块没向量不应把整个文件的索引判为失败"
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "done"
    assert row[1], "status_detail 必须留下失败说明"
    assert re.search(r"\d+ 个分块向量化失败", row[1])
    assert "2048" in row[1]
    assert vs.count() < len(res.chunk_ids), "失败块不应有向量"
    assert search(fts_path, "超限", 10), "全文检索不应受向量失败影响"


def test_index_file_all_chunks_vectorless_still_reports(tmp_path, monkeypatch):
    """所有块都嵌不进向量：status 也留 done + 明确数量说明，向量数为 0 可查。"""
    normal = "正常段落内容，简短说明。\n" * 130
    content = MARK * 100 + "\n" + normal
    calls = []
    _patch_oversize_fails(monkeypatch, calls)

    conn, fts_path, vs, cid, path = _setup(tmp_path, content)
    res = index_file(conn, fts_path, vs, cid, path)

    assert res.ok is True and res.warnings, "警告要返回给调用方"
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "done"
    assert re.search(rf"^{len(res.chunk_ids)} 个分块向量化失败", row[1])
    assert vs.count() == 0


def test_index_file_normal_doc_still_single_batch(tmp_path, monkeypatch):
    """正常文档不受影响：仍然一次整批调用嵌入，status_detail 保持干净 None。"""
    content = "信创资源池由总部统一规划建设。\n" * 200
    calls = []

    def fake_embed(texts, model="bge-m3", keep_alive="30m"):
        calls.append(list(texts))
        return [_unit() for _ in texts]

    monkeypatch.setattr(kb.llm, "embed", fake_embed)

    conn, fts_path, vs, cid, path = _setup(tmp_path, content)
    res = index_file(conn, fts_path, vs, cid, path)

    assert res.ok is True
    assert len(calls) == 1 and len(calls[0]) == len(res.chunk_ids), \
        "正常文档必须仍是一次批量嵌入，不能退化成逐条调用"
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "done" and row[1] is None
    assert vs.count() == len(res.chunk_ids)
