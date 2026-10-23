# 企业知识平台 2.0 · P0 文件底座 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有平台上升级出「登录 → 功能桌面 → 文件管理器 → 预览 → 文件名秒搜 → 共享接入 → 上传即解析 / 删除即清理」这条完整链路，让平台先能像企业 NAS 一样日常使用。

**Architecture:** 保留现有 FastAPI + SQLite + 原生 JS 单页应用。新增 `storage_sources / grants / fs_entries / trash_entries` 四张表；用 `StorageAdapter` 抽象本地磁盘与 SMB 共享（每个共享独立凭据，`smbprotocol` 直连，不依赖 Windows 凭据管理器）；路径索引与文件名搜索放在 SQLite FTS5（trigram）；内容解析继续走现有 `catalog + jobs + pipeline`。前端新增桌面页、文件页、回收站页与存储接入管理页，仍无构建步骤。

**Tech Stack:** Python 3.12、FastAPI、SQLite 3.49（FTS5 trigram）、`smbprotocol` 1.17（内含 `smbclient`）、`python-multipart`、LibreOffice（预览转换）、原生 HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-10-23-kb-platform-v2-design.md`

## Global Constraints

- 平台在 Windows 上运行，解释器为 `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`；测试命令统一用该解释器。
- 前端**不引入构建链**：不新增 npm/打包器，脚本用普通 `<script>` 加载。
- 所有文件与 AI 接口必须在**服务端**按 `grants` 鉴权；越权一律 404，不泄露「存在但无权」。
- 一个存储源 = 一个共享根；**每个共享单独填写账号密码**，密码 AES-GCM 加密入库，接口永不回显明文。
- 取消「涉密目录」：可见即可预览/下载；旧 `user_scopes` 与 `dir_scopes` 保留但新文件接口只用 `grants`。
- 网页上传完成 → 立即入解析队列；删除进回收站的同时清理路径/全文/向量/图谱产物；恢复后重新解析。
- 直接在 NAS 上删除的文件**不进平台回收站**，只清理索引；平台回收站只负责网页删除。
- 路径规范化不做 Unicode 归一；拒绝 `..`、盘符、绝对路径等越界输入。
- 回收站与索引目录 `_kb_trash`、`_personal` 不参与扫描；`~$` 锁文件继续跳过。
- 提交信息用中文，经 UTF-8 无 BOM 文件传入（PowerShell 用 `.NET UTF8Encoding($false)`）。
- 回归命令：`& $py -m pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py -q`。

## Review Focus

1. **越界路径**：`rel` 含 `..`、绝对路径或盘符时，必须 400/404，绝不能读到共享根之外。
2. **授权根被删除/改名**：用户不能删除或重命名自己的授权根本身，否则 grants 悬空；只能操作根内部。
3. **同服务器多账号**：同一 NAS 上 A 共享用账号甲、B 共享用账号乙，扫描/预览/下载不能串用会话。
4. **覆盖上传与在途解析**：覆盖后旧 chunks / FTS / vectors / 图谱必须清干净，并保证再解析一次。
5. **搜索越权**：结果先按授权过滤再截断，不能因为先取 top-k 再过滤而漏掉合法结果或泄露无权文件名。

---

## File Structure

**新增（后端）**

| 文件 | 职责 |
|---|---|
| `kbplatform/storage.py` | `Source`/`Entry` 数据类；`LocalAdapter`、`SmbAdapter`；路径安全；测试连接 |
| `kbplatform/fsindex.py` | `fs_entries` 与 `fs_fts` 的写入/删除/扫描/搜索 |
| `kbplatform/grants.py` | 授权读取、读写判断、目录过滤、个人空间、旧 scope 迁移 |
| `kbplatform/scanner.py` | 后台扫描线程与单源同步扫描；把可解析文件送入 `catalog + jobs` |
| `kbplatform/fs_service.py` | `/api/v1/fs/*` 与 `/api/v1/admin/sources/*` 路由 |
| `kbplatform/tests/test_storage.py` | 适配器与路径安全 |
| `kbplatform/tests/test_fsindex.py` | 路径索引、增量扫描、文件名搜索 |
| `kbplatform/tests/test_grants.py` | 授权、导航占位、个人空间 |
| `kbplatform/tests/test_scanner.py` | 扫描入队、删除清理、覆盖重解析 |
| `kbplatform/tests/test_fs_api.py` | 浏览/搜索/预览/上传/回收站 API |
| `kbplatform/tests/test_sources_api.py` | 存储源与授权管理 API |
| `web/static/files.js` | 文件管理器、搜索、预览、右键菜单、上传/删除 |
| `web/static/desktop.js` | 功能桌面渲染与导航 |
| `web/static/admin-sources.js` | 存储接入与目录授权管理 UI |
| `web/static/v2.css` | 登录页、桌面、文件页、预览层样式 |
| `requirements.txt` | 直接依赖清单（含本次新增 `smbprotocol`、`python-multipart`） |

**修改**

| 文件 | 改动 |
|---|---|
| `kbplatform/db.py` | 新表、索引、`fs_fts` 虚拟表；`init_db` 幂等建表 |
| `kbplatform/config.py` | `trash_days`、`upload_max_mb`、`preview_dir`、`source_scan_interval_sec` |
| `kbplatform/auth.py` | 暴露 `encrypt_secret` / `decrypt_secret` |
| `kbplatform/indexer.py` | 公开 `clear_artifacts()`；删除/覆盖时复用 |
| `kbplatform/jobs.py` | `enqueue(..., force=False)` 支持覆盖后强制重排 |
| `kbplatform/converters.py` | 新增 `convert_to_pdf()`（Office → PDF，供预览） |
| `kbplatform/catalog.py` | 增加 `_kb_trash` 到跳过目录；新增 `walk_all` 可选（由 `fsindex` 复用扫描逻辑） |
| `kbplatform/service.py` | `make_app` 里注册 `fs_service` 路由、启动/停止扫描线程、挂 `app.state.scanner` |
| `web/index.html` | 登录页重构、桌面页、文件页、回收站页、管理台「存储接入」页签 |
| `web/static/app.js` | 路由增加 `desktop/files/trash`；`showMain` 默认进桌面；管理台分发到新模块 |
| `web/static/style.css` | 只保留旧页面样式；新样式进 `v2.css` |

---

## Task 1: 依赖、配置与数据库骨架

**Files:**
- Create: `requirements.txt`
- Modify: `kbplatform/config.py`
- Modify: `kbplatform/db.py`
- Test: `kbplatform/tests/test_db.py`, `kbplatform/tests/test_config.py`, `kbplatform/tests/conftest.py`

**Interfaces:**
- Produces: `Config.trash_days: int = 30`、`Config.upload_max_mb: int = 2048`、`Config.preview_dir: str`、`Config.source_scan_interval_sec: int = 300`
- Produces: 表 `storage_sources`、`grants`、`fs_entries`、`trash_entries`、`fs_fts`
- Produces: pytest fixture `conn(tmp_path)`（已初始化完整库）

- [ ] **Step 1: 安装依赖并落清单**

```powershell
& $py -m pip install smbprotocol python-multipart
```

`requirements.txt` 写入：`fastapi`、`uvicorn`、`pytest`、`cryptography`、`argon2-cffi`、`pymupdf`、`openpyxl`、`smbprotocol>=1.17`、`python-multipart>=0.0.9`。

- [ ] **Step 2: 写失败测试**

`test_config.py` 增加：

```python
def test_storage_config_defaults_and_toml(tmp_path):
    from kbplatform.config import load_config, Config
    c = Config()
    assert c.trash_days == 30
    assert c.upload_max_mb == 2048
    assert c.source_scan_interval_sec == 300
    assert c.preview_dir.endswith("preview")
    p = tmp_path / "config.toml"
    p.write_text('[paths]\ndata_dir="X:/d"\n[storage]\ntrash_days=7\n'
                 'upload_max_mb=100\nsource_scan_interval_sec=0\n', encoding="utf-8")
    c2 = load_config(str(p))
    assert (c2.trash_days, c2.upload_max_mb, c2.source_scan_interval_sec) == (7, 100, 0)
```

`test_db.py` 增加：

```python
def test_v2_tables_and_fts(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"storage_sources", "grants", "fs_entries", "trash_entries", "fs_fts"} <= names
    conn.execute("INSERT INTO fs_fts(rowid,name,rel_path) VALUES (1,'年度计划.docx','a/年度计划.docx')")
    assert conn.execute("SELECT name FROM fs_fts WHERE fs_fts MATCH ?", ("年度计",)).fetchone()[0] == "年度计划.docx"
```

`conftest.py` 增加：

```python
import pytest

@pytest.fixture()
def conn(tmp_path):
    from kbplatform.db import init_db
    c = init_db(str(tmp_path / "kb.db"))
    yield c
    c.close()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_db.py::test_v2_tables_and_fts kbplatform/tests/test_config.py -q`
Expected: FAIL（表不存在 / 字段不存在）

- [ ] **Step 4: 实现**

`config.py`：`Config` 增加四个字段，`__post_init__` 里 `preview_dir` 默认 `data_dir/preview`；`load_config` 读取 `[storage]`。

`db.py` 的 `SCHEMA_SQL` 追加（放在 `users` 之后）：

```sql
CREATE TABLE IF NOT EXISTS storage_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,                 -- local | smb
  root TEXT NOT NULL,
  username TEXT, secret_enc TEXT, domain TEXT,
  writable INTEGER DEFAULT 0,
  personal_root TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  scan_state TEXT DEFAULT 'idle',     -- idle | scanning | paused | error
  scan_error TEXT,
  last_scan_at REAL,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL,
  subpath TEXT NOT NULL DEFAULT '',
  can_write INTEGER DEFAULT 0,
  created_at REAL,
  UNIQUE(user_id, source_id, subpath)
);
CREATE INDEX IF NOT EXISTS idx_grants_user ON grants(user_id);
CREATE TABLE IF NOT EXISTS fs_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  rel_path TEXT NOT NULL,
  rel_key TEXT NOT NULL,
  parent_rel TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  is_dir INTEGER NOT NULL DEFAULT 0,
  size INTEGER, mtime REAL, ext TEXT,
  parseable INTEGER DEFAULT 0,
  catalog_id INTEGER,
  updated_at REAL,
  UNIQUE(source_id, rel_key)
);
CREATE INDEX IF NOT EXISTS idx_fs_source_parent ON fs_entries(source_id, parent_rel);
CREATE INDEX IF NOT EXISTS idx_fs_name ON fs_entries(name);
CREATE TABLE IF NOT EXISTS trash_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  original_rel TEXT NOT NULL,
  trash_rel TEXT NOT NULL,
  name TEXT NOT NULL,
  is_dir INTEGER DEFAULT 0,
  size INTEGER,
  deleted_by INTEGER,
  deleted_at REAL,
  expires_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fs_fts USING fts5(name, rel_path, tokenize='trigram');
```

- [ ] **Step 5: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_db.py kbplatform/tests/test_config.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```powershell
git add requirements.txt kbplatform/config.py kbplatform/db.py kbplatform/tests
git commit -m "2.0 P0：新增存储源、授权、文件索引与回收站表"
```

---

## Task 2: 凭据与存储适配器

**Files:**
- Modify: `kbplatform/auth.py`
- Create: `kbplatform/storage.py`
- Test: `kbplatform/tests/test_storage.py`

**Interfaces:**
- Produces: `auth.encrypt_secret(plain: str) -> str | None`、`auth.decrypt_secret(blob: str | None) -> str | None`
- Produces: `storage.Source`、`storage.Entry`、`storage.source_from_row(row)`
- Produces: `storage.normalize_rel(rel) -> str`、`storage.is_safe_rel(rel) -> bool`
- Produces: `storage.adapter_for(source, password=None)`、`storage.test_connection(source, password, limit=20) -> dict`
- Produces: `LocalAdapter` / `SmbAdapter`，方法：`listdir(rel="")`、`stat(rel)`、`open_read(rel)`、`write_stream(rel, src, size=None)`、`mkdirs(rel)`、`rename(src_rel, dst_rel)`、`remove(rel)`、`local_path(rel)`

- [ ] **Step 1: 写失败测试**

```python
import io
from kbplatform import auth
from kbplatform.storage import (Entry, LocalAdapter, Source, adapter_for,
                                is_safe_rel, normalize_rel, test_connection)


def test_secret_roundtrip_and_wrong_key():
    auth.set_encryption_key("unit-test-secret")
    blob = auth.encrypt_secret("p@ss")
    assert blob and "p@ss" not in blob
    assert auth.decrypt_secret(blob) == "p@ss"
    auth.set_encryption_key("other")
    assert auth.decrypt_secret(blob) is None
    auth.set_encryption_key("unit-test-secret")


def test_safe_rel_rejects_escape():
    assert is_safe_rel("a/b") and is_safe_rel("a\\b")
    for bad in ("../x", "a/../../x", "\\\\host\\share", "C:\\x", "a/.."):
        assert not is_safe_rel(bad)
    assert normalize_rel("\\a\\b\\") == "a\\b"


def test_local_adapter_crud(tmp_path):
    root = tmp_path / "share"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    ad = LocalAdapter(str(root))
    assert [e.name for e in ad.listdir("")] == ["docs"]
    assert ad.listdir("docs")[0].name == "a.txt"
    assert ad.stat("docs/a.txt").size == 5
    assert ad.open_read("docs/a.txt").read() == b"hello"
    ad.mkdirs("new/sub")
    ad.write_stream("new/sub/b.txt", io.BytesIO(b"bbb"), 3)
    ad.rename("new/sub/b.txt", "new/sub/c.txt")
    assert ad.stat("new/sub/c.txt").size == 3
    ad.remove("new")
    assert ad.stat("new") is None


def test_local_adapter_blocks_escape(tmp_path):
    ad = LocalAdapter(str(tmp_path))
    for bad in ("../x", "a/../../x", "C:\\Windows\\win.ini"):
        try:
            ad.stat(bad)
        except ValueError:
            continue
        raise AssertionError(f"未拒绝 {bad}")
```

`test_connection` 用本地源断言 `{"ok": True, "count": 1, "detail": ""}`。

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_storage.py -q`
Expected: FAIL（`ImportError: storage`）

- [ ] **Step 3: 实现**

`auth.py` 末尾增加两个薄封装，复用 `_encrypt/_decrypt`：

```python
def encrypt_secret(plain: str) -> str | None:
    return _encrypt(plain)

def decrypt_secret(blob: str | None) -> str | None:
    return _decrypt(blob)
```

`storage.py`：

- `normalize_rel`：`rel.replace("/", "\\").strip("\\")`，折叠重复分隔符；空串返回 `""`。
- `is_safe_rel`：拒绝空段 `..`、以 `\`/`/` 开头、含 `:`、含 `*?"<>|`；允许中文与空格。
- `Entry(rel, name, is_dir, size, mtime)`。
- `Source` 字段与表一致；`source_from_row` 做 `bool()` 转换。
- `LocalAdapter._abs(rel)`：`is_safe_rel` 不通过就 `raise ValueError`；`os.path.realpath` 后必须仍位于 `realpath(root)` 之下。
- `LocalAdapter.listdir` 用 `os.scandir`，跳过 `.` 开头与 `_kb_trash`；目录 size=0；排序「目录优先、名称不区分大小写」。
- `SmbAdapter`：
  - `_unc(rel) = source.root.rstrip("\\") + ("\\" + rel if rel else "")`
  - 统一 `_kw = {"username": ..., "password": ..., "domain": ...}`（空值不传）
  - `listdir` 用 `smbclient.scandir`；`stat` 用 `smbclient.stat`；`open_read` 用 `smbclient.open_file(mode="rb")`；`write_stream` 用 `mode="wb"` 分块写；`mkdirs` 用 `smbclient.makedirs(exist_ok=True)`；`rename`/`remove` 用同名函数；`local_path` 返回 `None`。
- `adapter_for`：`local` 返回 `LocalAdapter`，`smb` 返回 `SmbAdapter`；其它 `ValueError`。
- `test_connection`：try 内 `listdir("")`，返回 `{"ok": True, "count": len(items), "detail": ""}`；异常返回 `{"ok": False, "count": 0, "detail": f"{type(e).__name__}: {e}"}`。
- SMB 测试连接前调用 `smbclient.delete_session(host)` 强制重新认证，避免连接池复用旧会话掩盖密码错误。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_storage.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/auth.py kbplatform/storage.py kbplatform/tests/test_storage.py
git commit -m "2.0 P0：存储适配器与共享凭据加密"
```

---

## Task 3: 路径索引与文件名秒搜

**Files:**
- Create: `kbplatform/fsindex.py`
- Test: `kbplatform/tests/test_fsindex.py`

**Interfaces:**
- Produces: `fsindex.rel_key(rel) -> str`、`fsindex.entry_path(source, rel) -> str`
- Produces: `fsindex.upsert_entry(conn, source, entry) -> int`
- Produces: `fsindex.delete_subtree(conn, source_id, rel) -> list[str]`
- Produces: `fsindex.scan_source(conn, source, adapter, qps=50) -> dict`
- Produces: `fsindex.search(conn, q, allowed, limit=20) -> list[dict]`
- `scan_source` 返回：`{"files","dirs","added","updated","removed","removed_rels","changed_parseable","errors"}`

- [ ] **Step 1: 写失败测试**

```python
from kbplatform.fsindex import scan_source, search
from kbplatform.storage import LocalAdapter, Source


def _src(tmp_path, sid=1):
    return Source(id=sid, name="本地", kind="local", root=str(tmp_path), writable=True)


def test_scan_indexes_all_files_and_is_incremental(tmp_path, conn):
    (tmp_path / "资料").mkdir()
    (tmp_path / "资料" / "年度信息化计划.docx").write_text("x", encoding="utf-8")
    (tmp_path / "资料" / "照片.jpg").write_bytes(b"jpg")
    src, ad = _src(tmp_path), LocalAdapter(str(tmp_path))
    r1 = scan_source(conn, src, ad)
    assert (r1["files"], r1["dirs"]) == (2, 1)
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 3
    assert search(conn, "信息化", None)[0]["name"] == "年度信息化计划.docx"
    assert search(conn, "计划", None)[0]["name"] == "年度信息化计划.docx"   # 2 字走 LIKE
    r2 = scan_source(conn, src, ad)
    assert (r2["added"], r2["updated"]) == (0, 0)
    (tmp_path / "资料" / "照片.jpg").unlink()
    r3 = scan_source(conn, src, ad)
    assert r3["removed"] == 1
    assert search(conn, "照片", None) == []


def test_search_respects_allowed_scope(conn):
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
                 "VALUES (1,'甲','local','X:/甲',1,0)")
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
                 "VALUES (2,'乙','local','X:/乙',1,0)")
    names = ["甲部门预算.docx", "乙部门预算.docx"]
    for i, name in enumerate(names):
        cur = conn.execute(
            "INSERT INTO fs_entries(source_id,rel_path,rel_key,parent_rel,name,is_dir,parseable) "
            "VALUES (?,?,?,?,?,0,1)", (i + 1, name, name.lower(), "", name))
        conn.execute("INSERT INTO fs_fts(rowid,name,rel_path) VALUES (?,?,?)",
                     (cur.lastrowid, name, name))
    conn.commit()
    assert [r["name"] for r in search(conn, "预算", [(1, "")])] == ["甲部门预算.docx"]
    assert [r["name"] for r in search(conn, "预算", [(2, "")])] == ["乙部门预算.docx"]
    assert search(conn, "预算", [(3, "")]) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_fsindex.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `rel_key`：`normalize_rel(rel).lower()`；`entry_path`：`source.root.rstrip("\\") + "\\" + rel`（空 rel 时就是 root）。
- `upsert_entry`：`INSERT ... ON CONFLICT(source_id, rel_key) DO UPDATE`，同步 `fs_fts`（先 `DELETE FROM fs_fts WHERE rowid=?` 再插入，rowid = `fs_entries.id`）。
- `delete_subtree`：按 `rel_key == key OR rel_key LIKE key + '\\%'` 找 id，删除 `fs_fts`、`fs_entries`，返回被删 `rel_path`。
- `scan_source`：
  - 递归 `adapter.listdir`，维护 `seen`；每个条目 `parseable = (not is_dir and ext in catalog.PARSEABLE_EXT)`。
  - 与库里现有行比对 `size/mtime`；新增/变化才写；未出现的旧行删除。
  - 单目录失败记入 `errors` 并继续；`qps` 控制每次 listdir 后 `sleep(1/qps)`。
  - 跳过根下的 `_kb_trash` 与 `_personal`（个人空间不建全局索引，避免所有用户看到）。
- `search`：
  - `q` 去空白；`len(q) >= 3` 用 `fs_fts MATCH '"' + q.replace('"','""') + '"'`；否则 `name LIKE '%q%' OR rel_path LIKE '%q%'`。
  - JOIN `fs_entries` 与 `storage_sources`，先按 `allowed`（`[(source_id, subpath)]`，`None` 表示管理员不限）过滤，**再截断 limit**。
  - 排序：文件名完全相等 > 前缀 > 其余；同级按 `mtime DESC`。
  - 返回 `{"id","source_id","source_name","rel","name","is_dir","size","mtime","ext"}`。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_fsindex.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/fsindex.py kbplatform/tests/test_fsindex.py
git commit -m "2.0 P0：路径索引与文件名秒搜"
```

---

## Task 4: 授权、导航占位与个人空间

**Files:**
- Create: `kbplatform/grants.py`
- Test: `kbplatform/tests/test_grants.py`

**Interfaces:**
- Produces: `Grant(source_id, subpath, can_write)`
- Produces: `user_grants(conn, user) -> list[Grant]`
- Produces: `can_read(user, grants, source_id, rel) -> bool`、`can_write(...) -> bool`
- Produces: `filter_entries(grants, source_id, rel, entries) -> list[Entry]`
- Produces: `set_grants(conn, user_id, items: list[dict]) -> int`
- Produces: `migrate_user_scopes(conn, cfg) -> int`

- [ ] **Step 1: 写失败测试**

```python
from kbplatform.grants import (Grant, can_read, can_write, filter_entries,
                               set_grants, user_grants)
from kbplatform.storage import Entry


def _user(role="user", uid=7):
    return {"id": uid, "role": role, "username": "u1"}


def _e(rel, is_dir=True):
    return Entry(rel=rel, name=rel.split("\\")[-1], is_dir=is_dir, size=0, mtime=0)


def test_prefix_and_write_rules(conn):
    set_grants(conn, 7, [{"source_id": 1, "subpath": "A\\B", "can_write": True}])
    gs = user_grants(conn, _user())
    assert can_read(_user(), gs, 1, "A\\B\\c.txt")
    assert can_write(_user(), gs, 1, "A\\B\\c.txt")
    assert not can_write(_user(), gs, 1, "A")
    assert not can_read(_user(), gs, 1, "A\\C")


def test_ancestor_is_visible_but_siblings_are_hidden():
    gs = [Grant(1, "A\\B\\C", False)]
    entries = [_e("A"), _e("A\\B"), _e("A\\X")]
    assert [e.rel for e in filter_entries(gs, 1, "", entries)] == ["A"]
    entries = [_e("A\\B"), _e("A\\X")]
    assert [e.rel for e in filter_entries(gs, 1, "A", entries)] == ["A\\B"]


def test_admin_sees_everything():
    u = _user("admin")
    assert can_read(u, [], 99, "任意") and can_write(u, [], 99, "任意")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_grants.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `user_grants`：读 `grants`；再为每个 `personal_root` 非空的启用源追加 `Grant(source_id, f"{personal_root}\\{username}", True)`（去重）。管理员返回全部源的空 grant（`None` 语义由 `can_*` 处理，列表可返回 `[]`）。
- `can_read`：管理员恒真；普通用户当 `rel` 在某个 grant 之下，或 `rel` 是某个 grant 路径的祖先（用于进入深层授权）时为真。
- `can_write`：管理员恒真；普通用户必须 `rel` 在 `can_write=True` 的 grant 之下。
- `filter_entries`：若存在 `subpath==""` 的 grant，全放行；否则只保留「在 grant 内」或「等于下一段导航占位」的条目；占位目录即使真实不存在也可由 API 补。
- `set_grants`：事务内先删后插，返回写入条数；拒绝 `..`。
- `migrate_user_scopes`：对每个启用源，取共享名（`split_share(source.root)[0]`），把 `user_scopes` 中同 share 且 subpath 能映射到源内相对路径的行写成 grants；无法映射的跳过并计数。仅在一个标记表 `kv2_migrated` 不存在时执行一次（用 `PRAGMA user_version` 或单独表 `meta(key,value)`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_grants.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/grants.py kbplatform/tests/test_grants.py
git commit -m "2.0 P0：目录授权、导航占位与个人空间"
```

---

## Task 5: 扫描编排与自动解析入队

**Files:**
- Modify: `kbplatform/indexer.py`（公开 `clear_artifacts`）
- Modify: `kbplatform/jobs.py`（`enqueue(..., force=False)`）
- Create: `kbplatform/scanner.py`
- Test: `kbplatform/tests/test_scanner.py`

**Interfaces:**
- Produces: `indexer.clear_artifacts(conn, fts_path, vs, path) -> None`
- Produces: `JobQueue.enqueue(kind, catalog_id, force=False) -> int`
- Produces: `SourceScanner(conn, cfg, interval=None)`，方法 `start()`、`stop()`、`scan_now(source_id)`、`trigger(source_id)`、`status(source_id=None)`

- [ ] **Step 1: 写失败测试**

```python
import time
from kbplatform.auth import create_user
from kbplatform.config import Config
from kbplatform.scanner import SourceScanner
from kbplatform.storage import Source


def _seed_source(conn, root):
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('测试共享','local',?,1,1,?)", (str(root), time.time()))
    conn.commit()
    return conn.execute("SELECT id FROM storage_sources").fetchone()[0]


def test_scan_enqueues_parseable_and_indexes_paths(tmp_path, conn):
    (tmp_path / "制度").mkdir()
    (tmp_path / "制度" / "考勤办法.docx").write_text("x", encoding="utf-8")
    (tmp_path / "制度" / "logo.png").write_bytes(b"png")
    sid = _seed_source(conn, tmp_path)
    sc = SourceScanner(conn, Config(data_dir=str(tmp_path / "data")), interval=0)
    r = sc.scan_now(sid)
    assert r["files"] == 2
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM catalog WHERE name='考勤办法.docx'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index'").fetchone()[0] == 1
    sc.scan_now(sid)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='index'").fetchone()[0] == 1


def test_nas_side_delete_clears_index_but_keeps_no_trash(tmp_path, conn):
    (tmp_path / "a.md").write_text("正文" * 50, encoding="utf-8")
    sid = _seed_source(conn, tmp_path)
    sc = SourceScanner(conn, Config(data_dir=str(tmp_path / "data")), interval=0)
    sc.scan_now(sid)
    (tmp_path / "a.md").unlink()
    r = sc.scan_now(sid)
    assert r["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_scanner.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `indexer.clear_artifacts(conn, fts_path, vs, path)` 直接调用 `_delete_artifacts`。
- `JobQueue.enqueue(..., force=False)`：`force=True` 时若已有 `running` 任务仍插入一个 `queued`（保证再解析），但若已有 `queued` 则复用。
- `SourceScanner.scan_now(source_id)`：
  1. 读源行，解密密码，`adapter_for`。
  2. 置 `scan_state='scanning'`、`scan_error=NULL`。
  3. `fsindex.scan_source`；对 `changed_parseable`：
     - 用 `FileRecord(path=entry_path, share=源名, rel_path, name, ext, size, mtime)` `upsert_catalog`；
     - 若该路径此前已有 catalog 行（覆盖），先 `clear_artifacts`，把 `status='pending', tier1_done=0, tier2_done=0`，再 `enqueue("index", cid, force=True)`；否则普通 `enqueue`。
     - 可选：直接调用 `indexer.index_file`？P0 交给现有 pipeline 消费，接口测试只断言排队。
  4. `removed_rels` 中可解析的：按 `path` 找 catalog → `indexer.remove_file`（清产物、标 skipped）。
  5. 更新 `last_scan_at`、`scan_state='idle'`；异常置 `error` 并写入 `scan_error`。
  6. 返回 `fsindex` 统计 + `catalog_enqueued`。
- `trigger(source_id)` 把源 id 放入线程安全队列；`start()` 起 daemon 线程，按 `cfg.source_scan_interval_sec` 周期扫描所有启用源；`stop()` 置停止事件。
- `status` 返回每个源的 `{id,name,state,error,last_scan_at}`，当前内存中的 `progress`（`scanning` 时返回已扫文件数）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_scanner.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/indexer.py kbplatform/jobs.py kbplatform/scanner.py kbplatform/tests/test_scanner.py
git commit -m "2.0 P0：共享扫描入队与删除清理"
```

---

## Task 6: 文件浏览与搜索 API

**Files:**
- Create: `kbplatform/fs_service.py`
- Modify: `kbplatform/service.py`
- Test: `kbplatform/tests/test_fs_api.py`

**Interfaces:**
- Produces: `register_fs_routes(app, cfg, conn, require_user, require_admin, audit)`（在 `make_app` 内调用）
- Produces: `app.state.scanner`
- 路由：
  - `GET /api/v1/fs/sources` → `{"sources":[{"id","name","kind","writable","can_write","personal"}]}`
  - `GET /api/v1/fs/list?source_id=&rel=` → `{"source","rel","breadcrumb","can_write","entries"}`
  - `GET /api/v1/fs/search?q=&limit=20` → `{"q","results"}`
  - `GET /api/v1/fs/stat?source_id=&rel=`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from fastapi.testclient import TestClient
from kbplatform.auth import create_user
from kbplatform.service import make_app


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir="{str(tmp_path / "data").replace(chr(92), "/")}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return make_app(str(cfg))


def _login(app, name="alice", role="user"):
    conn = app.state.conn
    create_user(conn, name, "pw123456", role=role)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    c = TestClient(app)
    assert c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"}).status_code == 200
    return c, uid


def test_list_only_granted_subtree(app, tmp_path):
    share = tmp_path / "share"
    (share / "A" / "B" / "C").mkdir(parents=True)
    (share / "A" / "X").mkdir()
    (share / "A" / "B" / "C" / "c.txt").write_text("hi", encoding="utf-8")
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('共享','local',?,1,1,0)", (str(share),))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c, uid = _login(app)
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) VALUES (?,?,?,?,0)",
                 (uid, sid, r"A\B\C", 0))
    conn.commit()
    body = c.get(f"/api/v1/fs/list?source_id={sid}&rel=").json()
    assert [e["name"] for e in body["entries"]] == ["A"]
    body = c.get(f"/api/v1/fs/list?source_id={sid}&rel=A").json()
    assert [e["name"] for e in body["entries"]] == ["B"]
    assert c.get(f"/api/v1/fs/list?source_id={sid}&rel=A/X").status_code == 404
    assert c.get(f"/api/v1/fs/list?source_id={sid}&rel=../../").status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py -q`
Expected: FAIL（404 路由 / 表未接）

- [ ] **Step 3: 实现**

`fs_service.py` 用闭包接收依赖，路由内统一：

1. `rows = storage_sources`（启用）；`grants = user_grants`。
2. `rel = normalize_rel`；`is_safe_rel` 失败 → 400。
3. `can_read` → 否则 404（不使用 403，避免泄露存在性）。
4. `adapter = adapter_for(source, decrypt_secret(secret_enc))`。
5. `entries = filter_entries(...)`；对每个条目补 `parse_status`（`fs_entries.catalog_id` JOIN catalog）与 `can_write`。
6. `breadcrumb` 按 `rel` 分段生成 `{name, rel}`；根为 `{"name": source.name, "rel": ""}`。
7. 若 `filter_entries` 产出的是占位目录但真实目录不存在，补一个 `is_dir=True` 的合成条目。

`fs/sources` 里对 `personal` 源标注 `personal: true`；管理员返回全部源。

`service.py`：
```python
from kbplatform.fs_service import register_fs_routes
from kbplatform.scanner import SourceScanner
...
register_fs_routes(app, cfg, conn, require_user, require_admin, audit)
app.state.scanner = SourceScanner(conn, cfg)
if cfg.source_scan_interval_sec > 0:
    app.state.scanner.start()
```
并在 FastAPI `shutdown` 事件里 `stop()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/fs_service.py kbplatform/service.py kbplatform/tests/test_fs_api.py
git commit -m "2.0 P0：文件浏览与文件名搜索接口"
```

---

## Task 7: 预览（Office → PDF 缓存）

**Files:**
- Modify: `kbplatform/converters.py`
- Modify: `kbplatform/fs_service.py`
- Test: `kbplatform/tests/test_converters.py`, `kbplatform/tests/test_fs_api.py`

**Interfaces:**
- Produces: `converters.convert_to_pdf(src_path, out_dir, stem=None) -> str | None`
- 路由：
  - `GET /api/v1/fs/preview?source_id=&rel=`（inline）
  - `GET /api/v1/fs/download?source_id=&rel=`（attachment）

- [ ] **Step 1: 写失败测试**

`test_converters.py` 增加（不依赖本机 LibreOffice）：

```python
def test_convert_to_pdf_returns_none_when_unavailable(tmp_path, monkeypatch):
    from kbplatform import converters
    monkeypatch.setattr(converters, "soffice_path", lambda: None)
    assert converters.convert_to_pdf(str(tmp_path / "a.docx"), str(tmp_path)) is None
```

`test_fs_api.py` 增加：

```python
def test_text_preview_and_download(app, tmp_path):
    share = tmp_path / "s"; (share / "d").mkdir(parents=True)
    (share / "d" / "a.txt").write_text("你好，世界", encoding="utf-8")
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
                 "VALUES ('s','local',?,1,1,0)", (str(share),))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c, uid = _login(app)
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) VALUES (?,?,?,1,0)",
                 (uid, sid, ""))
    conn.commit()
    r = c.get(f"/api/v1/fs/preview?source_id={sid}&rel=d/a.txt")
    assert r.status_code == 200 and "你好" in r.text
    r = c.get(f"/api/v1/fs/download?source_id={sid}&rel=d/a.txt")
    assert "attachment" in r.headers["content-disposition"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_converters.py kbplatform/tests/test_fs_api.py -q`
Expected: FAIL（函数/路由不存在）

- [ ] **Step 3: 实现**

- `convert_to_pdf`：用 `soffice_path()`；输出名 `stem + ".pdf"`；`subprocess.run(..., timeout=180)`；成功且非空返回路径，否则 None。
- 预览分类：
  - `pdf`、图片（`.png .jpg .jpeg .gif .webp .bmp`）：直接流。
  - `.txt .md .csv .log .json`：读前 2 MB，按 `utf-8` → `gbk` 解码，返回 `text/plain; charset=utf-8`。
  - Office（`.docx .xlsx .pptx .doc .xls .ppt .ofd`）：本地文件直接转；SMB 先下载到 `cfg.preview_dir/_src/<sha1>`，再转；PDF 缓存在 `cfg.preview_dir/<sha1>_<mtime>.pdf`。
  - 其它：仅允许下载，预览返回 415。
- 本地文件用 `FileResponse`（支持 Range）；SMB 用 `StreamingResponse` 64 KB 分块，设置 `Content-Length`，视频暂不做 Range（P1 补）。
- 文件名头：`Content-Disposition` 用 `filename*=UTF-8''...`，避免中文乱码。
- 每一次预览/下载都先 `can_read`；预览缓存命中也要校验。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_converters.py kbplatform/tests/test_fs_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/converters.py kbplatform/fs_service.py kbplatform/tests/test_converters.py kbplatform/tests/test_fs_api.py
git commit -m "2.0 P0：文件预览与 Office 转 PDF 缓存"
```

---

## Task 8: 分片上传与上传即解析

**Files:**
- Modify: `kbplatform/fs_service.py`
- Modify: `kbplatform/db.py`（`upload_sessions` 表）
- Test: `kbplatform/tests/test_fs_api.py`

**Interfaces:**
- 路由：
  - `POST /api/v1/fs/upload/init` body `{source_id, rel, name, size, overwrite}` → `{upload_id, chunk_size}`
  - `PUT /api/v1/fs/upload/{upload_id}?offset=N`（raw body）→ `{received}`
  - `POST /api/v1/fs/upload/{upload_id}/complete` → `{entry, catalog_id, queued}`
- 上传临时目录：`cfg.data_dir/uploads/<upload_id>`

- [ ] **Step 1: 写失败测试**

```python
def test_chunked_upload_creates_file_and_enqueues_parse(app, tmp_path):
    share = tmp_path / "s"; share.mkdir()
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) VALUES ('s','local',?,1,1,0)", (str(share),))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c, uid = _login(app)
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) VALUES (?,?,?,1,0)", (uid, sid, ""))
    conn.commit()
    data = "制度正文".encode("utf-8") * 1000
    init = c.post("/api/v1/fs/upload/init", json={"source_id": sid, "rel": "", "name": "制度.md", "size": len(data)}).json()
    uid_ = init["upload_id"]
    for off in range(0, len(data), 4096):
        r = c.put(f"/api/v1/fs/upload/{uid_}?offset={off}", content=data[off:off+4096])
        assert r.status_code == 200
    done = c.post(f"/api/v1/fs/upload/{uid_}/complete").json()
    assert (share / "制度.md").read_bytes() == data
    assert done["queued"] is True
    assert conn.execute("SELECT status FROM catalog WHERE name='制度.md'").fetchone()[0] == "pending"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py::test_chunked_upload_creates_file_and_enqueues_parse -q`
Expected: FAIL（404）

- [ ] **Step 3: 实现**

- `upload_sessions`：`id TEXT PRIMARY KEY, user_id, source_id, rel, name, size, temp_path, received, created_at`。
- `init`：校验写权限、`source.writable`、文件名非法字符、大小 ≤ `upload_max_mb`；若目标已存在且 `overwrite` 非真 → 409；生成 `uuid4().hex`，建临时文件。
- `PUT`：`offset` 必须等于当前 `received`（顺序写，简单可靠）；流式读 body 写临时文件；超过声明大小 → 413。
- `complete`：校验 `received == size`；`adapter.write_stream`；`fsindex.upsert_entry`；若已存在 catalog 行：
  - `clear_artifacts`、`status='pending'`、`tier1_done=0`、`tier2_done=0`，`enqueue("index", cid, force=True)`；
  - 否则 `upsert_catalog` + `enqueue("index", cid)`。
  - 删除临时文件与 session 行，返回条目。
- 启动时清理超过 24 小时的残留上传会话（在 `make_app` 里调用一次）。
- 审计：`action='upload'`，记源、目标路径、大小。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/fs_service.py kbplatform/db.py kbplatform/tests/test_fs_api.py
git commit -m "2.0 P0：分片上传与上传即解析"
```

---

## Task 9: 删除、回收站、恢复与彻底删除

**Files:**
- Modify: `kbplatform/fs_service.py`
- Test: `kbplatform/tests/test_fs_api.py`

**Interfaces:**
- 路由：
  - `POST /api/v1/fs/delete` body `{source_id, rels}` → `{deleted:[trash_id], skipped:[{rel,reason}]}`
  - `GET /api/v1/fs/trash` → `{items}`
  - `POST /api/v1/fs/trash/{trash_id}/restore` → `{restored}`
  - `DELETE /api/v1/fs/trash/{trash_id}`（管理员）→ `{purged: true}`

- [ ] **Step 1: 写失败测试**

```python
def test_delete_moves_to_trash_and_clears_search(app, tmp_path):
    share = tmp_path / "s"; (share / "d").mkdir(parents=True)
    (share / "d" / "a.md").write_text("正文" * 20, encoding="utf-8")
    conn = app.state.conn
    conn.execute("INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) VALUES ('s','local',?,1,1,0)", (str(share),))
    sid = conn.execute("SELECT id FROM storage_sources").fetchone()[0]
    c, uid = _login(app)
    conn.execute("INSERT INTO grants(user_id,source_id,subpath,can_write,created_at) VALUES (?,?,?,1,0)", (uid, sid, ""))
    conn.commit()
    from kbplatform.fsindex import scan_source, search
    from kbplatform.storage import LocalAdapter, source_from_row
    scan_source(conn, source_from_row(conn.execute("SELECT * FROM storage_sources").fetchone()), LocalAdapter(str(share)))
    assert search(conn, "a.md", None)
    r = c.post("/api/v1/fs/delete", json={"source_id": sid, "rels": ["d/a.md"]})
    assert r.status_code == 200 and len(r.json()["deleted"]) == 1
    assert not (share / "d" / "a.md").exists()
    assert search(conn, "a.md", None) == []
    assert (len(c.get("/api/v1/fs/trash").json()["items"])) == 1
    tid = r.json()["deleted"][0]
    assert c.post(f"/api/v1/fs/trash/{tid}/restore").status_code == 200
    assert (share / "d" / "a.md").exists()


def test_cannot_delete_grant_root(app, tmp_path):
    ...  # 授权根 A，删除 A → skipped，原目录仍在
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py -k trash -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- 回收站位置：`<source.root>\_kb_trash\<YYYYMMDD>\<uuid>_<name>`；扫描跳过 `_kb_trash`。
- `delete`：
  - `can_write`；若 `rel` 等于某个可写 grant 的 `subpath`（或空串）→ `skipped`，原因「授权根不可删除」。
  - `adapter.rename` 到回收站；失败则复制+删除。
  - `trash_entries` 插入，`expires_at = now + cfg.trash_days*86400`。
  - `fsindex.delete_subtree`；可解析文件的 catalog → `indexer.remove_file`（清 chunks/fts/vectors/knowledge/edges，标 skipped）。
  - 审计 `action='delete'`。
- `restore`：仅删除人或管理员；目标父目录不存在则 `mkdirs`；`adapter.rename` 回原路径；`upsert_entry`；可解析 → `upsert_catalog` + `enqueue`；删除 trash 行。
- 彻底删除：管理员 + 过期或显式 purge；`adapter.remove(trash_rel)`；删除行。
- 回收站列表按 `deleted_at DESC`；普通用户只看自己的；管理员看全部。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_fs_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/fs_service.py kbplatform/tests/test_fs_api.py
git commit -m "2.0 P0：回收站删除、恢复与彻底删除"
```

---

## Task 10: 存储接入与目录授权管理 API

**Files:**
- Modify: `kbplatform/fs_service.py`
- Test: `kbplatform/tests/test_sources_api.py`

**Interfaces:**
- 路由：
  - `GET/POST /api/v1/admin/sources`、`PUT/DELETE /api/v1/admin/sources/{id}`
  - `POST /api/v1/admin/sources/{id}/test`
  - `POST /api/v1/admin/sources/{id}/scan`、`GET /api/v1/admin/sources/{id}/scan/status`
  - `GET /api/v1/admin/sources/{id}/tree?rel=`
  - `GET/PUT /api/v1/admin/users/{user_id}/grants`

- [ ] **Step 1: 写失败测试**

```python
def test_create_source_encrypts_password_and_never_returns_it(app, tmp_path):
    c, _ = _login(app, "admin", role="admin")
    share = tmp_path / "share"; share.mkdir()
    r = c.post("/api/v1/admin/sources", json={
        "name": "信息化管理", "kind": "local", "root": str(share),
        "username": "", "password": "p@ss", "writable": True, "enabled": True})
    assert r.status_code == 200
    sid = r.json()["id"]
    body = c.get("/api/v1/admin/sources").json()
    assert body["sources"][0]["has_password"] is True
    assert "p@ss" not in r.text and "p@ss" not in c.get("/api/v1/admin/sources").text
    assert c.post(f"/api/v1/admin/sources/{sid}/test").json()["ok"] is True
    assert c.post(f"/api/v1/admin/sources/{sid}/scan").status_code == 200


def test_set_and_read_user_grants(app, tmp_path):
    c, _ = _login(app, "admin", role="admin")
    uid = app.state.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    r = c.put(f"/api/v1/admin/users/{uid}/grants", json={"grants": [
        {"source_id": 1, "subpath": "A\\B", "can_write": True}]})
    assert r.status_code == 200
    body = c.get(f"/api/v1/admin/users/{uid}/grants").json()
    assert body["grants"] == [{"source_id": 1, "subpath": "A\\B", "can_write": True}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_sources_api.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- 所有 admin 路由用 `require_admin`。
- 创建/更新：`password` 非空才加密写入；更新时 `password` 缺省或空串表示保留原密码；`secret_enc` 永不返回，只返回 `has_password`。
- 凭据变更后调用 `smbclient.delete_session(host)`（仅 smb），避免旧会话继续用旧密码。
- `test`：支持「未保存表单」场景，body 可带 `{kind, root, username, password, domain}`；不带则用库里配置。
- `scan`：`app.state.scanner.trigger(sid)`，立即返回 202；`status` 返回 `scan_state`、`last_scan_at`、当前计数。
- `tree`：`adapter.listdir(rel)` 只返回目录，用于授权选择器；管理员不受 grants 限制，但源必须启用。
- `grants` GET/PUT：`set_grants`；GET 返回真实行（不含个人空间合成授权）。
- DELETE source：拒绝删除仍有 grants 的源（返回 409 + 数量），除非 `?force=1`。

- [ ] **Step 4: 跑测试确认通过**

Run: `& $py -m pytest kbplatform/tests/test_sources_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```powershell
git add kbplatform/fs_service.py kbplatform/tests/test_sources_api.py
git commit -m "2.0 P0：存储接入与目录授权管理接口"
```

---

## Task 11: 登录页与功能桌面

**Files:**
- Modify: `web/index.html`
- Modify: `web/static/app.js`
- Create: `web/static/desktop.js`
- Create: `web/static/v2.css`
- Test: `kbplatform/tests/test_web_assets.py`

**Interfaces:**
- Produces: `window.KB.desktop.mount()`（幂等）
- `app.js` 路由页：`desktop`、`files`、`trash`（后两个由 Task 12/13 实现）
- 登录后默认 `#/desktop`

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"

def test_login_and_desktop_markup_present():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="p-desktop"' in html
    assert 'id="login-form"' in html
    assert 'data-open="files"' in html
    assert 'href="/static/v2.css"' in html
    assert 'src="/static/desktop.js"' in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- `index.html`：
  - 登录页改为左右分栏：左侧品牌/标语/网格光效，右侧表单；保留 `#login-form`、`#login-user`、`#login-pass`、`#login-err`。
  - 顶栏导航增加「桌面」「文件」「回收站」；管理台保留。
  - 新增 `#p-desktop`：`#desktop-grid`，图标项 `data-open="files|ask|graph|board|trash|admin"`；管理台图标 `.admin-only`。
  - 新增 `#p-files`、`#p-trash` 骨架（内容由后续任务填充），先放空容器。
  - `v2.css` 在 `style.css` 之后加载；`desktop.js`、`files.js`、`admin-sources.js` 在 `app.js` 之前加载。
- `v2.css`：深色科技感（近黑底、青蓝点缀）、网格/光晕用 CSS 渐变，`prefers-reduced-motion` 时关闭动画；文件页用三栏布局。
- `desktop.js`：`window.KB = window.KB || {}; KB.desktop = { mount(){...} }`；点击图标改 `location.hash`；不直接调 API。
- `app.js`：
  - `PAGES` 加 `desktop/files/trash`；`route()` 分发 `KB.files?.load()`、`KB.trash?.load()`。
  - `showMain` 后若 hash 为空或 `#/board`，改为 `#/desktop`（只在首次登录）。
  - `loadAdmin()` 增加 `if (state.adminTab === "sources") return KB.adminSources.render(body);`
- 手动验收：浏览器打开登录页，1366×768 与 1920×1080 不溢出；登录后进桌面；图标可跳转。

- [ ] **Step 4: 跑测试 + 浏览器冒烟**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
然后 `browser.tabs.open` 打开 `http://192.0.2.203:8000`，截图确认登录页与桌面。
Expected: PASS + 页面正常。

- [ ] **Step 5: 提交**

```powershell
git add web/index.html web/static/app.js web/static/desktop.js web/static/v2.css kbplatform/tests/test_web_assets.py
git commit -m "2.0 P0：科技感登录页与功能桌面"
```

---

## Task 12: 文件管理器核心（浏览 / 搜索 / 预览）

**Files:**
- Create: `web/static/files.js`
- Modify: `web/index.html`
- Modify: `web/static/v2.css`
- Test: `kbplatform/tests/test_web_assets.py`

**Interfaces:**
- `window.KB.files`：`load()`、`open(sourceId, rel)`、`refresh()`、`preview(entry)`、`closePreview()`
- 复用 `app.js` 的 `api()`、`toast()`、`esc()`、`fmtTime()`

- [ ] **Step 1: 写失败测试**

```python
def test_file_manager_markup_and_script():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("fm-sources", "fm-crumbs", "fm-search", "fm-list", "fm-preview", "fm-ctx"):
        assert f'id="{i}"' in html
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "KB.files" in js and "/api/v1/fs/list" in js and "/api/v1/fs/search" in js
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- 布局：左栏源/目录树（`#fm-sources`），顶部面包屑（`#fm-crumbs`）+ 搜索框（`#fm-search`），主区表格（`#fm-list`），右键菜单（`#fm-ctx`），预览层（`#fm-preview`）。
- `load()`：`GET /fs/sources` → 渲染源；自动打开第一个源根。
- `open()`：`GET /fs/list` → 渲染行（名称、大小、修改时间、解析状态徽标）；目录双击进入，文件双击 `preview()`。
- 多选：点击行切换 `.selected`，`Ctrl/Cmd` 多选，`Shift` 连选；表头复选框全选。
- 搜索：输入 150ms 防抖，`GET /fs/search`，结果下拉；`↑/↓` 选择，`Enter` 打开；Esc 关闭。
- 预览：图片/PDF 用 `<iframe>`/`<img>` 指向 `/fs/preview`；文本用 `fetch` 后 `<pre>`；Office 用 iframe（后端转 PDF）；视频用 `<video>`；右上角下载按钮指向 `/fs/download`。
- 右键菜单：预览、下载、复制路径、删除（删除调用 Task 13 的 API；若未实现则菜单项隐藏）。
- 键盘：`Enter` 打开、`Delete` 删除（确认框）、`Esc` 关预览。
- 所有用户内容用 `esc()` 转义；文件名不拼进 `innerHTML` 未转义。

- [ ] **Step 4: 跑测试 + 浏览器冒烟**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
浏览器：建一个本地源、授权给测试用户，确认树/列表/搜索/预览可用；越权目录不出现在树里。
Expected: PASS + 交互正常。

- [ ] **Step 5: 提交**

```powershell
git add web/index.html web/static/files.js web/static/v2.css kbplatform/tests/test_web_assets.py
git commit -m "2.0 P0：文件管理器浏览、搜索与预览"
```

---

## Task 13: 上传、回收站与管理台 UI

**Files:**
- Modify: `web/static/files.js`
- Create: `web/static/admin-sources.js`
- Modify: `web/index.html`
- Modify: `web/static/v2.css`
- Test: `kbplatform/tests/test_web_assets.py`

**Interfaces:**
- `window.KB.files.upload(files)`、`window.KB.trash.load()`、`window.KB.adminSources.render(body)`、`openGrants(userId)`
- 管理台新增页签：`data-t="sources"`

- [ ] **Step 1: 写失败测试**

```python
def test_upload_trash_and_admin_markup():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("fm-upload", "fm-upload-panel", "p-trash", "trash-list"):
        assert f'id="{i}"' in html
    assert 'data-t="sources"' in html
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "/api/v1/fs/upload/init" in js and "/api/v1/fs/delete" in js
    adm = (WEB / "static" / "admin-sources.js").read_text(encoding="utf-8")
    assert "/api/v1/admin/sources" in adm and "/api/v1/admin/users/" in adm
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- 上传：`#fm-upload` 选择多文件；按 5 MB 分片调用 init/PUT/complete；`#fm-upload-panel` 显示每个文件的进度、速度、成功/失败；完成后刷新当前目录；上传按钮在无写权限时置灰。
- 回收站：`#p-trash` 表格显示名称、原路径、删除时间、大小、剩余天数；恢复按钮；管理员显示「彻底删除」。
- 管理台「存储接入」：
  - 列表：名称、类型、根路径、已配密码、可写、扫描状态；按钮：测试、扫描、编辑、删除。
  - 表单：名称、类型（local/smb）、根路径、账号、密码（编辑时留空=不改）、域、可写、个人空间目录、启用。
  - 保存前可「测试连接」；错误就地显示。
  - 目录树选择器：`GET /admin/sources/{id}/tree` 展开，用于选择授权子路径。
- 用户授权：在「用户与权限」每行加「目录授权」按钮，弹窗选择源 + 子路径 + 读写；保存 `PUT /admin/users/{id}/grants`。
- 手动验收：上传一个 docx，列表显示「解析中」，随后变「可搜索」；删除后回收站可见，恢复后文件回来；管理台能接入本地共享并扫描。

- [ ] **Step 4: 跑测试 + 浏览器冒烟**

Run: `& $py -m pytest kbplatform/tests/test_web_assets.py -q`
浏览器走完「接入源 → 授权 → 上传 → 搜索 → 预览 → 删除 → 恢复」。
Expected: PASS + 全流程可用。

- [ ] **Step 5: 提交**

```powershell
git add web/index.html web/static/files.js web/static/admin-sources.js web/static/v2.css kbplatform/tests/test_web_assets.py
git commit -m "2.0 P0：上传、回收站与存储接入管理界面"
```

---

## Task 14: 回归、性能与验收

**Files:**
- Modify: `README.md`（新增 2.0 使用说明与依赖）
- Test: 全部

- [ ] **Step 1: 跑全量测试**

Run: `& $py -m pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py -q`
Expected: 全绿；如有既有失败，先记录再修，不得跳过。

- [ ] **Step 2: 回归旧功能**

- `/api/v1/documents`、`/api/v1/search`、`/api/v1/query`、`/api/v1/graph/*` 仍按 `user_scopes` 工作（本计划未改它们）。
- WorkBuddy token（id=50）调用 `/api/v1/me` 正常。
- 重启流水线后，旧监控根仍被扫描。

- [ ] **Step 3: 性能检查**

- 用 1 万个临时文件扫描本地源，`scan_source` ≤ 10 秒。
- 搜索 P95 ≤ 100 ms（FTS trigram）；2 字查询走 LIKE，≤ 300 ms。
- 目录列表 1000 项 ≤ 200 ms（本地）。

- [ ] **Step 4: 浏览器验收**

按验收清单逐条勾：
- 登录页 1366×768 / 1920×1080 完整；
- 桌面图标按权限显隐；
- 文件树只显示有权限目录；越权 URL 404；
- 搜索边输边出，键盘可选；
- 上传后 5 秒内可搜到，状态从「解析中」变「可搜索」；
- 删除后立刻搜不到，恢复后可再搜到；
- 新接入共享后自动扫描，管理台有进度；
- 预览 PDF/Office/图片正常，左右键切换（若未实现左右切换，记入 P1）。

- [ ] **Step 5: 提交**

```powershell
git add README.md
git commit -m "2.0 P0：使用说明与验收记录"
```

---

## 后续计划（不在本计划内）

- P1：目录树虚拟滚动、拖拽移动/复制、重命名、回收站定时清理、断点续传跨重启、视频 Range。
- P2：AI 智能体统一入口（找文件/查内容/澄清）、引用跳转、外部智能体工具扩展。
- P3：Excel 批量建号、部门批量授权、审计页扩展、模型接入 UI。
- P4：权限视角预览、搜索高亮、移动端适配、安装包与升级脚本。
