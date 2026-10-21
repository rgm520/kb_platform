from kbplatform.fts import (build_index, search, upsert, delete, tokenize,
                           needs_vector_fallback)


def test_tokenize_emits_unigrams_and_bigrams():
    toks = tokenize("数据机房")
    assert "数" in toks and "数据" in toks and "机房" in toks


def test_tokenize_lowercases_latin_and_keeps_digits():
    toks = tokenize("PDF 2026")
    assert "pdf" in toks and "2026" in toks


def test_two_char_chinese_query_matches(tmp_path):
    """Review Focus #1: 2 字中文词必须命中（trigram 会漏）。"""
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "信创资源池由总部统一规划建设和运营运维管理"),
                    (2, "采购评审小组由物资部门使用部门财务部门及外部专家组成")])
    assert [c for c, _ in search(p, "信创", 10)] == [1]
    assert [c for c, _ in search(p, "外部专家", 10)] == [2]


def test_three_char_query_matches(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "三级及以下子企业原则上不允许自建或租赁数据机房"),
                    (2, "无关内容")])
    assert [c for c, _ in search(p, "数据机房", 10)] == [1]


def test_single_char_query_returns_empty_and_signals_fallback(tmp_path):
    """Review Focus #2: 1 字查询不崩，返回空并提示降级。"""
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "薪酬福利")])
    assert search(p, "薪", 10) == []
    assert needs_vector_fallback("薪") is True
    assert needs_vector_fallback("信创") is False


def test_upsert_replaces_existing_row(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "旧内容关键词")])
    upsert(p, [(1, "新内容关键词甲")])
    assert [c for c, _ in search(p, "新内容关键词甲"[:4], 10)] == [1]
    assert search(p, "旧内容", 10) == []


def test_delete_removes_rows(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "甲文档内容"), (2, "乙文档内容")])
    delete(p, [1])
    assert search(p, "甲文档", 10) == []
    assert [c for c, _ in search(p, "乙文档", 10)] == [2]


def test_search_result_count_never_exceeds_limit(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(i, f"共同关键词文档{i}") for i in range(50)])
    assert len(search(p, "共同关键词", 10)) <= 10


def test_search_sorts_by_relevance_ascending_bm25(tmp_path):
    """bm25 分数越小越相关，调用方按升序取用。"""
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "数据机房审批"),
                    (2, "数据机房数据机房数据机房")])
    hits = search(p, "数据机房", 10)
    assert len(hits) == 2
    assert hits[0][1] <= hits[1][1]
