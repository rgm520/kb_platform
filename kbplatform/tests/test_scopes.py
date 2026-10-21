import pytest

from kbplatform.db import init_db
from kbplatform.scopes import (Scope, is_visible, is_restricted, load_scopes,
                               load_restricted, visible_predicate)


def test_scope_covers_whole_share():
    s = Scope("信息化管理", "")
    assert s.covers("信息化管理", "AI知识库\\a.pdf")
    assert not s.covers("video", "a.mp4")


def test_scope_respects_directory_boundary():
    """AI知识库 不能匹配 AI知识库2 —— 前缀必须按目录边界。"""
    s = Scope("信息化管理", "AI知识库")
    assert s.covers("信息化管理", "AI知识库\\a.pdf")
    assert not s.covers("信息化管理", "AI知识库2\\a.pdf")
    assert s.covers("信息化管理", "AI知识库")


def test_scope_is_case_insensitive():
    s = Scope("InfoManage", "AI知识库")
    assert s.covers("infomanage", "ai知识库\\a.pdf")


def test_is_visible_any_scope_matches():
    scopes = [Scope("A", "x"), Scope("B", "")]
    assert is_visible(scopes, "B", "anything")
    assert is_visible(scopes, "A", "x\\y.pdf")
    assert not is_visible(scopes, "C", "z.pdf")


def test_is_restricted():
    r = [Scope("HR", "4、薪酬")]
    assert is_restricted(r, "HR", "4、薪酬\\a.pdf")
    assert not is_restricted(r, "HR", "1、制度")
    assert not is_restricted(r, "其他", "4、薪酬\\a.pdf")


def test_visible_predicate_empty_scope_blocks_everything():
    pred, params = visible_predicate(1, "c", [])
    assert pred == "1=0"
    assert params == []


def test_visible_predicate_generates_params():
    pred, params = visible_predicate(1, "c", [Scope("A", ""), Scope("B", "x")])
    assert "A" in params and "B" in params
    assert "x" in params
    assert pred.count("OR") >= 1


def test_load_scopes_and_restricted_from_db(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO user_scopes(user_id,share,subpath) VALUES (1,'A','x')")
    conn.execute("INSERT INTO dir_scopes(share,subpath,restricted) VALUES ('B','y',1)")
    conn.commit()
    s = load_scopes(conn, 1)
    assert s == [Scope("A", "x")]
    r = load_restricted(conn)
    assert r == [Scope("B", "y")]


def test_visible_predicate_sql_filters_correctly(tmp_path):
    """SQL 层过滤：不在范围内的行必须被排除。"""
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO catalog(path,share,rel_path,name,ext) VALUES"
                 " ('p1','A','x\\1.pdf','1.pdf','.pdf')")
    conn.execute("INSERT INTO catalog(path,share,rel_path,name,ext) VALUES"
                 " ('p2','B','y\\2.pdf','2.pdf','.pdf')")
    conn.commit()
    pred, params = visible_predicate(1, "c", [Scope("A", "x")])
    rows = conn.execute(
        f"SELECT c.path FROM catalog c WHERE {pred}", params).fetchall()
    assert [r[0] for r in rows] == ["p1"]
