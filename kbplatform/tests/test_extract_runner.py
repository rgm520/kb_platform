"""Tier 2 提炼并发执行器。

回归背景（2026-11-11）：`pipeline.run_once()` 的 Tier 1 循环要把 index
队列抽干才轮到 Tier 2。新共享灌进来 18.5 万篇后，82,082 个 extract 任务
`attempts=0` 卡了两天，本地/远端大模型全程空转。执行器把 Tier 2 挪到
独立线程，索引积压再大也不影响提炼出发。
"""
from __future__ import annotations

import threading
import time

from kbplatform import models as M
from kbplatform.db import init_db
from kbplatform.extract_runner import ExtractRunner

FAKE = {
    "summary": "摘要",
    "key_points": [{"title": "要点", "detail": "细节", "entities": [], "category": "其它"}],
    "relations": [],
}


def _add_member(conn, name, url, mid, concurrency=1):
    return M.add_model(conn, name, "ollama", url, mid, in_parse_pool=True,
                       extra={"concurrency": concurrency})


def _add_doc(conn, i, kind):
    path = f"\\\\nas\\共享\\{kind}{i}.md"
    conn.execute(
        "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status)"
        " VALUES (?, '共享', ?, ?, '.md', 1, 1.0, 'pending')",
        (path, f"{kind}{i}.md", f"{kind}{i}.md"))
    cat = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES (?,0,'内容')", (path,))
    conn.commit()
    return cat


def _wait(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def _count(conn, kind, state):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND state=?",
                        (kind, state)).fetchone()[0]


def test_runner_processes_extract_jobs_while_index_backlog_remains(tmp_path, monkeypatch):
    """索引队列还有 100 个任务时，3 个提炼任务也必须做完。"""
    import kb.llm as llm
    db = str(tmp_path / "kb.db")
    conn = init_db(db)
    _add_member(conn, "本地", "http://127.0.0.1:11434", "qwen3.5-9b-64k")
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})
    monkeypatch.setattr(llm, "chat_json",
                        lambda *a, **k: (FAKE, {"wall": 0.01, "prompt_tokens": 10,
                                                "eval_tokens": 5}))

    # 索引积压
    for i in range(100):
        cat = _add_doc(conn, i, "index")
        conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                     " VALUES ('index',?,'queued',0,0)", (cat,))
    # 已切块、等提炼的文档
    for i in range(3):
        cat = _add_doc(conn, i, "extract")
        conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                     " VALUES ('extract',?,'queued',0,0)", (cat,))
    conn.commit()

    pool = M.ParsePool(conn)
    runner = ExtractRunner(db, pool, idle_sleep=0.01)
    assert runner.start() == 1
    try:
        ok = _wait(lambda: _count(conn, "extract", "queued") == 0
                   and _count(conn, "extract", "running") == 0)
    finally:
        runner.stop()

    assert ok, "提炼任务没有被执行（仍然被索引队列饿死）"
    assert _count(conn, "extract", "done") == 3
    assert _count(conn, "index", "queued") == 100, "索引积压应原样保留，不该被误领"
    assert conn.execute("SELECT COUNT(*) FROM catalog WHERE tier2_done=1"
                        ).fetchone()[0] == 3


def test_runner_runs_two_members_in_parallel(tmp_path, monkeypatch):
    """两个池成员各 1 并发 → 两条提炼线程同时工作（远端算力真正参与）。"""
    import kb.llm as llm
    db = str(tmp_path / "kb.db")
    conn = init_db(db)
    _add_member(conn, "本地", "http://127.0.0.1:11434", "m-local", concurrency=1)
    _add_member(conn, "远端", "http://192.0.2.77:11434", "m-remote", concurrency=1)
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})

    active = 0
    peak = 0
    used = []
    lock = threading.Lock()

    def fake_chat(prompt, schema=None, model=None, **kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            used.append((model, threading.get_ident()))
        time.sleep(0.2)
        with lock:
            active -= 1
        return FAKE, {"wall": 0.2, "prompt_tokens": 1, "eval_tokens": 1}

    monkeypatch.setattr(llm, "chat_json", fake_chat)

    for i in range(4):
        cat = _add_doc(conn, i, "extract")
        conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                     " VALUES ('extract',?,'queued',0,0)", (cat,))
    conn.commit()

    pool = M.ParsePool(conn)
    runner = ExtractRunner(db, pool, idle_sleep=0.01)
    assert runner.start() == 2
    try:
        assert _wait(lambda: _count(conn, "extract", "done") == 4), "4 个任务没做完"
    finally:
        runner.stop()

    assert peak >= 2, f"两个成员没有并行起来（峰值并发 {peak}）"
    assert {m for m, _ in used} == {"m-local", "m-remote"}, f"远端没被用到：{used}"
    assert len({t for _, t in used}) >= 2, "两个成员竟然跑在同一个线程上"


def test_runner_never_exceeds_member_concurrency(tmp_path, monkeypatch):
    """单成员 concurrent=2 时，同时最多 2 个请求（多线程不能把模型打爆）。"""
    import kb.llm as llm
    db = str(tmp_path / "kb.db")
    conn = init_db(db)
    _add_member(conn, "本地", "http://127.0.0.1:11434", "m-local", concurrency=2)
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_chat(prompt, schema=None, model=None, **kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return FAKE, {"wall": 0.15}

    monkeypatch.setattr(llm, "chat_json", fake_chat)

    for i in range(6):
        cat = _add_doc(conn, i, "extract")
        conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                     " VALUES ('extract',?,'queued',0,0)", (cat,))
    conn.commit()

    pool = M.ParsePool(conn)
    runner = ExtractRunner(db, pool, idle_sleep=0.01)
    assert runner.start() == 2
    try:
        assert _wait(lambda: _count(conn, "extract", "done") == 6), "6 个任务没做完"
    finally:
        runner.stop()

    assert peak == 2, f"并发没有被限制在 2（峰值 {peak}）"


def test_runner_skips_spreadsheets_without_calling_llm(tmp_path, monkeypatch):
    """表格类文档只索引、不提炼：块数占 81%，行数据对问答价值极低。"""
    import kb.llm as llm
    db = str(tmp_path / "kb.db")
    conn = init_db(db)
    _add_member(conn, "本地", "http://127.0.0.1:11434", "m-local")
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})
    calls = []

    def fake_chat(prompt, schema=None, model=None, **kw):
        calls.append(model)
        return FAKE, {"wall": 0.01}

    monkeypatch.setattr(llm, "chat_json", fake_chat)

    ids = {}
    for kind, ext in (("表格", ".xlsx"), ("正常", ".docx")):
        path = f"\\\\nas\\共享\\{kind}.{ext[1:]}"
        conn.execute(
            "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status)"
            " VALUES (?, '共享', ?, ?, ?, 1, 1.0, 'done')",
            (path, f"{kind}{ext}", f"{kind}{ext}", ext))
        cat = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]
        conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES (?,0,'内容')", (path,))
        conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                     " VALUES ('extract',?,'queued',0,0)", (cat,))
        ids[ext] = cat
    conn.commit()

    pool = M.ParsePool(conn)
    runner = ExtractRunner(db, pool, idle_sleep=0.01, skip_exts=[".xlsx"])
    runner.start()
    try:
        assert _wait(lambda: _count(conn, "extract", "done") == 2), "任务没有收尾"
    finally:
        runner.stop()

    assert calls == ["m-local"], f"表格也调用了大模型：{calls}"
    row = conn.execute("SELECT tier2_done, status_detail FROM catalog WHERE id=?",
                       (ids[".xlsx"],)).fetchone()
    assert row[0] == 1, "跳过的文档应标记为已处理，否则会永远显示待提炼"
    assert "跳过" in (row[1] or "")
    assert runner.take()["skipped"] == 1
