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

from kbplatform.backup import backup_db, prune_old
from kbplatform.db import connect, init_db


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
