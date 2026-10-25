# 企业知识平台 2.0 迭代计划（迭代 0 → 迭代 3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把平台从"能搜到文件"推进到"能被信任、能被日常使用"——先止血清账，再把检索做准，最后做协作与自动化。

**Architecture:** 不重写。沿用现有的 FastAPI + 原生 JS 单页 + SQLite/向量库 + supervisor 自愈三件套。每个迭代是一个可独立验收、独立发布的小版本；每个任务自带 TDD 循环与独立提交。AI 能力一律复用已有的 4 路召回、bge-m3 向量、算力池、目录授权（grants），不引入新的模型服务与新的前端构建链。

**Tech Stack:** Python 3.12 / FastAPI / SQLite（WAL）/ FTS5 / bge-m3（Ollama，1024 维 fp16）/ PaddleOCR / Cytoscape.js / 原生 ES-module 风格 JS（无构建）

**Spec:** `docs/superpowers/specs/2026-10-25-kb-platform-ux-review-and-roadmap.md`（本计划实现该文档的迭代 0 / 1，迭代 2 / 3 只列目标）

---

## Global Constraints

- **不改授权模型**：`grants` 是唯一授权源；文件页、看板、检索、问答共用 `scope_predicate`。任何新接口必须过同一套过滤。
- **不引入新的前端构建步骤**：`web/static/*.js` 保持原生脚本，改完必须能过 `kbplatform/tests/test_frontend_js_syntax.py`（esprima 真解析）。
- **不引入新的 Python 依赖**：能用标准库 + 已有依赖（fastapi/pydantic/numpy/httpx…）就不装新的。
- **不引入新的模型服务**：embedding 固定 bge-m3，生成模型走现有 `models` 表 + 算力池；批量 AI 任务只用算力池（本地 + `192.0.2.77`），**不得上云端**。
- **删除一律可恢复**：文件删除走平台回收站（`fs_service.move_to_trash`）；任何"清理"脚本不得物理删除 NAS 原文件。
- **会话密钥**：会话签名密钥用 `KB_SESSION_SECRET` 环境变量；`config.toml` 的 `session_secret` 是 **AES 主密钥**（token 密文 + SMB 密码解密），**只可改变存储方式，不可改变其值**（改了会废掉所有密钥的明文恢复与 5 个共享的 SMB 认证）。
- **涉密规则不可绕过**：命中涉密目录的内容一律不回传原文；**新增的会话历史、多轮 prompt 拼装、wiki 通道、轨迹持久化都不得把涉密原文带出去**。
- **提交约定**：`git commit -F <utf8-no-BOM 文件>`；PowerShell 脚本必须带 UTF-8 BOM；提交信息用中文写清「改了什么 / 为什么 / 怎么验证」。
- **测试约定**：`pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py` 必须全绿（该文件是外部依赖的飞书联调，需要单独环境）。

## Review Focus

以下五类输入/情形最可能伤到真实用户，每一条都要有对应的测试钉住：

1. **普通用户问了一个只在他无权目录里出现的词**——答案与引用里不能出现范围外文件名，也不能通过"引用卡片""轨迹面板""相似度分数"侧漏路径。→ 归 Task 0.3 / 0.5 / 0.7。
2. **一个块超过 embedding 单次输入上限（2,048 token）**——不能整篇文档的向量全丢，也不能静默丢（必须有可查的失败原因）。→ 归 Task 0.2。
3. **平台重启 / 流水线重启**——会话是否被踢、监控快照是否把"不在监控范围内的行"标成"源文件已删除"、待办任务是否重复入队。→ 归 Task 0.1 / 0.6。
4. **涉密内容进入多轮对话**——上一轮答案里的涉密原文不能出现在下一轮 prompt 里；涉密文档不能因为"人工校正层/缓存/轨迹"而泄露原文。→ 归 Task 0.7 / 迭代 1 Task 1.5。
5. **用户点了"这条不对"**——反馈必须真的落库并能被管理员看到；不能因为一次失败的写入让回答页面报错。→ 归 Task 0.3。

---

## 迭代总览

| 迭代 | 周期 | 目标 | 任务数 | 出口标准 |
|---|---|---|---|---|
| **迭代 0 · 止血与清账** | 第 1 周 | 台账可信、坏文件修好、能收反馈、有备份、堵上越权口子 | 7 | 看板"已跳过"不再含平台内部文件；4 篇制度文件可搜；文件页能搜正文；答案页能反馈；索引有可恢复备份 |
| **迭代 1 · 把"准"做出来** | 第 2–3 周 | 问答从"能答"到"能信" | 5 | 重排+改写上线；引用带真实相似度与原文定位；能追问；每篇文档有摘要与标签 |
| 迭代 2 · 治治理 | 第 4–6 周 | 台账干净、整理可自动化 | （见 §迭代 2） | 近似重复带版本差异说明；批量归类可用；告警通道打通 |
| 迭代 3 · 协作与自动化 | 第 7 周起 | 平台成为日常工作台 | （见 §迭代 3） | 收藏/最近打开；评论@与订阅；人工校正闭环；自动简报 |

**依赖关系**

```
迭代 0（反馈埋点 + 修数据 + 补安全口子）
   ├──► 迭代 1（按反馈数据决定重排/改写的力度）
   └──► 迭代 2（台账干净了才谈得上治理）
                    └──► 迭代 3（协作 / agent / 人工校正闭环）
```

**迭代 0 不能跳**：没有反馈数据就调检索等于闭着眼睛调。

## 决策门（到期不拍板就按"建议值"执行）

| # | 决策 | 需要谁 | 截止 | 建议默认值 |
|---|---|---|---|---|
| G1 | 涉密的 8 个目录标记是否属实 | 业务负责人 | 迭代 0 结束前 | 视为属实；**在放开索引范围前必须先有管理台入口** |
| G2 | 是否安排一次维护窗口做**全局重切块**（≈2,800 → ≈1,000 字符） | 决策者 | 迭代 1 开始前 | **暂不做**：先做按块嵌入（迭代 0 Task 0.2），全局重切块单独立项，代价是全部 1,530 块失效 + Tier2 重新排队 |
| G3 | 平台 wiki 导出目录 `_kb`（NAS 上 113 个文件）怎么处置 | 决策者 | 迭代 0 结束前 | 平台一律忽略 + 清索引行；**NAS 目录先不删**（留着不碍事，删了不可恢复） |
| G4 | 反馈只在 Web 收，还是飞书也收 | 决策者 | 迭代 0 结束前 | **两处都收**：飞书侧识别"这条不对"作为迭代 1 的独立任务项 |
| G5 | 索引范围是否放开到其余 4 个共享（≈20 万非文档文件） | 决策者 | 迭代 2 开始前 | **暂不放开**，按业务价值分批；前提是 G1 + 涉密入口已落地 |
| G6 | 17 组 / 44 份 / 74.2 MB 重复件是否批量清理 | 决策者 | 随时 | 先在界面人工过一眼再批量执行；删除进回收站可恢复 |

---

## 迭代 0 · 止血与清账（第 1 周）

### Task 0.1 平台内部目录 `_kb` 全链路清理

**Files:**
- Modify: `kbplatform/fsindex.py:16-17`（`SKIP_DIR_NAMES` 增加 `"_kb"`）
- Modify: `kbplatform/pipeline.py:38-48`（`_warm_snapshot` 只预热监控范围内的路径）
- Modify: `kbplatform/fs_service.py:385`（`fs_list` 过滤平台目录）
- Create: `kbplatform/platform_dirs.py`（平台内部目录名单的唯一来源）
- Create: `tools/clean-internal-entries.py`（一次性清理脚本，幂等，先 `--dry-run`）
- Test: `kbplatform/tests/test_internal_dirs.py`

**Interfaces:**
- Consumes: `kbplatform.paths.norm_unc`（路径归一）、`kbplatform.config.Config.monitor_roots`（监控根列表）
- Produces:
  - `kbplatform/platform_dirs.py` → `PLATFORM_DIR_NAMES: set[str] = {"_kb", "_kb_trash"}`、`is_platform_rel(rel: str) -> bool`（判断相对路径的第一段是否平台目录）
  - `kbplatform/pipeline.py` → `Pipeline._warm_snapshot()` 只写入 `path` 落在 `self.cfg.monitor_roots` 之下的行

**背景（执行者必须知道）：** 现在的"已跳过 126"里 113 条是 1.0 引擎导出的 `AI知识库\_kb\wiki\entities\*.md`（平台自己的产物，文件在 NAS 上真实存在）。三个成因：(a) 2.0 扫描器 `fsindex.SKIP_DIR_NAMES` 不含 `_kb`，1.0 的 `catalog.py` 却含 → 两套实现不一致；(b) 1.0 流水线 `_warm_snapshot` 用**整张 catalog 表**预热监控快照，每次重启就把范围外的行判成"源文件已删除"；(c) 文件页是实时列目录，跳过名单对它无效，必须单独过滤。

- [ ] **Step 1: 写失败测试**

`kbplatform/tests/test_internal_dirs.py`：

```python
def test_is_platform_rel():
    from kbplatform.platform_dirs import is_platform_rel
    assert is_platform_rel("_kb\\wiki\\entities\\a.md") is True
    assert is_platform_rel("_kb_trash\\20261025\\x.pdf") is True
    assert is_platform_rel("AI知识库\\正常目录\\a.pdf") is False

def test_scanner_skips_platform_dirs(tmp_path):
    # 建一个含 _kb/ 与正常目录/ 的源目录，跑 scan_source，
    # 断言 fs_entries 里没有 _kb 下的条目
    ...

def test_warm_snapshot_only_monitors_scoped_rows(app):
    # catalog 里插一行不在 monitor_roots 之内的路径（模拟 _kb/wiki 产物），
    # 跑 Pipeline._warm_snapshot()，断言该路径不在 monitor._snapshot 里
    ...

def test_fs_list_hides_platform_dirs(app, tmp_path):
    # 存储源根下建 _kb/ 与可见目录/，
    # GET /api/v1/fs/list 断言 entries 里没有 _kb
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_internal_dirs.py -q`
Expected: FAIL（`ModuleNotFoundError: kbplatform.platform_dirs`）

- [ ] **Step 3: 实现**

`kbplatform/platform_dirs.py` 新建：`PLATFORM_DIR_NAMES = {"_kb", "_kb_trash"}`；`is_platform_rel(rel)` 取 `normalize_rel(rel)` 的第一段（按 `\` 切分）判断是否在集合内。

`kbplatform/fsindex.py`：`from kbplatform.platform_dirs import PLATFORM_DIR_NAMES` → `SKIP_DIR_NAMES = PLATFORM_DIR_NAMES | {"#recycle", "#snapshot", "@eaDir", "$RECYCLE.BIN", "System Volume Information"}`（保持 `SKIP_DIR_NAMES` 名字不变，`catalog.py` 的旧引用不受影响）。

`kbplatform/pipeline.py` `_warm_snapshot`：查询加范围条件——对每个监控根 `root` 判断 `path` 是否以根开头（UNC 归一后比较，`norm_unc`），只把范围内的行写进 `self.monitor._snapshot`。

`kbplatform/fs_service.py` `fs_list`：在 `entries = filter_entries(...)` 之后加一行过滤 `entries = [e for e in entries if not is_platform_rel(e.rel)]`（管理员分支同样过滤）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_internal_dirs.py -q` → Expected: PASS

- [ ] **Step 5: 清理历史脏数据（一次性脚本，幂等）**

新建 `tools/clean-internal-entries.py`：连接生产库，删除 `path` 含 `_kb\` 的 `catalog` 行、对应 `fs_entries` 行、`jobs` 中 `catalog_id` 已不存在的行、以及 `catalog_stats` 里 `_kb` 的计数行；打印删除条数。
运行：`PYTHONPATH=. python tools/clean-internal-entries.py --dry-run` 先看数量，再去掉 `--dry-run` 实跑。
期望：catalog 少 113 行、jobs 少 113 行（572 → 459 队列）。

- [ ] **Step 6: 跑全量测试 + 提交**

Run: `pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py -q` → Expected: 全绿

```bash
git add kbplatform/platform_dirs.py kbplatform/fsindex.py kbplatform/pipeline.py kbplatform/fs_service.py kbplatform/tests/test_internal_dirs.py tools/clean-internal-entries.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.2 embedding 单次输入上限：按块嵌入 + 显式失败

**Files:**
- Create: `kbplatform/embedding.py`
- Modify: `kbplatform/indexer.py:164-170`（改用新模块）
- Test: `kbplatform/tests/test_embed_limit.py`

**Interfaces:**
- Consumes: `kb.llm.embed(texts, model=...)`（批量；失败时抛异常或返回空列表——实现时以现有行为为准，但要**显式区分"空结果"与"超限"**）
- Produces:
  - `kbplatform/embedding.py` → `MAX_INPUT_TOKENS: int = 2048`、`embed_chunks(texts: list[str], model: str) -> tuple[list[list[float] | None], list[str]]`（返回与 `texts` 等长的向量列表，失败位置为 `None`；第二个返回值是警告文案列表）

**背景（执行者必须知道）：** embedding 服务对**单次输入**有 2,048 token 上限。现在 `indexer.index_file` 把一篇文件的所有块打包成一次 `llm.embed(texts)`，**一个超限块就让整篇的向量全部作废**；实测 3,156 字符的单块单独就失败，而 12 块 / 20,677 token 的整批却成功——限制是单输入 token 数，不是请求体大小。当前 4 篇文档因此没有向量（126 个块永远搜不到），且失败被 `if vecs:` 静默跳过。

- [ ] **Step 1: 写失败测试**

`kbplatform/tests/test_embed_limit.py`：

```python
def test_embed_chunks_survives_one_oversized_chunk(monkeypatch):
    # 假 embed：输入里含 "超长标记" 就抛 RuntimeError("input length exceeds")
    # 断言：正常块的向量回来了，超限块的位置是 None，且有警告文案
    ...

def test_embed_chunks_splits_oversized_chunk(monkeypatch):
    # 假 embed：长度 > 阈值才抛错
    # 断言：超限块被二分后成功，warnings 为空
    ...

def test_index_file_keeps_vectorless_chunks_visible(monkeypatch, app):
    # 断言：某块嵌入失败时，catalog.status_detail 里有可读原因
    # （如"2 个分块向量化失败：输入超长"），而不是静默 None
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_embed_limit.py -q` → Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `kbplatform/embedding.py`**

```python
MAX_INPUT_TOKENS = 2048        # embedding 服务单次输入上限（实测）
CHARS_PER_TOKEN = 1.6           # 中文为主的粗略估算，仅用于预判是否需要二分

def embed_chunks(texts: list[str], model: str) -> tuple[list[list[float] | None], list[str]]:
    """逐块向量化；对超出单次输入上限的块自动二分重试。

    返回 (vectors, warnings)：vectors 与 texts 等长，失败位置为 None。
    warnings 面向人读（会写进 catalog.status_detail）。
    """
```
行为：先整批尝试 → 抛错或返回条数不足时，逐条尝试 → 单条仍失败时按字符数二分（上限 `MAX_INPUT_TOKENS * CHARS_PER_TOKEN`）递归重试，二分到最小仍失败则该位置置 `None` 并记一条 warning。

> **实施时修正（2026-10-25，Task 0.2）**：字符换算只是**预判**——实测限制按 **token** 计（3,156 字符的单块单独就失败，而 12 块 / 20,677 token 的整批却成功），所以二分**不看长度、只等服务是否仍拒绝**；`MAX_INPUT_TOKENS * CHARS_PER_TOKEN` 仅用于预判是否值得先切一刀。

- [ ] **Step 4: `indexer.index_file` 改用它**

把 `vecs = llm.embed(texts, model=embed_model) if texts else []` + `if vecs: vs.add(chunk_ids, vecs)` 换成：调用 `embed_chunks`，把非 `None` 的向量与对应 `chunk_ids` 传给 `vs.add`；把 warnings 拼到 `status_detail`（**成功时也保留"部分块未向量化"的说明**，不要覆盖成 `None`）。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_embed_limit.py -q` → Expected: PASS

- [ ] **Step 6: 救回 4 篇失向量文档（一次性）**

把这 4 个 catalog id（3395 / 3397 / 3419 / 3495）置 `status='pending', tier1_done=0` 并入队 `index`；跑完后确认 `chunks` 有向量条数上升、`status_detail` 干净。
验收口径：这 4 篇在 `/api/v1/search` 里能被搜到正文片段。

- [ ] **Step 7: 全量测试 + 提交**

```bash
git add kbplatform/embedding.py kbplatform/indexer.py kbplatform/tests/test_embed_limit.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.3 答案反馈 + 检索轨迹（把"不准"变成可见）

**Files:**
- Modify: `kbplatform/db.py:15-288`（`SCHEMA_SQL` 加两张表）
- Modify: `kbplatform/retrieval.py:144-213`（`retrieve` 增加 `trace` 出参）
- Modify: `kbplatform/service.py:400-467`（`do_query` 落轨迹；新增反馈与轨迹接口）
- Modify: `web/static/app.js:1012-1041`（答案页加反馈按钮）
- Modify: `web/index.html:232-233`（`#a-answer` 旁加 `#a-feedback` 容器）
- Test: `kbplatform/tests/test_feedback_trace.py`

**Interfaces:**
- Produces（后续任务依赖这些名字）：
  - 表 `query_feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, user_id INTEGER, rating TEXT NOT NULL, comment TEXT, created_at REAL NOT NULL)`
  - 表 `retrieval_traces(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, user_id INTEGER, question_hash TEXT NOT NULL, route_stats TEXT NOT NULL, candidates TEXT NOT NULL, top_ids TEXT NOT NULL, created_at REAL NOT NULL)`
  - `POST /api/v1/query/{request_id}/feedback`，body `{"rating": "up"|"down"|"wrong_cite", "comment": str?}` → `{"ok": true}`
  - `GET /api/v1/admin/traces?limit=50` → `{"traces": [...]}`（**仅管理员**）
  - `GET /api/v1/admin/traces/{request_id}` → `{"trace": {...}}`（**仅管理员**）
  - `Retriever.retrieve(query, user_id=None, topk=8, use_graph=True, use_wiki=True, trace: dict | None = None) -> tuple[list[Hit], dict]`（**向后兼容**：多一个可选参数）
  - `trace` 结构：`{"routes": {"keyword": [id...], "vector": [id...], "graph": [id...], "wiki": [id...]}, "fused": [{"chunk_id": int, "score": float, "routes": [str...]}], "acl_filtered": bool}`

**约束：** 轨迹**只存 id 与分数，不存原文**；`question` 只存哈希（`sha256` 前 16 位）；轨迹表与反馈表都**不含涉密原文**。

- [ ] **Step 1: 写失败测试**

```python
def test_query_returns_request_id_and_writes_trace(app):
    # POST /api/v1/query → 响应含 request_id；
    # 断言 retrieval_traces 有一行，request_id 对得上
    ...

def test_feedback_is_recorded(app):
    # POST /api/v1/query/{request_id}/feedback {"rating":"down","comment":"答非所问"}
    # → 200；断言 query_feedback 有一行且 comment 存下来了
    ...

def test_trace_is_admin_only(app):
    # 普通用户 GET /api/v1/admin/traces → 403
    ...

def test_trace_contains_no_raw_text(app):
    # 断言 candidates / top_ids 字段里不出现文件正文片段
    ...

def test_trace_records_all_routes(app):
    # 断言 trace["routes"] 至少有 keyword 与 vector 两个 key
    ...
```

- [ ] **Step 2: 跑测试确认失败** → `pytest kbplatform/tests/test_feedback_trace.py -q` → FAIL

- [ ] **Step 3: 建表**（`kbplatform/db.py` 的 `SCHEMA_SQL` 追加两段 `CREATE TABLE IF NOT EXISTS`，并加进 `init_db` 的补列/建表流程）

- [ ] **Step 4: `retrieval.retrieve` 支持 `trace` 出参**

在 `retrieve` 内部把四路的 id 列表、融合后的 `[{chunk_id, score, routes}]` 写进传入的 `trace` 字典（若为 `None` 则跳过，**不要改变现有返回结构**）。

- [ ] **Step 5: `service.do_query` 落轨迹 + 新增三个接口**

`do_query` 里在 `retriever.retrieve(...)` 时传入 `trace={}`，结束时写一行 `retrieval_traces`（`question_hash` 用 `hashlib.sha256(question.encode()).hexdigest()[:16]`）。`request_id` 沿用现有 `resp["request_id"]`（`service.py:453`），**必须与响应里的一致**。
新增接口按 Interfaces 实现；反馈接口写库失败**不能抛错**（参考 `audit()` 的容错风格：写不进去只记日志）。

- [ ] **Step 6: 前端加反馈按钮**

`web/index.html` 在 `#a-answer` 后加 `<div id="a-feedback" class="ask-feedback hidden"></div>`；
`web/static/app.js` 的 `ask()` 里，`r.request_id` 存到局部变量，答案渲染完后填三个按钮（👍 有用 / 👎 没用 / 引用不对），点击 `POST /api/v1/query/{request_id}/feedback`，成功后按钮置灰并提示"已记录，谢谢"。

- [ ] **Step 7: 管理台轨迹面板**

`web/index.html` 管理台页签区加 `<button data-t="traces" class="admin-only">检索轨迹</button>`；
`web/static/app.js` 的 `loadAdmin()` 分支加 `if (state.adminTab === "traces") return adminTraces(body);`，
新函数 `adminTraces(body)` 调 `GET /api/v1/admin/traces?limit=50` 并渲染成表格（时间 / request_id / 各路命中数 / 反馈情况）。
`adminOnlyTabs` 数组里补上 `"traces"`。

- [ ] **Step 8: 跑测试确认通过 + 全量 + 提交**

```bash
git add kbplatform/db.py kbplatform/retrieval.py kbplatform/service.py web/index.html web/static/app.js kbplatform/tests/test_feedback_trace.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.4 全文搜索接进文件页

**Files:**
- Modify: `web/index.html`（文件页工具栏加全文搜索开关与结果容器）
- Modify: `web/static/files.js`（新增全文搜索逻辑，复用现有 `#fm-search` 的下拉样式）
- Test: `kbplatform/tests/test_content_search_ui.py` + `kbplatform/tests/test_frontend_js_syntax.py`（已有，前端改动后必须仍绿）

**Interfaces:**
- Consumes: `POST /api/v1/search`，body `{"query": str, "top_k": int, "share": str?}` → `{"total": int, "results": [{"chunk_id","document_id","file","path","locator","snippet","score","matched_by","restricted"}], "stats": {...}}`
- Produces: `#fm-content-search`（输入框）、`#fm-content-results`（结果容器）；`files.js` 导出 `KB.files.searchContent(query) -> Promise<Array>`

- [ ] **Step 1: 写失败测试**（HTML 标记 + 接口可达）

```python
def test_file_page_has_content_search_markers():
    html = (Path("web/index.html")).read_text(encoding="utf-8")
    assert 'id="fm-content-search"' in html
    assert 'id="fm-content-results"' in html

def test_search_api_returns_snippets(app):
    # 建 catalog + chunk，POST /api/v1/search 断言返回 snippet 与 score
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现前端**

在文件页搜索框旁加一个复选框「搜正文」（默认关，避免和文件名秒搜抢输入框）：勾选后，`#fm-search` 的输入改为同时触发文件名秒搜与全文检索，全文结果显示在 `#fm-content-results`（命中显示文件名 + 所在目录 + 片段 + 命中通道），点击结果跳到该文件所在目录（复用现有的 `openAt`/`pendingAt` 交接机制）。

- [ ] **Step 4: 跑测试 + 全量 + 提交**

```bash
git add web/index.html web/static/files.js kbplatform/tests/test_content_search_ui.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.5 失败原因可见 + 涉密目录管理台入口

**Files:**
- Modify: `kbplatform/service.py:670-672`（`status_detail` 不再对普通用户剥掉，但涉密路径要脱敏）
- Modify: `web/index.html:245-253`（管理台加「涉密目录」页签按钮）
- Modify: `web/static/app.js`（`adminOnlyTabs` 补 `"restricted"`；确认 `adminRestricted(body)` 已可用）
- Test: `kbplatform/tests/test_status_detail_visible.py`

**Interfaces:**
- Produces: `GET/PUT /api/v1/admin/restricted` 已有，前端入口补齐即可；普通用户在 `/api/v1/documents` 看到 `status_detail`（脱敏后）。

- [ ] **Step 1: 写失败测试**

```python
def test_status_detail_visible_to_normal_user(app):
    # 普通用户登录，GET /api/v1/documents，断言某条记录的 status_detail 非空
    ...

def test_status_detail_redacts_restricted_path(app):
    # 涉密目录下的文档，断言返回的 status_detail 里不含完整路径
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

`service.py` 文档列表接口：不再无条件剥掉 `status_detail`；改为——若该文档命中涉密范围，则把路径替换成 `（涉密目录）` 再返回，其余原样返回。

- [ ] **Step 4: 补管理台入口**

`web/index.html` 的管理台页签加 `<button data-t="restricted" class="admin-only">涉密目录</button>`；
`web/static/app.js` 的 `adminOnlyTabs` 数组加 `"restricted"`（前端 `adminRestricted()` 函数已存在，无需重写）。

- [ ] **Step 5: 跑测试 + 全量 + 提交**

```bash
git add kbplatform/service.py web/index.html web/static/app.js kbplatform/tests/test_status_detail_visible.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.6 可恢复的备份 + WAL 排查

**Files:**
- Create: `kbplatform/backup.py`
- Create: `tools/backup-db.ps1`（计划任务调用）
- Modify: `tools/install-task.ps1`（新增每日备份任务）
- Create: `docs/runbook/索引重建与备份.md`
- Test: `kbplatform/tests/test_backup.py`

**Interfaces:**
- Produces: `kbplatform/backup.py` → `backup_db(conn: sqlite3.Connection, dest_dir: str, keep: int = 7) -> list[str]`（返回保留的备份文件路径列表；用 SQLite 备份 API，**不复制文件**）；`prune_old(dest_dir, keep)`。

**背景（执行者必须知道）：** `kb.db` 2.25 GB 而 `kb.db-wal` **2.49 GB**（WAL 比主库还大 → checkpoint 饥饿）。直接复制文件做的备份很可能不可恢复，必须用 `sqlite3.Connection.backup()` 或 `VACUUM INTO`。备份目录要落在**被监控根之外**（否则备份文件本身会被扫进索引）。

- [ ] **Step 1: 写失败测试**

```python
def test_backup_db_writes_restorable_snapshot(tmp_path):
    # 建库 → 写入数据 → backup_db → 用 sqlite3 打开备份文件，断言表和数据都在
    ...

def test_backup_prunes_to_keep(tmp_path):
    # 造 9 份旧备份 → backup_db(keep=7) → 断言只剩 7 份
    ...

def test_backup_snapshot_contains_latest_writes(tmp_path):
    # 写库 → 备份 → 再写库 → 打开备份 → 断言备份里是备份时刻的数据
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现 `kbplatform/backup.py`**

```python
def backup_db(conn, dest_dir: str, keep: int = 7) -> list[str]:
    """用 SQLite 备份 API 做一致性快照（WAL 模式下不能直接复制文件）。"""
```
实现要点：目标文件 `kb-YYYYMMDD-HHMM.db`；用 `conn.backup(dst_conn)`；成功后 `prune_old` 只保留最近 `keep` 份；返回保留列表。

- [ ] **Step 4: 备份脚本 + 计划任务**

`tools/backup-db.ps1`：调用 `python -m kbplatform.backup --dest <备份目录>`（**PowerShell 文件必须 UTF-8 BOM**）；
`tools/install-task.ps1` 增加一个每日任务 `OpenCode-KB-Backup`（触发器：每天 02:00；失败重试 3 次）。

- [ ] **Step 5: WAL 排查与处置**

执行 `PRAGMA wal_checkpoint(TRUNCATE);` 看能否回收；再用 `PRAGMA journal_size_limit` 设上限（建议 256 MB）并在 `kbplatform/db.py` 的 `connect()` 里固化。跑完后记录 `kb.db-wal` 大小变化，写进 runbook。

- [ ] **Step 6: 写重建 runbook**

`docs/runbook/索引重建与备份.md`：备份在哪、怎么手工触发一次、重建索引的步骤（清 `catalog`/`chunks`/`vectors` → 重跑扫描 → 重建 FTS → 重跑 Tier2）、预计耗时。

- [ ] **Step 7: 跑测试 + 全量 + 提交**

```bash
git add kbplatform/backup.py tools/backup-db.ps1 tools/install-task.ps1 docs/runbook kbplatform/tests/test_backup.py
git commit -F <utf8-no-BOM 文件>
```

### Task 0.7 wiki 人工校正层通道补 ACL + 回归测试

**Files:**
- Modify: `kbplatform/retrieval.py:124-141, 152, 169-170`（wiki 候选做可见性过滤）
- Test: `kbplatform/tests/test_wiki_acl.py`

**Interfaces:**
- Consumes: `kbplatform.scopes.is_visible(scopes, share, rel_path)`、`kbplatform.grants` 的可见性判定（与 `scope_predicate` 同一套）
- Produces: `Retriever._wiki_ids_for(query, topk, allowed_paths: set[tuple[str,str]] | None)`——`wiki_pages.path` 解析出 (share, rel_path)，不在可见集合里的候选直接剔除

**背景（执行者必须知道）：** 检索层的关键词/向量/图谱三路都过了 ACL（`retrieval.py:155-158, 166-167`），**唯独 wiki 路没有**——`wiki_ids` 直接进了 RRF 列表。现在 `wiki_pages` 是 0 行所以无害；一旦迭代 3 给人工校正层做 UI 并开始写内容，这就是越权泄露路径。**这个任务必须先于任何 wiki UI 工作。**

- [ ] **Step 1: 写失败测试**

```python
def test_wiki_candidates_respect_scope(app):
    # 建 wiki_pages 一条，path 指向用户**无权**目录；
    # 用有权用户检索 → 断言 wiki 候选没进 results
    ...

def test_wiki_candidates_visible_inside_scope(app):
    # path 指向有权目录 → 断言能召回
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

`wiki_pages` 表结构有 `path` 字段；用 `scope_from_catalog_row` 同类逻辑把它拆成 (share, rel_path)，再用与 `scope_predicate` 同一套的可见性判定过滤。注意：**管理员不受限**（`scope_user_id()` 对 admin 返回 `None`，此时不过滤）。

- [ ] **Step 4: 跑测试 + 全量 + 提交**

```bash
git add kbplatform/retrieval.py kbplatform/tests/test_wiki_acl.py
git commit -F <utf8-no-BOM 文件>
```

### 迭代 0 出口验收

- [ ] `pytest kbplatform/tests --ignore=kbplatform/tests/test_feishu_kbq.py -q` 全绿
- [ ] 看板"已跳过"卡片显示的不再含平台内部文件（对照：修复前 126 / 修复后应 ≈13）
- [ ] 4 篇制度文档（3395 / 3397 / 3419 / 3495）在 `/api/v1/search` 能搜到正文片段
- [ ] 文件页勾选「搜正文」能搜到正文并跳转
- [ ] 答案页点「没用」能落库；管理台「检索轨迹」页签能看到这次召回
- [ ] 普通用户能看到失败原因（涉密路径已脱敏）
- [ ] 管理台「涉密目录」页签可进入
- [ ] 备份目录里有快照，且**能用 SQLite 打开**；计划任务已注册
- [ ] 一次真实问答的轨迹里**不出现任何原文片段**

---

## 迭代 1 · 把"准"做出来（第 2–3 周）

> 前置：迭代 0 全部完成，且已有 ≥20 条真实反馈数据。没有反馈数据就开始调重排，等于闭着眼睛调。

### Task 1.1 轻量重排（块级二次打分）

**Files:**
- Create: `kbplatform/rerank.py`
- Modify: `kbplatform/retrieval.py:144-213`（融合后加一层重排）
- Modify: `kbplatform/service.py`（`SearchReq`/`QueryReq` 增加 `rerank: bool = True`）
- Test: `kbplatform/tests/test_rerank.py`

**Interfaces:**
- Produces: `kbplatform/rerank.py` → `rerank(query: str, candidates: list[Hit], conn, embed_model: str = "bge-m3", topk: int = 8) -> list[Hit]`（按 query-chunk 余弦相似度 + 标题/路径/时间加权重排；返回的 `Hit.score` 是**真实余弦相似度（0–1）**，不是 RRF 分）；`Hit`（`kbplatform/retrieval.py:26-37`）在**末尾**追加字段 `rerank_score: float | None = None`（末尾追加是安全的：其后所有字段都带默认值）。

**约束：** 重排只在**已经过 ACL 过滤**的候选里做；不得为了让相似度高而绕过授权。

- [ ] **Step 1: 写失败测试**

```python
def test_rerank_orders_by_real_similarity(app):
    # 造 3 个候选，其中 1 个与 query 向量最像；
    # 断言重排后它排第一，且 score 是 0–1 的余弦值（不是 0.0x 的 RRF 分）
    ...

def test_rerank_respects_acl(app):
    # 无权目录的候选即使最像也不得进入最终 top-k
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现 `kbplatform/rerank.py` 并接入 `retrieval.retrieve`**（新增可选参数 `rerank: bool = True`；`use_rerank=False` 时行为与现在完全一致，便于 A/B）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### Task 1.2 查询改写 + 意图路由

**Files:**
- Modify: `kbplatform/retrieval.py`（多检索式召回）
- Modify: `kbplatform/service.py`（`do_query` 里调用改写）
- Test: `kbplatform/tests/test_query_rewrite.py`

**Interfaces:**
- Produces: `kbplatform/rewrite.py` → `rewrite_query(question: str, model: str) -> list[str]`（返回 1–3 个检索式；模型不可用时**降级为原问题**，不报错）；`detect_intent(question: str) -> str`（返回 `"find_file"` | `"ask_fact"` | `"ask_process"`）。

- [ ] **Step 1: 写失败测试**

```python
def test_rewrite_returns_original_when_model_unavailable(monkeypatch):
    # 模型不可用 → 返回 [原问题]，不抛异常
    ...

def test_multi_query_recall_improves_hit(app):
    # 断言多检索式比单检索式多召回目标文档
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**（改写用现有 `models_mod.chat`，要求返回 JSON；解析失败则降级）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### Task 1.3 引用可信度 + 原文高亮

**Files:**
- Modify: `kbplatform/service.py:427-443`（`citations` 加 `score`、`routes`、`locator`）
- Modify: `web/static/app.js:1026-1038`（引用卡片显示分数与通道，答案里高亮定位）
- Test: `kbplatform/tests/test_citation_score.py`

**Interfaces:**
- Produces: `citations[]` 每项新增 `score: float`（**真实余弦相似度，0–1，一位小数**）、`routes: list[str]`；`/api/v1/query` 响应新增 `evidence_count: int`。

**约束：** 若 Task 1.1 未上线，**不得显示任何数字**（RRF 分量级 0.016–0.065，展示成"3%"会误导用户）——只显示命中通道标签。

- [ ] **Step 1: 写失败测试**

```python
def test_citations_carry_score_in_zero_one(app):
    # 断言 score 是 0–1 的 float（有重排时）
    ...

def test_citations_have_routes(app):
    # 断言 routes 非空且含 "keyword"/"vector" 之一
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**（后端加字段；前端引用卡片显示 `相关度 0.83` + 通道 tag；答案正文里把与引用片段重合的关键词标 `<mark>`）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### Task 1.4 文档级摘要 + 标签

**Files:**
- Create: `kbplatform/summary.py`
- Modify: `kbplatform/db.py`（`catalog` 加列 `summary TEXT`、`tags TEXT`）
- Modify: `kbplatform/pipeline.py`（Tier2 完成后顺带生成摘要/标签；或新增队列种类 `summarize`）
- Test: `kbplatform/tests/test_summary.py`

**Interfaces:**
- Produces: `kbplatform/summary.py` → `summarize(conn, catalog_id: int, model: str) -> tuple[str, list[str]]`（返回摘要文本与标签列表；失败返回 ("", []) 且不抛）；`catalog.summary` / `catalog.tags`（JSON 数组字符串）两列。
- `GET /api/v1/documents/{id}/summary` → `{"summary": str, "tags": list[str]}`

- [ ] **Step 1: 写失败测试**

```python
def test_summary_is_stored_on_catalog(app):
    # 断言 summary 与 tags 落到 catalog 行
    ...

def test_summary_failure_is_not_fatal(app):
    # 模型不可用 → 摘要为空、状态不被改成 failed
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**（批量生成走**算力池**，不得上云；单篇耗时要记录）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### Task 1.5 多轮追问 + 会话历史 + 流式输出

**Files:**
- Modify: `kbplatform/db.py`（新增 `chat_session` / `chat_message` 两表）
- Modify: `kbplatform/service.py`（`POST /api/v1/query` 支持 `session_id`；新增会话接口）
- Modify: `web/static/app.js`（问答页加会话列表与输入历史）
- Test: `kbplatform/tests/test_chat_session.py`

**Interfaces:**
- Produces:
  - 表 `chat_session(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)`
  - 表 `chat_message(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, restricted INTEGER DEFAULT 0, created_at REAL NOT NULL)`
  - `POST /api/v1/query` 接受可选 `session_id`；响应新增 `session_id`
  - `GET /api/v1/sessions` → `{"sessions": [...]}`；`GET /api/v1/sessions/{sid}/messages` → `{"messages": [...]}`；`DELETE /api/v1/sessions/{sid}` → `{"ok": true}`
  - `restricted` 标记：**来自涉密片段的历史消息，在重新拼进 prompt 前必须剔除或脱敏**

**约束：** prompt 只带**最近 2–3 轮**；涉密派生内容**永不回灌**（与"涉密原文不回传"同级的要求）。

- [ ] **Step 1: 写失败测试**

```python
def test_second_turn_uses_history(app):
    # 第一轮问"X 项目"，第二轮问"它的工期？"；
    # 断言第二轮的检索/上下文里带上了第一轮
    ...

def test_restricted_history_is_not_replayed(app):
    # 造一条 restricted=1 的历史消息 → 断言它不出现在下一次 prompt 里
    ...

def test_session_is_scoped_to_owner(app):
    # 用户 B 读不到用户 A 的会话
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**（流式：模型调用改 `stream=True` 并用 `StreamingResponse`；若现有 `models_mod.chat` 不支持流式，先在 `models.py` 加 `chat_stream()`，前端用 `fetch` + `ReadableStream` 增量渲染）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### Task 1.6 图谱首屏：覆盖进度 + 分层概览（不让用户面对空画布）

**Files:**
- Modify: `kbplatform/service.py`（`GET /api/v1/graph/overview` 增加覆盖进度字段）
- Modify: `web/static/app.js`（图谱页首屏渲染"重建进度 + 分层概览"，空态不再是一片空白）
- Test: `kbplatform/tests/test_graph_overview.py`（已有该文件，追加断言）

**Interfaces:**
- Produces: `GET /api/v1/graph/overview` 响应新增 `progress: {"tier2_done": int, "total": int, "pending": int, "percent": int}`；
  前端 `web/index.html` 的 `#p-graph` 工具栏后加 `<div id="g-progress" class="muted"></div>`。

**背景（执行者必须知道）：** Tier2 提炼还在排队（迭代 0 清掉 113 个无效任务后会快一截），这期间图谱页对用户就是"空的"——而图谱是最容易被拿去演示的功能。首屏要**明说还在建、建到哪一步**，并先展示已完成部分的实体/知识点/文档分层概览，而不是干等。

- [ ] **Step 1: 写失败测试**

```python
def test_graph_overview_reports_tier2_progress(app):
    # catalog 里造 3 篇已提炼 + 2 篇未提炼；
    # 断言 overview["progress"]["total"] == 5 且 percent 在 0–100
    ...
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**（后端按调用者可见范围统计 `tier2_done` / 总数；前端显示"知识图谱正在重建：已完成 X/Y 篇（Z%）"）
- [ ] **Step 4: 跑测试 + 全量 + 提交**

### 迭代 1 出口验收

- [ ] 全量测试绿；`/api/v1/query` 响应含 `score` / `routes` / `evidence_count` / `session_id`
- [ ] 同一个问题，重排前后各跑 20 次，在轨迹面板里能看到名次变化
- [ ] 反馈数据里 👍 比例相比迭代 0 基线**有提升**（基线在迭代 0 上线首周统计）
- [ ] 多轮追问不丢上下文，且**涉密历史不回灌**（有测试钉住）
- [ ] 每篇已解析文档都有摘要与标签

---

## 迭代 2 · 治治理（第 4–6 周，任务清单，不展开步骤）

1. **近似重复与版本差异**：感知哈希（simhash/pHash）找近似件 → LLM 输出"哪版更新、改了什么"；看板加「近似重复」分组（默认折叠，与现有重复组一致）
2. **批量归类与规范命名**：用 Tier2 结果 + 目录先验给新文件推荐归属目录与规范名，一键批量执行
3. **OCR 策略校正**：存量扫描件批量 OCR 做成管理台可选项（可中断、可见进度）；OCR 前先合并重复件
4. **运行状态可见**：`GET /api/v1/system/status`（队列深度 / 失败数 / 最近扫描 / 模型可用性 / WAL 大小）+ 管理台「运行状态」页签
5. **告警通道**：队列积压 / 解析连续失败 / 模型不可用 / 看门狗重启 → 飞书推送
6. **会话与密钥治理**：会话密钥走环境变量（解决"重启全员登出"）；`session_secret` 只改存储方式不改值
7. **回收站自动清理 + 日志轮转**
8. **技术债收敛**：老 `user_scopes` 与 `grants` 合一；1.0/2.0 两套流水线合一；两套统计口径合一

## 迭代 3 · 协作与自动化（第 7 周起，任务清单，不展开步骤）

1. 收藏 / 最近打开 / 常用目录
2. 评论 @ + 订阅变更提醒
3. 人工校正闭环（wiki 层 UI + 反馈回流抽取；**必须先有 Task 0.7 的 ACL 修复**）
4. 平台内 agent 代办（批量整理 / 改名 / 归类 / 生成目录摘要）
5. 自动简报推送（每周新增 / 失败 / 重复 / 待确认）
6. 表格与图片理解（视觉模型 + 结构化抽取，针对 90 个 xlsx）

---

## 里程碑指标与测量口径

| 指标 | 口径 | 基线 | 目标 |
|---|---|---|---|
| 答案 👍 比例 | `query_feedback` 中 rating='up' / 总反馈数 | 迭代 0 上线首周统计 | 迭代 1 末 ≥ 70% |
| 引用点击率 | 引用卡片被打开 / 回答次数 | 同上 | ≥ 50% |
| 追问率 | 有第二轮提问的会话 / 会话数 | 迭代 1 上线后 | ≥ 30% |
| 可搜正文率 | 向量库**有效向量数**（`vectors.ids.json` 里非 `-1` 的条目）/ `chunks` 总行数 | 修复前 1,408 / 1,530 | 迭代 0 末 ≥ 99% |
| 台账干净度 | "已跳过"里平台内部文件数 | 113 | 0 |
| 解析队列中无效任务 | 队列里非用户文档的任务数 | 113 | 0 |

**测量前提：** 迭代 0 的反馈埋点上线前，以上指标都**无法测量**——这正是迭代 0 不能跳的原因。

## 风险与回滚

| 风险 | 影响 | 回滚方式 |
|---|---|---|
| 改了 `SKIP_DIR_NAMES` 导致漏扫真实文件 | 索引缺文件 | 改动只影响 `_kb`/`_kb_trash`，回滚即 `git revert`；清库脚本 `--dry-run` 先看 |
| 全局重切块（若 G2 通过） | 全部块失效、检索质量短时下降 | 决策前**不执行**；真要做必须先有可用备份（Task 0.6） |
| 重排改错导致答案变差 | 用户信任下降 | `use_rerank=False` 保留旧路径，可配置开关即时回退 |
| 会话历史把涉密内容带出去 | **安全事故** | 涉密消息 `restricted=1` 永不回灌（测试钉住）；发现即清 `chat_message` 表并轮换会话密钥 |
| 备份占满磁盘 | 服务异常 | 备份目录与保留份数可配；`prune_old` 每次执行 |
| 新表/新列影响老库启动 | 平台起不来 | 所有 DDL 走 `init_db` 的 `try/except sqlite3.OperationalError` 幂等模式 |

## 执行约定

- 每个任务一个提交；提交前必须跑该任务自己的测试文件 + 全量测试。
- 每个任务完成后在 `.superpowers/sdd/2026-10-25-kb-platform-v2-iteration/progress.md` 追加一行 ledger（任务号、提交号、测试结果、遗留问题）。
- 任务之间可并行（无依赖），但 **Task 0.7 必须早于任何 wiki UI**，**Task 0.2 必须早于任何依赖向量的新功能**。
- 生产数据变更（清库、批量重跑）一律先 `--dry-run`。
