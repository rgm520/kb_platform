"""`write_with_retry`：并发入库时的锁冲突必须能被吸收。

背景：入库流水线在事务里调 LLM 时会攥着写锁几十秒。Web 端的审计写入
和 last_used_at 更新如果直接抛 `database is locked`，就会把登录、查库
这些正常请求变成 500 —— 用户看到的就是"平台不稳定"。
"""
import sqlite3

from kbplatform.db import write_with_retry


class FlakyConn:
    """前 N 次 execute 抛 database is locked，之后成功。"""

    def __init__(self, failures: int = 0, error: str = "database is locked"):
        self.failures = failures
        self.error = error
        self.calls = 0
        self.rollbacks = 0
        self.commits = 0

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.error)
        return self

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_retries_until_success():
    conn = FlakyConn(failures=2)
    ok = write_with_retry(conn, "UPDATE t SET a=?", (1,),
                          attempts=5, base_delay=0.001)
    assert ok is True
    assert conn.calls == 3          # 扛了两次锁冲突后写成功
    assert conn.rollbacks == 2      # 每次失败都回滚旧快照
    assert conn.commits == 1


def test_returns_false_instead_of_raising_when_exhausted():
    conn = FlakyConn(failures=99)
    ok = write_with_retry(conn, "INSERT INTO audit_log VALUES (?)", ("x",),
                          attempts=3, base_delay=0.001)
    assert ok is False              # 旁路写入失败不能拖垮业务请求
    assert conn.calls == 3


def test_busy_message_is_also_retried():
    conn = FlakyConn(failures=1, error="database is busy")
    assert write_with_retry(conn, "UPDATE t SET a=?", (1,),
                            attempts=2, base_delay=0.001) is True
    assert conn.calls == 2


def test_other_errors_still_raise():
    conn = FlakyConn(failures=1, error="no such table: nope")
    try:
        write_with_retry(conn, "UPDATE nope SET a=?", (1,),
                         attempts=2, base_delay=0.001)
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非锁冲突的错误应原样抛出，不能被静默吞掉")
