"""存储接入：点「扫描」= 把这个共享纳入解析（按钮三态背后的行为）。

用户要求（2026-10-26）：
  * 新接入的存储，点「扫描」就按**整个共享**开始解析，不再受 [monitor].roots 限制；
  * 按钮从「扫描」（白）→「解析中」（红）→「已解析」（绿）。

之前的语义是"浏览范围=整个共享、解析范围=[monitor].roots"，所以新接入的共享
点扫描只会得到一行红字「该共享不在配置的监控范围内，仅浏览不解析」。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.service import make_app


def _cfg(tmp_path, monitor_roots=None):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    text = f'[paths]\ndata_dir="{data}"\n[storage]\nsource_scan_interval_sec=0\n'
    if monitor_roots:
        vals = ", ".join('"%s"' % str(m).replace("\\", "\\\\") for m in monitor_roots)
        text += "[monitor]\nroots = [%s]\n" % vals
    cfg.write_text(text, encoding="utf-8")
    return str(cfg)


def _boot(cfg):
    """生产里 source_scan_interval_sec>0 时由 make_app 启动扫描线程；
    测试里配的是 0（避免后台轮询干扰），所以显式启动。"""
    app = make_app(cfg)
    app.state.scanner.start()
    return app


def _admin(app):
    create_user(app.state.conn, "root", "pw123456", role="admin")
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "root", "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def _add_local_source(conn, root, name="新共享"):
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,created_at) "
        "VALUES (?,'local',?,1,0)", (name, str(root)))
    conn.commit()
    return conn.execute("SELECT id FROM storage_sources WHERE name=?", (name,)).fetchone()[0]


def _wait_done(app, sid, want_error=False, timeout=25.0):
    """等扫描线程跑完（scan_done_at 落库 / error 落库）。"""
    conn = app.state.conn
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute(
            "SELECT scan_state, scan_error, scan_done_at FROM storage_sources WHERE id=?",
            (sid,)).fetchone()
        if want_error:
            if row["scan_state"] == "error":
                return row
        elif row["scan_state"] != "scanning" and row["scan_done_at"]:
            return row
        time.sleep(0.15)
    raise AssertionError("扫描没有在 %.0f 秒内结束: %s" % (
        timeout, dict(conn.execute("SELECT * FROM storage_sources WHERE id=?", (sid,)).fetchone())))


def test_scan_indexes_whole_share_when_source_not_in_monitor_roots(tmp_path):
    """不在 [monitor].roots 里的共享：点扫描 → 整个共享都被解析。"""
    share = tmp_path / "share"
    (share / "sub").mkdir(parents=True)
    (share / "a.txt").write_text("alpha", encoding="utf-8")
    (share / "sub" / "b.md").write_text("beta", encoding="utf-8")
    (share / "blob.bin").write_bytes(b"\x00\x01")      # 非文档：只登记不解析
    other = tmp_path / "elsewhere" / "only"
    other.mkdir(parents=True)

    app = _boot(_cfg(tmp_path, [str(other)]))
    try:
        conn = app.state.conn
        sid = _add_local_source(conn, share)
        c = _admin(app)

        r = c.post(f"/api/v1/admin/sources/{sid}/scan")
        assert r.status_code == 200, r.text
        row = _wait_done(app, sid)

        assert row["scan_done_at"], "扫描完成时间没落库（按钮无法变成「已解析」）"
        assert conn.execute(
            "SELECT index_all FROM storage_sources WHERE id=?", (sid,)).fetchone()[0] == 1, \
            "点扫描后应把该源标记为「整共享解析」"

        paths = {r[0] for r in conn.execute("SELECT rel_path FROM catalog")}
        assert paths == {"a.txt", "sub\\b.md"}, f"整个共享里只有可解析文件进 catalog: {paths}"

        # 解析任务已入队（不是只建了文档记录）
        jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index'").fetchone()[0]
        assert jobs == 2, f"应入队 2 个解析任务，实际 {jobs}"
    finally:
        app.state.scanner.stop()


def test_scan_keeps_monitor_root_subdir_scope(tmp_path):
    """已受 [monitor].roots 覆盖的源：扫描仍只解析监控范围内的子目录。"""
    share = tmp_path / "share2"
    (share / "keep").mkdir(parents=True)
    (share / "other").mkdir()
    (share / "keep" / "k.txt").write_text("k", encoding="utf-8")
    (share / "other" / "o.txt").write_text("o", encoding="utf-8")

    app = _boot(_cfg(tmp_path, [str(share / "keep")]))
    try:
        conn = app.state.conn
        sid = _add_local_source(conn, share, name="已监控共享")
        c = _admin(app)
        assert c.post(f"/api/v1/admin/sources/{sid}/scan").status_code == 200
        _wait_done(app, sid)

        paths = {r[0] for r in conn.execute("SELECT rel_path FROM catalog")}
        assert paths == {"keep\\k.txt"}, f"不应越界解析监控范围外的目录: {paths}"
        assert conn.execute(
            "SELECT index_all FROM storage_sources WHERE id=?", (sid,)).fetchone()[0] == 0, \
            "已有监控范围时，不该把源改成整共享解析"
    finally:
        app.state.scanner.stop()


def test_scan_failure_keeps_done_time_empty(tmp_path):
    """扫描失败：状态是 error，按钮不应显示成「已解析」。"""
    app = _boot(_cfg(tmp_path, [str(tmp_path / "elsewhere")]))
    try:
        conn = app.state.conn
        sid = _add_local_source(conn, tmp_path / "not-exist")
        c = _admin(app)
        assert c.post(f"/api/v1/admin/sources/{sid}/scan").status_code == 200
        row = _wait_done(app, sid, want_error=True)
        assert row["scan_error"], "失败原因应落库"
        assert row["scan_done_at"] is None, "失败了不应记完成时间"
    finally:
        app.state.scanner.stop()


def test_sources_api_exposes_button_state(tmp_path):
    """列表接口要带 index_all / scan_done_at，前端才能画三态按钮。"""
    app = _boot(_cfg(tmp_path))
    try:
        conn = app.state.conn
        _add_local_source(conn, tmp_path / "s3")
        c = _admin(app)
        src = c.get("/api/v1/admin/sources").json()["sources"][0]
        assert "index_all" in src and "scan_done_at" in src, src
    finally:
        app.state.scanner.stop()


def test_frontend_scan_button_has_three_states():
    """按钮文字/颜色三态：扫描（白）→ 解析中（红）→ 已解析（绿）。"""
    js = Path("web/static/admin-sources.js").read_text(encoding="utf-8")
    for label in ("扫描", "解析中", "已解析"):
        assert label in js, f"按钮缺少「{label}」状态"
    assert "scan-running" in js and "scan-done" in js, "按钮缺少状态样式类"
    assert "scan_done_at" in js, "按钮状态应由扫描完成时间驱动"

    css = Path("web/static/v2.css").read_text(encoding="utf-8")
    assert ".scan-running" in css and "var(--err)" in css.split(".scan-running")[1][:160], \
        "「解析中」应为红色"
    assert ".scan-done" in css and "var(--ok)" in css.split(".scan-done")[1][:160], \
        "「已解析」应为绿色"
