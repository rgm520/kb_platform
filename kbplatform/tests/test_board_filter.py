"""看板统计卡片与文件清单联动的回归测试。

需求（2026-10-24）：点上面的统计卡片，下面的文件清单切到对应主题
（全部 / Tier1 已解析 / Tier2 已进图谱 / 待处理 / 失败 / 已跳过）。
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


def _seed(conn):
    rows = [
        ("done", 1, 1), ("done", 1, 0), ("pending", 0, 0),
        ("failed", 0, 0), ("skipped", 0, 0),
    ]
    for i, (status, t1, t2) in enumerate(rows):
        path = f"\\\\nas\\share\\d{i}.md"
        conn.execute(
            "INSERT INTO catalog(path,share,rel_path,name,ext,status,tier1_done,tier2_done) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (path, "share", f"d{i}.md", f"d{i}.md", ".md", status, t1, t2))
    conn.commit()


def test_documents_filter_by_status_and_tier2(app):
    c = _login(app)
    _seed(app.state.conn)
    all_docs = c.get("/api/v1/documents?page=1&page_size=50").json()
    assert all_docs["total"] == 5

    done = c.get("/api/v1/documents?page=1&page_size=50&status=done").json()
    assert done["total"] == 2

    pending = c.get("/api/v1/documents?page=1&page_size=50&status=pending").json()
    assert pending["total"] == 1

    failed = c.get("/api/v1/documents?page=1&page_size=50&status=failed").json()
    assert failed["total"] == 1

    skipped = c.get("/api/v1/documents?page=1&page_size=50&status=skipped").json()
    assert skipped["total"] == 1

    # Tier 2：已进入知识图谱
    tier2 = c.get("/api/v1/documents?page=1&page_size=50&tier2=1").json()
    assert tier2["total"] == 1
    assert tier2["items"][0]["name"] == "d0.md"

    # Tier2 + status 组合
    both = c.get("/api/v1/documents?page=1&page_size=50&status=done&tier2=1").json()
    assert both["total"] == 1


def test_stats_counts_match_filters(app):
    c = _login(app)
    _seed(app.state.conn)
    s = c.get("/api/v1/documents/stats").json()
    assert s["total"] == 5
    assert s["tier1_done"] == 2
    assert s["tier2_done"] == 1
    assert s["by_status"]["done"] == 2
    assert s["by_status"]["pending"] == 1
    assert s["by_status"]["failed"] == 1
    assert s["by_status"]["skipped"] == 1
    # 卡片数字 = 对应过滤后的条数
    assert c.get("/api/v1/documents?page=1&page_size=50&status=done").json()["total"] == s["tier1_done"]
    assert c.get("/api/v1/documents?page=1&page_size=50&tier2=1").json()["total"] == s["tier2_done"]
