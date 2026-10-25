"""Task 0.7：wiki 人工校正层通道补 ACL 的回归测试。

背景：检索层的关键词/向量/图谱三路都过了目录授权过滤，唯独 wiki 路没有——
`retrieve()` 把 `wiki_ids` 直接放进 RRF 融合列表。`wiki_pages` 现在是 0 行所以
无痛，但迭代 3 一旦给人工校正层做 UI 并开始写内容，这就是越权泄露路径。

本文件验证 wiki 候选与其**源文件路径**同权：用户看不到那个源文件，
就看不到挂在它上面的人工校正条目；判定用统一的授权入口（grants），
而且**不得放宽**——过滤只能让 wiki 候选变少。管理员不受限。

wiki 召回走 llm.embed（本环境要连 Ollama），测试里 monkeypatch 掉；
向量给成同一单位向量 → 所有候选相似度为 1，全部越过 0.30 阈值，
这样候选的去留只取决于 ACL，不取决于相似度排序。
"""
from __future__ import annotations

import kb.llm
from kbplatform.catalog import FileRecord
from kbplatform.db import init_db, upsert_catalog
from kbplatform.fts import build_index
from kbplatform.retrieval import Retriever
from kbplatform.vectors import VectorStore

SHARE = "信息化管理"
ROOT = r"\\nas\信息化管理"
REL_IN = r"AI知识库\办法.md"      # 用户有权
REL_OUT = r"保密\工资表.md"       # 用户无权


def _harness(tmp_path, monkeypatch, wiki_rel: str):
    """建库 + catalog 两行（有权目录 / 无权目录）+ 一条 wiki_pages 行。

    wiki 页的 path 指向 wiki_rel 指定的源文件；用户 9 通过 grants
    （grants 是唯一授权源，管理台「目录授权」就只写它）拿到 AI知识库。
    """
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
        "VALUES (?, 'smb', ?, 1, 0)", (SHARE, ROOT))
    for rel in (REL_IN, REL_OUT):
        path = ROOT + "\\" + rel
        upsert_catalog(conn, FileRecord(path=path, share=SHARE, rel_path=rel,
                                        name=rel.split("\\")[-1], ext=".md",
                                        size=10, mtime=1700000000.0))
    conn.execute(
        "INSERT INTO wiki_pages(slug,title,body,path,updated_at) "
        "VALUES ('w1','验收办法修订','服务器 硬件 更换验收记录 修订版 有效地',?,0)",
        (ROOT + "\\" + wiki_rel,))
    conn.execute(
        "INSERT INTO users(id,username,password_hash,role) VALUES (9,'u9','x','user')")
    sid = conn.execute(
        "SELECT id FROM storage_sources WHERE name=?", (SHARE,)).fetchone()[0]
    conn.execute(
        "INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) "
        "VALUES (9,?,'AI知识库',0,0)", (sid,))
    conn.commit()

    def fake_embed(texts, model="bge-m3", keep_alive="30m"):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(kb.llm, "embed", fake_embed)
    fts_path = str(tmp_path / "fts.db")
    build_index(fts_path, [])          # keyword 路留空：本文件只考察 wiki 通道
    vs = VectorStore(str(tmp_path / "vectors"), dim=1024)
    return conn, Retriever(conn, fts_path, vs)


def test_wiki_candidate_hidden_outside_scope(tmp_path, monkeypatch):
    """wiki 页挂在无权目录的源文件上：候选不得进入召回。"""
    conn, r = _harness(tmp_path, monkeypatch, REL_OUT)
    trace: dict = {}
    hits, stats = r.retrieve("服务器 验收", user_id=9, trace=trace)
    assert stats["wiki"] == 0, f"无权路径的 wiki 候选不应被召回：stats={stats}"
    assert trace["routes"]["wiki"] == [], f"轨迹里 wiki 通道应为空：{trace['routes']}"
    assert not any(h.chunk_id >= 1_000_000 for h in hits), \
        "结果里不应出现 wiki 候选"
    # 反证召回链路是通的：同一份数据，不设限的调用方（管理员）能召回该候选，
    # 说明上面的 0 是 ACL 过滤的结果，不是召回本身断了。
    _, stats_admin = r.retrieve("服务器 验收", user_id=None)
    assert stats_admin["wiki"] == 1, \
        f"管理员应能召回该 wiki 候选：stats={stats_admin}"


def test_wiki_candidate_visible_inside_scope(tmp_path, monkeypatch):
    """wiki 页挂在有权目录的源文件上：候选应保留在召回里（不得放宽）。"""
    conn, r = _harness(tmp_path, monkeypatch, REL_IN)
    trace: dict = {}
    hits, stats = r.retrieve("服务器 验收", user_id=9, trace=trace)
    assert stats["wiki"] == 1, f"有权路径的 wiki 候选应被召回：stats={stats}"
    route = trace["routes"]["wiki"]
    assert len(route) == 1 and route[0] >= 1_000_000, \
        f"轨迹里 wiki 通道应有恰好一个候选（id 段位 >= 1_000_000）：{route}"


def test_wiki_candidate_hidden_without_any_grant(tmp_path, monkeypatch):
    """既没有 grants 也没有 user_scopes 的用户：wiki 候选同样一个都看不见。"""
    conn, r = _harness(tmp_path, monkeypatch, REL_IN)
    conn.execute("DELETE FROM grants WHERE user_id=9")
    conn.commit()
    _, stats = r.retrieve("服务器 验收", user_id=9)
    assert stats["wiki"] == 0, f"零授权用户不得看到 wiki 候选：stats={stats}"


def test_wiki_candidate_without_catalog_row_is_hidden(tmp_path, monkeypatch):
    """wiki 页的 path 在 catalog 里解析不到（无法判定可见性）→ 按不可见处理。

    这类行常见于源文件已下架 / 尚未扫描；按「不得放宽」原则，
    解析不出 (share, rel_path) 的候选对非管理员一律剔除，管理员不受限。
    """
    conn, r = _harness(tmp_path, monkeypatch, r"AI知识库\幽灵.md")
    _, stats = r.retrieve("服务器 验收", user_id=9)
    assert stats["wiki"] == 0, \
        f"path 解析不到 catalog 行的 wiki 候选不应召回：stats={stats}"
    _, stats_admin = r.retrieve("服务器 验收", user_id=None)
    assert stats_admin["wiki"] == 1, \
        f"管理员应能召回该 wiki 候选：stats={stats_admin}"
