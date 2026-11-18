"""图谱扩展通道的 SQL 绑定量。

回归：`retrieval._graph()` 第三步的 SQL 里 `{bm}` 出现两次（src IN / dst IN），
但参数只传了一遍，sqlite 直接抛
    ProgrammingError: Incorrect number of bindings supplied (uses 19, supplied 10)
表现为 `/api/v1/query` **随机 500**——只有图谱恰好扩展出邻居时才触发，
所以同一句话 admin 走通、受限账号 500，被外部 AI 当成"连接不稳定"。
"""
from kbplatform.db import init_db
from kbplatform.retrieval import Retriever


class _Fake:
    """_graph 只用到 self.conn，不需要 FTS/向量库/模型。"""

    def __init__(self, conn):
        self.conn = conn


def _seed(conn):
    # 两跳链：chunk 1 -> 节点 10/20 -> 邻居 30 -> chunk 2
    conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,20,'r',1)")
    conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,30,'r',NULL)")
    conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (30,99,'r',2)")
    conn.commit()


def test_graph_expansion_binds_all_placeholders(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _seed(conn)
    out = Retriever._graph(_Fake(conn), [1])
    assert 2 in out, f"两跳邻居所在 chunk 应被召回，实际 {out}"


def test_graph_returns_empty_for_unknown_seed(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _seed(conn)
    assert Retriever._graph(_Fake(conn), [999]) == []


def test_graph_multiple_neighbours(tmp_path):
    """邻居多于一个时绑定量最容易算错（2N 个占位符）。"""
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,20,'r',1)")
    for i, nb in enumerate((31, 32, 33, 34, 35, 36, 37, 38, 39)):
        conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,?,'r',NULL)", (nb,))
        conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (?,99,'r',?)",
                     (nb, 100 + i))
    conn.commit()
    out = Retriever._graph(_Fake(conn), [1])
    assert set(range(100, 109)).issubset(set(out)), f"应召回 9 个邻居 chunk，实际 {out}"


def test_graph_expansion_survives_huge_neighbour_set(tmp_path):
    """邻居数超过 SQLite 变量上限时不能 500。

    线上「人力有哪些流程」这类宽泛问题，图谱能从几十个种子扩出上万个邻居，
    第三步 `src IN (...) OR dst IN (...)` 直接
        OperationalError: too many SQL variables
    → /api/v1/query 500，用户看到「提问失败」。
    """
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,20,'r',1)")
    n = 20000
    conn.executemany(
        "INSERT INTO edges(src,dst,rel,chunk_id) VALUES (10,?,'r',NULL)",
        [(100000 + i,) for i in range(n)])
    conn.executemany(
        "INSERT INTO edges(src,dst,rel,chunk_id) VALUES (?,999,'r',?)",
        [(100000 + i, 200000 + i) for i in range(n)])
    conn.commit()
    out = Retriever._graph(_Fake(conn), [1])
    assert out, "大量邻居时也应召回 chunk"
    assert all(200000 <= c < 200000 + n for c in out), out[:10]
