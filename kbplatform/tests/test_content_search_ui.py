"""Task 0.4：全文搜索接进文件页。

后端 POST /api/v1/search（关键词+向量双路，chunk 级片段）早就有了，
但 Web 端只有「按文件名秒搜」——记不住文件名（"扫描件001.pdf"）的用户
找不到文件。本文件锁住两件事：
  1. 文件页要有全文搜索的入口（「搜正文」开关 + 结果容器），
     files.js 要导出 KB.files.searchContent(query) -> Promise<Array>；
  2. /api/v1/search 返回 chunk 级 snippet/score/matched_by，涉密片段
     必须置空（前端照空显示"涉密·原文不回传"），没授权的用户搜不到
     范围外的目录（grants 是唯一授权源，前端不做二次过滤）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbplatform import fts
from kbplatform.auth import create_user
from kbplatform.service import make_app

WEB = Path(__file__).resolve().parents[2] / "web"

SHARE = "信息化管理"
UNC = r"\\192.0.2.19\信息化管理"

DOC_TEXT = "预算管理细则：全年采购金额上限为一百万元，超上限需总部审批后执行。"


# ------------------------------------------------------------ 前端标记
def test_file_page_has_content_search_markers():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="fm-content-search"' in html, "文件页要有「搜正文」开关"
    assert 'id="fm-content-results"' in html, "全文检索结果要有自己的容器"
    assert "搜正文" in html


def test_files_js_exports_search_content_and_masks_restricted():
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "searchContent" in js, "files.js 要导出 KB.files.searchContent"
    assert "/api/v1/search" in js, "全文搜索要打 POST /api/v1/search"
    assert "fm-content-results" in js, "结果要渲染进 #fm-content-results"
    assert "涉密·原文不回传" in js, "涉密片段要照后端口径显示"
    assert "openAt" in js, "点击结果要复用 openAt/pendingAt 交接定位"


# ------------------------------------------------------------ 接口行为
@pytest.fixture()
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    a = make_app(str(cfg))
    # 向量路要外呼 Ollama，测试里屏蔽；关键字路（FTS）走真实现
    monkeypatch.setattr("kbplatform.retrieval.Retriever._vector",
                        lambda self, query, topk: [])
    monkeypatch.setattr("kbplatform.retrieval.Retriever._wiki_ids_for",
                        lambda self, query, topk, allowed_paths=None, acl_dropped=None: [])
    cid = _seed_doc(a.state.conn)
    fts.upsert(a.state.cfg.fts_path, [(cid, DOC_TEXT)])
    yield a


def _seed_doc(conn, rel=r"制度\预算管理.md"):
    """造 catalog + chunk 给检索用（chunk_meta 要 JOIN catalog）。"""
    path = UNC + "\\" + rel
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status) "
        "VALUES(?,?,?,?,?,'done')",
        (path, SHARE, rel, "预算管理.md", ".md"))
    cur = conn.execute(
        "INSERT INTO chunks(file_path,file_id,chunk_idx,text,locator,tokens) "
        "VALUES(?,NULL,0,?,?,NULL)", (path, DOC_TEXT, "第1节"))
    conn.commit()
    return cur.lastrowid


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_search_api_returns_snippets(app):
    """正文搜索返回 chunk 级片段与召回统计（关键词+向量双路）。"""
    c = _login(app)
    r = c.post("/api/v1/search", json={"query": "预算管理", "top_k": 5})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] >= 1
    assert "keyword" in data["stats"], "召回统计里要有 keyword 路计数"
    hit = data["results"][0]
    assert "预算" in hit["snippet"], f"snippet 应含命中片段，实际 {hit}"
    assert hit["score"] > 0
    assert "keyword" in hit["matched_by"]
    assert hit["file"] == "预算管理.md"
    assert hit["path"].startswith(UNC), "path 要能对回实际文件位置"
    assert hit["locator"] == "第1节"


def test_restricted_chunk_snippet_is_masked(app):
    """涉密目录：只返回「命中了」，片段必须置空——前端照空显示。"""
    conn = app.state.conn
    conn.execute(
        "INSERT INTO dir_scopes(share,subpath,restricted,label) VALUES(?,?,1,'涉密') "
        "ON CONFLICT(share,subpath) DO UPDATE SET restricted=1",
        (SHARE, "制度"))
    conn.commit()
    c = _login(app)
    r = c.post("/api/v1/search", json={"query": "预算管理", "top_k": 5})
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits, f"涉密目录仍应给出有命中这一信息：{r.json()}"
    hit = hits[0]
    assert hit["restricted"] is True
    assert hit["snippet"] == "", "涉密片段后端必须置空，前端照空显示"


def test_user_without_grants_cannot_search_content(app):
    """grants 是唯一授权源：普通用户没被授权的目录搜不到（前端不二次过滤）。"""
    c = _login(app, name="xiaowang", role="user")
    r = c.post("/api/v1/search", json={"query": "预算管理", "top_k": 5})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0, "没授权的目录，正文一个片段也不能漏出去"
