"""备份与恢复的测试。

核心要求（迭代计划 0.6）：
  - 备份必须用 SQLite 备份 API（WAL 模式下直接复制文件很可能不可恢复）；
  - 备份文件能被 sqlite3 独立打开、数据完整（WAL 里已提交的数据也要在）；
  - 备份是"备份时刻"的快照，备份之后写库不能渗进备份文件；
  - prune_old 只保留最近 keep 份。
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from kbplatform.backup import backup_all, backup_db, prune_old
from kbplatform.db import checkpoint_wal, connect, init_db


def _seed(conn: sqlite3.Connection, path: str) -> int:
    """插入一行 catalog，返回行 id。name 取文件名。"""
    conn.execute(
        "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime)"
        " VALUES (?, 'share', 'a', ?, '.pdf', 1, 1.0)",
        (path, os.path.basename(path)))
    conn.commit()
    return conn.execute(
        "SELECT id FROM catalog WHERE path=?", (path,)).fetchone()[0]


def test_backup_db_writes_restorable_snapshot(tmp_path):
    dest = tmp_path / "backups"
    conn = init_db(str(tmp_path / "kb.db"))   # WAL 模式，部分已提交数据只在 WAL 里
    _seed(conn, str(tmp_path / "doc1.pdf"))

    out = backup_db(conn, str(dest), keep=3)

    assert len(out) == 1
    backup_path = out[0]
    assert os.path.exists(backup_path)
    # 备份文件能被 sqlite3 独立打开（不依赖源库 / 源库的 -wal 文件），且数据在
    copy = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in copy.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "catalog" in tables
        row = copy.execute("SELECT name FROM catalog WHERE name='doc1.pdf'").fetchone()
        assert row is not None, "备份里必须有已提交（哪怕还躺在 WAL）的数据"
    finally:
        copy.close()
    # 源库不被破坏
    assert conn.execute("SELECT count(*) FROM catalog").fetchone()[0] == 1


def test_backup_prunes_to_keep(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    conn = init_db(str(tmp_path / "kb.db"))
    # 造 9 份"往日"的备份文件，时间戳严格递增、全在过去
    now = datetime.now()
    for i in range(9, 0, -1):
        ts = (now - timedelta(minutes=10 * i)).strftime("%Y%m%d-%H%M")
        p = str(dest / f"kb-{ts}.db")
        sqlite3.connect(p).close()
    # 刚做的第 10 份是最新的
    out = backup_db(conn, str(dest), keep=7)

    files = sorted(f for f in os.listdir(dest) if f.startswith("kb-"))
    assert len(files) == 7, f"应只保留 7 份，实际 {len(files)}: {files}"
    assert len(out) == 7
    # 最新的必然在，最旧的 3 份被清走
    newest_ts = os.path.basename(out[-1])
    assert newest_ts in files
    oldest_ts = (now - timedelta(minutes=90)).strftime("%Y%m%d-%H%M")
    assert f"kb-{oldest_ts}.db" not in files


def test_backup_snapshot_contains_latest_writes(tmp_path):
    dest = tmp_path / "backups"
    conn = init_db(str(tmp_path / "kb.db"))
    _seed(conn, str(tmp_path / "old.pdf"))
    out = backup_db(conn, str(dest), keep=3)
    backup_path = out[0]

    # 备份之后写库：新数据不该出现在备份里
    _seed(conn, str(tmp_path / "new.pdf"))
    copy = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        old = copy.execute(
            "SELECT name FROM catalog WHERE name='old.pdf'").fetchone()
        new = copy.execute(
            "SELECT name FROM catalog WHERE name='new.pdf'").fetchone()
        assert old is not None
        assert new is None, "备份必须是备份时刻的快照，备份后的写入不许泄漏进去"
    finally:
        copy.close()


def test_connect_sets_wal_journal_size_limit(tmp_path):
    """journal_size_limit 上限必须在 connect() 里、且在 journal_mode=WAL 之后设置。

    背景：kb.db-wal 曾涨到 2.49 GB（比主库大）→ journal_size_limit 默认 0
    （不设上限），checkpoint 挨饿时 WAL 只会无限堆积。
    """
    conn = connect(str(tmp_path / "kb.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit == 256 * 1024 * 1024
    finally:
        conn.close()


def test_checkpoint_wal_truncates_oversized_wal(tmp_path):
    """WAL 超过阈值时 checkpoint_wal 必须把它截断到 0。

    背景：2026-11-17 WAL 涨到 125GB 写满磁盘，web 启动即 database is locked。
    自动 checkpoint 是 PASSIVE 的，被旧读事务钉住时只能无限追加，所以流水线
    每轮要有一层主动兜底。
    """
    db = str(tmp_path / "kb.db")
    conn = connect(db)
    try:
        conn.execute("CREATE TABLE t(x BLOB)")
        for _ in range(50):
            conn.execute("INSERT INTO t VALUES (zeroblob(65536))")
            conn.commit()
        wal = db + "-wal"
        assert os.path.getsize(wal) > 0
        assert checkpoint_wal(conn, db, limit_bytes=1) is True
        assert os.path.getsize(wal) == 0, "checkpoint 成功后 WAL 必须被截断"
    finally:
        conn.close()


def test_checkpoint_wal_leaves_small_wal_alone(tmp_path):
    """没到阈值时不要无谓地抢 checkpoint 锁（避免干扰正常读写）。"""
    db = str(tmp_path / "kb.db")
    conn = connect(db)
    try:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        wal = db + "-wal"
        before = os.path.getsize(wal)
        assert before > 0
        assert checkpoint_wal(conn, db, limit_bytes=10 * 1024 ** 2) is True
        assert os.path.getsize(wal) == before
    finally:
        conn.close()


def test_prune_old_keeps_newest(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    now = datetime.now()
    for i in range(5, 0, -1):
        ts = (now - timedelta(minutes=10 * i)).strftime("%Y%m%d-%H%M")
        prune_able = (dest / f"kb-{ts}.db")
        prune_able.write_bytes(b"")
    prune_old(str(dest), keep=2)
    left = sorted(os.listdir(dest))
    assert len(left) == 2
    # 留下的必须是最新的两份
    t2 = (now - timedelta(minutes=10)).strftime("%Y%m%d-%H%M")
    t1 = (now - timedelta(minutes=20)).strftime("%Y%m%d-%H%M")
    assert left == [f"kb-{t1}.db", f"kb-{t2}.db"]


# ---------------------------------------------------------------- 修复一轮

class _Cfg:
    """backup_all 需要的最小 Config（不拉真实 config.toml，全部落在 tmp_path）。"""

    def __init__(self, tmp):
        self.data_dir = str(tmp / "data")
        self.db_path = str(tmp / "data" / "kb.db")
        self.fts_path = str(tmp / "data" / "fts.db")
        self.vector_dir = str(tmp / "data" / "vectors")


def _seed_chunk(db_path: str, text: str) -> None:
    """另开一个连接插入 chunk（模拟流水线在备份窗口里写入）。"""
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO chunks(file_path, chunk_idx, text) "
                  "VALUES ('doc.pdf', 99, ?)", (text,))
        c.commit()
    finally:
        c.close()


def _prepare_env(tmp_path):
    """init_db 一份带 chunks 的库 + 一个向量目录，返回 (cfg, dest)。"""
    cfg = _Cfg(tmp_path)
    conn = init_db(cfg.db_path)
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) "
                 "VALUES ('old.pdf', 0, 'x')")
    conn.commit()
    conn.close()
    # 空的 fts.db（真正的 fts 库也是 SQLite，快照 API 处理方式一致）
    sqlite3.connect(cfg.fts_path).close()
    os.makedirs(cfg.vector_dir, exist_ok=True)
    with open(os.path.join(cfg.vector_dir, "vectors.f16"), "wb") as f:
        f.write(b"\x00" * 8)
    with open(os.path.join(cfg.vector_dir, "vectors.ids.json"), "wb") as f:
        f.write(b"[1]")
    dest = tmp_path / "backups"
    dest.mkdir()
    return cfg, str(dest)


def test_backup_one_failure_leaves_no_half_file(tmp_path):
    """快照中途失败 → 目标目录里不能留下半截文件（会被当有效备份）。"""
    from kbplatform.backup import _backup_one
    bad = sqlite3.connect(str(tmp_path / "src.db"))
    bad.close()   # 关闭后的连接一调 backup 就抛 ProgrammingError（确定性失败）
    dest = tmp_path / "backups"
    dest.mkdir()
    with pytest.raises(sqlite3.ProgrammingError):
        _backup_one(bad, str(dest / "kb-fail.db"))
    assert os.listdir(dest) == [], "失败的目标文件必须被清掉"


def test_backup_all_retries_until_self_consistent(tmp_path, monkeypatch):
    """备份窗口里流水线写入新 chunk → 第 1 轮作废、第 2 轮成功。

    打桩 _backup_one 在第一轮 kb 快照**之前**往源库塞一行 chunk，
    模拟"kb.db 快照和向量复制之间库还在变"——第 1 轮指纹必变、整组
    作废重来；第 2 轮不再塞行，指纹一致 → 成功且产物齐全。
    """
    import kbplatform.backup as bk
    cfg, dest = _prepare_env(tmp_path)

    orig = bk._backup_one
    state = {"kb_calls": 0}

    def fake_backup_one(src, dest_path):
        base = os.path.basename(dest_path)
        if base.startswith("kb-"):
            state["kb_calls"] += 1
            if state["kb_calls"] == 1:
                _seed_chunk(cfg.db_path, "written-in-window")
        return orig(src, dest_path)

    monkeypatch.setattr(bk, "_backup_one", fake_backup_one)

    out = bk.backup_all(cfg, dest, keep=3)

    names = {os.path.basename(p) for p in out}
    assert any(n.startswith("kb-") for n in names)
    assert any(n.startswith("fts-") for n in names)
    assert any(n.startswith("vectors-") and n.endswith(".f16") for n in names)
    assert any(n.startswith("vectors-") and n.endswith(".ids.json") for n in names)
    assert state["kb_calls"] == 2, "第 1 轮作废后应重照一轮"
    on_disk = [f for f in os.listdir(dest) if f.startswith("kb-")]
    assert len(on_disk) == 1, "作废的那轮不能留文件"
    # 最终快照里必须含窗口期间写入的 chunk（自洽：与向量同刻）
    kb_file = [f for f in os.listdir(dest) if f.startswith("kb-")][0]
    snap = sqlite3.connect(os.path.join(dest, kb_file))
    try:
        assert snap.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    finally:
        snap.close()


def test_backup_all_fails_loudly_when_source_keeps_changing(tmp_path, monkeypatch):
    """源库每轮都在变 → 明确失败，目录里不留半截恢复集。

    宁可今晚没有备份，也不要一个"看着完整、实际错位"的恢复集。
    """
    import kbplatform.backup as bk
    cfg, dest = _prepare_env(tmp_path)

    orig = bk._backup_one
    state = {"kb_calls": 0}

    def fake_backup_one(src, dest_path):
        base = os.path.basename(dest_path)
        if base.startswith("kb-"):
            state["kb_calls"] += 1
            _seed_chunk(cfg.db_path, f"still-growing-{state['kb_calls']}")
        return orig(src, dest_path)

    monkeypatch.setattr(bk, "_backup_one", fake_backup_one)

    with pytest.raises(RuntimeError, match="chunks"):
        bk.backup_all(cfg, dest, keep=3)

    assert not any(f.startswith("kb-") or f.startswith("fts-")
                   or f.startswith("vectors-")
                   for f in os.listdir(dest)), "失败的恢复集必须整组作废"
    assert state["kb_calls"] == bk._MAX_TRIES
