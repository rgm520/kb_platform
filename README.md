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
| `admin` / `admin12345` | 默认管理员账号，**请立即改密码** |

## 四个页面

| 页面 | 能做什么 |
|---|---|
| **文档看板** | 监控范围内文档的解析状态、失败原因、两层进度；非文档文件计数 |
| **知识图谱** | cytoscape 交互式图谱，节点按类型着色，可按名搜索、点击展开邻居 |
| **AI 问答** | 选模型提问，答案带 `[片段N]` 引用，可点开看原文出处 |
| **管理台** | 用户与目录授权、涉密目录标记、模型配置、API 密钥签发/吊销、已接入智能体、调用审计 |

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
