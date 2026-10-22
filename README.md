# 企业知识平台

把 NAS 上的文档变成可检索、可视化、可被其他智能体调用的知识资产。

**当前状态：已交付并通过 50 项验收（0 失败）。**

---

## 快速开始

```powershell
# 启动（Web 服务 + 摄入流水线）
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1

# 停止
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1 -Stop

# 只重启 Web 服务
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\restart-service.ps1
```

| 地址 | 说明 |
|---|---|
| `http://192.0.2.203:8000` | 平台网页（局域网内任意机器可访问） |
| `http://127.0.0.1:8000` | 本机 |
| `admin` | 管理员账号。**密码由首次运行时随机生成并打印一次**，无默认口令；或用 `KB_ADMIN_PASSWORD` 指定 |

## 四个页面

| 页面 | 能做什么 |
|---|---|
| **文档看板** | 监控范围内文档的解析状态、失败原因、两层进度；非文档文件计数 |
| **知识图谱** | cytoscape 交互式图谱，节点按类型着色，可按名搜索、点击展开邻居 |
| **AI 问答** | 选模型提问，答案带 `[片段N]` 引用，可点开看原文出处 |
| **管理台** | 用户与目录授权、涉密目录标记、模型配置、API 密钥签发/吊销/删除、已接入智能体、调用审计 |

## 账户与密钥

**密码**

- 管理员初始密码**无默认值**：首次运行 `python -m kbplatform.seed` 时随机生成并打印一次；
  或用环境变量 `KB_ADMIN_PASSWORD` 指定
- 存储用 argon2id 哈希，库里没有明文
- 任何用户可在界面右上角「修改密码」自助改密（`POST /api/v1/auth/change-password`）
  - 必须校验原密码；新密码 ≥ 8 位；不能与原密码相同
- 管理员在管理台建用户时设定初始密码，员工首次登录后可自行修改

**API 密钥**

- 每用户一枚，`kb_` 开头；**库里只存哈希**，明文仅签发时显示一次
- 生命周期是三步：**签发 → 吊销 → 删除**
  - 吊销：立即失效，但记录保留便于人工核对（`DELETE /api/v1/admin/tokens/{id}`）
  - 删除：**仅允许删除已吊销的**，彻底移除记录（`DELETE /api/v1/admin/tokens/{id}/purge`）
  - 未吊销的密钥直接删会被拒（400），避免"点一下就永久消失"而管理员没意识到该 token 还在被某个智能体使用
- 支持过期时间（默认 90 天）、最后使用时间

## 部署与自愈

平台由**监督进程**托管，并注册了两个 Windows 计划任务：

| 任务 | 作用 |
|---|---|
| OpenCode-KB-Platform | 登录时启动，**动作就是监督进程本身**（前台常驻） |
| OpenCode-KB-Watchdog | 每 1 分钟检查一次，异常则重新拉起平台任务 |

`
计划任务 OpenCode-KB-Platform
  └─ 监督进程 kbplatform.supervisor          (心跳写 supervisor-heartbeat.json)
       ├── uvicorn kbplatform.service:app    (Web 服务，:8000)
       └── kbplatform.pipeline               (摄入流水线)
`

**为什么不用 Start-Process 直接起**：那样起的进程与调用它的 shell 在同一个
job object 里，shell 一被回收（会话结束 / 终端关闭 / 命令超时被杀），服务会被
**连带杀掉且日志里没有任何异常**——这是实际踩过的坑。计划任务由 Task Scheduler
托管，不受交互 shell 生命周期影响。

**自愈能力**（均已实测）：

| 故障 | 恢复 |
|---|---|
| Web 服务子进程被杀 | 监督进程 ~10 秒内重启 |
| 摄入流水线被杀 | 监督进程 ~25 秒内重启 |
| Web 服务健康检查连续失败 3 次 | 监督进程重启它 |
| 全部进程被杀 | 看门狗 ~30 秒内恢复（其中约 16 秒不可达） |
| 监督进程被杀 | 看门狗检测到心跳过期后重新拉起 |

运维命令：

`powershell
# 注册/启动（首次部署或改配置后）
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\install-task.ps1

# 手动启停
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1 -Stop

# 看状态
Get-ScheduledTask | Where-Object TaskName -like 'OpenCode-KB*' | Get-ScheduledTaskInfo
Get-Content D:\opencode\kb\logs\supervisor-heartbeat.json    # 心跳与重启次数
Get-Content D:\opencode\kb\logs\watchdog.log -Tail 20          # 看门狗记录
`

> ⚠️ Task Scheduler 自带的「失败后重启」对**外部强杀**不生效（实测
> LastTaskResult=0xFFFFFFFF 但任务直接转 Ready 不会重启），所以用周期看门狗兜底。

## 目录结构

```
D:\opencode\
├── config.toml                监控根、轮询间隔、路径、限流
├── kb\                        引擎层（已验证：解析/分块/提炼/图谱/检索/问答）
├── kbplatform\                平台层
│   ├── config.py              配置加载
│   ├── paths.py               UNC 路径规范化
│   ├── db.py                  平台库 schema + 路径迁移
│   ├── fts.py                 FTS5 全文索引（中文二元组分词）
│   ├── vectors.py             fp16 mmap 向量库
│   ├── catalog.py             目录扫描器
│   ├── monitor.py             轮询变更检测
│   ├── jobs.py                任务队列
│   ├── indexer.py             Tier 1：解析→分块→FTS→向量
│   ├── extract.py             Tier 2：LLM 提炼→knowledge→图谱
│   ├── converters.py          老格式转换 + OCR
│   ├── retrieval.py           四路召回 + RRF + ACL
│   ├── scopes.py              可见范围 / 涉密判定
│   ├── auth.py                密码、token、会话
│   ├── models.py              模型注册表 + 涉密自动路由
│   ├── service.py             FastAPI 服务（Web + 对外 API）
│   ├── seed.py                初始数据
│   └── pipeline.py            编排
├── web\                       前端（无构建步骤）
├── workbuddy-zs-kb\           WorkBuddy 技能包
├── tools\                     启动/重启/更新脚本
└── docs\
    ├── 接入说明.md              给员工和管理员的操作手册
    └── superpowers\
        ├── specs\2026-10-21-kb-platform-design.md   设计文档
        └── plans\2026-10-21-ingestion-pipeline.md   实施计划
```

## 对外 API

Base: `http://<主机>:8000/api/v1`，鉴权 `Authorization: Bearer kb_xxx`

```
POST /api/v1/query            知识问答（带引用溯源）
POST /api/v1/search           文档检索
GET  /api/v1/documents        文档状态列表
GET  /api/v1/documents/stats  状态汇总
GET  /api/v1/documents/{id}   文档详情 + 知识点
GET  /api/v1/graph/search     图谱节点检索
GET  /api/v1/graph/neighbors  图谱邻居展开
GET  /api/v1/models           可用模型
GET  /api/v1/agents           当前 token 名下的智能体
POST /api/v1/agents           智能体注册
GET  /api/v1/me               当前身份与可见范围
```

管理员接口在 `/api/v1/admin/*`（用户、范围、涉密、模型、密钥、审计）。

## WorkBuddy 接入

技能包 `workbuddy-zs-kb.zip`（8 KB）可直接导入 WorkBuddy。

```
workbuddy-zs-kb/
├── SKILL.md              触发条件、调用流程、涉密转发禁令
├── scripts/query.py      问答
├── scripts/search.py     检索
└── references/api.md     完整接口契约
```

配置两个环境变量后即可用：

```powershell
$env:ZB_KB_BASE_URL = "http://192.0.2.203:8000"
$env:ZB_KB_TOKEN   = "kb_管理员签发的密钥"
```

详见 `docs/接入说明.md`。

## 模型接入

平台的模型来自 models 表，管理台可增删。两种 provider：

| provider | 说明 | 必填 |
|---|---|---|
| ollama | 内网自建 Ollama（可跨主机） | base_url / model_id / **num_ctx** |
| openai_compatible | 云端 OpenAI 兼容接口 | base_url / model_id / api_key |

### 已接入的 Ollama

| 模型 | 地址 | num_ctx |
|---|---|---|
| 本地 qwen3.5-9b-64k | http://localhost:11434 | 变体自带 65536 |
| 本地 qwen3.5-9b-32k | http://localhost:11434 | 变体自带 32768 |
| 远端 qwen3.5:9b | http://192.0.2.77:11434 | **65536（请求级覆盖）** |

### ⚠️ 远端 Ollama 的上下文陷阱

Ollama 的 runner **默认 num_ctx=4096**，超长提示会被**静默截断**——不报错、不提示，
只是悄悄丢掉后面的内容。实测：49500 字符的提示在默认配置下只处理了 2050 token。

远端机器往往无法 SSH（本例 22 端口关闭），建不了 qwen3.5-9b-64k 这样的变体，
**唯一可靠的办法是请求级覆盖** options.num_ctx——Ollama 支持按请求指定，
并会写回驻留 runner。平台已按此实现，配置在模型 extra.num_ctx。

管理台的「模型配置 → 测试」按钮会回显**实际处理的 prompt token 数**，
一眼就能看出有没有被截断。

## 文档解析算力池

知识提炼（Tier 2）是最吃算力的环节——约 30 s/块，全量 28 万份文档需要 194 天。
平台支持把**多个大模型**（本机 + 内网其它机器）配进算力池，由平台调度分摊。

管理台 → 模型配置 → 「文档解析算力池」勾选参与提炼的模型并设权重。

| 规则 | 说明 |
|---|---|
| **只有生成模型能入池** | 向量化必须全局唯一用 bge-m3，混用会污染向量空间、检索直接失效 |
| **按文档分派（文件级粘性）** | 同一份文档的所有块交给同一个模型，知识点风格一致；不同文档分给不同模型，吞吐叠加 |
| **加权轮转** | nginx 式平滑加权轮转，权重 3:1 → 分配序列 A,A,A,B,A,A,A,B |
| **每成员独立并发上限** | 默认 1。本地 3060 只能并发 1，远端另算，否则会被单个模型打满 |
| **健康检查** | 不可用成员自动跳过（Ollama 探活 /api/tags；云端查有没有 Key） |
| **同族提醒** | 池内模型族不同会产生知识点风格差异，界面上会红字警告 |

当前池成员（两者同族，产物风格一致）：

`
本地 Ollama (qwen3.5-9b-64k)      http://localhost:11434        权重 1
远端 Ollama (192.0.2.77 · qwen3.5:9b)  http://192.0.2.77:11434  权重 1  num_ctx 65536
`

## 安全模型

| 概念 | 含义 | 存储 |
|---|---|---|
| **可见范围** | 谁能看到什么 | `user_scopes` (user, share, subpath) |
| **涉密标记** | 目录本身敏不敏感，与授权无关 | `dir_scopes` (share, subpath, restricted) |

- 检索四路（全文 / 向量 / 图谱 / wiki）**每一路都在 SQL 层过滤**
- 命中涉密 → 自动切本地模型 + 不回传片段原文
- token 只存哈希，明文只显示一次
- 管理员不受范围限制
- 每次调用记审计

## 转换器（老格式 / OCR）

| 能力 | 状态 | 实测 |
|---|---|---|
| LibreOffice（.doc/.ppt/.xls → 现代格式） | ✅ 可用 | 真实 .doc 3.6MB → 11.7s → 解析出 685 块 / 38,707 字 / 32 表 |
| PaddleOCR（扫描件 PDF → 文本） | ✅ 可用 | **约 136s/页**（100dpi，CPU） |

OCR 的两个实测结论：

1. **必须禁用 oneDNN**（`FLAGS_use_mkldnn=0` + `enable_mkldnn=False`）。本机 CPU 上
   oneDNN 路径会抛 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`。
2. **100 DPI 与 150 DPI 识别质量相同**（248 行 vs 246 行）但快 21%，故默认 100。
   可用环境变量 `KB_OCR_DPI` 调整。

> ⚠️ **CPU OCR 不适合批量处理存量**：5.8 万份扫描件（按 3 页算）约需 **275 天**连续运行。
> 要处理存量需要 GPU 版 paddlepaddle-gpu（本机 RTX 3060 可跑）或云 OCR（但涉密内容不可出内网）。
> 当前策略与决策一致——**只处理监控范围内新进来的文件**，存量暂不识别。

## 关键约束（改动前务必读 `kb/README.md`）

1. 生成模型必须用 `qwen3.5-9b-32k` / `qwen3.5-9b-64k`，**不得**用默认的 `qwen3.5:9b`（4096 上下文会静默截断）
2. 结构化生成必须用 **JSON Schema + `think=false` + `num_predict>=2500`**（裸输出 40% 概率是非法 JSON）
3. 全文检索必须用 **FTS5 + 预写入字符二元组**，**不得**用 trigram（2 字中文词会全部失效）
4. 向量必须 **fp16 存盘 + mmap**，不得物化 fp32
5. 实体归一必须**分批**，不得对全量实体做稠密 `M @ M.T`
6. 路径一律 **UNC 绝对路径**，不做 Unicode NFC/NFD 归一
