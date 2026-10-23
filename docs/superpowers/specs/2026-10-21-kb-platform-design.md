# 企业知识平台 · 设计文档

- 日期：2026-10-21
- 状态：待评审
- 位置：`D:\kb-platform`（项目根）

---

## 1. 意图与背景

公司 NAS（Synology DS218play）上存着 507,182 个文件，是事实上的文档云。当前痛点：

1. **存量文档无法检索**——员工知道"公司有规定"但找不到在哪份文件里
2. **文档处于黑盒状态**——没人知道哪些文档已进知识库、哪些还没处理
3. **知识不成体系**——同一主题散落在几十份文件里，没有关联

目标：建一个**给全公司员工用的知识平台**，把 NAS 里的文档变成可检索、可视化、可被其他智能体调用的知识资产。

**核心认知**：本项目不是"从零造 RAG"。底层引擎（解析 → LLM 知识提炼 → 向量化 → 图谱 → 混合检索 → 带引用溯源问答）已在 `D:\kb-platform\kb` 跑通并实测。本项目是**在已验证的引擎外面套一个可用的平台**，并把规模从 1 个文档扩到 28 万份。

## 2. 目标 / 非目标 / 反目标

### 目标

| # | 目标 | 验收标准 |
|---|---|---|
| G1 | 文档状态看板 | 列出**监控范围内**可解析文档的处理状态（待处理/排队中/已解析/失败），失败要有原因；并显示两级摄入进度 |
| G2 | 知识图谱可视化 | 浏览器里交互式浏览节点与边，支持按类型过滤、按实体下钻 |
| G3 | AI 问答 | 可切换模型；答案带片段引用与原文定位；命中涉密目录时自动锁定本地模型 |
| G4 | 对外 API | 凭 token 调用问答与文档搜索；token 按用户签发；智能体可注册到用户名下 |
| G5 | WorkBuddy 接入 | 交付可直接安装的 skill 包（可选附 MCP server） |
| G6 | 权限隔离 | 管理员为每个用户勾选可见的共享/目录；检索层强制过滤；API 继承用户权限 |
| G7 | 自动摄入 | 文件放进监控目录后自动完成解析、索引、提炼、建图；失败可在看板看到原因 |

### 2.1 摄入范围与两层模型

**监控范围（已定）：** 只监测 `信息化管理\AI知识库` 路径下的文件；其他路径的文件**暂时不提取**。

这条把整个项目从"全量回填"降级为"增量摄入"，量级完全不同：

| | 全量 281,000 份 | 监控范围 = AI知识库（当前） |
|---|---|---|
| catalog 构建 | 11.3 h（SMB 全量遍历） | **分钟级**（目录小，轮询即可） |
| Tier 1（检索层） | 约 2.5 天 | **分钟级**（每新增一个文件即时处理） |
| Tier 2（知识层） | 194–486 天 | **约 30 s/块**，随用随长 |
| 存储占用 | 9.5–23.7 GB | **可忽略**（当前 1 份文档 244 KB） |
| 上线周期 | 2.5 天起 | **当天** |

**监控根路径是配置项**，不是硬编码。将来把某个共享加进监控列表，看板、权限、检索、统计自动扩展，**无需改动架构**。权限 scope 仍按 (share, subpath) 粒度存储，为扩展留好接口。

**两层摄入仍然保留**，因为它是扩展到全量时的排期依据：

| 层 | 内容 | 触发 | 产出 |
|---|---|---|---|
| **Tier 1 · 检索层** | 解析 → 分块 → FTS5 → bge-m3 向量 | 文件一进监控目录就自动跑 | 立即可被搜索、可问答 |
| **Tier 2 · 知识层** | LLM 提炼 → 知识点 → 图谱 | Tier 1 完成后自动跑 | 摘要、知识点、实体关系、图谱 |

> 关键性质：**问答不需要 Tier 2**。混合检索召回片段后直接交给 LLM 生成答案。Tier 2 只增强答案质量、提供图谱可视化与实体下钻。因此新文件落地后**秒级可搜、约 30 秒后进入知识图谱**。

### 非目标（本期不做）

- 文档在线预览 / 编辑 / 版本管理
- 全文编辑与协同
- 移动端适配（桌面浏览器优先）
- 跨实例 / 跨公司的多租户
- 训练或微调模型

### 反目标（做到这些也算失败）

- **答案不可溯源**——任何一条回答都必须能点回原文的具体页/段
- **权限只做界面层**——靠前端隐藏按钮而不是后端过滤，等于没有权限
- **知识出内网**——涉密目录的内容原文不得发往公司外部
- **把非文档文件塞进知识库**——15 万张党建照片、4600 个视频进索引既无价值也压垮存储

## 3. 已确定的决策（由需求方拍板）

| # | 决策 |
|---|---|
| D1 | 平台自建用户账户，管理员创建并授权 |
| D2 | 每员工账号一枚 token，管理员签发；员工把 token 交给 WorkBuddy |
| D3 | WorkBuddy 接入的智能体注册在该用户名下，平台可见 |
| D4 | 权限粒度：管理员为每个用户勾选**可见的共享/目录** |
| D5 | 模型配置：预置常见大模型厂商 + 支持自定义模型配置 |
| D6 | 状态看板范围：**全部可见共享下的可解析文档**（约 28.1 万份），非文档类文件只在统计里计数不建索引 |
| D7 | 对接 WorkBuddy 的交付物：**skill 包**（另可选 MCP server 作为第二条路径） |
| D8 | **摄入范围：只监测 `信息化管理\AI知识库`**，其他路径暂不提取。监控根路径做成配置项 |
| D9 | **OCR 与老格式转换：本期安装**（PaddleOCR + LibreOffice），**但不批量跑存量**；监控范围内新文件自动处理 |
| D10 | **涉密是目录的属性**（`dir_scopes`），与用户授权正交 |
| D11 | **涉密命中 + 选了云端模型 → 自动路由到本地模型**（不报错、不中断），响应标注 `restricted` / `model_switched` |
| D12 | **API 默认不回传片段原文**（涉密时 snippet 置空），响应头带 `X-KB-Restricted`；skill 包写明转发禁令 |
| D13 | **涉密标记精确到子目录**，不整共享标注（整共享标注会让半涉密共享无法细粒度授权） |
| D14 | **提供"严格模式"开关**：开启后 API 一律只回传答案、片段内容（含 snippet）全部不回传。用于对残余风险零容忍的场景 |

## 4. 约束条件（全部为本机实测，非估算）

### 4.1 硬件

| 资源 | 规格 | 影响 |
|---|---|---|
| AI 服务器 | i5-12400F (6C/12T)、15.8 GB RAM | 单进程服务，无横向扩展 |
| GPU | RTX 3060 12 GB | **同时只能常驻一个大模型** |
| 磁盘 | C: 180GB(可用95) / D: 285GB(可用209) | 索引与数据库放 D: |
| NAS | DS218play，1 GB RAM，ARM 4核1.4GHz | **不能在上面跑任何计算**；磁盘已用 94.9%（剩 185GB） |

### 4.2 已验证的引擎性能

| 指标 | 实测值 |
|---|---|
| qwen3.5:9b-64k prefill | ~1,600 tok/s |
| qwen3.5:9b-64k decode | ~46 tok/s |
| 64K 上下文显存占用 | 7.05 GB（32K 为 6.13 GB） |
| 单块抽取（2000 tok 提示 + 1400 tok 输出） | ~30 s |
| bge-m3 向量化 | 0.07 s/段（1024 维） |
| 问答端到端 | 8.4 s/题（8 问实测均值） |
| 模型切换（卸载重载） | 10–17 s |
| 文档摄取全流程（14 页 PDF → 4 块 → 图谱） | 117 s |

### 4.3 已验证的基础设施选型

| 事项 | 结论 |
|---|---|
| Python | 3.12.10 已装（`%LOCALAPPDATA%\Programs\Python\Python312`） |
| Node / npm / git / bun / Docker | **均未安装**——方案不依赖它们 |
| 全文检索 | SQLite 3.49.1 FTS5，**`tokenize=unicode61` + 预写入字符二元组**（trigram 方案作废，见下） |
| 关键词检索实测 | **真实文档样本（220 份 / 313 块 / 平均 2191 字符）**：建索引 0.88 ms/块；**索引 10.15 KB/块**；56 万块 → 5.42 GB |
| 分词器对比 | `trigram` 对"信创""审批"等 2 字中文词**全部查不到**（要求 ≥3 字符）；预写入二元组方案 2 字词全命中 |
| 前端库来源 | npmmirror / unpkg 可用（**jsdelivr 不可用**），全部本地 vendored |
| 依赖安装 | pypi.org 可达，fastapi / uvicorn / pydantic 可下载 |

### 4.4 网络与数据出内网

- `github.com` / `raw.githubusercontent.com` / `codeload` **被墙**；`api.github.com` 与 `gh-proxy.com` 可用
- `registry.npmjs.org` / `registry.npmmirror.com` / `unpkg.com` 可用；`cdn.jsdelivr.net` 不可用
- **涉密目录（党建 / 审计 / HR 薪酬）内容不得发往公司外部**——见 §7.5.4

### 4.5 关键规模数字

| 项 | 值 |
|---|---|
| NAS 文件总数 | 507,182 |
| 其中可解析文档 | ~281,000（pdf 132K / docx 68K / xlsx 36K / doc 21K / pptx 12K / xls 8K / txt 2K / ofd 554） |
| 非文档文件 | ~226,000（jpg 152K / png 22K / mp4 4.6K / 压缩包 / CAD / psd 等） |
| **PDF 中扫描件占比** | **44%**（实测 238 份抽样：文本型 46% / 扫描件 44% / 弱文本 8%） |
| SMB 全量遍历耗时 | **11.3 小时**（12.5 文件/秒） |
| 预估分块量 | 56 万 ~ 140 万 块（按 2–5 块/份） |
| 真实平均块长 | 2,191 字符（313 块样本，中位 2,525，最大 5,985） |

**存储占用（按真实样本推算，非估算）**

| 组成 | 56 万块 | 140 万块 |
|---|---|---|
| FTS5 索引 | 5.42 GB | 13.55 GB |
| 原文 (UTF-8) | 2.97 GB | 7.43 GB |
| 向量 (fp16, 1024 维) | 1.07 GB | 2.67 GB |
| **小计** | **9.46 GB** | **23.65 GB** |
| + 每日冷备 ×2 | ~19 GB | ~47 GB |

D: 盘可用 209 GB，**可容纳**。

> ⚠️ 上一版设计稿曾按"索引 0.2 KB/块"估算（得 120–280 MB），那是用同一句 50 字文本重复 5 万次的**合成探针**，严重失真。真实中文文本的二元组词表丰富度远高，索引膨胀约 50 倍。**以本表为准。**
>
> **注：本表是"扩展到全量 5 个共享"时的规模。当前监控范围只有 `信息化管理\AI知识库`，实际存储占用可忽略（现有 1 份文档 = 244 KB）。**

### 4.6 SMB 目录 mtime 传播：不可靠（实测）

在 DS218play 上通过 SMB 直接测试（5 轮）：

| 操作 | 目录 mtime 变化 |
|---|---|
| 新增文件 | **1/5** |
| 删除文件 | 4/5 |

**结论：不能用"目录 mtime 变化 → 只重扫该子树"来做增量。** 增量策略见 §7.1。文件自身的 mtime 是准确的，可靠。

## 5. 系统架构

```
┌──────────────────────── 员工浏览器 (LAN) ────────────────────────┐
│  文档状态看板 │ 知识图谱 │ AI问答 │ 管理台(用户/权限/模型/密钥)   │
└──────────────────────────────┬────────────────────────────────────┘
                               │ HTTP (内网)
┌──────────────────────────────▼────────────────────────────────────┐
│                    平台服务 (FastAPI + uvicorn)                   │
│  ┌──────────┬──────────┬───────────┬──────────┬────────────────┐  │
│  │ 认证/会话 │ 权限过滤  │ 模型路由   │ API网关  │ 任务调度        │  │
│  └──────────┴──────────┴───────────┴──────────┴────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 检索层: FTS5(二元组) + 向量(bge-m3) + 图谱扩展 + RRF 融合     │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────┬──────────────────┬──────────────────────┬────────────────┘
        │ SMB              │ HTTP                 │ HTTP
┌───────▼────────┐  ┌──────▼───────┐   ┌──────────▼──────────────┐
│ NAS 文档云     │  │ Ollama       │   │ 云端模型(可选)            │
│ (只读)         │  │ qwen3.5:9b   │   │ DeepSeek/通义/混元/...    │
└───────┬────────┘  │ bge-m3       │   └─────────────────────────┘
        │           └──────────────┘
┌───────▼────────────────────────────────────────────────────────┐
│ SQLite (D:\kb-platform\kb\data\kb.db)                             │
│  catalog(28万文件目录) │ chunks │ vectors │ knowledge           │
│  nodes │ edges │ users │ scopes │ tokens │ agents │ models      │
└────────────────────────────────────────────────────────────────┘
```

## 6. 数据模型

在现有 `kb.db` 基础上扩展，现有表（files / chunks / vectors / knowledge / nodes / edges）保持兼容。

```sql
-- 目录索引（离线全量构建 + 增量更新）
CREATE TABLE catalog (
  id            INTEGER PRIMARY KEY,
  path          TEXT UNIQUE NOT NULL,     -- UNC 绝对路径
  share         TEXT NOT NULL,            -- 所属共享
  rel_path      TEXT NOT NULL,            -- 共享内相对路径
  name          TEXT NOT NULL,
  ext           TEXT NOT NULL,
  size          INTEGER,
  mtime         REAL,                     -- 文件自身 mtime（可靠，用于变更判定）
  parseable     INTEGER,                  -- 恒为 1：catalog 只存可解析文档
  status        TEXT,                     -- pending|queued|parsing|done|failed|skipped
  status_detail TEXT,                     -- 失败原因
  chunk_count   INTEGER DEFAULT 0,
  tier1_done    INTEGER DEFAULT 0,        -- 已完成分块+索引+向量化
  tier2_done    INTEGER DEFAULT 0,        -- 已完成 LLM 提炼 + 建图
  kp_count      INTEGER DEFAULT 0,
  edge_count    INTEGER DEFAULT 0,
  needs_ocr     INTEGER DEFAULT 0,
  scanned       INTEGER DEFAULT 0,
  updated_at    REAL
);
CREATE INDEX idx_catalog_status ON catalog(status);
CREATE INDEX idx_catalog_share  ON catalog(share);
CREATE INDEX idx_catalog_t1     ON catalog(tier1_done);
CREATE INDEX idx_catalog_t2     ON catalog(tier2_done);

-- 非文档文件统计（不建 catalog 行）
CREATE TABLE catalog_stats (
  share TEXT, ext TEXT, count INTEGER, PRIMARY KEY(share, ext)
);

-- 平台用户
CREATE TABLE users (
  id         INTEGER PRIMARY KEY,
  username   TEXT UNIQUE NOT NULL,
  display_name TEXT,
  password_hash TEXT NOT NULL,            -- argon2/bcrypt
  role       TEXT NOT NULL DEFAULT 'user',-- admin | user
  dept       TEXT,
  active     INTEGER DEFAULT 1,
  created_at REAL
);

-- 用户可见范围（管理员勾选）
CREATE TABLE user_scopes (
  id      INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  share   TEXT,                           -- 共享级
  subpath TEXT,                           -- 或目录级（相对共享根）
  UNIQUE(user_id, share, subpath)
);

-- 目录级「涉密」标记（与 user_scopes 正交，见 §7.5.4）
-- 这是目录的属性，不是授权的属性：同一个目录对所有人都是涉密的。
CREATE TABLE dir_scopes (
  id        INTEGER PRIMARY KEY,
  share     TEXT NOT NULL,
  subpath   TEXT NOT NULL,                -- '' 表示整个共享
  restricted INTEGER NOT NULL DEFAULT 0, -- 1=涉密，命中则强制本地模型
  label     TEXT,
  UNIQUE(share, subpath)
);

-- 人工校正层（wiki 页面）
CREATE TABLE wiki_pages (
  id         INTEGER PRIMARY KEY,
  slug       TEXT UNIQUE NOT NULL,        -- 文件名(不含扩展名)
  title      TEXT,
  body       TEXT,
  node_id    INTEGER,                     -- 关联的图谱节点
  path       TEXT,                        -- 源文件路径（用于溯源/重生成）
  edited_by  INTEGER,                     -- 人工编辑则记录
  updated_at REAL
);

-- API token（每用户一枚）
CREATE TABLE api_tokens (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,        -- 只存哈希
  prefix     TEXT NOT NULL,               -- 明文前 8 位，便于人工识别
  label      TEXT,
  revoked    INTEGER DEFAULT 0,
  created_at REAL,
  last_used_at REAL,
  expires_at REAL
);

-- 注册到用户名下的外部智能体
CREATE TABLE agents (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT,                        -- workbuddy | mcp | other
  client_id  TEXT,
  meta       TEXT,
  created_at REAL,
  last_seen_at REAL
);

-- 模型配置（预置 + 自定义）
CREATE TABLE models (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,               -- 展示名
  provider   TEXT NOT NULL,               -- ollama | openai_compatible
  base_url   TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  api_key    TEXT,                        -- 加密存储
  is_local   INTEGER DEFAULT 0,
  is_default INTEGER DEFAULT 0,
  extra      TEXT,                        -- JSON: 上下文长度/超时/温度
  created_at REAL
);

-- 任务队列
CREATE TABLE jobs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,               -- parse | ocr | embed | graph
  catalog_id INTEGER,
  state      TEXT NOT NULL,               -- queued|running|done|failed
  attempts   INTEGER DEFAULT 0,
  error      TEXT,
  created_at REAL, started_at REAL, finished_at REAL
);

-- 审计（涉密环境必需）
CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER,
  token_prefix TEXT,
  agent_id    INTEGER,
  action      TEXT NOT NULL,              -- query | search | graph | admin
  question    TEXT,
  hit_paths   TEXT,                       -- JSON 数组: 命中的文件路径
  model_used  TEXT,
  restricted  INTEGER DEFAULT 0,
  ip          TEXT,
  created_at  REAL
);
CREATE INDEX idx_audit_user ON audit_log(user_id, created_at);

-- 模型用量（按用户/模型统计与配额）
CREATE TABLE model_usage (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER,
  model_id   INTEGER,
  day        TEXT,                        -- YYYY-MM-DD
  calls      INTEGER DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  UNIQUE(user_id, model_id, day)
);
```

## 7. 模块设计

### 7.1 文档目录索引（catalog）

**这是新增工作量最大的一块。**

**监控范围（配置项）**

```
# config.toml
[monitor]
roots = ["\\\\192.0.2.19\\信息化管理\\AI知识库"]   # 当前只监控这一个
poll_interval_sec = 60
```

- 监控根路径列表是**配置**，不是硬编码。加一个共享进 `roots` 即可扩展，看板/权限/检索/统计自动生效。
- 当前 `roots` 只含 `信息化管理\AI知识库`（对应 `W:\AI知识库`）。

**权威口径（唯一真源）**

`catalog` 表**只存可解析文档**。判定白名单固定为：

```
.pdf .docx .xlsx .xlsm .pptx .doc .xls .txt .md .ofd
```

- `kb/parsers.py` 的 `PARSERS` 字典**不是**白名单（它还含 `.dwg` `.js` `.java` `.py` `.html` `.csv` `.json`），仅是"解析器能力表"。catalog 的 `parseable` 判定用上面的固定清单。
- 非文档文件（jpg / png / mp4 / 压缩包 / CAD / psd 等）**不建 catalog 行**，只在 `catalog_stats` 里按共享/类型计数，供看板展示"已跳过的非文档文件"。

```sql
CREATE TABLE catalog_stats (
  share TEXT, ext TEXT, count INTEGER, PRIMARY KEY(share, ext)
);
```

**两套路径的身份统一**

- `catalog.path` 一律用 **UNC 绝对路径**（`\\192.0.2.19\信息化管理\AI知识库\x.pdf`）。
  原因：计划任务若以不同账户运行，映射的 `U:` `V:` `W:` `X:` `Y:` 驱动器字母不可见，UNC 必然可见。
- 现有 `kb.db` 中 `files.path` / `chunks.file_path` 用的是 `W:\...` 形式，**迁移时统一重写为 UNC**，否则 ACL JOIN 会断。
- 路径规范化规则：反斜杠统一、分隔符前后不重复、保留中文原样、去掉末尾空格、**不做 Unicode NFC/NFD 归一**（Windows 与 DSM 均为 NFC，归一会破坏原路径匹配）。匹配时大小写不敏感（Windows 语义）但存储保留原样。

**扫描策略**

监控范围小时，**轮询优先于增量判定**：

| 场景 | 策略 | 耗时 |
|---|---|---|
| **当前（AI知识库）** | 每 60 秒全量 listdir 监控目录（目录小，无需依赖 mtime 判定） | **秒级** |
| 扩展到大目录时 | 每 60 秒全量 listdir 热目录 + 每日全量遍历 | 热目录秒级；全量 11.3 h |

- **文件级 mtime + size 是可靠的变更判据**（实测：目录 mtime 不可靠，但文件自身 mtime 准确）。
- 删除检测：轮询时对比 listdir 快照即可发现（比目录 mtime 可靠）。
- **限流是硬要求**：遍历必须可配置 QPS / 并发上限，避免把 1 GB 内存的 DS218play 压垮。遍历任务与在线服务解耦，且不得阻塞 API 响应。
- 监控范围小时，遍历压力可忽略；但配置项必须保留，因为扩展时需要。

### 7.2 解析流水线

复用 `kb/parsers.py` + `kb/llm.py` + `kb/chunker.py`，新增：

- **任务队列**（jobs 表）+ 后台 worker（asyncio 协程，串行或小并发）
- **两阶段执行**，对应 §2.1 的 Tier 1 / Tier 2：
  - `kind='index'`：解析 → 分块 → FTS5 写入 → bge-m3 向量 → `tier1_done=1`（**约 2.5 天跑完全量**）
  - `kind='extract'`：LLM 提炼 → knowledge 表 → 图谱建边 → `tier2_done=1`（194–486 天，按优先级渐进）
  - 看板分别展示两层进度
- **OCR 分支**：`parsers.py` 已把 `cpg < 50` 的 PDF 标记 `need_ocr=True`。**44% 的 PDF 走这条路**。
  **本期安装 PaddleOCR 与 LibreOffice，但不批量跑存量**（存量不属于监控范围，见 §2.1）。监控范围内新进来的扫描件与老格式会被自动处理。
  - OCR 选 PaddleOCR（中文效果好），**不用 qwen3.5:9b 的 vision**——vision 单图 24.6 s，太慢
  - LibreOffice headless 负责 .doc/.ppt/.xls → 办公格式转换
  - 两者装在 AI 服务器上，不装在 NAS 上
- **OFD**：当前是粗粒度抽文本，精确版式需 ofdparser

### 7.3 知识图谱

复用 `kb/graph.py` 的建图逻辑，但**必须改造**三处：

#### 7.3.1 实体归一必须改成分批，不能全局做

`kb/graph.py:canonicalize_entities` 当前实现把**全部实体名一次性送进 `llm.embed()`**，再算稠密矩阵 `M @ M.T`。到"数十万实体"规模时：

- 单次 embed 数十万个名字 → 请求体数 MB，Ollama 侧不可行
- `M @ M.T` 的中间矩阵是 N² —— 50 万实体 = 2500 亿次点积 × 4 字节 = **1 TB 内存**，直接爆

**改造为分批流水线**：

1. 解析阶段**只做精确归一**（`norm_name` 去括号/全半角/空白）后合并，不做向量聚类
2. 解析完成的文档分批（每批 2000 份）做**批内**向量聚类 + LLM 复核，产出候选合并对
3. 候选对写入 `entity_merge_candidates` 表，**跨批累积、异步合并**（用 `nodes.merged_to` 软链，不删记录）
4. 提供一个"全局归一"后台任务：把累积的候选对分批送去 LLM 复核，结果增量应用

`merge_edges` 当前会 `DELETE FROM edges` 全表重写，改为**增量 upsert**（`ON CONFLICT` 不设唯一键则改为先查后插 + 权重累加）。

#### 7.3.2 边方向归一

已知缺陷：同一关系两次抽取可能 from/to 相反（实测 `采购评审小组 --组成成员--> 物资部门` 与 `物资部门 --组成成员--> 采购评审小组` 两种都出现过）。

归一策略（不引入额外 LLM 成本）：
- 关系标签本身携带方向语义（"负责"、"隶属于" 天然有向；"组成成员" 这类应对称化）
- 建立**关系标签方向表**：`rel_direction(rel) -> 'directed' | 'symmetric'`
- 对称关系（"组成成员"、"协同"）在建图时统一规范为 `(词序较小的节点, 词序较大的节点)`，与抽取顺序无关
- 有向关系保持原样，但记录 `direction_confidence`，供图谱 UI 标注

#### 7.3.3 图谱的 ACL 维度（关键安全缺口）

`nodes` 表当前是**全局**的：`UNIQUE(norm, type)` —— 意味着不同共享、不同密级的同名实体会被合并成一个节点，而 `nodes.props.detail` 里存着某一份源文档的抽取正文。**这构成一条绕过检索层 ACL 的泄露路径。**

改造：
- `nodes` 保留全局唯一，但 `props` 不再存原文 detail；detail 移到 `knowledge`（已有，按 file_path 归属）
- **节点可见性 = 其关联的 knowledge 记录中，至少有一条落在用户可见范围内**
- 图谱 API 的所有查询（`/graph/neighbors`、`/graph/subgraph`）**必须**带 scope 过滤，且**只返回用户可见的节点**
- 节点若部分可见，返回的 detail 只取可见来源的；全部不可见则不返回该节点
- `graph_expand` 拉进来的块也必须过滤（当前 `kb/query.py:152` 从任意 file_path 拉块，**无过滤**）

#### 7.3.4 可视化策略（规模决定）

28 万份文档 → 约 280 万知识点节点、数十万实体，**不可能全量渲染**。分层策略：

| 视图 | 渲染内容 | 规模 |
|---|---|---|
| 全库概览 | 聚合采样：top-N 实体 + 关系类型矩阵 + 分类分布 | ≤ 500 节点 |
| 实体下钻 | 以某实体为中心的 N 跳邻域（默认 N=2） | ≤ 300 节点 |
| 查询联动 | 当前查询命中片段关联的子图 | ≤ 200 节点 |

- 渲染用 cytoscape.js 的 `cose-bilkent` 布局（力导向，适合大图）
- 节点按类型着色、边按关系标签着色；点击节点侧栏显示 detail 与来源跳转
- **前端不做全图遍历**：所有子图数据由后端按需计算后返回

### 7.4 检索层（重写，不是扩展）

> ⚠️ **更正**：上一版设计稿称"当前 `kb/query.py` 已实现"。这是错的。现有 `kb/query.py` 在全量规模下不可用：
> - `chunk_map()`（`query.py:83`）把**全部块文本载入内存** → 140 万中文块约 8 GB
> - 进程内 Python BM25（`query.py:36-68`）是 O(语料) 的 Python dict
> - `store.load_matrix()`（`store.py:192-203`）物化 **fp32** 矩阵 → 140 万 × 1024 × 4B = 5.4 GB（应为 fp16 的 2.67 GB）
> - **仓库里根本没有 FTS5 代码**——关键词通道是全新实现
>
> **结论：检索层按重写对待，不基于 `query.py` 增量改。** `query.py` 的**提示词、JSON Schema、RRF 融合、图谱扩展算法**可复用，数据访问层重写。

四路召回 + RRF 融合：

| 通道 | 实现 | 规模与性能 |
|---|---|---|
| 关键词 | **FTS5（`fts.db` 独立文件）+ 预写入字符二元组** | 索引 5.4–13.6 GB；建索引 0.88 ms/块；查询 3–12 ms（5 万块） |
| 向量 | bge-m3 1024 维 **fp16 mmap** + numpy 分块余弦 | 常驻 1.07–2.67 GB |
| 图谱 | 命中块 → 节点 → 邻居 → 关联块 | 限跳数 + 限数量 |
| **Wiki（人工校正层）** | `wiki_pages` 表，向量加权 1.8×，**保证至少 1 条进入结果集** | 已是 4 路之一，不可丢 |

**实现要点**

- 向量用 `np.memmap` 读 fp16 文件，**不物化 fp32 矩阵**；检索时按 query 维度分块计算，分块载入再释放
- 块正文**按需从 SQLite 取**（`SELECT text FROM chunks WHERE id IN (...)`），不预载全量
- FTS5 独立 `fts.db`，用 `rowid ↔ chunk_id` 显式映射（`fts.db` 的 rowid 就是 chunk_id）
- **ACL 过滤在 SQL 层生效**：FTS 检索先拿 rowid 集合，再与 scope JOIN 后取 top-k；**不能先召回再过滤**（会漏召回，且会泄露被拒文档的存在性）
- 单词查询（如"薪"）无法构成二元组 MATCH → **短于 2 字符的查询降级为向量检索**，并在前端提示

### 7.4.1 四路检索与 ACL 的执行顺序

```
用户问题
  → 解析用户 scope → 构造 ACL 谓词 (share, subpath)
  → ①FTS5: MATCH 得到 rowid 集合 → JOIN chunks + scope 过滤 → 关键词分数
  → ②向量: bge-m3 embed(query) → mmap fp16 矩阵 → top-N 候选 → JOIN scope 过滤
  → ③图谱: 从①②的命中块出发 → 节点 → 邻居 → 关联块 → JOIN scope 过滤
  → ④Wiki: wiki_pages 向量 + 关键词 → JOIN scope 过滤（wiki 页按其来源路径定 scope）
  → RRF 融合 → top-k → 交给生成模型
```

**每一路都必须过 scope 过滤**，包括图谱扩展与 wiki 通道。漏掉任何一路就是一条泄露路径。

### 7.5 模型抽象层

#### 7.5.1 统一 provider

```
OllamaProvider            base_url=http://localhost:11434
OpenAICompatibleProvider  base_url=<配置>  Authorization: Bearer <key>
```

国内主流厂商（DeepSeek、通义千问、混元、智谱 GLM、Kimi、MiniMax 等）均提供 OpenAI 兼容接口；平台只实现这一个通用 provider + 预置配置项（填 base_url / model_id / api_key 即可）。

#### 7.5.2 预置配置项

预置厂商列表：本地 Ollama、DeepSeek、通义千问、腾讯混元、智谱 GLM、月之暗面 Kimi、MiniMax、OpenAI、Anthropic（走 OpenAI 兼容网关）。每个预置项预填 base_url 和常见 model_id，用户只需填 API key。

#### 7.5.3 自定义配置

任填 base_url + model_id + api_key + 上下文长度/超时/温度。

#### 7.5.4 涉密目录的出网管控（硬规则）

**涉密是目录的属性，不是授权的属性。** `dir_scopes.restricted` 独立于 `user_scopes`：同一个目录对所有用户都是涉密的，不随"谁被授权"而变化。这样管理员配错授权不会意外放开门禁。

**判定**：检索命中的任何一条片段，其来源路径若匹配任一 `dir_scopes.restricted=1` 的 (share, subpath)，本次回答即判定为 **restricted**。

**执行策略（已定，不再二选一）**

| 情形 | 行为 |
|---|---|
| 命中 restricted 片段 + 选了本地模型 | 正常执行，响应标注 `restricted: true` |
| 命中 restricted 片段 + 选了云端模型 | **自动路由到本地模型**执行（不报错、不中断），响应标注 `restricted: true` 与 `model_switched: true`、`model_used` 为实际使用的本地模型 |
| 命中 restricted 片段 + 无可用本地模型 | 返回 409，错误体明确"内容涉密且无可用本地模型" |

- **自动路由而非拒绝**：员工不会因为选了云端模型就得到一个错误。代价是可能发生一次 10–17 s 的模型重载——在响应里告知即可。
- 混合情形（部分片段 restricted、部分不 restricted）：**只要有一条 restricted 就整体锁定本地**，不做片段级裁剪（避免"一半外发"的模糊地带）。
- 界面提示："本次回答因涉密已锁定本地模型"。

**离开平台的最后一公里（关键）**

§7.5.4 只管得住平台自己发出的 LLM 调用。但 `/api/v1/query` 的返回包含**引用片段原文**，这些原文会经 skill / MCP 交给 WorkBuddy，再由 WorkBuddy 转发给它自己背后的模型——**平台管不到这一段**。

因此 API 契约必须承担这个责任：

| 字段 | 语义 |
|---|---|
| `restricted` | 本次回答是否命中涉密内容 |
| `citations[].snippet` | 涉密命中时**默认置空**（只给 `locator` + `file` + `chunk_id`）；非涉密时给前 200 字摘录 |
| `citations[].text` | **默认不返回全文**，仅在请求带 `include_text=1` 且该文件非涉密时返回 |
| `X-KB-Restricted: true` | 响应头标记，便于 skill 侧强制拒绝转发 |

skill 包（§7.7.2）的 SKILL.md 必须写明：**当 `restricted=true` 时，不得把任何片段内容转发给外部模型，只可转述答案本身。**

> ⚠️ 这一层是**约定**而非**技术强制**——平台无法阻止 WorkBuddy 转发已经拿到的数据。真正的强制点在"不把涉密原文发出去"：默认不回传 snippet 是关键。是否接受这一残余风险，见 §9 未决事项。

### 7.6 权限与账户

- **密码**：argon2id（或 bcrypt）。平台自建，管理员创建账号时设置初始密码
- **会话**：HttpOnly + SameSite=Lax 的会话 cookie，或短期 JWT + refresh
- **Token**：格式 `kb_<16位随机>`，**只存哈希**（argon2/sha256+pepper），库里存 prefix 便于人工识别；支持吊销、过期、最后使用时间
- **Scope 匹配**：`user_scopes` 中 (share, subpath) 命中即放行；`subpath` 用前缀匹配（注意路径分隔符规范化与中文编码）
- **管理台**：用户 CRUD、目录勾选、涉密目录标记、模型配置、token 签发/吊销、已注册智能体列表、解析任务监控

### 7.7 对外 API 与 WorkBuddy

#### 7.7.1 API 设计

所有接口前缀 `/api/v1`，鉴权 `Authorization: Bearer kb_xxx`。

**问答**

```http
POST /api/v1/query
{
  "question": "三级及以下子企业能不能自建数据机房？",
  "model": "local-qwen",          // 模型 id；"auto" = 用用户默认模型
  "top_k": 8,
  "include_text": false,          // 是否回传片段全文（涉密内容永不受影响）
  "stream": false                 // true 时走 SSE
}
```

```json
{
  "answer": "不能。三级及以下子企业原则上不允许自建或租赁数据机房。",
  "citations": [
    { "chunk_id": 84213, "file": "信息化管理规定.pdf", "path": "\\\\192.0.2.19\\信息化管理\\...\\信息化管理规定.pdf",
      "locator": "p9", "snippet": "…原则上不允许自建或租赁…", "text": null }
  ],
  "model_used": "local-qwen",
  "model_requested": "deepseek-chat",
  "restricted": false,
  "model_switched": false,
  "usage": { "prompt_tokens": 1840, "output_tokens": 260, "latency_ms": 8421 },
  "request_id": "req_8f2a..."
}
```

**文档搜索**

```http
POST /api/v1/search
{ "query": "数据机房 审批", "top_k": 20, "share": "信息化管理", "status": "done", "file_type": ["pdf", "docx"] }
```

```json
{
  "total": 137,
  "results": [
    { "chunk_id": 84213, "file": "...", "path": "...", "locator": "p9",
      "snippet": "…", "score": 0.87, "matched_by": ["vector", "keyword"] }
  ],
  "took_ms": 42
}
```

**图谱**

```http
GET /api/v1/graph/search?q=数据机房&limit=20      # 节点检索（UI 下钻的入口）
GET /api/v1/graph/neighbors?node=<node_id>&hops=1
GET /api/v1/graph/subgraph?node=<node_id>&hops=2&types=entity,knowledge_point
```

节点 id 在**问答与搜索的响应里也必须回传**（`citations[].node_ids`），否则前端拿不到可用于下钻的 id。

```json
{ "nodes": [ { "id": 8812, "name": "数据机房建设或租赁", "type": "entity", "mentions": 3 } ],
  "edges": [ { "src": 8812, "dst": 9021, "rel": "原则上不允许", "weight": 1.0 } ] }
```

**文档与状态**

```http
GET /api/v1/documents?status=pending&share=信息化管理&page=1&page_size=50
GET /api/v1/documents/{catalog_id}     # 元信息 + 解析出的知识点
GET /api/v1/documents/stats             # 看板汇总数字
```

**模型 / 智能体 / 账户**

```http
GET  /api/v1/models
GET  /api/v1/agents                     # 当前 token 名下注册的智能体
POST /api/v1/agents                     # { "name": "...", "kind": "workbuddy", "client_id": "..." }
GET  /api/v1/me                         # 当前 token 对应的用户与其 scope
```

**错误契约**

| 状态码 | 含义 | 响应体 |
|---|---|---|
| 400 | 参数错误 | `{"error":{"code":"invalid_param","message":"...","field":"top_k"}}` |
| 401 | token 无效/已吊销/过期 | `{"error":{"code":"invalid_token","message":"..."}}` |
| 403 | 超出该用户的可见范围 | `{"error":{"code":"out_of_scope","message":"..."}}` |
| 404 | 资源不存在或不可见 | `{"error":{"code":"not_found"}}`（**不区分"不存在"与"不可见"，避免探测**） |
| 409 | 涉密且无可用本地模型 | `{"error":{"code":"restricted_no_local_model","message":"..."}}` |
| 429 | 超配额 | `{"error":{"code":"rate_limited","message":"..."}}` + `Retry-After` 头 |
| 503 | 模型/GPU 不可用 | `{"error":{"code":"model_unavailable"}}` + `Retry-After` |

**流式**：Web UI 需要流式答案。`POST /api/v1/query` 带 `"stream": true` 返回 **SSE**（`text/event-stream`，事件类型 `delta` / `citations` / `done` / `error`）。对外 API **同样支持**流式，WorkBuddy 的 skill 可选用流式或一次性。

**限流**：按 token 限流（QPS + 日配额），响应带 `X-KB-RateLimit-Remaining` / `X-KB-RateLimit-Reset`。防止单点刷爆唯一的 GPU。

**审计**：每次调用写 `audit_log`（谁、何时、问了什么、命中哪些文件、用了哪个模型、是否涉密）。涉密环境必须。

#### 7.7.2 WorkBuddy 接入

**交付物 1：Skill 包**（主路径）

WorkBuddy 使用 SKILL.md 格式，技能放 `~/.workbuddy/skills/`，也支持导入本地技能包（上传 .zip）。包结构：

```
workbuddy-zs-kb/
├── SKILL.md                 # 触发条件、调用流程、参数说明、涉密转发禁令
├── scripts/
│   ├── query.py             # 调 /api/v1/query
│   └── search.py            # 调 /api/v1/search
└── references/
    └── api.md               # 接口契约 + 错误码 + 排查
```

SKILL.md 必须写清：

- **触发条件**：用户问公司制度/规定/流程/某份文档内容时触发
- **凭据来源**：环境变量 `ZB_KB_BASE_URL` 与 `ZB_KB_TOKEN`
- **涉密转发禁令**：`restricted=true` 时**不得把片段内容转发给外部模型**，只可转述答案本身（见 §7.5.4）
- **不编造**：检索不到就说没有，不要凭记忆补充
- **引用呈现**：把 `citations` 渲染为"来源：文件名 + 位置"

**交付物 2：MCP server**（可选第二路径）

WorkBuddy 支持 MCP 插件。把 API 包一层 MCP tools 暴露（`kb_query` / `kb_search` / `kb_graph_neighbors` / `kb_list_documents`），让 WorkBuddy 以工具调用方式使用，比 skill 脚本更稳（schema 约束参数、错误由框架处理）。

**交付物 3：接入说明文档**

- 给员工：怎么找管理员拿 token、怎么装 skill、怎么在 WorkBuddy 里启用、怎么验证（附一条可直接问的问题）
- 给管理员：怎么签发/吊销 token、怎么在管理台看已注册智能体与调用量

### 7.8 Web 前端

- **技术栈**：单页应用，**不引入构建步骤**（本机无 node）——原生 ES Module + vendored 库
- **图谱**：cytoscape.js 3.34.4（本地 vendored）+ cose-bilkent 布局
- **图表**：ECharts（本地 vendored）用于状态分布、模型用量等
- **Markdown 渲染**：marked + highlight.js（本地 vendored）
- **鉴权**：同源 cookie 会话
- **页面**：
  1. **登录**
  2. **状态看板**——总览卡片（总数/已解析/待处理/失败/扫描件待 OCR）+ 按共享/状态/类型筛选的表格 + 批量触发解析
  3. **图谱**——canvas 渲染、按类型着色、点击节点侧栏展示详情与关联、跳转到源文档位置
  4. **问答**——问题框、模型下拉（含"涉密锁定本地"提示）、流式答案、引用片段列表（点击展开原文并高亮）
  5. **管理台**——用户、目录权限、涉密标记、模型配置、token 签发与吊销、已注册智能体、任务队列监控
  6. **API 文档**——在线接口说明 + 示例 curl + 一键生成 WorkBuddy 接入片段

## 8. 部署与运维

- **运行方式**：Windows 服务。本机无 NSSM，用 `sc.exe create` 指向 `python.exe -m uvicorn ...`（需配合一个 .cmd 包装器）；或用计划任务设为开机自启。**建议先用计划任务**，出错更好排查
- **配置**：单一 `config.toml`（服务端口、NAS 路径、模型默认值、扫描限流、ACL 策略）
- **日志**：`D:\kb-platform\kb\logs\`，按天轮转
- **数据目录**：`D:\kb-platform\kb\data\`（kb.db、fts.db、catalog.db、logs）
- **备份**：kb.db + catalog.db 每日冷备到 D 盘另一目录；**NAS 侧只读挂载，绝不写业务数据**（NAS 磁盘只剩 5%）

## 9. 风险与未决事项

**监控范围 = `信息化管理\AI知识库`（已定）**，因此下表按"当前范围"和"扩展到全量"两栏评估。

| 风险 | 当前范围（AI知识库） | 扩展到全量时 | 缓解 |
|---|---|---|---|
| **GPU 单模型并发** | 同样存在 | 同样存在 | 单 worker 串行 + 队列；限制同时可用模型数 |
| **LLM 提炼吞吐** | 30 s/块，可接受 | 194–486 天 | 两层摄入：Tier 1 即时可用，Tier 2 渐进 |
| **catalog 全量构建 11.3 h** | 不适用（目录小，秒级轮询） | 11.3 h，后台断点续跑 | 范围做成配置项 |
| **目录 mtime 不可靠** | 不影响（轮询全量 listdir） | 需文件级 mtime + 每日全量 | §7.1 |
| **44% PDF 需 OCR** | 本期装好 PaddleOCR，新文件自动处理 | 5.8 万份存量不批量跑 | 看板计数可见 |
| **doc/ppt/xls 老格式 3 万+** | 本期装好 LibreOffice，新文件自动处理 | 存量不批量跑 | 看板计数可见 |
| **图谱规模** | 随监控范围线性增长 | 280 万节点无法全量渲染 | §7.3.4 分层 + 按需下钻 |
| **向量常驻** | 可忽略 | 1.1–2.7 GB | fp16 mmap + 分块计算 |
| **FTS5 索引** | 可忽略 | 5.4–13.6 GB | 独立 fts.db |
| **全局实体归一不可行** | 实体少时可做 | 数十万实体会爆内存 | §7.3.1 分批 + 软链合并 |
| **云端模型合规风险** | 同样存在 | 同样存在 | §7.5.4 硬规则 + 单测 + 审计 + API 默认不回传 snippet |
| **API 出口无法技术强制** | 同样存在 | 同样存在 | 默认不回传 snippet；skill 写明禁令；见 Q-C |
| **NAS 磁盘 94.9%** | 只读挂载，不写 | 同 | 所有产物落 D 盘 |
| **单点无 HA** | 接受（内网工具） | 同 | 文档与配置冷备 |

**已解决的问题**

| # | 问题 | 结论 |
|---|---|---|
| ~~Q-A~~ | 上线验收线 | **只监测 AI知识库 路径**，其他路径暂不提取。范围做成配置项，架构不变。 |
| ~~Q-D~~ | OCR / 老格式是否本期 | **本期安装 PaddleOCR + LibreOffice**，但不批量跑存量；监控范围内新文件自动处理。 |
| ~~Q-B~~ | 涉密目录初值 | **精确到子目录**（D13）。`dir_scopes` 初始预置：`综合系统\2.党建（党）`、`2.党建（团）`、`2.党建（宣）`、`2.党建（工）`、`制造物资管理平台开发项目组\AAA2025年审计资料汇集`、`人力资源共享空间\4、薪酬与福利（加密）`、`人力资源共享空间\8、涉及审计`。**`人力资源共享空间` 整个共享同时标涉密**（HR 制度与薪酬不宜放行给非 HR 角色），该规则由管理员在管理台调整。 |
| ~~Q-C~~ | API 出口残余风险 | **接受残余风险，但提供严格模式开关**（D14）：`config.toml` 的 `[api] strict_mode = true` 时，所有响应只含答案与元数据，不含任何片段原文。默认 `false`（涉密片段仍强制不回传）。 |
| ~~Q-E~~ | 密码 / token / 限流 | 管理员设初始密码且首次登录可改；token 默认 90 天可配可吊销；单用户 5 QPS / 500 次每日。 |
| ~~Q-F~~ | 失败详情可见性 | `catalog.status_detail` 仅管理员可见；普通用户只看到失败计数与类型汇总。 |

**扩展到全量时的注意事项（留给未来）**

- 加入新共享前，**先配 `dir_scopes` 再开监控**，否则敏感目录会先被索引后补标记
- 单个共享加入监控后，Tier 1 成本约 2.5 天/28 万份，需评估 NAS 遍历压力
- 实体归一在实体数破 10 万后必须切到 §7.3.1 的分批流水线

## 10. 留给实现者的部分

以下由实现者自行决定，不影响外部行为：

- 目录遍历的具体实现（SMB vs WebDAV；`os.scandir` 递归 vs 显式栈）与限流参数
- SQLite 的 PRAGMA 调优、批量提交策略、索引创建时机
- 前后端目录结构、组件拆分、样式方案
- 队列 worker 的并发度与调度策略
- 向量检索是否用 numpy 矩阵全量余弦，还是改用 faiss/hnswlib
- 日志格式与指标采集方式

以下**必须**按设计实现，不得自行更改（会破坏已验证的结论或安全要求）：

- **检索层按重写对待**，不基于 `kb/query.py` 增量改（`chunk_map` 全量预载、fp32 矩阵、无 FTS5 都不满足规模要求）。可复用的是提示词、JSON Schema、RRF 融合、图谱扩展**算法**
- 关键词检索必须用 **FTS5 + 预写入字符二元组**，**不得**改用 trigram（2 字中文词会全部失效）
- 所有 LLM 结构化生成必须用 **JSON Schema 约束 + `think=false` + `num_predict≥2500`**（否则 40% 输出是非法 JSON）
- 生成模型必须用 `qwen3.5-9b-32k` / `qwen3.5-9b-64k`，**不得**用默认的 `qwen3.5:9b`（4096 上下文会静默截断）
- **ACL 过滤必须在四路检索的每一路、且在 SQL 层生效**；图谱 API 与 wiki 通道同样适用
- 涉密自动路由本地模型必须由后端强制，并有单元测试覆盖
- `catalog.path` 一律用 UNC 绝对路径；`kb.db` 现有 `W:\` 路径必须迁移为 UNC
- 实体归一必须走分批流水线，**不得**对全量实体做稠密 `M @ M.T`
- 扫描遍历必须限速，不得阻塞 API 响应

## 11. 相关文档

- `kb/README.md` — 引擎层的设计决策与实测数据
- `kb/data/kb.db` — 现有 1 份文档的完整图谱样本（155 节点 / 235 边）
- `W:\AI知识库\_kb\wiki\` — 现有 wiki 导出（108 实体页），可作为 wiki 层改造的参照
- `_probe/` — 本设计引用的所有实测脚本

## 12. 评审记录

**Builder Check（独立子代理审阅，2026-10-21）**

审阅结论：*"设计可构建，但按现状不可排期"*。提出 14 项问题，处置如下：

| 编号 | 问题 | 处置 |
|---|---|---|
| Q1 | 回填验收目标缺失 | → §2.1 两层摄入 + §9 Q-A（需需求方定） |
| Q2 | 涉密标记无数据模型 | → §6 新增 `dir_scopes`（目录属性，与授权正交） |
| Q3 | 涉密规则管不到 API 出口 | → §7.5.4 新增 snippet 默认不回传 + skill 禁令 + §9 Q-C |
| Q4 | 自动路由还是拒绝 | → §7.5.4 明确为**自动路由**，并定义混合情形 |
| Q5 | 图谱无 ACL 维度（泄露路径） | → §7.3.3 节点可见性按来源知识记录判定，图谱 API 全量过滤 |
| Q6 | wiki 通道被漏掉 | → §7.4 补为第四路召回 |
| Q7 | `query.py` 不可扩展 | → §7.4 明确检索层按重写对待 |
| Q8 | FTS5 索引体积与原文体积估错 | → §4.5 按真实文档重测修正（10.15 KB/块） |
| Q9 | 全局实体归一会爆内存 | → §7.3.1 改分批 + 软链合并 |
| Q10 | 三处可解析白名单冲突 | → §7.1 固定唯一白名单，catalog 只存可解析文档 |
| Q11 | OCR / LibreOffice 范围未定 | → §9 Q-D（需需求方定） |
| Q12 | API 契约不完整、图谱缺节点检索入口 | → §7.7.1 补全响应体、错误码、SSE、`/graph/search`、引用回传 node_ids |
| Q13 | 目录 mtime 传播未验证 | → §4.6 实测 5 轮（新增仅 1/5 变化）→ §7.1 改策略 |
| Q14 | 路径身份不统一 | → §7.1 统一 UNC + 迁移方案 + 中文路径不做 Unicode 归一 |

> 审阅员报告中"共享名为 党群工作系统 / 财务共享 / 信息知识库"一句有误——已用 DSM `SYNO.Core.Share` 复核，实际为 `video` / `人力资源共享空间` / `信息化管理` / `制造物资管理平台开发项目组` / `综合系统`，共享本身不变，涉密标记应落在**子目录**层级。

**本轮补充实测**（审阅后补做）

- 目录 mtime 传播：5 轮实测，新增文件仅 1/5 变化 → §4.6
- FTS5 索引体积：220 份真实文档 / 313 块 → 10.15 KB/块 → §4.5
- 存储总量修正：9.46–23.65 GB（原估 1.32–6.61 GB，**低估约 1.5–4 倍**）

**需求方最终决策（2026-10-21）**

| 项 | 决策 | 影响 |
|---|---|---|
| 摄入范围 | **只监测 `信息化管理\AI知识库`，其他路径暂不提取** | 取消 11.3 h 全量扫描与 194–486 天回填；监控范围改为配置项，架构不变 |
| OCR / 老格式 | **本期安装 PaddleOCR + LibreOffice，但不批量跑存量** | 监控范围内新文件自动获得完整处理能力；存量不处理 |
| 涉密标记落点 | 目录属性（`dir_scopes`），与用户授权正交 | 已在 §6/§7.5.4 落实 |
| 涉密遇云端模型 | **自动路由到本地模型**（不报错），响应标注 `restricted` / `model_switched` | 已在 §7.5.4 落实 |
| API 出口 | 默认不回传涉密片段原文；响应头 `X-KB-Restricted` | 已在 §7.7.1 落实；残余风险见 Q-C |
