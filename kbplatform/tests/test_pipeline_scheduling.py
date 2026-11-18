"""Tier 1 索引积压不能把 Tier 2 提炼饿死。

线上事故（2026-11-11）：新共享灌进 18.5 万篇，`run_once()` 的 Tier 1
while 循环要抽干 index 队列才轮到 Tier 2 → 82,082 个 extract 任务
`attempts=0` 卡了两天，两台 Ollama 的生成模型全程没加载、零调用。
"""
from __future__ import annotations

import threading
import types

from kbplatform import models as M
from kbplatform.config import load_config
from kbplatform.db import init_db
import kbplatform.pipeline as pipeline_mod
from kbplatform.pipeline import Pipeline

FAKE = {
    "summary": "摘要",
    "key_points": [{"title": "要点", "detail": "细节", "entities": [], "category": "其它"}],
    "relations": [],
}


def _cfg(tmp_path):
    data = str(tmp_path / "data").replace("\\", "/")
    p = tmp_path / "config.toml"
    p.write_text(f'[paths]\ndata_dir="{data}"\n'
                 '[security]\nsession_secret = "unit-secret"\n'
                 '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return load_config(str(p))


def test_index_backlog_no_longer_starves_extraction(tmp_path, monkeypatch):
    p = Pipeline(_cfg(tmp_path))          # monitor_roots 为空，不会去扫 NAS
    conn = p.conn
    M.add_model(conn, "本地", "ollama", "http://127.0.0.1:11434",
                "qwen3.5-9b-64k", in_parse_pool=True)
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})

    extracted = threading.Event()

    def fake_chat(*a, **k):
        extracted.set()
        return FAKE, {"wall": 0.01, "prompt_tokens": 1, "eval_tokens": 1}

    monkeypatch.setattr("kb.llm.chat_json", fake_chat)

    # 20 篇待索引（模拟积压）；size 各不相同，避开 MD5 重复识别去读不存在的文件
    for i in range(20):
        conn.execute(
            "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status)"
            " VALUES (?, '共享', ?, ?, '.md', ?, 1.0, 'pending')",
            (f"\\\\nas\\共享\\index{i}.md", f"index{i}.md", f"index{i}.md", 1000 + i))
        cat = conn.execute("SELECT id FROM catalog WHERE path=?",
                           (f"\\\\nas\\共享\\index{i}.md",)).fetchone()[0]
        p.queue.enqueue("index", cat)

    # 1 篇已切块、等提炼
    path = "\\\\nas\\共享\\extract.md"
    conn.execute(
        "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status,"
        " tier1_done) VALUES (?, '共享', 'extract.md', 'extract.md', '.md', 5000, 1.0,"
        " 'done', 1)", (path,))
    cat = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES (?,0,'内容')", (path,))
    conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                 " VALUES ('extract',?,'queued',0,0)", (cat,))
    conn.commit()

    def fake_index(*a, **k):
        # 索引循环被卡在这里等提炼先跑完：若提炼仍被饿死，run_once 永远不返回
        assert extracted.wait(5.0), "索引队列没排空前，提炼完全没有开始（被饿死）"
        return types.SimpleNamespace(ok=True)

    monkeypatch.setattr(pipeline_mod.indexer, "index_file", fake_index)

    p.runner.start()
    try:
        stats = p.run_once()
    finally:
        p.runner.stop()

    assert stats["indexed"] == 20
    assert conn.execute("SELECT state FROM jobs WHERE kind='extract'").fetchone()[0] == "done"
    assert conn.execute("SELECT tier2_done FROM catalog WHERE id=?",
                        (cat,)).fetchone()[0] == 1


def test_run_once_without_runner_still_extracts_synchronously(tmp_path, monkeypatch):
    """`--once` / 单测没有后台线程时，Tier 2 仍要同步执行（行为不回退）。"""
    p = Pipeline(_cfg(tmp_path))
    conn = p.conn
    M.add_model(conn, "本地", "ollama", "http://127.0.0.1:11434",
                "qwen3.5-9b-64k", in_parse_pool=True)
    monkeypatch.setattr(M, "check_usable", lambda cfg: {"usable": True, "reason": ""})
    monkeypatch.setattr("kb.llm.chat_json",
                        lambda *a, **k: (FAKE, {"wall": 0.01}))

    path = "\\\\nas\\共享\\only.md"
    conn.execute(
        "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status,"
        " tier1_done) VALUES (?, '共享', 'only.md', 'only.md', '.md', 9, 1.0, 'done', 1)",
        (path,))
    cat = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES (?,0,'内容')", (path,))
    conn.execute("INSERT INTO jobs(kind,catalog_id,state,attempts,created_at)"
                 " VALUES ('extract',?,'queued',0,0)", (cat,))
    conn.commit()

    stats = p.run_once()
    assert stats["extracted"] == 1
    assert conn.execute("SELECT tier2_done FROM catalog WHERE id=?",
                        (cat,)).fetchone()[0] == 1


def test_reconcile_pending_query_uses_jobs_catalog_index(tmp_path):
    """启动补队必须走索引。

    18.5 万 catalog × 20 万 jobs 的相关子查询，没有 (kind,catalog_id,state)
    索引时实测 390 秒，流水线重启会卡在启动阶段进不了主循环。
    """
    conn = init_db(str(tmp_path / "kb.db"))
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT c.id FROM catalog c WHERE c.tier1_done=0"
        " AND c.status='pending' AND NOT EXISTS ("
        "  SELECT 1 FROM jobs j WHERE j.kind='index' AND j.catalog_id=c.id"
        "  AND j.state IN ('queued','running'))"))
    assert "idx_jobs_catalog" in plan, f"补队查询退化成全扫：{plan}"


def test_pipeline_does_not_enqueue_extract_for_skipped_ext(tmp_path, monkeypatch):
    """索引完成后，表格类文档不应再生成提炼任务（省掉 1.1 万个空转任务）。"""
    p = Pipeline(_cfg(tmp_path))
    p.cfg.extract_skip_exts = [".xlsx"]
    conn = p.conn

    path = "\\\\nas\\共享\\表.xlsx"
    conn.execute(
        "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status)"
        " VALUES (?, '共享', '表.xlsx', '表.xlsx', '.xlsx', 3, 1.0, 'pending')",
        (path,))
    cat = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]
    p.queue.enqueue("index", cat)
    monkeypatch.setattr(pipeline_mod.indexer, "index_file",
                        lambda *a, **k: types.SimpleNamespace(ok=True))

    stats = p.run_once()
    assert stats["indexed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='extract'"
                        ).fetchone()[0] == 0, "表格类不该入提炼队列"
