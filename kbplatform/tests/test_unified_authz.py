"""文件页与文档看板用**同一套授权**的回归测试。

背景（2026-10-24）：普通用户在文件页看到"没有可访问的存储源"，但文档看板
有数据——因为文件页按 2.0 的 `grants` 过滤，看板还在按 1.0 的 `user_scopes`。
现在：grants 是唯一授权源，老范围启动时自动同步；看板/检索也认 grants。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.db import upsert_catalog
from kbplatform.catalog import FileRecord
from kbplatform.grants import migrate_user_scopes
from kbplatform.service import make_app

SHARE = "信息化管理"
UNC = r"\\192.0.2.19\信息化管理"


def _cfg(tmp_path, with_share=True):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    extra = ""
    if with_share:
        extra = ('[drives]\n'
                 '"W:" = "\\\\\\\\192.0.2.19\\\\信息化管理"\n'
                 '[shares]\nnames = ["信息化管理"]\n')
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n' + extra,
                   encoding="utf-8")
    return cfg


@pytest.fixture()
def app(tmp_path):
    return make_app(str(_cfg(tmp_path)))


def _login(app, name="zhangsan", role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c, uid


def _doc(conn, rel="AI知识库\\办法.pdf"):
    path = UNC + "\\" + rel
    fr = FileRecord(path=path, share=SHARE, rel_path=rel, name="办法.pdf",
                    ext=".pdf", size=10, mtime=1700000000.0)
    return upsert_catalog(conn, fr)


def test_legacy_scope_migrates_to_grants_on_startup(tmp_path):
    """老范围在启动时同步成 2.0 目录授权（幂等）。"""
    # 先造一个"只有老范围"的环境
    cfg = _cfg(tmp_path, with_share=False)
    app0 = make_app(str(cfg))
    conn = app0.state.conn
    create_user(conn, "zhangsan", "pw123456", role="user")
    uid = conn.execute("SELECT id FROM users WHERE username='zhangsan'").fetchone()[0]
    conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                 (uid, SHARE, "AI知识库"))
    conn.commit()
    # 重新启动（这次配置里有了共享）
    app2 = make_app(str(_cfg(tmp_path)))
    grants = app2.state.conn.execute(
        "SELECT source_id, subpath FROM grants WHERE user_id=?", (uid,)).fetchall()
    assert len(grants) == 1, f"老范围没有同步成目录授权：{grants}"
    assert grants[0][1] == "AI知识库"
    # 再跑一次不应重复
    migrate_user_scopes(app2.state.conn, app2.state.cfg)
    assert app2.state.conn.execute(
        "SELECT COUNT(*) FROM grants WHERE user_id=?", (uid,)).fetchone()[0] == 1


def test_normal_user_sees_files_and_documents_with_same_grant(app):
    """同一份授权：文件页能看到目录，看板也能看到对应文档。"""
    c, uid = _login(app)
    conn = app.state.conn
    sid = conn.execute("SELECT id FROM storage_sources WHERE name=?", (SHARE,)).fetchone()[0]
    # 只给 2.0 授权（不碰老表）
    from kbplatform.grants import set_grants
    set_grants(conn, uid, [{"source_id": sid, "subpath": "AI知识库", "can_write": False}])
    _doc(conn)

    # 文件页：能看到这个源
    srcs = c.get("/api/v1/fs/sources").json()["sources"]
    assert [s["name"] for s in srcs] == [SHARE]
    # 文件页：能进入授权目录
    r = c.get(f"/api/v1/fs/list?source_id={sid}&rel=AI知识库")
    assert r.status_code == 200, r.text
    # 看板：同一份授权下能看到对应文档
    docs = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs["total"] >= 1, "看板看不到已授权目录下的文档"
    # 越权目录：文件和文档都看不到
    conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                 (uid, SHARE, "别的目录"))
    conn.commit()
    docs2 = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs2["total"] == docs["total"], "老范围不该凭空多出可见文档"


def test_grant_without_legacy_scope_hides_documents(app):
    """没有任何授权的普通用户：文件和文档都看不到。"""
    c, uid = _login(app, "lisi")
    conn = app.state.conn
    _doc(conn)
    docs = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs["total"] == 0
    assert c.get("/api/v1/fs/sources").json()["sources"] == []


def test_legacy_scope_api_writes_grants(app):
    """老的「共享/子目录」授权接口内部改写 grants（统一授权源）。"""
    c, uid = _login(app)
    r = c.put(f"/api/v1/admin/users/{uid}/scopes", ctx=None) if False else None
    # 用管理员身份来改普通用户的范围
    c2, _ = _login(app, "admin", role="admin")
    resp = c2.put(f"/api/v1/admin/users/{uid}/scopes",
                   json={"scopes": [[SHARE, "AI知识库"]]})
    assert resp.status_code == 200, resp.text
    conn = app.state.conn
    grants = conn.execute("SELECT subpath FROM grants WHERE user_id=?", (uid,)).fetchall()
    assert [g[0] for g in grants] == ["AI知识库"]
    # 文件页立刻能用
    srcs = c.get("/api/v1/fs/sources").json()["sources"]
    assert [s["name"] for s in srcs] == [SHARE]


# ================= 热修 2026-10-25：检索层认 grants =================
# 管理台「目录授权」只写 grants（没有 user_scopes 行）后，看板能看到文档，
# 但 /api/v1/search 与 /api/v1/query 四路召回全为 0：
# retrieval._allowed_chunk_ids 只按老范围（visible_predicate）算，
# grants-only 用户的 chunk 全被 SQL 层过滤掉了。

import kb.llm as llm                                              # noqa: E402
from kbplatform import fts                                        # noqa: E402

_CHUNK_TEXT = "服务器 硬件 更换验收记录"


def _seed_local_doc(app, tmp_path, username, grant=True, text=_CHUNK_TEXT,
                    rel="AI知识库\\办法.md"):
    """本地存储源 + catalog 行 + chunk，可选给 username 一条 grants（只写 grants）。"""
    conn = app.state.conn
    dir_src = tmp_path / "本地库"
    dir_src.mkdir(parents=True, exist_ok=True)
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,writable,created_at) "
        "VALUES ('本地库','local',?,1,1,0)", (str(dir_src),))
    sid = conn.execute("SELECT id FROM storage_sources WHERE name='本地库'").fetchone()[0]
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    if grant:
        # 管理台「目录授权」的实际效果：grants 表插一行，**不写** user_scopes
        conn.execute(
            "INSERT INTO grants(user_id,source_id,subpath,can_write) VALUES (?,?,?,0)",
            (uid, sid, "AI知识库"))
    path = str(dir_src / rel)
    upsert_catalog(conn, FileRecord(path=path, share="本地库", rel_path=rel,
                                    name="办法.md", ext=".md",
                                    size=10, mtime=1700000000.0))
    conn.execute("INSERT INTO chunks(file_path,chunk_idx,text,locator) VALUES (?,0,?,'1段')",
                 (path, text))
    chunk_id = conn.execute("SELECT id FROM chunks WHERE file_path=?", (path,)).fetchone()[0]
    conn.commit()
    # keyword 通道走真实 FTS（不依赖 embedding 服务）
    fts.build_index(app.state.cfg.fts_path, [(chunk_id, text)])
    return path


def test_retrieval_sees_documents_for_grants_only_user(app, tmp_path, monkeypatch):
    """grants-only 用户（没有 user_scopes 行）的文档必须能被检索层看见。"""
    c, _ = _login(app, "zhangsan")
    path = _seed_local_doc(app, tmp_path, "zhangsan")
    monkeypatch.setattr(llm, "embed", lambda *a, **k: [])   # 不依赖真实 embedding 服务
    r = c.post("/api/v1/search", json={"query": "验收"})
    assert r.status_code == 200, r.text
    body = r.json()
    hits = [x for x in body["results"] if x["path"] == path]
    assert hits, f"grants-only 用户搜不到已授权目录下的文档：total={body['total']} stats={body['stats']}"
    assert "keyword" in hits[0]["matched_by"], f"keyword 通道没有召回：{body['stats']}"


def test_retrieval_blocks_user_without_any_grant(app, tmp_path, monkeypatch):
    """既没有 grants 也没有 user_scopes 的用户：什么都搜不到（不得放宽）。

    先让管理员搜到同一篇，证明召回链路是通的，total==0 才是过滤的结果。
    """
    c_admin, _ = _login(app, "admin", role="admin")
    c_user, _ = _login(app, "lisi")
    path = _seed_local_doc(app, tmp_path, "admin")   # 造数据，不给 lisi 任何授权
    monkeypatch.setattr(llm, "embed", lambda *a, **k: [])
    r_admin = c_admin.post("/api/v1/search", json={"query": "验收"}).json()
    assert any(x["path"] == path for x in r_admin["results"]), \
        f"管理员都搜不到，召唤链路有问题，过滤结论不成立：{r_admin['stats']}"
    r_user = c_user.post("/api/v1/search", json={"query": "验收"}).json()
    assert r_user["total"] == 0, f"无任何授权的用户搜出了结果：{r_user['results']}"


def test_board_and_search_agree_on_visibility(app, tmp_path, monkeypatch):
    """同一个 grants-only 用户：看板能看到的文档，检索也命中同一篇。"""
    c, _ = _login(app, "zhangsan")
    path = _seed_local_doc(app, tmp_path, "zhangsan")
    monkeypatch.setattr(llm, "embed", lambda *a, **k: [])
    docs = c.get("/api/v1/documents?page=1&page_size=20").json()
    assert docs["total"] >= 1, "看板看不到 grants-only 用户的文档"
    body = c.post("/api/v1/search", json={"query": "验收"}).json()
    assert body["total"] >= 1 and any(x["path"] == path for x in body["results"]), \
        f"看板与检索范围不一致：documents total={docs['total']}，search total={body['total']}"


def test_unified_predicate_is_or_of_legacy_and_grants(conn):
    """既有老范围又有 grants 的用户：谓词要同时含两边的子句与参数。"""
    from kbplatform.scopes import unified_predicate
    conn.execute(
        "INSERT INTO users(id,username,password_hash,role) VALUES (9,'u9','x','user')")
    conn.execute("INSERT INTO user_scopes(user_id,share,subpath) VALUES (9,'共享A','制度')")
    conn.execute(
        "INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
        "VALUES (3,'库','local','D:/库',1,0)")
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write) VALUES (9,3,'项目',0)")
    conn.commit()
    pred, params = unified_predicate(conn, 9, "c")
    assert " OR " in pred, f"老范围与 grants 应该 OR 起来：{pred}"
    assert "c.share" in pred and "c.path" in pred, f"两边都该出现在谓词里：{pred}"
    sparams = [str(p) for p in params]
    assert "共享A" in sparams, f"老范围侧的参数不在 params 里：{sparams}"
    assert any(str(p).startswith("d:/库\\项目") for p in sparams), \
        f"grants 侧的参数不在 params 里：{sparams}"


def test_unified_predicate_blocks_user_without_any_grant(conn):
    """两边都没有授权：返回 ("1=0", [])，钉住「不得放宽」。"""
    from kbplatform.scopes import unified_predicate
    conn.execute(
        "INSERT INTO users(id,username,password_hash,role) VALUES (9,'u9','x','user')")
    conn.commit()
    pred, params = unified_predicate(conn, 9, "c")
    assert (pred, params) == ("1=0", []), \
        f"没有任何授权的用户必须什么都看不到：{pred!r} {params!r}"
