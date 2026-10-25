"""「已跳过」语义与手动重跑的回归测试。

背景（2026-10-24）：看板卡片写的是"已跳过(源文件删除)"，但实际这 8 条全是
**扫描件**（PDF 无文字层，存量不自动 OCR），文件在 NAS 上完好可打开 —— 标签误导。
同时详情里承诺"可在看板手动触发重跑"，界面上却没有这个入口。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _login(app, name="admin", role="admin"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _skipped_doc(app, tmp_path, name="扫描件.pdf", detail="扫描件：存量不自动 OCR（可在看板手动触发重跑）"):
    conn = app.state.conn
    f = tmp_path / name
    f.write_bytes(b"%PDF-1.4 fake")
    path = str(f)
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status,status_detail,needs_ocr,scanned) "
        "VALUES (?,?,?,?,?,'skipped',?,1,1)", (path, "共享", name, name, ".pdf", detail))
    conn.commit()
    return conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]


def test_reparse_endpoint_requeues_and_allows_ocr(app, tmp_path):
    cid = _skipped_doc(app, tmp_path)
    c = _login(app)
    r = c.post(f"/api/v1/documents/{cid}/reparse")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True
    conn = app.state.conn
    row = conn.execute("SELECT status, ocr_requested, needs_ocr FROM catalog WHERE id=?",
                       (cid,)).fetchone()
    assert row[0] == "pending"
    assert row[1] == 1, "手动重跑要允许 OCR（绕过存量判定）"
    assert row[2] == 1
    # 已入队
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index' AND catalog_id=? "
                     "AND state='queued'", (cid,)).fetchone()[0]
    assert n == 1


def test_reparse_of_missing_file_returns_404(app):
    conn = app.state.conn
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status,status_detail) "
        "VALUES (?,?,?,?,?,'skipped','源文件已删除，索引与图谱产物已清理')",
        (r"\\nas\共享\没了.pdf", "共享", "没了.pdf", "没了.pdf", ".pdf"))
    conn.commit()
    cid = conn.execute("SELECT id FROM catalog WHERE name='没了.pdf'").fetchone()[0]
    c = _login(app)
    assert c.post(f"/api/v1/documents/{cid}/reparse").status_code == 404


def test_reparse_batch_queues_all_scanned_documents(app, tmp_path):
    """管理员可以一次性把扫描件全排进队列（OCR 在后台慢慢跑）。"""
    for i in range(3):
        _skipped_doc(app, tmp_path, f"扫描件{i}.pdf")
    c = _login(app)
    r = c.post("/api/v1/documents/reparse-batch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 3
    conn = app.state.conn
    n = conn.execute("SELECT COUNT(*) FROM catalog WHERE status='pending' AND ocr_requested=1"
                     ).fetchone()[0]
    assert n == 3
    jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index' AND state='queued'"
                        ).fetchone()[0]
    assert jobs == 3, "每个文档都要排进 jobs 表由流水线执行"


def test_reparse_batch_is_admin_only(app, tmp_path):
    _skipped_doc(app, tmp_path, "扫描件.pdf")
    c = _login(app, "lisi", role="user")
    assert c.post("/api/v1/documents/reparse-batch").status_code == 403


def test_stats_expose_skipped_reasons(app, tmp_path):
    """统计里要能区分"扫描件待 OCR"与"源文件已删除"，别再笼统叫"源文件删除"。"""
    _skipped_doc(app, tmp_path, "扫描件1.pdf")
    _skipped_doc(app, tmp_path, "扫描件2.pdf")
    conn = app.state.conn
    conn.execute(
        "INSERT INTO catalog(path,share,rel_path,name,ext,status,status_detail) "
        "VALUES (?,?,?,?,?,'skipped','源文件已删除，索引与图谱产物已清理')",
        (r"\\nas\共享\删了.pdf", "共享", "删了.pdf", "删了.pdf", ".pdf"))
    conn.commit()
    c = _login(app)
    s = c.get("/api/v1/documents/stats").json()
    assert s["by_status"]["skipped"] == 3
    reasons = {x["reason"]: x["count"] for x in s["skipped_reasons"]}
    assert reasons.get("扫描件待 OCR") == 2
    assert reasons.get("源文件已删除") == 1
