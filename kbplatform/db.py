"""平台库：schema、连接、路径迁移、catalog 读写。

现有 kb.db 里的表（files / chunks / vectors / knowledge / nodes / edges）保持兼容，
本模块只追加平台需要的表与函数。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from kbplatform.paths import to_unc

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 非文档文件统计（不建 catalog 行）
CREATE TABLE IF NOT EXISTS catalog_stats (
  share TEXT NOT NULL, ext TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(share, ext)
);

-- 目录索引
CREATE TABLE IF NOT EXISTS catalog (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  path          TEXT UNIQUE NOT NULL,
  share         TEXT NOT NULL,
  rel_path      TEXT NOT NULL,
  name          TEXT NOT NULL,
  ext           TEXT NOT NULL,
  size          INTEGER,
  mtime         REAL,
  parseable     INTEGER DEFAULT 1,
  status        TEXT DEFAULT 'pending',
  status_detail TEXT,
  chunk_count   INTEGER DEFAULT 0,
  tier1_done    INTEGER DEFAULT 0,
  tier2_done    INTEGER DEFAULT 0,
  kp_count      INTEGER DEFAULT 0,
  edge_count    INTEGER DEFAULT 0,
  needs_ocr     INTEGER DEFAULT 0,
  scanned       INTEGER DEFAULT 0,
  updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_catalog_status ON catalog(status);
CREATE INDEX IF NOT EXISTS idx_catalog_share  ON catalog(share);
CREATE INDEX IF NOT EXISTS idx_catalog_t1     ON catalog(tier1_done);
CREATE INDEX IF NOT EXISTS idx_catalog_t2     ON catalog(tier2_done);

-- 平台用户
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  display_name  TEXT,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  dept          TEXT,
  active        INTEGER DEFAULT 1,
  created_at    REAL
);

-- 用户可见范围（管理员勾选）
CREATE TABLE IF NOT EXISTS user_scopes (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  share   TEXT NOT NULL,
  subpath TEXT NOT NULL DEFAULT '',
  UNIQUE(user_id, share, subpath)
);

-- 目录级「涉密」标记：目录的属性，与用户授权正交
CREATE TABLE IF NOT EXISTS dir_scopes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  share      TEXT NOT NULL,
  subpath    TEXT NOT NULL DEFAULT '',
  restricted INTEGER NOT NULL DEFAULT 0,
  label      TEXT,
  UNIQUE(share, subpath)
);

-- ---------------------------------------------------------------- 2.0 文件底座
-- 存储源：一个存储源 = 一个共享根；SMB 共享各自的账号密码
CREATE TABLE IF NOT EXISTS storage_sources (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL,                -- local | smb
  root          TEXT NOT NULL,
  username      TEXT,
  secret_enc    TEXT,
  domain        TEXT,
  writable      INTEGER DEFAULT 0,
  personal_root TEXT DEFAULT '',
  enabled       INTEGER DEFAULT 1,
  scan_state    TEXT DEFAULT 'idle',          -- idle | scanning | paused | error
  scan_error    TEXT,
  last_scan_at  REAL,
  created_at    REAL
);

-- 目录授权：subpath 为空表示整个共享根
CREATE TABLE IF NOT EXISTS grants (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  source_id  INTEGER NOT NULL,
  subpath    TEXT NOT NULL DEFAULT '',
  can_write  INTEGER DEFAULT 0,
  created_at REAL,
  UNIQUE(user_id, source_id, subpath)
);
CREATE INDEX IF NOT EXISTS idx_grants_user ON grants(user_id);

-- 全量路径索引：目录与文件都有一行
CREATE TABLE IF NOT EXISTS fs_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id  INTEGER NOT NULL,
  rel_path   TEXT NOT NULL,
  rel_key    TEXT NOT NULL,                   -- lower(rel_path)，Windows 语义去重
  parent_rel TEXT NOT NULL DEFAULT '',
  name       TEXT NOT NULL,
  is_dir     INTEGER NOT NULL DEFAULT 0,
  size       INTEGER,
  mtime      REAL,
  ext        TEXT,
  parseable  INTEGER DEFAULT 0,
  catalog_id INTEGER,
  updated_at REAL,
  UNIQUE(source_id, rel_key)
);
CREATE INDEX IF NOT EXISTS idx_fs_source_parent ON fs_entries(source_id, parent_rel);
CREATE INDEX IF NOT EXISTS idx_fs_name ON fs_entries(name);

-- 平台回收站：只记录经网页删除的条目
CREATE TABLE IF NOT EXISTS trash_entries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id    INTEGER NOT NULL,
  original_rel TEXT NOT NULL,
  trash_rel    TEXT NOT NULL,
  name         TEXT NOT NULL,
  is_dir       INTEGER DEFAULT 0,
  size         INTEGER,
  deleted_by   INTEGER,
  deleted_at   REAL,
  expires_at   REAL
);

-- 分片上传会话
CREATE TABLE IF NOT EXISTS upload_sessions (
  id         TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  source_id  INTEGER NOT NULL,
  rel        TEXT NOT NULL DEFAULT '',
  name       TEXT NOT NULL,
  size       INTEGER NOT NULL,
  temp_path  TEXT NOT NULL,
  received   INTEGER DEFAULT 0,
  overwrite  INTEGER DEFAULT 0,
  created_at REAL
);

-- 文件名搜索（trigram 支持中文子串；2 字查询走 LIKE 兜底）
CREATE VIRTUAL TABLE IF NOT EXISTS fs_fts USING fts5(
  name, rel_path, tokenize='trigram'
);

-- API token（每用户一枚，只存哈希）
CREATE TABLE IF NOT EXISTS api_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  token_hash   TEXT UNIQUE NOT NULL,
  prefix       TEXT NOT NULL,
  label        TEXT,
  revoked      INTEGER DEFAULT 0,
  created_at   REAL,
  last_used_at REAL,
  expires_at   REAL,
  -- AES-GCM 密文（可恢复明文），密钥由 session_secret 派生。
  -- 见 kbplatform/auth.py 模块 docstring。
  token_enc    TEXT
);

-- 注册到用户名下的外部智能体
CREATE TABLE IF NOT EXISTS agents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT,
  client_id    TEXT,
  meta         TEXT,
  created_at   REAL,
  last_seen_at REAL
);

-- 模型配置（预置 + 自定义）
CREATE TABLE IF NOT EXISTS models (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  provider   TEXT NOT NULL,
  base_url   TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  api_key    TEXT,
  is_local   INTEGER DEFAULT 0,
  is_default INTEGER DEFAULT 0,
  in_parse_pool INTEGER DEFAULT 0,        -- 是否参与文档解析（知识提炼）算力池
  pool_weight REAL DEFAULT 1,             -- 调度权重
  extra      TEXT,
  created_at REAL
);

-- 人工校正层
CREATE TABLE IF NOT EXISTS wiki_pages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  slug       TEXT UNIQUE NOT NULL,
  title      TEXT,
  body       TEXT,
  node_id    INTEGER,
  path       TEXT,
  edited_by  INTEGER,
  updated_at REAL
);

-- 任务队列
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  catalog_id INTEGER,
  state      TEXT NOT NULL DEFAULT 'queued',
  attempts   INTEGER DEFAULT 0,
  error      TEXT,
  created_at REAL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

-- 审计
CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER,
  token_prefix TEXT,
  agent_id     INTEGER,
  action       TEXT NOT NULL,
  question     TEXT,
  hit_paths    TEXT,
  model_used   TEXT,
  restricted   INTEGER DEFAULT 0,
  ip           TEXT,
  created_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);

-- 飞书用户：open_id ↔ 平台账号
CREATE TABLE IF NOT EXISTS feishu_users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_open_id  TEXT UNIQUE NOT NULL,
  feishu_union_id TEXT,
  feishu_name     TEXT,
  user_id         INTEGER,
  created_at      REAL,
  last_seen_at    REAL
);

-- 飞书会话：每个 (飞书用户, 群/会话) 一个 opencode session
CREATE TABLE IF NOT EXISTS feishu_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_user_id INTEGER NOT NULL,
  chat_id        TEXT NOT NULL,
  chat_type      TEXT,
  session_id     TEXT,
  model_id       TEXT,
  created_at     REAL,
  updated_at     REAL,
  UNIQUE(feishu_user_id, chat_id)
);

-- 模型用量
CREATE TABLE IF NOT EXISTS model_usage (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER,
  model_id       INTEGER,
  day            TEXT,
  calls          INTEGER DEFAULT 0,
  prompt_tokens  INTEGER DEFAULT 0,
  output_tokens  INTEGER DEFAULT 0,
  UNIQUE(user_id, model_id, day)
);

-- 答案反馈：「这个答案没用」的显式信号。只存打分与备注，不含原文。
CREATE TABLE IF NOT EXISTS query_feedback (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  user_id    INTEGER,
  rating     TEXT NOT NULL,
  comment    TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_request ON query_feedback(request_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON query_feedback(created_at);

-- 检索轨迹：一次问答到底召回了什么、被哪路压下去了。
-- 只存 chunk id / 名次 / 分数 / 命中通道；问题只存 sha256 前 16 位，
-- 绝不存原文片段、文件路径或问题原文（审计红线）。
CREATE TABLE IF NOT EXISTS retrieval_traces (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id    TEXT NOT NULL,
  user_id       INTEGER,
  question_hash TEXT NOT NULL,
  route_stats   TEXT NOT NULL,
  candidates    TEXT NOT NULL,
  top_ids       TEXT NOT NULL,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_request ON retrieval_traces(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_created ON retrieval_traces(created_at);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    # check_same_thread=False：FastAPI 的同步端点跑在 anyio 线程池里，
    # 连接在主线程创建会导致 "SQLite objects created in a thread can only
    # be used in that same thread"。
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    # WAL：读写不互相阻塞。平台是"读多写少"，而摄入流水线一直在写，
    # 默认的 rollback journal 会让 API 请求排队等写锁。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    # WAL 文件上限（256 MB）。必须在 journal_mode=WAL 之后设置才生效。
    # 为什么需要：journal_size_limit 默认 0（不限制），而平台是常驻进程，
    # uvicorn 每个线程一个长连接 —— 一旦 checkpoint 挨饿（被动 checkpoint
    # 无法把 WAL 归零，只能继续往后追加），WAL 会无限堆大。实测 kb.db 2.25GB
    # 时 kb.db-wal 已到 2.49GB，比主库还大。设上限后：checkpoint 完成/库关闭
    # 时 WAL 文件会被截到上限以内，不再无限涨。
    # 风险说明：上限只是"文件大小收缩"，不改变任何锁行为；被截掉的帧要是
    # 还没 checkpoint 进主库，TRUNCATE 恢复时会先按 WAL 恢复 —— 不会丢数据。
    try:
        conn.execute("PRAGMA journal_size_limit=268435456")
    except sqlite3.OperationalError:
        pass
    return conn


def write_with_retry(conn, sql: str, params=(), attempts: int = 6,
                     base_delay: float = 0.1) -> bool:
    """执行一条写入；遇到锁冲突就回滚重试，返回是否成功。

    为什么需要它：
        WAL 让读写可以并行，但**写锁全局只有一个**。入库流水线如果在
        事务里调 LLM，写锁会被攥住几十秒，Web 端同一时刻的审计/登录
        写入等不到就会抛 `database is locked`，把正常请求变成 500。
        这类写入（审计、last_used_at）都是旁路，重试几次仍失败也不该
        拖垮业务请求，所以调用方拿到 False 只记日志、不抛异常。
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            conn.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            try:
                conn.rollback()
            except Exception:
                pass
            time.sleep(base_delay * (i + 1))
    if last is not None:
        print(f"[db] 写入重试 {attempts} 次仍失败：{last}", flush=True)
    return False


class ThreadLocalConn:
    """按线程复用的 sqlite 连接代理（接口与 sqlite3.Connection 兼容）。

    为什么不能让所有请求线程共用一个连接：
        一个 sqlite3 连接内部只有**一份**语句状态。线程 A 正在
        `cursor.fetchone()`，线程 B 此时调 `conn.commit()` 或执行下一条 SQL，
        A 的游标会被重置，fetchone() 返回 None —— 对外表现为随机的
        `TypeError: 'NoneType' object is not subscriptable`（踩过两次）。

        CPython 的 sqlite3 在 Windows 上虽然是 SERIALIZED 模式（库级互斥），
        但那只保护"调用 sqlite 库"这一步，不保护**每条语句的游标状态**——
        所以共享连接并不安全。

    用法：调用方照旧 `conn.execute(...)` / `conn.commit()`，无感知。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self._db_path)
            self._local.conn = c
        return c

    def execute(self, *args, **kwargs):
        return self._conn().execute(*args, **kwargs)

    def commit(self):
        self._conn().commit()

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def __getattr__(self, name):
        # 其余属性（row_factory / in_transaction / ...）转发给当前线程的连接
        return getattr(self._conn(), name)


def _repair_chunk_fk(conn: sqlite3.Connection) -> bool:
    """修复既有库里 chunks 表的坏外键。

    旧 schema 写的是 ``FOREIGN KEY(file_id) REFERENCES files(id)``，但 files 表
    没有 id 列（主键是 path），且 file_id 从未被写入。一旦打开
    ``PRAGMA foreign_keys=ON``，任何 chunks 插入都会直接报
    "foreign key mismatch"。这里重建该表去掉约束，保留全部数据。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks'").fetchone()
    if not row or "FOREIGN KEY" not in (row[0] or ""):
        return False
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE chunks RENAME TO chunks_old")
    conn.execute("""CREATE TABLE chunks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path  TEXT NOT NULL,
        file_id    INTEGER,
        chunk_idx  INTEGER,
        text       TEXT,
        locator    TEXT,
        tokens     INTEGER
    )""")
    conn.execute(
        """INSERT INTO chunks(id, file_path, file_id, chunk_idx, text, locator, tokens)
           SELECT id, file_path, file_id, chunk_idx, text, locator, tokens FROM chunks_old""")
    conn.execute("DROP TABLE chunks_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_fid ON chunks(file_id)")
    conn.commit()
    return True


# ---------------------------------------------------------------- 观测数据保留期

TRACE_RETENTION_DAYS = 30     # spec §3 A1：轨迹与反馈保留 30 天


def purge_expired_trace_data(conn: sqlite3.Connection,
                             max_age_days: int = TRACE_RETENTION_DAYS) -> int:
    """清掉超过保留期的检索轨迹与答案反馈，返回删除行数。

    为什么必须做：spec §3 A1 明确「保留 30 天」，这两张表是旁路观测数据，
    不清理就无限增长。清错也不伤业务正确性，所以失败只记日志、下轮再清；
    DELETE 走 created_at 索引（两张表都建了），到期行很少时开销可忽略。
    谁来调：pipeline.py 每轮 + service.py 写轨迹/反馈时节流顺带——双保险，
    两个进程哪个活着都能保住保留期承诺；操作幂等，并发或重复执行无害。
    """
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for table in ("retrieval_traces", "query_feedback"):
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            deleted += cur.rowcount or 0
            conn.commit()
        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[db] 清理 {table} 过期数据失败（下轮重试）: {e}", flush=True)
    return deleted


def init_db(db_path: str) -> sqlite3.Connection:
    """初始化完整数据库：平台表 + 引擎表（files/chunks/vectors/knowledge/nodes/edges）。

    平台库与引擎库是**同一个文件**（kb/data/kb.db），两套 schema 必须一起建，
    否则 chunks / knowledge / edges 等表缺失，路径迁移与 catalog JOIN 都会失败。
    """
    conn = connect(db_path)
    _repair_chunk_fk(conn)
    conn.executescript(SCHEMA_SQL)
    try:
        from kb.store import SCHEMA as ENGINE_SCHEMA
        conn.executescript(ENGINE_SCHEMA)
    except ImportError:
        pass
    # 老库补列
    for ddl in ("ALTER TABLE models ADD COLUMN in_parse_pool INTEGER DEFAULT 0",
                "ALTER TABLE models ADD COLUMN pool_weight REAL DEFAULT 1",
                # 密钥的可恢复密文：让管理员能重新查看完整密钥，
                # 避免"密钥只能显示一次 -> 丢失就必须换密钥 -> 正在用旧密钥的
                # 智能体全部 401" 这个死循环。
                "ALTER TABLE api_tokens ADD COLUMN token_enc TEXT",
                "ALTER TABLE upload_sessions ADD COLUMN overwrite INTEGER DEFAULT 0",
                 # 管理员在看板手动触发重跑时置 1：这次允许对扫描件做 OCR
                 # （默认"存量扫描件不自动 OCR"，见 converters.is_ocr_backlog）
                 "ALTER TABLE catalog ADD COLUMN ocr_requested INTEGER DEFAULT 0",
                 # 重复文件识别用的内容哈希（不按文件名判断）
                 "ALTER TABLE catalog ADD COLUMN md5 TEXT",
                 "ALTER TABLE catalog ADD COLUMN md5_at REAL"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def migrate_paths(conn: sqlite3.Connection, drive_map: dict[str, str]) -> int:
    """把盘符形式路径改写为 UNC。返回改写行数。可重复执行。"""
    changed = 0
    for table, col in (("catalog", "path"), ("chunks", "file_path"),
                        ("knowledge", "file_path"), ("edges", "file_path"),
                        ("files", "path"), ("wiki_pages", "path")):
        try:
            rows = conn.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            p = row[1]
            if not p or p.startswith("\\\\"):
                continue
            new = to_unc(p, drive_map)
            if new != p:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, row[0]))
                changed += 1
    conn.commit()
    return changed


# ---------------------------------------------------------------- catalog

def upsert_catalog(conn: sqlite3.Connection, fr) -> int:
    conn.execute(
        """INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime,
                              parseable, status, updated_at)
           VALUES (?,?,?,?,?,?,?,1,'pending',?)
           ON CONFLICT(path) DO UPDATE SET
             share=excluded.share, rel_path=excluded.rel_path, name=excluded.name,
             ext=excluded.ext, size=excluded.size, mtime=excluded.mtime,
             updated_at=excluded.updated_at""",
        (fr.path, fr.share, fr.rel_path, fr.name, fr.ext, fr.size, fr.mtime, time.time()))
    conn.commit()
    return conn.execute("SELECT id FROM catalog WHERE path=?", (fr.path,)).fetchone()[0]


def set_status(conn: sqlite3.Connection, catalog_id: int, status: str,
               detail: str | None = None) -> None:
    conn.execute("UPDATE catalog SET status=?, status_detail=? WHERE id=?",
                 (status, detail, catalog_id))
    conn.commit()


def get_by_path(conn: sqlite3.Connection, unc: str):
    return conn.execute("SELECT * FROM catalog WHERE path=?", (unc,)).fetchone()


def list_catalog(conn: sqlite3.Connection, status: str | None = None,
                 limit: int = 100, offset: int = 0):
    if status:
        return conn.execute(
            "SELECT * FROM catalog WHERE status=? ORDER BY id LIMIT ? OFFSET ?",
            (status, limit, offset)).fetchall()
    return conn.execute(
        "SELECT * FROM catalog ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()


def catalog_stats(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT share, ext, count FROM catalog_stats ORDER BY count DESC").fetchall()


def bump_stats(conn: sqlite3.Connection, share: str, counts: dict[str, int]) -> None:
    for ext, n in counts.items():
        conn.execute(
            """INSERT INTO catalog_stats(share, ext, count) VALUES(?,?,?)
               ON CONFLICT(share, ext) DO UPDATE SET count=count+excluded.count""",
            (share, ext, n))
    conn.commit()
