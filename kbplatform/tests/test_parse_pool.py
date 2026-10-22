import pytest

from kbplatform.db import init_db
from kbplatform import models as M


def _mk(conn, name, url, mid, weight=1.0, pool=True, extra=None):
    i = M.add_model(conn, name, "ollama", url, mid, is_local=True,
                    in_parse_pool=pool, pool_weight=weight, extra=extra or {})
    return M.get_model(conn, i)


def test_model_family_normalizes_context_variants():
    """qwen3.5:9b 与 qwen3.5-9b-64k 是同一个模型族。"""
    assert M.model_family("qwen3.5:9b") == M.model_family("qwen3.5-9b-64k")
    assert M.model_family("qwen3.5-9b-32k") == M.model_family("qwen3.5:9b")
    assert M.model_family("deepseek-chat") != M.model_family("qwen3.5:9b")


def test_pool_only_contains_marked_members(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    a = _mk(conn, "A", "http://127.0.0.1:11434", "m-a")
    b = _mk(conn, "B", "http://127.0.0.1:11434", "m-b", pool=False)
    pool = M.ParsePool(conn)
    ids = [m.id for m in pool.members()]
    assert a.id in ids
    assert b.id not in ids


def test_pool_pick_alternates_between_members(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _mk(conn, "A", "http://127.0.0.1:11434", "m-a")
    _mk(conn, "B", "http://127.0.0.1:11434", "m-b")
    pool = M.ParsePool(conn)
    picked = []
    for _ in range(6):
        m = pool.pick()
        assert m is not None
        picked.append(m.id)
        pool.release(m)
    assert len(set(picked)) == 2, f"应该在两个成员间轮转，实际 {picked}"
    # 交替出现
    assert picked[0] != picked[1]


def test_pool_respects_concurrency_limit(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    a = _mk(conn, "A", "http://127.0.0.1:11434", "m-a")
    b = _mk(conn, "B", "http://127.0.0.1:11434", "m-b")
    pool = M.ParsePool(conn, max_concurrency={a.id: 1, b.id: 1})
    first = pool.pick()
    second = pool.pick()
    assert first is not None and second is not None
    assert first.id != second.id, "满载的成员不应再被分配"
    pool.release(first)
    pool.release(second)


def test_pool_returns_none_when_all_busy(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    a = _mk(conn, "A", "http://127.0.0.1:11434", "m-a")
    pool = M.ParsePool(conn, max_concurrency={a.id: 1})
    assert pool.pick() is not None
    assert pool.pick() is None, "全部满载时应返回 None 让调用方排队"


def test_pool_weight_ratio(tmp_path):
    """权重 3:1 时，A 拿到的活应明显多于 B。"""
    conn = init_db(str(tmp_path / "kb.db"))
    _mk(conn, "A", "http://127.0.0.1:11434", "m-a", weight=3.0)
    _mk(conn, "B", "http://127.0.0.1:11434", "m-b", weight=1.0)
    pool = M.ParsePool(conn)
    counts = {}
    for _ in range(8):
        m = pool.pick()
        counts[m.id] = counts.get(m.id, 0) + 1
        pool.release(m)
    a_id = [m.id for m in pool.members() if m.name == "A"][0]
    b_id = [m.id for m in pool.members() if m.name == "B"][0]
    assert counts[a_id] == 6, f"期望 A 拿 6 次，实际 {counts}"
    assert counts[b_id] == 2, f"期望 B 拿 2 次，实际 {counts}"


def test_pool_excludes_unusable_members(tmp_path):
    """连不上的主机不应进池。"""
    conn = init_db(str(tmp_path / "kb.db"))
    i = M.add_model(conn, "连不上", "ollama", "http://127.0.0.1:59998", "x",
                    in_parse_pool=True)
    pool = M.ParsePool(conn)
    assert pool.members() == []
    assert pool.pick() is None


def test_update_pool_member_toggles_membership(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    i = M.add_model(conn, "X", "ollama", "http://127.0.0.1:11434", "x")
    M.update_pool_member(conn, i, True, 2.5)
    m = M.get_model(conn, i)
    assert m.in_parse_pool is True
    assert m.pool_weight == 2.5
    M.update_pool_member(conn, i, False)
    assert M.get_model(conn, i).in_parse_pool is False


def test_pool_status_reports_mixed_family(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _mk(conn, "A", "http://127.0.0.1:11434", "qwen3.5:9b-64k")
    _mk(conn, "B", "http://127.0.0.1:11434", "deepseek-chat")
    st = M.ParsePool(conn).status()
    assert st["mixed_family"] is True
    assert len(st["families"]) == 2
