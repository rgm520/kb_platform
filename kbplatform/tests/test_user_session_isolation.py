"""不同用户之间的"提问记录"必须互相隔离。

用户报障（2026-10-26）：共享电脑上，用户 A 退出、用户 B 登录后，
B 的 AI 问答页上**还留着 A 刚才的问题与回答**（同一个浏览器、不刷新页面）。

两道防线，本文件逐条钉住：

1. **前端**：换用户（登录/退出）时必须清空上一个用户的数据容器。
   SPA 换用户只切显隐、不清 DOM → 上一个人的提问、回答、引用片段、
   看板清单、文件列表全都留在屏幕上。
2. **后端**：Web 会话的提问也要进审计（按 user_id 归属），
   普通用户在「调用审计」里只看得到自己的记录。

浏览器实测（2026-10-26，修复前）:
  A 登录 → 提问 → 退出 → B 登录（不刷新）→ B 的 #a-answer 里仍是
  "资料中没有覆盖"用户A的私密提问 AAA 隔离标记"这一问题的具体内容…"
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbplatform import models as models_mod
from kbplatform.auth import create_user
from kbplatform.service import make_app

FAKE_ANSWER = "（模拟回答）这是测试用的回答。"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    a = make_app(str(cfg))
    # 提供一个可用模型并屏蔽真正的 LLM 调用（问答要真的走通，才能验证审计归属）
    models_mod.add_model(a.state.conn, "fake", "openai_compatible",
                         "http://localhost", "fake-model", api_key="sk-x",
                         is_default=True)
    monkeypatch.setattr(models_mod, "chat",
                        lambda *args, **kwargs: (FAKE_ANSWER, {"wall": 0.1}))
    # 四路召回里最贵的两路直接给确定结果
    monkeypatch.setattr("kbplatform.retrieval.Retriever._keyword",
                        lambda self, query, topk: [])
    monkeypatch.setattr("kbplatform.retrieval.Retriever._vector",
                        lambda self, query, topk: [])
    monkeypatch.setattr("kbplatform.retrieval.Retriever._wiki_ids_for",
                        lambda self, query, topk, allowed_paths=None, acl_dropped=None: [])
    yield a


def _login(app, name, role="user", password="pw123456"):
    conn = app.state.conn
    create_user(conn, name, password, role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return c


def _ask(client, question):
    r = client.post("/api/v1/query", json={"question": question, "top_k": 1})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


def test_web_session_query_is_audited_with_owner(app):
    """Web 会话（不是 token）的提问也要进审计，并归属到提问的用户。"""
    c = _login(app, "alice")
    conn = app.state.conn
    uid = conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    _ask(c, "Alice 的问题原文")

    row = conn.execute(
        "SELECT user_id, action, question FROM audit_log WHERE action='query' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "Web 会话的提问没有进审计"
    assert row[0] == uid, f"审计没归属到提问的人：{tuple(row)}"
    assert row[2] == "Alice 的问题原文", tuple(row)


def test_normal_user_audit_only_shows_own_records(app):
    """普通用户在「调用审计」里只看得到自己的提问记录。"""
    ca = _login(app, "bob")
    cb = _login(app, "carol")
    _ask(ca, "BOB 的私密问题")
    _ask(cb, "CAROL 的私密问题")

    a = ca.get("/api/v1/admin/audit?limit=100").json()["audit"]
    b = cb.get("/api/v1/admin/audit?limit=100").json()["audit"]
    assert any(r["question"] == "BOB 的私密问题" for r in a), "bob 看不到自己的记录"
    assert not any(r["question"] == "CAROL 的私密问题" for r in a), "bob 看到了 carol 的记录"
    assert any(r["question"] == "CAROL 的私密问题" for r in b), "carol 看不到自己的记录"
    assert not any(r["question"] == "BOB 的私密问题" for r in b), "carol 看到了 bob 的记录"


def test_admin_audit_shows_all_records(app):
    """管理员仍看得到全部（审计是管理职能，与用户隔离是两回事）。"""
    admin = _login(app, "root", role="admin")
    ca = _login(app, "dave")
    _ask(ca, "DAVE 的问题")
    rows = admin.get("/api/v1/admin/audit?limit=100").json()["audit"]
    assert any(r["question"] == "DAVE 的问题" for r in rows)


def test_frontend_clears_user_scoped_ui_when_user_changes():
    """换用户（登录/退出）必须清空上一个用户的数据容器。"""
    js = Path("web/static/app.js").read_text(encoding="utf-8")
    assert "clearUserScopedUI" in js, "没有换用户时清屏的逻辑"
    assert js.count("clearUserScopedUI") >= 3, "showLogin/showMain 都要调用清屏逻辑"
    # 问答页的容器必须都在清理名单里
    for sel in ('"#a-answer"', '"#a-cites"', '"#a-q"'):
        assert sel in js, f"清理名单里缺少 {sel}（上一个人的提问/回答会留在屏幕上）"


def test_frontend_ask_page_containers_are_in_cleanup_list():
    """把要清的容器抽成一份名单，便于测试与后续维护。"""
    js = Path("web/static/app.js").read_text(encoding="utf-8")
    assert "USER_SCOPED_CONTAINERS" in js, "清理容器名单没抽出来"
