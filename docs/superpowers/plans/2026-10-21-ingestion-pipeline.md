# 摄入流水线（引擎层）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把监控目录（`\\192.0.2.19\信息化管理\AI知识库`）里新增或修改的文件，自动变成可被全文检索、可被向量检索、可被 LLM 提炼并建图的知识资产。

**Architecture:** 单进程 Python 服务，SQLite 存元数据与图谱，FTS5 独立文件存全文索引，向量以 fp16 原始文件 + mmap 存。三个阶段解耦：catalog 扫描器只负责"目录里有什么"，monitor 只负责"变了什么"，indexer/extracter 只负责"把一个文件变成什么"。每一层都可独立测试。

**Tech Stack:** Python 3.12.10 · SQLite 3.49.1（FTS5）· numpy · pytest 9.1.1 · 现有 `kb/` 模块（parsers / chunker / llm / graph）

**Spec:** `docs/superpowers/specs/2026-10-21-kb-platform-design.md`

## 计划拆分

本设计横跨 5 个子系统，拆成 4 份计划，每份产出可独立验收的软件：

| 计划 | 范围 | 产出 |
|---|---|---|
| **计划 1（本文件）** | 摄入流水线 | 文件放进目录 → 自动出现在 catalog、FTS 索引、向量库、图谱里 |
| 计划 2 | 检索 + 问答服务 + 账户权限 Token + 对外 API | 一个可登录、可签发 token、可调 `/api/v1/query` 拿到带引用答案的服务 |
| 计划 3 | Web 前端 | 看板 / 图谱 / 问答 / 管理台 四个页面 |
| 计划 4 | WorkBuddy 交付物 | skill 包 + MCP server + 接入文档 |

**本计划只做计划 1。** 计划 2–4 在本计划验收通过后按同一格式编写。

---

## Global Constraints

以下约束来自 spec，**每条任务的隐含要求都包含本节**。违反任何一条都算实现错误。

1. **路径一律 UNC 绝对路径**（`\\192.0.2.19\信息化管理\AI知识库\a.pdf`）。盘符形式（`W:\...`）只允许出现在 `to_unc()` 的入参里。
2. **路径规范化不做 Unicode NFC/NFD 归一**。Windows 与 DSM 都是 NFC，做归一会破坏原路径匹配。
3. **关键词检索必须用 FTS5 + 预写入字符二元组**（`tokenize='unicode61'`，写入前把中文切成单字+相邻二元组）。**不得使用 `trigram`**——实测 trigram 对"信创""审批"这类 2 字中文词全部查不到。
4. **向量必须以 fp16 存盘 + mmap 读**，**不得物化 fp32 矩阵**。
5. **LLM 结构化生成必须用 JSON Schema 约束 + `think=false` + `num_predict >= 2500`**。裸输出实测有 40% 概率是非法 JSON。
6. **生成模型只能用 `qwen3.5-9b-32k` / `qwen3.5-9b-64k`**。默认的 `qwen3.5:9b` runner 上下文只有 4096，会静默截断长文档且不报错。
7. **实体归一必须分批**，不得对全量实体做稠密 `M @ M.T`（50 万实体 = 1 TB 中间矩阵）。
8. **扫描必须限流**，且不得阻塞其他任务。轮询间隔从配置读取。
9. **监控根路径是配置项**（`config.toml` 的 `[monitor] roots`），不得硬编码任何共享名。
10. **可解析白名单固定为**：`.pdf .docx .xlsx .xlsm .pptx .doc .xls .txt .md .ofd`。`kb/parsers.py` 的 `PARSERS` 字典**不是**白名单（它还含 `.dwg` `.js` `.java` `.py` `.html` `.csv` `.json`），只是解析器能力表。

## Review Focus

以下输入/情形是 spec 蕴含但没有任务必然覆盖、最容易在真实使用中咬人的。各行在对应任务里补测试。

| # | 情形 | 期望行为 |
|---|---|---|
| 1 | 用户查 **2 字中文词**（"信创""审批""机房"） | 必须命中。trigram 分词器会静默返回空 |
| 2 | 用户查 **1 个字**（"薪"） | 不崩；无二元组可匹配，应返回空并由调用方降级为向量检索 |
| 3 | 文件**被删除**后 | 其 chunk、向量、FTS 行、knowledge、图谱边必须一并消失，不得残留 |
| 4 | 监控目录里有 **非文档文件**（jpg/mp4/zip） | 不进 catalog、不进索引，只在 `catalog_stats` 计数 |
| 5 | **NAS 断连 / 文件被独占 / 权限不足** | 该文件标记失败并写入 `status_detail`，流水线继续处理下一个文件，不整体卡死 |

---

## 文件结构

```
D:\kb-platform\
├── config.toml                     配置（监控根、轮询间隔、路径、限流）
├── kbplatform\                    本计划新建的包（不叫 platform —— 与标准库冲突）
│   ├── __init__.py
│   ├── config.py                   配置加载
│   ├── paths.py                    UNC 规范化、盘符映射、共享切分
│   ├── db.py                       平台库 schema + 连接
│   ├── fts.py                      FTS5 索引层
│   ├── vectors.py                  fp16 mmap 向量库
│   ├── catalog.py                  目录扫描器
│   ├── monitor.py                  轮询与变更检测
│   ├── jobs.py                     任务队列
│   ├── indexer.py                  Tier 1：解析→分块→FTS→向量
│   ├── extract.py                  Tier 2：LLM 提炼→knowledge→图谱
│   ├── pipeline.py                 编排
│   └── tests\
│       ├── conftest.py
│       ├── test_paths.py
│       ├── test_db.py
│       ├── test_fts.py
│       ├── test_vectors.py
│       ├── test_catalog.py
│       ├── test_monitor.py
│       ├── test_indexer.py
│       ├── test_extract.py
│       └── test_e2e.py
├── kb\                             现有引擎（复用；仅 graph.py 在 Task 8 改）
└── pytest.ini
```

> **包名必须是 `kbplatform`，不能是 `platform`** —— `platform` 是 Python 标准库模块名，会导致 import 冲突。

**边界约定**

| 模块 | 只负责 | 不碰 |
|---|---|---|
| `paths.py` | 路径字符串 | 任何 I/O |
| `db.py` | schema 与连接 | 业务逻辑 |
| `fts.py` | 全文索引读写 | 知道 chunk 是什么（只认 int id + text） |
| `vectors.py` | 向量读写 | 知道 chunk 是什么 |
| `catalog.py` | 目录 → FileRecord | 不写库（写库由调用方） |
| `monitor.py` | 轮询 → Change 列表 | 不做解析 |
| `indexer.py` | 单文件 → chunk + 索引 | 不做 LLM 调用 |
| `extract.py` | chunk → knowledge + 图谱 | 不做检索 |

---

### Task 1: 项目基线、配置与路径规范化

**Files:**
- Create: `config.toml`
- Create: `kbplatform/__init__.py`
- Create: `kbplatform/config.py`
- Create: `kbplatform/paths.py`
- Create: `kbplatform/tests/__init__.py`
- Create: `kbplatform/tests/conftest.py`
- Create: `kbplatform/tests/test_paths.py`
- Create: `kbplatform/tests/test_config.py`
- Create: `pytest.ini`
- Create: `.gitignore`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  ```python
  # platform/paths.py
  def to_unc(path: str, drive_map: dict[str, str]) -> str
  def norm_unc(path: str) -> str
  def split_share(unc: str, roots: list[str]) -> tuple[str, str]   # (share, rel_path)
  def path_key(unc: str) -> str                                     # 比较用 key

  # platform/config.py
  @dataclass
  class Config:
      monitor_roots: list[str]        # UNC 绝对路径
      poll_interval_sec: int
      data_dir: str
      db_path: str
      fts_path: str
      vector_dir: str
      drive_map: dict[str, str]       # {"W:": "\\\\192.0.2.19\\信息化管理"}
      scan_qps: int
      max_workers: int
  def load_config(path: str) -> Config
  ```

- [ ] **Step 1: 初始化 git 仓库并安装依赖**

```bash
cd D:\kb-platform
git init
git config user.email "admin@local"
git config user.name "platform-admin"
```

`C:\Program Files\Git\cmd` 需在 PATH 中（winget 已装 Git 2.55.0，新开 shell 生效）。

- [ ] **Step 2: 写失败的路径测试**

`kbplatform/tests/test_paths.py`：

```python
from platform.paths import to_unc, norm_unc, split_share, path_key

DRIVES = {"W:": "\\\\192.0.2.19\\信息化管理"}


def test_drive_letter_maps_to_unc():
    assert to_unc("W:\\AI知识库\\a.pdf", DRIVES) == \
           "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"


def test_unc_input_is_idempotent():
    u = "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"
    assert to_unc(u, DRIVES) == u


def test_unc_input_without_drive_map_is_still_normalized():
    assert to_unc("\\\\192.0.2.19\\信息化管理\\a.pdf", {}) == \
           "\\\\192.0.2.19\\信息化管理\\a.pdf"


def test_norm_collapses_duplicate_separators():
    assert norm_unc("\\\\192.0.2.19\\\\信息化管理\\\\a.pdf") == \
           "\\\\192.0.2.19\\信息化管理\\a.pdf"


def test_norm_strips_segment_edge_spaces_only():
    assert norm_unc("\\\\192.0.2.19\\ 信息化管理 \\a b.pdf") == \
           "\\\\192.0.2.19\\信息化管理\\a b.pdf"


def test_norm_does_not_unicode_normalize():
    s = "\\\\192.0.2.19\\信息化管理\\AI知识库"
    assert norm_unc(s) == s
    assert unicodedata.normalize("NFC", norm_unc(s)) == norm_unc(s)


def test_split_share_returns_share_and_relpath():
    share, rel = split_share(
        "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf",
        ["\\\\192.0.2.19\\信息化管理"])
    assert share == "信息化管理"
    assert rel == "AI知识库\\a.pdf"


def test_split_share_falls_back_to_first_segment():
    share, rel = split_share("\\\\192.0.2.19\\video\\a.mp4", [])
    assert share == "video"
    assert rel == "a.mp4"


def test_path_key_is_case_insensitive_and_separator_agnostic():
    assert path_key("\\\\192.0.2.19\\Info\\A.PDF") == path_key("\\\\192.0.2.19\\info\\a.pdf")
```

- [ ] **Step 3: 写失败的配置测试**

`kbplatform/tests/test_config.py`：

```python
import pathlib
from platform.config import load_config


def test_load_config_reads_monitor_roots_and_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[monitor]\n'
        'roots = ["\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库"]\n'
        'poll_interval_sec = 30\n'
        'scan_qps = 20\n\n'
        '[paths]\n'
        'data_dir = "D:/kb-platform/kb/data"\n\n'
        '[drives]\n'
        '"W:" = "\\\\\\\\192.0.2.19\\\\信息化管理"\n',
        encoding="utf-8")
    c = load_config(str(cfg))
    assert c.monitor_roots == ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert c.poll_interval_sec == 30
    assert c.scan_qps == 20
    assert c.data_dir == "D:/kb-platform/kb/data"
    assert c.drive_map == {"W:": "\\\\192.0.2.19\\信息化管理"}
    assert c.db_path.endswith("kb.db")
    assert c.fts_path.endswith("fts.db")
    assert c.vector_dir.endswith("vectors")


def test_load_config_applies_defaults(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[monitor]\nroots = []\n', encoding="utf-8")
    c = load_config(str(cfg))
    assert c.poll_interval_sec == 60
    assert c.max_workers == 1
```

- [ ] **Step 4: 运行测试确认失败**

```bash
cd D:\kb-platform
python -m pytest kbplatform/tests/test_paths.py kbplatform/tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform'`

- [ ] **Step 5: 写实现**

`kbplatform/paths.py` 的 `norm_unc` 是唯一需要逐字实现的部分（其余三个由测试完全决定）：

```python
def norm_unc(path: str) -> str:
    """统一分隔符、去掉重复分隔符、每段去首尾空格。**不做 Unicode NFC/NFD 归一**。"""
    if not path:
        return ""
    p = path.replace("/", "\\")
    parts = [seg.strip() for seg in p.split("\\") if seg.strip() != ""]
    return "\\".join(parts)
```

`to_unc` 逻辑：非 `\\` 开头时按 `drive_map` 把盘符前缀替换成 UNC（前缀匹配，忽略大小写），然后交给 `norm_unc`。

`split_share` 逻辑：在 `roots` 里找**最长匹配**的监控根，命中则返回 `(根的最后一段, 剩余相对路径)`；未命中则取 UNC 的第 3 段作共享名。

`path_key` 逻辑：`norm_unc(path).lower()`。

`kbplatform/config.py` 的 `Config` 字段与默认值见上方 Interfaces 块；`load_config` 用标准库 `tomllib` 读 `[monitor]` / `[paths]` / `[drives]` / `[runtime]` 四段，缺省值：`poll_interval_sec=60`、`scan_qps=20`、`max_workers=1`、`data_dir="D:/kb-platform/kb/data"`。`db_path` / `fts_path` / `vector_dir` 在 `__post_init__` 里由 `data_dir` 派生（`kb.db` / `fts.db` / `vectors`）。

`config.toml`：

```toml
[monitor]
# 只监测 AI知识库；加共享时在此追加，架构不变
roots = ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
poll_interval_sec = 60
scan_qps = 20

[paths]
data_dir = "D:/kb-platform/kb/data"

[drives]
"W:" = "\\\\192.0.2.19\\信息化管理"
"U:" = "\\\\192.0.2.19\\综合系统"
"V:" = "\\\\192.0.2.19\\制造物资管理平台开发项目组"
"X:" = "\\\\192.0.2.19\\人力资源共享空间"
"Y:" = "\\\\192.0.2.19\\video"

[runtime]
max_workers = 1
```

`pytest.ini`：

```ini
[pytest]
pythonpath = .
testpaths = kbplatform/tests
python_files = test_*.py
addopts = -q --tb=short
```

`.gitignore`：

```
__pycache__/
*.pyc
.pytest_cache/
kb/data/
_probe/
*.db
*.db-wal
*.db-shm
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd D:\kb-platform
python -m pytest kbplatform/tests/test_paths.py kbplatform/tests/test_config.py -v
```

Expected: PASS，8 + 2 个用例全绿

- [ ] **Step 7: 提交**

```bash
git add .gitignore pytest.ini config.toml kbplatform/
git commit -m "feat: project baseline, config loading, UNC path normalization"
```

---

### Task 2: 平台库 schema 与现有数据迁移

**Files:**
- Create: `kbplatform/db.py`
- Create: `kbplatform/tests/test_db.py`
- Modify: `kbplatform/config.py`（增加 `migrate_existing_paths`）

**Interfaces:**
- Consumes: Task 1 的 `Config.db_path`
- Produces:
  ```python
  # kbplatform/db.py
  SCHEMA_SQL: str
  def connect(db_path: str) -> sqlite3.Connection
  def init_db(db_path: str) -> sqlite3.Connection
  def migrate_paths(conn: sqlite3.Connection, drive_map: dict[str, str]) -> int
      # 把 files.path / chunks.file_path / knowledge.file_path / edges.file_path
      # 从盘符形式改写为 UNC；返回改写行数
  ```

- [ ] **Step 1: 写失败的 schema 测试**

`kbplatform/tests/test_db.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd D:\kb-platform
python -m pytest kbplatform/tests/test_db.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.db'`

- [ ] **Step 3: 实现 `kbplatform/db.py`**

schema 直接照抄 spec §6 的 SQL（`catalog` / `catalog_stats` / `users` / `user_scopes` / `dir_scopes` / `api_tokens` / `agents` / `models` / `wiki_pages` / `jobs` / `audit_log` / `model_usage`），`catalog` 用 spec 里带 `tier1_done` / `tier2_done` 的版本。

`migrate_paths` 的实现要点：

```python
def migrate_paths(conn, drive_map):
    changed = 0
    for table, col in (("catalog", "path"), ("chunks", "file_path"),
                        ("knowledge", "file_path"), ("edges", "file_path"),
                        ("files", "path"), ("wiki_pages", "path")):
        try:
            rows = conn.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        for rowid, p in rows:
            if not p or p.startswith("\\\\"):
                continue
            new = to_unc(p, drive_map)
            if new != p:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, rowid))
                changed += 1
    conn.commit()
    return changed
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_db.py -v
```

Expected: PASS，4 个用例

- [ ] **Step 5: 对真实库执行迁移并核对**

```bash
python -c "import sys; sys.path.insert(0,'.'); from kbplatform.db import connect, migrate_paths; c=connect('D:/kb-platform/kb/data/kb.db'); print('rewritten:', migrate_paths(c, {'W:':'\\\\\\\\192.0.2.19\\\\信息化管理'}))"
```

Expected: 输出 `rewritten: 4`（现有 1 份文档：files 1 行 + chunks 4 行）

- [ ] **Step 6: 提交**

```bash
git add kbplatform/db.py kbplatform/tests/test_db.py
git commit -m "feat: platform database schema and UNC path migration"
```

---

### Task 3: FTS5 全文索引层

**Files:**
- Create: `kbplatform/fts.py`
- Create: `kbplatform/tests/test_fts.py`

**Interfaces:**
- Consumes: Task 1 的 `Config.fts_path`
- Produces:
  ```python
  # kbplatform/fts.py
  def tokenize(text: str) -> list[str]
      # 中文: 单字 + 相邻二元组; 英文/数字: 按词; 全部小写
  def build_index(path: str, rows: Iterable[tuple[int, str]]) -> None
      # rows = (chunk_id, text); rowid 即 chunk_id
  def upsert(path: str, items: Iterable[tuple[int, str]]) -> None
  def delete(path: str, chunk_ids: Iterable[int]) -> None
  def search(path: str, query: str, limit: int = 20) -> list[tuple[int, float]]
      # 返回 (chunk_id, bm25 分数)，分数越小越相关；查询 <2 字符返回 []
  def needs_vector_fallback(query: str) -> bool
      # True 表示该查询无法构成二元组，应降级为向量检索
  ```

- [ ] **Step 1: 写失败的 FTS 测试**

`kbplatform/tests/test_fts.py`：

```python
import pytest
from kbplatform.fts import build_index, search, upsert, delete, tokenize, needs_vector_fallback


def test_tokenize_emits_unigrams_and_bigrams():
    toks = tokenize("数据机房")
    assert "数" in toks and "数据" in toks and "机房" in toks


def test_tokenize_lowercases_latin_and_keeps_digits():
    toks = tokenize("PDF 2026")
    assert "pdf" in toks and "2026" in toks


def test_two_char_chinese_query_matches(tmp_path):
    """Review Focus #1: 2 字中文词必须命中（trigram 会漏）。"""
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "信创资源池由总部统一规划建设和运营运维管理"),
                    (2, "采购评审小组由物资部门使用部门财务部门及外部专家组成")])
    assert [c for c, _ in search(p, "信创", 10)] == [1]
    assert [c for c, _ in search(p, "外部专家", 10)] == [2]


def test_three_char_query_matches(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "三级及以下子企业原则上不允许自建或租赁数据机房"),
                    (2, "无关内容")])
    assert [c for c, _ in search(p, "数据机房", 10)] == [1]


def test_single_char_query_returns_empty_and_signals_fallback(tmp_path):
    """Review Focus #2: 1 字查询不崩，返回空并提示降级。"""
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "薪酬福利")])
    assert search(p, "薪", 10) == []
    assert needs_vector_fallback("薪") is True
    assert needs_vector_fallback("信创") is False


def test_upsert_replaces_existing_row(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "旧内容")])
    upsert(p, [(1, "新内容关键词")])
    assert [c for c, _ in search(p, "新内容关键词"[:4], 10)] == [1]
    assert search(p, "旧内容", 10) == []


def test_delete_removes_rows(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(1, "甲文档内容"), (2, "乙文档内容")])
    delete(p, [1])
    assert search(p, "甲文档", 10) == []
    assert [c for c, _ in search(p, "乙文档", 10)] == [2]


def test_search_result_count_never_exceeds_limit(tmp_path):
    p = str(tmp_path / "fts.db")
    build_index(p, [(i, f"共同关键词文档{i}") for i in range(50)])
    assert len(search(p, "共同关键词", 10)) <= 10
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_fts.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.fts'`

- [ ] **Step 3: 实现 `kbplatform/fts.py`**

分词器与 `kb/query.py` 的 `tokenize` **保持一致**（`CJK = re.compile(r"[一-鿿]")`，`WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")`），但本实现额外提供 `needs_vector_fallback`。

索引写入用 `content=''` 的 contentless 表 + 预分词 token 串：

```python
def _tokens_for_index(text: str) -> str:
    return " ".join(tokenize(text))


def build_index(path: str, rows) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(tok, content='', tokenize='unicode61')")
    con.execute("DELETE FROM ft")
    con.executemany("INSERT INTO ft(rowid, tok) VALUES (?,?)",
                    [(cid, _tokens_for_index(txt)) for cid, txt in rows])
    con.commit()
    con.close()
```

`search` 的 MATCH 表达式用 AND 连接所有 token：

```python
def search(path: str, query: str, limit: int = 20) -> list[tuple[int, float]]:
    toks = tokenize(query)
    if not toks:
        return []
    # 2 字中文词只有 "字" + "字字" 两个 token；单字查询无法构成有效二元组
    cjk = [t for t in toks if len(t) == 1]
    if len(cjk) == len(toks) and len(toks) < 2:
        return []
    expr = " AND ".join(f'"{t}"' for t in toks)
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT rowid, bm25(ft) FROM ft WHERE ft MATCH ? ORDER BY bm25(ft) LIMIT ?",
            (expr, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
```

> **注意**：`bm25()` 返回的分数越小越相关。调用方排序时**升序**。

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_fts.py -v
```

Expected: PASS，7 个用例

- [ ] **Step 5: 对真实 chunk 验证索引体积与查询延迟**

```bash
python -c "import sys,time,sqlite3; sys.path.insert(0,'.'); from kbplatform.fts import build_index, search; c=sqlite3.connect('D:/kb-platform/kb/data/kb.db'); rows=c.execute('SELECT id,text FROM chunks').fetchall(); t=time.time(); build_index('D:/kb-platform/kb/data/fts.db', rows); print('build %.2fs'%(time.time()-t)); t=time.time(); print(search('D:/kb-platform/kb/data/fts.db','信创',5)); print('query %.1fms'%((time.time()-t)*1000))"
```

Expected: build < 0.1s；`信创` 命中 chunk（当前样本含"信创"）；query < 50ms

- [ ] **Step 6: 提交**

```bash
git add kbplatform/fts.py kbplatform/tests/test_fts.py
git commit -m "feat: FTS5 full-text index layer with CJK bigram tokenization"
```

---

### Task 4: fp16 向量库

**Files:**
- Create: `kbplatform/vectors.py`
- Create: `kbplatform/tests/test_vectors.py`

**Interfaces:**
- Consumes: Task 1 的 `Config.vector_dir`
- Produces:
  ```python
  # kbplatform/vectors.py
  class VectorStore:
      def __init__(self, dirpath: str, dim: int = 1024)
      def add(self, ids: list[int], vectors: list[list[float]]) -> None
      def search(self, q: list[float], topk: int = 20) -> list[tuple[int, float]]
          # 返回 (chunk_id, 余弦相似度)，降序
      def remove(self, ids: Iterable[int]) -> None
      def count(self) -> int
      def ids(self) -> list[int]
      def reset(self) -> None
  ```

- [ ] **Step 1: 写失败的向量库测试**

`kbplatform/tests/test_vectors.py`：

```python
import numpy as np
import pytest
from kbplatform.vectors import VectorStore


def test_add_and_search_returns_nearest_first(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1, 2], [[1, 0, 0, 0], [0, 1, 0, 0]])
    hits = vs.search([1, 0, 0, 0], topk=2)
    assert hits[0][0] == 1
    assert hits[0][1] > hits[1][1]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-3)


def test_storage_is_fp16_on_disk(tmp_path):
    """Global Constraint #4: 必须 fp16 存盘。"""
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[0.1, 0.2, 0.3, 0.4]])
    f = tmp_path / "vectors.f16"
    assert f.exists()
    assert f.stat().st_size == 4 * 2


def test_ids_persist_and_reload(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([7, 9], [[1, 0, 0, 0], [0, 1, 0, 0]])
    vs2 = VectorStore(str(tmp_path), dim=4)
    assert vs2.ids() == [7, 9]
    assert vs2.count() == 2


def test_remove_marks_tombstone_and_excludes_from_search(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1, 2], [[1, 0, 0, 0], [0, 1, 0, 0]])
    vs.remove([1])
    assert [i for i, _ in vs.search([1, 0, 0, 0], topk=5)] == [2]
    assert 1 not in vs.ids()


def test_search_handles_zero_vector_without_crash(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[1, 0, 0, 0]])
    assert vs.search([0, 0, 0, 0], topk=5) == [] or vs.search([0, 0, 0, 0], topk=5) is not None


def test_reset_clears_everything(tmp_path):
    vs = VectorStore(str(tmp_path), dim=4)
    vs.add([1], [[1, 0, 0, 0]])
    vs.reset()
    assert vs.count() == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_vectors.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.vectors'`

- [ ] **Step 3: 实现 `kbplatform/vectors.py`**

存储布局（`vector_dir` 下）—— 这是本任务唯一需要执行者自行决策的部分，**布局固定如下**：

| 文件 | 内容 |
|---|---|
| `vectors.f16` | N × dim 的 fp16 矩阵，行序即写入序；**只追加不重排** |
| `vectors.ids.json` | 行序对应的 chunk_id 列表；`-1` 表示已删除的墓碑 |

方法行为（由测试钉死）：

- `add(ids, vectors)`：把 `np.asarray(vectors, dtype=np.float16)` 以 `'ab'` 追加到 `vectors.f16`，把 id 追加到 `vectors.ids.json` 并落盘
- `search(q, topk)`：`np.memmap(vectors.f16, dtype=np.float16, mode="r", shape=(n, dim)).astype(np.float32)` → 逐行 L2 归一 → 与归一化 query 点积 → 按相似度降序 → **跳过墓碑行** → 取 topk。query 范数为 0 时返回空列表
- `remove(ids)`：把对应行在 `vectors.ids.json` 里置为 `-1`，**不改动 `.f16`**（行序必须稳定）
- `count()` / `ids()`：只统计非墓碑
- `reset()`：清空 ids 列表并删除 `.f16`

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_vectors.py -v
```

Expected: PASS，6 个用例

- [ ] **Step 5: 提交**

```bash
git add kbplatform/vectors.py kbplatform/tests/test_vectors.py
git commit -m "feat: fp16 mmap vector store"
```

---

### Task 5: catalog 扫描器

**Files:**
- Create: `kbplatform/catalog.py`
- Create: `kbplatform/tests/test_catalog.py`
- Modify: `kbplatform/db.py`（加 `upsert_catalog` / `set_status` / `get_by_path` / `list_catalog`）

**Interfaces:**
- Consumes: Task 1 `to_unc` / `split_share`；Task 2 `connect` / `init_db`
- Produces:
  ```python
  # kbplatform/catalog.py
  PARSEABLE_EXT: set[str]
  @dataclass
  class FileRecord:
      path: str        # UNC
      share: str
      rel_path: str
      name: str
      ext: str
      size: int
      mtime: float
  def scan_root(root_unc: str, drive_map: dict[str, str], roots: list[str],
                qps: int = 20) -> tuple[list[FileRecord], dict[str, int]]
      # 返回 (可解析文件记录, 非文档文件的 {ext: count})
  def upsert_catalog(conn, fr: FileRecord) -> int
      # 返回 catalog.id；path 已是 UNC
  def set_status(conn, catalog_id: int, status: str, detail: str | None = None) -> None
  def get_by_path(conn, unc: str) -> sqlite3.Row | None
  def list_catalog(conn, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]
  ```

- [ ] **Step 1: 写失败的扫描器测试**

`kbplatform/tests/test_catalog.py`：

```python
import os
import pytest
from kbplatform.catalog import PARSEABLE_EXT, scan_root, upsert_catalog, set_status
from kbplatform.db import init_db


def _mk(tmp_path, name, content=b"x"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_parsetable_whitelist_is_exact():
    assert PARSEABLE_EXT == {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx",
                             ".doc", ".xls", ".txt", ".md", ".ofd"}


def test_scan_root_returns_only_parseable_files(tmp_path):
    """Review Focus #4: 非文档文件不进 catalog。"""
    _mk(tmp_path, "a.pdf")
    _mk(tmp_path, "b.docx")
    _mk(tmp_path, "c.jpg")
    _mk(tmp_path, "d.zip")
    recs, stats = scan_root(str(tmp_path), {}, [], qps=1000)
    assert sorted(r.ext for r in recs) == [".docx", ".pdf"]
    assert stats == {".jpg": 1, ".zip": 1}


def test_scan_root_paths_are_unc(tmp_path):
    _mk(tmp_path, "a.pdf")
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    assert recs[0].path.startswith("\\\\") or recs[0].path.startswith("\\\\")


def test_scan_root_records_size_and_mtime(tmp_path):
    p = _mk(tmp_path, "a.pdf", b"hello")
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    assert recs[0].size == 5
    assert recs[0].mtime == pytest.approx(p.stat().st_mtime, abs=1.0)


def test_scan_root_skips_recycle_and_hidden(tmp_path):
    _mk(tmp_path, "a.pdf")
    _mk(tmp_path, "#recycle/b.pdf")
    _mk(tmp_path, ".hidden/c.pdf")
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    assert [r.name for r in recs] == ["a.pdf"]


def test_upsert_catalog_is_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    i1 = upsert_catalog(conn, recs[0])
    i2 = upsert_catalog(conn, recs[0])
    assert i1 == i2
    assert conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 1


def test_set_status_records_detail(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    set_status(conn, cid, "failed", "文件被独占，无法打开")
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "failed"
    assert "独占" in row[1]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_catalog.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.catalog'`

- [ ] **Step 3: 实现 `kbplatform/catalog.py`**

`scan_root` 的行为（由测试钉死）：

1. `os.walk` 遍历；`dirnames` 里剔除 `#recycle` / `#snapshot` / `@eaDir` / `$RECYCLE.BIN` / `System Volume Information` 与以 `.` 开头的目录
2. 文件名以 `.` 开头的跳过
3. 扩展名不在 `PARSEABLE_EXT` 里的，**只计数**（键为小写扩展名，无扩展名记为 `"(none)"`）后跳过
4. 每个文件 `os.stat` 取 size 与 mtime；`os.stat` 失败则跳过该文件
5. 路径经 `to_unc` 转 UNC，`split_share` 拆出共享名与相对路径
6. **限流**：每个文件之间 `time.sleep(1.0 / qps)`（`qps <= 0` 时不限流）
7. 返回 `(recs, stats)`

`kbplatform/db.py` 追加 `upsert_catalog`（`ON CONFLICT(path) DO UPDATE` 共享/相对路径/大小/mtime，`status` 新行置 `'pending'`）、`set_status(conn, catalog_id, status, detail=None)`、`get_by_path(conn, unc)`、`list_catalog(conn, status=None, limit=100)`。

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_catalog.py -v
```

Expected: PASS，7 个用例

- [ ] **Step 5: 对真实 NAS 目录扫描验证**

```bash
python -c "import sys; sys.path.insert(0,'.'); from kbplatform.catalog import scan_root; r,s=scan_root('W:\\\\AI知识库', {'W:':'\\\\\\\\192.0.2.19\\\\信息化管理'}, ['\\\\\\\\192.0.2.19\\\\信息化管理'], qps=1000); print('parseable:', [(x.name,x.ext,x.size) for x in r]); print('non-document stats:', s)"
```

Expected: `parseable` 至少包含 `附件：某公司信息化管理规定.pdf`（.pdf, 258881 字节）；`non-document stats` 不为空

- [ ] **Step 6: 提交**

```bash
git add kbplatform/catalog.py kbplatform/tests/test_catalog.py kbplatform/db.py
git commit -m "feat: catalog scanner with parseable whitelist and non-document stats"
```

---

### Task 6: 监控器与任务队列

**Files:**
- Create: `kbplatform/monitor.py`
- Create: `kbplatform/jobs.py`
- Create: `kbplatform/tests/test_monitor.py`

**Interfaces:**
- Consumes: Task 5 `scan_root` / `FileRecord`；Task 2 `connect`
- Produces:
  ```python
  # kbplatform/monitor.py
  @dataclass
  class Change:
      path: str      # UNC
      kind: str      # "added" | "modified" | "removed"
      size: int
      mtime: float
  class Monitor:
      def __init__(self, conn, roots: list[str], drive_map: dict[str, str],
                   qps: int = 20, poll_interval: int = 60)
      def poll_once(self) -> list[Change]
      def run_forever(self, on_change) -> None

  # kbplatform/jobs.py
  @dataclass
  class Job:
      id: int
      kind: str          # "index" | "extract"
      catalog_id: int
      state: str
      error: str | None
  class JobQueue:
      def __init__(self, conn)
      def enqueue(self, kind: str, catalog_id: int) -> int
      def claim(self) -> Job | None
      def finish(self, job_id: int, error: str | None = None) -> None
      def requeue_stale(self) -> int
      def pending_count(self) -> int
  ```

- [ ] **Step 1: 写失败的监控器测试**

`kbplatform/tests/test_monitor.py`：

```python
import os
import time
import pytest
from kbplatform.monitor import Monitor
from kbplatform.db import init_db


def _mk(d, name, content=b"x"):
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_added_file_detected_on_next_poll(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["added"]
    assert changes[0].path.endswith("a.pdf")


def test_unchanged_file_produces_no_change(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    m.poll_once()
    assert m.poll_once() == []


def test_modified_file_detected_by_size_or_mtime(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    p = _mk(tmp_path, "a.pdf", b"a")
    m.poll_once()
    time.sleep(1.1)
    p.write_bytes(b"bbbb")
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["modified"]


def test_removed_file_detected(tmp_path):
    """Review Focus #3: 删除必须被发现，后续由 indexer 清理索引。"""
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    p = _mk(tmp_path, "a.pdf")
    m.poll_once()
    p.unlink()
    changes = m.poll_once()
    assert [c.kind for c in changes] == ["removed"]


def test_change_paths_are_unc(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    m = Monitor(conn, [str(tmp_path)], {}, qps=1000)
    _mk(tmp_path, "a.pdf")
    assert m.poll_once()[0].path.startswith("\\\\")


def test_job_queue_claim_and_finish(tmp_path):
    from kbplatform.jobs import JobQueue
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    jid = q.enqueue("index", 42)
    job = q.claim()
    assert job.id == jid and job.kind == "index" and job.catalog_id == 42
    q.finish(jid)
    assert q.claim() is None
    assert q.pending_count() == 0


def test_job_queue_marks_failure(tmp_path):
    from kbplatform.jobs import JobQueue
    conn = init_db(str(tmp_path / "kb.db"))
    q = JobQueue(conn)
    jid = q.enqueue("index", 7)
    q.claim()
    q.finish(jid, error="文件被独占")
    row = conn.execute("SELECT state, error FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row[0] == "failed"
    assert "独占" in row[1]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_monitor.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.monitor'`

- [ ] **Step 3: 实现 `kbplatform/monitor.py` 与 `kbplatform/jobs.py`**

`monitor.py` 的 `poll_once` 逻辑：调用 `scan_root` 拿到当前快照，与上次快照（存在内存 dict）逐项比对：

- 新出现的 path → `added`
- 已存在但 size 或 mtime 变了 → `modified`
- 上次有、这次没有 → `removed`

快照存内存即可（服务重启后首轮全部视为 `added`，幂等由 indexer 保证）。

`jobs.py` 的 `claim` 用一条 UPDATE 原子取：

```python
def claim(self):
    row = self.conn.execute(
        "SELECT id, kind, catalog_id, state, error FROM jobs "
        "WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    self.conn.execute("UPDATE jobs SET state='running', started_at=? WHERE id=?",
                       (time.time(), row[0]))
    self.conn.commit()
    return Job(row[0], row[1], row[2], row[3], row[4])
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_monitor.py -v
```

Expected: PASS，7 个用例

- [ ] **Step 5: 提交**

```bash
git add kbplatform/monitor.py kbplatform/jobs.py kbplatform/tests/test_monitor.py
git commit -m "feat: polling monitor with change detection and job queue"
```

---

### Task 7: Tier 1 索引器

**Files:**
- Create: `kbplatform/indexer.py`
- Create: `kbplatform/tests/test_indexer.py`
- Modify: `kbplatform/db.py`（加 `replace_chunks` / `add_vectors` / `delete_chunks_of` / `get_chunk_ids`）

**Interfaces:**
- Consumes: Task 3 `fts`；Task 4 `VectorStore`；Task 5 `catalog`；现有 `kb.parsers` / `kb.chunker`
- Produces:
  ```python
  # kbplatform/indexer.py
  @dataclass
  class IndexResult:
      catalog_id: int
      chunk_ids: list[int]
      ok: bool
      error: str | None
  def index_file(conn, fts_path: str, vs: VectorStore, catalog_id: int,
                 unc_path: str) -> IndexResult
  def remove_file(conn, fts_path: str, vs: VectorStore, catalog_id: int) -> None
  def process_changes(conn, fts_path: str, vs: VectorStore,
                      changes: list[Change]) -> list[IndexResult]
  ```

- [ ] **Step 1: 写失败的索引器测试**

`kbplatform/tests/test_indexer.py`：

```python
import pytest
from kbplatform.db import init_db
from kbplatform.catalog import scan_root, upsert_catalog
from kbplatform.fts import build_index, search
from kbplatform.vectors import VectorStore
from kbplatform.indexer import index_file, remove_file


def _setup(tmp_path, name="a.md", content=None):
    p = tmp_path / name
    p.write_text(content or ("信创资源池由总部统一规划建设。\n" * 200), encoding="utf-8")
    conn = init_db(str(tmp_path / "kb.db"))
    recs, _ = scan_root(str(tmp_path), {}, [], qps=1000)
    cid = upsert_catalog(conn, recs[0])
    fts_path = str(tmp_path / "fts.db")
    build_index(fts_path, [])
    vs = VectorStore(str(tmp_path / "vectors"), dim=1024)
    return conn, fts_path, vs, cid, str(p)


def test_index_file_creates_chunks(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    res = index_file(conn, fts_path, vs, cid, path)
    assert res.ok is True
    assert len(res.chunk_ids) >= 1
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(res.chunk_ids)


def test_index_file_populates_fts(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    res = index_file(conn, fts_path, vs, cid, path)
    hits = search(fts_path, "信创", 10)
    assert len(hits) >= 1


def test_index_file_populates_vectors(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    res = index_file(conn, fts_path, vs, cid, path)
    assert vs.count() == len(res.chunk_ids)


def test_index_file_marks_tier1_done(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    index_file(conn, fts_path, vs, cid, path)
    row = conn.execute("SELECT tier1_done, status FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == 1
    assert row[1] == "done"


def test_reindex_replaces_old_chunks(tmp_path):
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    index_file(conn, fts_path, vs, cid, path)
    first = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    index_file(conn, fts_path, vs, cid, path)
    second = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert first == second


def test_index_failure_records_reason_and_does_not_raise(tmp_path):
    """Review Focus #5: 失败要有原因，流水线不崩。"""
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file
    res = index_file(conn, fts_path, vs, cid, str(tmp_path / "missing.md"))
    assert res.ok is False
    assert res.error
    row = conn.execute("SELECT status, status_detail FROM catalog WHERE id=?", (cid,)).fetchone()
    assert row[0] == "failed"
    assert row[1]


def test_remove_file_clears_chunks_fts_and_vectors(tmp_path):
    """Review Focus #3: 删除后不得残留。"""
    conn, fts_path, vs, cid, path = _setup(tmp_path)
    from kbplatform.indexer import index_file, remove_file
    res = index_file(conn, fts_path, vs, cid, path)
    remove_file(conn, fts_path, vs, cid)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE file_path=?", (path,)).fetchone()[0] == 0
    assert search(fts_path, "信创", 10) == []
    assert vs.count() == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_indexer.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.indexer'`

- [ ] **Step 3: 实现 `kbplatform/indexer.py`**

核心流程：

```python
def index_file(conn, fts_path, vs, catalog_id, unc_path):
    try:
        blocks, meta = kb.parsers.parse_file(unc_path)
    except Exception as e:
        return _fail(conn, catalog_id, f"解析失败: {type(e).__name__}: {e}")
    if not blocks or sum(len(b.text) for b in blocks) < 30:
        return _fail(conn, catalog_id, "文件无有效文本")
    try:
        chunks = kb.chunker.chunk_blocks(blocks)
        chunk_ids = replace_chunks(conn, unc_path, chunks)
        texts = [c["text"] for c in chunks]
        fts.upsert(fts_path, list(zip(chunk_ids, texts)))
        vecs = kb.llm.embed(texts) if texts else []
        if vecs:
            vs.add(chunk_ids, vecs)
        set_status(conn, catalog_id, "done")
        conn.execute("UPDATE catalog SET tier1_done=1, chunk_count=?, scanned=?, needs_ocr=? WHERE id=?",
                     (len(chunk_ids), 1 if meta.get("need_ocr") else 0,
                      1 if meta.get("need_ocr") else 0, catalog_id))
        conn.commit()
        return IndexResult(catalog_id, chunk_ids, True, None)
    except Exception as e:
        return _fail(conn, catalog_id, f"索引失败: {type(e).__name__}: {e}")
```

`_fail` 把 `status='failed'` 与 `status_detail` 写进 catalog 并返回 `IndexResult(ok=False)`，**绝不向上抛异常**。

`remove_file` 删除该文件的 chunks（连带 vectors 行）、FTS 行、向量、knowledge 与 edges。

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_indexer.py -v
```

Expected: PASS，7 个用例

- [ ] **Step 5: 提交**

```bash
git add kbplatform/indexer.py kbplatform/tests/test_indexer.py kbplatform/db.py
git commit -m "feat: tier-1 indexer (parse, chunk, FTS, vectorize)"
```

---

### Task 8: Tier 2 提炼与建图

**Files:**
- Create: `kbplatform/extract.py`
- Create: `kbplatform/tests/test_extract.py`
- Modify: `kb/graph.py`（`canonicalize_entities` 改分批；`merge_edges` 改增量 upsert）

**Interfaces:**
- Consumes: Task 7 的 chunk；现有 `kb.llm` / `kb.graph`
- Produces:
  ```python
  # kbplatform/extract.py
  @dataclass
  class ExtractResult:
      catalog_id: int
      key_points: int
      relations: int
      ok: bool
      error: str | None
  def extract_file(conn, catalog_id: int, unc_path: str,
                  chunk_ids: list[int], model: str = "qwen3.5-9b-64k") -> ExtractResult
  def canonicalize_pending(conn, batch: int = 2000) -> int
      # 分批处理待归一实体，返回处理数
  ```

- [ ] **Step 1: 写失败的提炼测试**

`kbplatform/tests/test_extract.py`：

```python
import json
import pytest
from kbplatform.db import init_db


FAKE = {
    "summary": "测试文档摘要",
    "key_points": [{"title": "数据机房建设管控", "detail": "三级以下不允许自建",
                    "entities": ["数据机房", "三级及以下子企业"], "category": "标准"}],
    "relations": [{"from": "三级及以下子企业", "to": "数据机房", "relation": "原则上不允许"}],
}


def _seed(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO chunks(file_path, chunk_idx, text) VALUES ('\\\\a.pdf',0,'内容')")
    conn.commit()
    return conn, conn.execute("SELECT id FROM chunks").fetchone()[0]


def test_extract_writes_knowledge_and_graph(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid = _seed(tmp_path)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    res = ex.extract_file(conn, 1, "\\\\a.pdf", [cid])
    assert res.ok is True
    assert res.key_points == 1
    assert res.relations == 1
    assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
    nodes = conn.execute("SELECT name, type FROM nodes").fetchall()
    assert any(r[0] == "数据机房" and r[1] == "entity" for r in nodes)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] >= 1


def test_extract_marks_tier2_done(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid = _seed(tmp_path)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))
    ex.extract_file(conn, 1, "\\\\a.pdf", [cid])
    assert conn.execute("SELECT tier2_done FROM catalog WHERE id=1").fetchone()[0] == 1


def test_extract_passes_think_false_and_num_predict_2500(tmp_path, monkeypatch):
    """Global Constraint #5: 结构化生成必须 think=false + num_predict>=2500。"""
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid = _seed(tmp_path)
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return FAKE, {"wall": 0.1}
    monkeypatch.setattr(llm, "chat_json", spy)
    ex.extract_file(conn, 1, "\\\\a.pdf", [cid])
    assert seen.get("think") is False
    assert seen.get("num_predict", 0) >= 2500


def test_extract_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    import kb.llm as llm
    import kbplatform.extract as ex
    conn, cid = _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("结构化输出失败")
    monkeypatch.setattr(llm, "chat_json", boom)
    res = ex.extract_file(conn, 1, "\\\\a.pdf", [cid])
    assert res.ok is False
    assert "结构化输出" in res.error


def test_canonicalize_pending_handles_empty(tmp_path):
    from kbplatform.extract import canonicalize_pending
    conn = init_db(str(tmp_path / "kb.db"))
    assert canonicalize_pending(conn) == 0
```

> **monkeypatch 目标**：`kb.llm.chat_json`。`kb/graph.py` 里是 `from . import llm`（模块引用），所以 patch 模块属性对两处都生效。**不要** patch `ex.kb.llm.chat_json` —— `extract.py` 里是 `import kb.llm as llm`，没有 `ex.kb` 这个属性。

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_extract.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.extract'`

- [ ] **Step 3: 实现 `kbplatform/extract.py`**

```python
"""Tier 2：LLM 提炼 -> knowledge -> 图谱。"""
from dataclasses import dataclass

import kb.llm as llm
import kb.graph as graph
from kb.llm import EXTRACTION_SCHEMA

EXTRACT_PROMPT = """你是企业知识库构建助手。阅读下面的文档片段，抽取其中的知识点。

要求：
1. key_points 抽取 5-10 条，覆盖该片段的主要规定、流程、标准、职责；
2. title 是凝练的知识点标题（不超过 30 字），detail 是具体说明（保留数字、金额、比例、时限等关键事实）；
3. entities 只保留文档中真实出现的专有名词（部门、岗位、术语、系统名称、文件名称），不要臆造；
4. category 从 制度/流程/标准/职责/数据/组织/其它 中选最贴切的一个；
5. relations 抽取片段中明确表述的实体关系（from/to 用文档里的原词）。

文档片段：
---
{chunk}
---"""


@dataclass
class ExtractResult:
    catalog_id: int
    key_points: int
    relations: int
    ok: bool
    error: str | None
```

`extract_file` 遍历 `chunk_ids`，逐块调用 `llm.chat_json(EXTRACT_PROMPT.format(chunk=...), schema=EXTRACTION_SCHEMA, model=model, num_predict=2500, think=False)`，写 `knowledge` 表，调 `graph.build_from_extraction`，最后置 `tier2_done=1`。

**注意**：`llm.chat_json` 必须显式传 `think=False`（Global Constraint #5）。

- [ ] **Step 4: 改造 `kb/graph.py` 的两处**

| 改造 | 要求 |
|---|---|
| `canonicalize_entities` | **分批**：每次取最多 2000 个未归一实体名做 `llm.embed`，算批内 `M @ M.T`，候选对送 LLM 复核；**禁止**把全量实体一次性送进 embed。合并用 `nodes.merged_to` 软链，不删记录 |
| `merge_edges` | 从 `DELETE FROM edges` 全表重写改为**增量 upsert**：`(src,dst,rel)` 已存在则 `weight += 1`，否则插入。**必须保留 `chunk_id`**（`kb/graph.py` 原实现把它置空，会导致图谱扩展完全失效） |

- [ ] **Step 5: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_extract.py -v
```

Expected: PASS，5 个用例

- [ ] **Step 6: 回归测试现有引擎未被破坏**

```bash
python -m pytest kbplatform/tests/ -v
python kb\test_qa.py
```

Expected: 全部 PASS；`test_qa.py` 仍能对 `W:\AI知识库` 里的 PDF 回答问题

- [ ] **Step 7: 提交**

```bash
git add kbplatform/extract.py kbplatform/tests/test_extract.py kb/graph.py
git commit -m "feat: tier-2 extraction and batched entity canonicalization"
```

---

### Task 9: 端到端验证

**Files:**
- Create: `kbplatform/tests/test_e2e.py`
- Create: `kbplatform/pipeline.py`（编排：monitor → jobs → indexer → extract）
- Modify: `config.toml`

**Interfaces:**
- Consumes: Task 6 `Monitor` / `JobQueue`；Task 7 `index_file`；Task 8 `extract_file`
- Produces:
  ```python
  # kbplatform/pipeline.py
  class Pipeline:
      def __init__(self, config: Config)
      def run_once(self) -> dict          # 返回本轮处理统计
      def run_forever(self) -> None
  ```

- [ ] **Step 1: 写端到端测试**

`kbplatform/tests/test_e2e.py`：

```python
import pytest
from kbplatform.config import Config
from kbplatform.pipeline import Pipeline


def test_pipeline_processes_new_file_end_to_end(tmp_path, monkeypatch):
    cfg = Config(monitor_roots=[str(tmp_path)], data_dir=str(tmp_path / "data"),
                 poll_interval_sec=1, scan_qps=1000)
    (tmp_path / "a.md").write_text("信创资源池由总部统一规划建设。\n" * 100, encoding="utf-8")

    import kb.llm as llm
    import kbplatform.extract as ex
    FAKE = {"summary": "s",
            "key_points": [{"title": "t", "detail": "d", "entities": ["信创"], "category": "标准"}],
            "relations": []}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (FAKE, {"wall": 0.1}))

    p = Pipeline(cfg)
    stats = p.run_once()
    assert stats["indexed"] == 1
    assert stats["extracted"] >= 1
```

> `Pipeline.run_once` 必须**同步跑完**本轮（供测试断言），`run_forever` 才是循环版。

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_e2e.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.pipeline'`

- [ ] **Step 3: 实现 `kbplatform/pipeline.py`**

`Pipeline.__init__` 建立：`self.conn = init_db(config.db_path)`、`self.vs = VectorStore(config.vector_dir, dim=1024)`、`self.queue = JobQueue(self.conn)`、`self.monitor = Monitor(self.conn, config.monitor_roots, config.drive_map, qps=config.scan_qps, poll_interval=config.poll_interval_sec)`。**FTS 层直接用模块级函数 `fts.build_index` / `fts.upsert` / `fts.search`，不引入包装类。**

`run_once()` 流程（**必须同步跑完本轮**，供测试断言）：

1. 对每个监控根调 `catalog.scan_root` → `upsert_catalog` 落库
2. 调 `monitor.poll_once()` 得到 changes
3. `added` / `modified` → `queue.enqueue("index", catalog_id)`
4. `removed` → `indexer.remove_file(...)` 并清 knowledge / edges
5. 循环 `queue.claim()` 直到空：kind=`index` → `indexer.index_file(...)`；成功后 `queue.enqueue("extract", catalog_id)`
6. 再循环 claim 到空：kind=`extract` → `extract.extract_file(...)`
7. 返回统计 dict，键固定为 `{"indexed": int, "extracted": int, "removed": int, "failed": int}`

`run_forever()`：循环调用 `run_once()`，两次之间 `time.sleep(config.poll_interval_sec)`；单次 `run_once` 抛异常时**记录并继续**（不退出循环）。

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_e2e.py -v
```

Expected: PASS

- [ ] **Step 5: 对真实 NAS 目录跑一轮并核对**

```bash
python -c "import sys; sys.path.insert(0,'.'); from kbplatform.config import load_config; from kbplatform.pipeline import Pipeline; c=load_config('config.toml'); p=Pipeline(c); print(p.run_once())"
```

Expected: `{'indexed': 1, 'extracted': 1, ...}`（对应当前 `W:\AI知识库` 里的那份 PDF）

- [ ] **Step 6: 核对结果**

```bash
python -c "import sqlite3; c=sqlite3.connect('D:/kb-platform/kb/data/kb.db'); print('catalog:', c.execute('SELECT name, status, tier1_done, tier2_done, chunk_count, kp_count FROM catalog').fetchall()); print('knowledge:', c.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]); print('nodes:', c.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]); print('edges:', c.execute('SELECT COUNT(*) FROM edges').fetchone()[0])"
```

Expected: catalog 有 1 行且 `status='done'`、`tier1_done=1`、`tier2_done=1`；knowledge ≥ 1；nodes ≥ 1；edges ≥ 0

- [ ] **Step 7: 提交**

```bash
git add kbplatform/pipeline.py kbplatform/tests/test_e2e.py config.toml
git commit -m "feat: ingestion pipeline orchestration"
```

---

### Task 10: OCR 与老格式转换（spec 决策 D9）

**Files:**
- Create: `kbplatform/converters.py`
- Create: `kbplatform/tests/test_converters.py`
- Modify: `kbplatform/indexer.py`（在 `parse_file` 之前走转换/OCR 分支）

**Interfaces:**
- Consumes: Task 7 `index_file`；现有 `kb.parsers`
- Produces:
  ```python
  # kbplatform/converters.py
  LEGACY_EXT: set[str]                     # {".doc", ".ppt", ".xls"}
  def soffice_path() -> str | None          # 找到 soffice.exe；找不到返回 None
  def convert_legacy(unc_path: str, out_dir: str) -> str | None
      # 老格式 -> 转换后的现代格式路径；转换器不可用或失败返回 None
  def ocr_pdf(unc_path: str, out_dir: str) -> str | None
      # 扫描件 PDF -> 纯文本文件路径；PaddleOCR 不可用或失败返回 None
  def available() -> dict[str, bool]        # {"soffice": bool, "ocr": bool}
  ```

- [ ] **Step 1: 写失败的转换器测试**

`kbplatform/tests/test_converters.py`：

```python
import pytest
from kbplatform.converters import LEGACY_EXT, available, convert_legacy, ocr_pdf


def test_legacy_ext_set_is_exact():
    assert LEGACY_EXT == {".doc", ".ppt", ".xls"}


def test_available_reports_booleans():
    a = available()
    assert set(a) == {"soffice", "ocr"}
    assert all(isinstance(v, bool) for v in a.values())


def test_convert_legacy_returns_none_when_converter_unavailable(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "soffice_path", lambda: None)
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    assert c.convert_legacy(str(p), str(tmp_path / "out")) is None


def test_ocr_returns_none_when_ocr_unavailable(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "ocr_available", lambda: False)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert c.ocr_pdf(str(p), str(tmp_path / "out")) is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest kbplatform/tests/test_converters.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'kbplatform.converters'`

- [ ] **Step 3: 安装 LibreOffice 与 PaddleOCR**

```bash
winget install --id TheDocumentFoundation.LibreOffice -e --accept-package-agreements --accept-source-agreements
python -m pip install paddlepaddle paddleocr
```

> LibreOffice 装完 `soffice.exe` 默认在 `C:\Program Files\LibreOffice\program\soffice.exe`。
> PaddleOCR 首次运行会下载中文模型（约 15 MB，落在 `%USERPROFILE%\.paddleocr`）。**若下载失败，`available()["ocr"]` 返回 False，流水线把扫描件标为"待 OCR 支持"而不是崩溃。**

- [ ] **Step 4: 实现 `kbplatform/converters.py`**

| 函数 | 行为 |
|---|---|
| `soffice_path()` | 在 `C:\Program Files\LibreOffice\program\soffice.exe` 与 PATH 里找；找不到返回 `None` |
| `convert_legacy(unc, out_dir)` | 调 `soffice --headless --convert-to <目标格式> --outdir <out_dir> <unc>`，超时 120s；失败或 `soffice_path()` 为 `None` 时返回 `None` |
| `ocr_available()` | 探测 `paddleocr` 是否可 import 且模型目录存在 |
| `ocr_pdf(unc, out_dir)` | 用 PaddleOCR 逐页识别，输出 `.txt`；不可用时返回 `None` |
| `available()` | 返回 `{"soffice": soffice_path() is not None, "ocr": ocr_available()}` |

- [ ] **Step 5: 在 `indexer.py` 里接上分支**

`index_file` 的解析顺序改为：

1. `ext` 在 `LEGACY_EXT` → 先 `convert_legacy`，成功则用转换结果解析，失败则记 `status_detail="老格式转换失败（LibreOffice 不可用或转换出错）"`
2. `ext == ".pdf"` 且 `kb.parsers.parse_pdf` 返回 `need_ocr=True` → 先 `ocr_pdf`，成功则用 OCR 文本解析，失败则记 `status_detail="扫描件待 OCR 支持"`
3. 其余走原路径

- [ ] **Step 6: 运行测试确认通过**

```bash
python -m pytest kbplatform/tests/test_converters.py -v
python -m pytest kbplatform/tests/ -v
```

Expected: 全部 PASS

- [ ] **Step 7: 验证实际转换能力（人工确认，样本取自 NAS）**

```bash
python -c "import sys; sys.path.insert(0,'.'); from kbplatform.converters import available; print(available())"
```

Expected: `{'soffice': True, 'ocr': True}`（若 OCR 模型下载失败则为 `{'soffice': True, 'ocr': False}`，**不阻塞流水线**）

- [ ] **Step 8: 提交**

```bash
git add kbplatform/converters.py kbplatform/tests/test_converters.py kbplatform/indexer.py
git commit -m "feat: legacy format conversion and OCR pipeline"
```

---

## 验收标准

本计划完成时，以下全部成立：

```bash
cd D:\kb-platform
python -m pytest kbplatform/tests/ -v          # 全部通过
python kb\test_qa.py                           # 现有问答仍工作
```

| # | 标准 |
|---|---|
| 1 | `kbplatform/tests/` 下全部测试通过 |
| 2 | 真实 NAS 目录 `\\192.0.2.19\信息化管理\AI知识库` 里的 PDF 被自动 catalog → index → extract，catalog 行 `status='done'`、`tier1_done=1`、`tier2_done=1` |
| 3 | FTS 索引能命中 2 字中文词（"信创"） |
| 4 | 向量以 fp16 存盘（`vectors.f16` 大小 = 行数 × 1024 × 2） |
| 5 | 删除文件后，其 chunk / 向量 / FTS 行 / knowledge 全部消失 |
| 6 | 解析失败的文件在 catalog 里有 `status='failed'` 与具体 `status_detail`，且不影响其他文件 |
| 7 | `kb/` 现有模块的问答能力未被破坏 |
| 8 | `git status` 干净，提交历史按任务切分 |
| 9 | `available()` 返回 `soffice: True`；OCR 不可用时流水线标记"待 OCR 支持"而不崩溃 |

## 后续

本计划验收通过后，按同一格式编写：

- **计划 2**：检索层（FTS5 + 向量 + 图谱扩展 + wiki 四路召回，全部过 ACL）+ 问答服务 + 账户权限 Token + 对外 API + **服务部署**（spec §8 的 Windows 服务/计划任务开机自启归入本计划，因为服务进程在本计划产生）
- **计划 3**：Web 前端（看板 / 图谱 / 问答 / 管理台）
- **计划 4**：WorkBuddy 交付物（skill 包 + MCP server + 接入文档）
