"""新文档入库后能不能**立刻**被检索到——三个回归点。

背景（2026-10-23 实测）：
  * 新文档 14:39 入库完成，但 Web 进程的向量库索引是启动时加载的，
    直到重启前 vector 通道都看不到它（`/search` 里该 chunk 的 matched_by 为空，
    是图谱通道"蹭"进来的）。对用户的表现就是「上传了但搜不到，重启才好」。
  * 顺着这条线还发现：图谱通道的召回结果没有过 ACL，只有种子过了；
    跨目录/跨共享的邻居块可能被带出来。
  * 多词中文查询 `任职资格 版本变更` 的 FTS 召回为 0：tokenize 把词间空格
    去掉后拼出跨词二元组（`格版`），文档里当然没有。
"""
import numpy as np

from kbplatform import fts
from kbplatform.db import init_db
from kbplatform.retrieval import Retriever
from kbplatform.vectors import VectorStore

DIM = 1024


def _unit(i: int) -> list[float]:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v.tolist()


# ---------------------------------------------------------------- 1 向量索引刷新
def test_vector_store_sees_vectors_written_by_another_process(tmp_path):
    """模拟：入库进程追加了向量，Web 进程里早已构造好的 VectorStore 必须能看见。"""
    reader = VectorStore(str(tmp_path))           # Web 进程：启动时构造
    reader.add([1], [_unit(0)])

    writer = VectorStore(str(tmp_path))           # ingest 进程：另一个实例
    writer.add([2], [_unit(1)])

    ids = [cid for cid, _ in reader.search(_unit(1), 5)]
    assert 2 in ids, f"新写入的向量对已存在的实例不可见（实际 {ids}）"


def test_vector_store_refresh_keeps_deleted_tombstones(tmp_path):
    """刷新不能把墓碑（-1）当成有效 id 复活。"""
    a = VectorStore(str(tmp_path))
    a.add([1, 2], [_unit(0), _unit(1)])
    a.remove([1])

    b = VectorStore(str(tmp_path))
    b.add([3], [_unit(2)])

    ids = [cid for cid, _ in a.search(_unit(0), 5)]
    assert 1 not in ids and 3 in ids, f"墓碑或刷新有误：{ids}"


# ---------------------------------------------------------------- 2 图谱通道 ACL
def _fake_meta():
    return {
        1: {"file_path": r"\\nas\共享A\库\可见.md", "share": "共享A",
            "rel_path": r"库\可见.md", "text": "可见内容", "locator": "L1",
            "document_id": 1},
        2: {"file_path": r"\\nas\共享B\机密\外泄.md", "share": "共享B",
            "rel_path": r"机密\外泄.md", "text": "不该看到的内容", "locator": "L1",
            "document_id": 2},
    }


def test_graph_hits_must_pass_scope_filter(tmp_path):
    """种子在范围内，但图谱扩展出的邻居在范围外——不能返回给用户。"""
    conn = init_db(str(tmp_path / "kb.db"))
    r = Retriever(conn, "unused", None)
    r._allowed_chunk_ids = lambda uid: {1}          # 只允许 chunk 1
    r._keyword = lambda q, k: [1]
    r._vector = lambda q, k: []
    r._graph = lambda seeds: [2]                    # 图谱把范围外的 2 带了出来
    r.chunk_meta = lambda ids: {i: _fake_meta()[i] for i in ids if i in _fake_meta()}

    hits, stats = r.retrieve("任意问题", user_id=24, topk=5, use_wiki=False)
    ids = [h.chunk_id for h in hits]
    assert 2 not in ids, f"越权：范围外 chunk 2 被返回（hits={ids}）"
    assert ids == [1], f"范围内 chunk 1 应正常返回（hits={ids}）"
    assert stats["graph"] == 0, f"stats 应反映过滤后的图谱召回数（stats={stats}）"


def test_graph_hits_are_attributed(tmp_path):
    """图谱召回的块要标记 matched_by=['graph']，否则无法定位"它从哪条路来的"。"""
    conn = init_db(str(tmp_path / "kb.db"))
    r = Retriever(conn, "unused", None)
    r._allowed_chunk_ids = lambda uid: {1, 2}
    r._keyword = lambda q, k: [1]
    r._vector = lambda q, k: []
    r._graph = lambda seeds: [2]
    r.chunk_meta = lambda ids: {i: _fake_meta()[i] for i in ids if i in _fake_meta()}

    hits, _ = r.retrieve("任意问题", user_id=24, topk=5, use_wiki=False)
    by_id = {h.chunk_id: h for h in hits}
    assert 2 in by_id and "graph" in by_id[2].matched_by, \
        f"图谱命中未标注来源：{[ (h.chunk_id, h.matched_by) for h in hits ]}"


# ---------------------------------------------------------------- 3 多词查询
def test_multi_term_query_does_not_glue_bigrams_across_terms(tmp_path):
    """`任职资格 版本变更` 不能在词与词之间拼出 `格版` 这种不存在的二元组。"""
    p = str(tmp_path / "fts.db")
    fts.build_index(p, [(14, "某公司任职资格智能评审平台版本变更说明")])

    hits = [cid for cid, _ in fts.search(p, "任职资格 版本变更", 5)]
    assert hits == [14], f"多词查询召回为空，跨词二元组污染了 MATCH（实际 {hits}）"


def test_multi_term_query_still_requires_all_terms(tmp_path):
    """不能为了能搜到就退化成 OR：只含一个词的块不该被召回。"""
    p = str(tmp_path / "fts.db")
    fts.build_index(p, [(1, "某公司任职资格智能评审平台"),
                        (2, "另一份部署包版本变更说明")])

    hits = [cid for cid, _ in fts.search(p, "任职资格 版本变更", 5)]
    assert hits == [], f"AND 语义被破坏，只含单个词的块被召回（实际 {hits}）"
