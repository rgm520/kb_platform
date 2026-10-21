import sqlite3

from kbplatform.db import connect, init_db, migrate_paths


def test_init_db_creates_required_tables(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("catalog", "catalog_stats", "users", "user_scopes", "dir_scopes",
              "api_tokens", "agents", "models", "wiki_pages", "jobs",
              "audit_log", "model_usage"):
        assert t in names, f"缺少表 {t}"


def test_catalog_has_two_tier_progress_columns(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog)")}
    assert {"tier1_done", "tier2_done", "status", "status_detail"} <= cols


def test_migrate_paths_rewrites_drive_letter_to_unc(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime)"
                 " VALUES ('W:\\AI知识库\\a.pdf','信息化管理','AI知识库\\a.pdf','a.pdf','.pdf',1,1.0)")
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES ('W:\\AI知识库\\a.pdf',0,'x')")
    conn.commit()
    n = migrate_paths(conn, {"W:": "\\\\192.0.2.19\\信息化管理"})
    assert n >= 2
    got = conn.execute("SELECT path FROM catalog").fetchone()[0]
    assert got == "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"
    got2 = conn.execute("SELECT file_path FROM chunks").fetchone()[0]
    assert got2 == "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"


def test_migrate_paths_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime)"
                 " VALUES ('W:\\AI知识库\\a.pdf','信息化管理','a.pdf','a.pdf','.pdf',1,1.0)")
    conn.commit()
    dm = {"W:": "\\\\192.0.2.19\\信息化管理"}
    migrate_paths(conn, dm)
    migrate_paths(conn, dm)
    got = conn.execute("SELECT path FROM catalog").fetchone()[0]
    assert got == "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"
