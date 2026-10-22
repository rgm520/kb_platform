"""生成对外 API 调用手册（Markdown）。

用途：管理员在「API 密钥」页把它复制/下载给 AI 智能体，对方照着就能调接口。
内容必须**从运行时的真实配置生成**（地址、模型列表、部署信息），
不能写死，否则会过期。
"""
from __future__ import annotations

import json


def build_guide(base_url: str, token: str | None = None,
                model_labels: list[str] | None = None,
                deploy: dict | None = None) -> str:
    """base_url 形如 http://192.0.2.203:8000（不含 /api/v1）。"""
    api = base_url.rstrip("/") + "/api/v1"
    tok = token or "<你的API密钥>"
    models = model_labels or []
    dp = deploy or {}

    md = f"""# 企业知识库 API 调用手册

> 用途：AI 智能体调用公司内部知识库做**知识问答**、**文档检索**、**文档获取**。
> 所有回答都来自公司内部文档，并附带可核对的原文出处。

## 〇、三十秒上手

```bash
curl -X POST "{api}/query" \\
  -H "Authorization: Bearer {tok}" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"三级及以下子企业能不能自建数据机房？","model":"auto","top_k":8}}'
```

返回 `answer`（带 `[片段N]` 标注的答案）+ `citations`（每个片段的出处）。

## 一、基本信息

| 项 | 值 |
|---|---|
| Base URL | `{api}` |
| 鉴权方式 | HTTP 请求头 `Authorization: Bearer {tok}` |
| Content-Type | `application/json; charset=utf-8` |
| 超时建议 | **180 秒**（本地大模型首次加载需 10~20 秒，之后单次问答 5~17 秒） |
| 字符编码 | UTF-8（请求与响应都是） |

## 二、知识问答

### 2.1 基础调用

```http
POST {api}/query
```

```json
{{
  "question": "三级及以下子企业能不能自建数据机房？",
  "model": "auto",
  "top_k": 8,
  "include_text": false
}}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `question` | string | 是 | 用户原话，**不要自己改写**（改写会降低检索命中率） |
| `model` | string | 否 | `"auto"` 用默认模型；也可传模型 id（见 `/models`） |
| `top_k` | int | 否 | 召回片段数，默认 8；问题宽泛时调到 12~20 |
| `include_text` | bool | 否 | 是否回传片段全文，默认 false |

### 2.2 响应结构

```json
{{
  "answer": "根据资料，**三级及以下子企业原则上不允许自建或租赁数据机房**。[片段1]",
  "citations": [
    {{
      "chunk_id": 84213,
      "document_id": 1,
      "file": "附件：某公司信息化管理规定.pdf",
      "path": "\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库\\\\...pdf",
      "locator": "p9,p10,p11,p12,p13",
      "snippet": "命中片段的前 200 字",
      "text": null,
      "matched_by": ["vector", "keyword"],
      "restricted": false,
      "content_url": "/api/v1/documents/1/content",
      "download_url": "/api/v1/documents/1/download"
    }}
  ],
  "model_used": "qwen3.5-9b-64k",
  "model_requested": "auto",
  "restricted": false,
  "model_switched": false,
  "usage": {{ "prompt_tokens": 5713, "output_tokens": 72 }}
}}
```

| 字段 | 说明 |
|---|---|
| `answer` | 已基于检索到的原文生成的答案，**`[片段N]` 对应 `citations[N-1]`** |
| `citations[].snippet` | 命中片段前 200 字；**涉密命中时为空串** |
| `citations[].text` | 片段全文，仅在 `include_text=true` 且非涉密时才有 |
| `citations[].locator` | 原文位置（`p9` = 第 9 页，`sheet:名` = 工作表，`slide5` = 第 5 页幻灯片） |
| `citations[].matched_by` | 命中通道：`vector` 语义 / `keyword` 关键词 / `wiki` 人工校正 / `graph` 图谱扩展 |
| `citations[].content_url` | 取**整篇文档纯文本**的接口路径（见第四节） |
| `citations[].download_url` | 下载**原始文件**的接口路径 |
| `restricted` | 本次回答是否命中涉密目录 |
| `model_switched` | 是否因涉密把云端模型自动换成了内网模型 |

### 2.3 案例：一次完整问答 + 追原文

```python
import json, urllib.request

API = "{api}"
TOKEN = "{tok}"

def _call(path, body=None, method="POST"):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={{"Authorization": f"Bearer {{TOKEN}}",
                  "Content-Type": "application/json; charset=utf-8"}})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

res = _call("/query", {{"question": "软件正版化率要求是多少？", "model": "auto"}})

print("答案：", res["answer"])
for i, c in enumerate(res.get("citations", []), 1):
    print(f"[片段{{i}}] {{c['file']}} @ {{c['locator']}}")
    print("   ", c.get("snippet", "")[:120])

if not res.get("citations"):
    print("知识库中未检索到相关文档")
else:
    # 需要通读整篇时，取全文
    doc = _call(res["citations"][0]["content_url"], method="GET")
    print("全文长度：", doc["chars"], "字符")
```

## 三、文档检索

需要一次拿到**一批**相关片段时用检索（问答内部已经含检索，一般不需要单独调）。

```http
POST {api}/search
```

```json
{{ "query": "数据机房 审批", "top_k": 20, "share": null, "status": null }}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 检索词。**空格分隔表示同时包含**；中文建议用 2~4 字关键词 |
| `top_k` | int | 返回条数，默认 20 |
| `share` | string | 限定共享名，可空 |
| `status` | string | 限定状态（`done`/`pending`/`failed`），可空 |

响应：

```json
{{
  "total": 2,
  "results": [
    {{
      "chunk_id": 10, "document_id": 1,
      "file": "...pdf", "locator": "p5,p6,p7,p8",
      "snippet": "…", "score": 0.87,
      "matched_by": ["vector"], "restricted": false,
      "content_url": "/api/v1/documents/1/content",
      "download_url": "/api/v1/documents/1/download"
    }}
  ],
  "stats": {{ "keyword": 3, "vector": 8, "wiki": 0, "graph": 0 }}
}}
```

## 四、文档获取（拿全文 / 原文件 / 知识点）

检索与问答只给**片段**。要通读整篇或取原始文件，用下面三个接口。

| 方法 | 接口 | 拿到什么 | 适用 |
|---|---|---|---|
| **A. 取全文纯文本** | `GET {api}/documents/{{document_id}}/content` | 整篇文档的纯文本（按块顺序拼接） | AI 要通读全文、做摘要、逐条核对 |
| **B. 下载原始文件** | `GET {api}/documents/{{document_id}}/download` | 原始文件流（PDF/Word/…），带 `Content-Disposition` | 需要原格式、表格、版式 |
| **C. 取知识点** | `GET {api}/documents/{{document_id}}/knowledge` | 已提炼的知识点与关系（JSON） | 直接用结构化结论，不再自己读原文 |

### A. 取全文纯文本

```http
GET {api}/documents/1/content
```

```json
{{
  "document_id": 1,
  "name": "附件：某公司信息化管理规定.pdf",
  "path": "\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库\\\\...pdf",
  "chars": 10763,
  "chunks": 4,
  "locators": ["p1,p2,p3,p4", "p5,p6,p7,p8", "p9,p10,p11,p12,p13", "p14"],
  "text": "第 1 页\\n……\\n\\n第 5 页\\n……"
}}
```

```bash
curl -s "{api}/documents/1/content" \\
  -H "Authorization: Bearer {tok}"
```

### B. 下载原始文件

```http
GET {api}/documents/1/download
```

返回原始字节流，响应头带：

```
Content-Disposition: attachment; filename*=UTF-8''%E9%99%84%E4%BB%B6...pdf
X-Document-Id: 1
```

```bash
curl -s -o "文件.pdf" "{api}/documents/1/download" \\
  -H "Authorization: Bearer {tok}"
```

> **涉密目录的原文不提供**：A 和 B 对涉密文档返回 **403**。
> 需要涉密内容时请让有权限的人登录平台网页端查看。

### C. 取知识点

```http
GET {api}/documents/1/knowledge
```

```json
{{
  "document_id": 1, "name": "...pdf",
  "chunks": 4, "key_points": 40, "relations": 21,
  "knowledge": [
    {{ "chunk_id": 1, "summary": "……",
       "key_points": [
         {{"title": "数据机房建设管控", "detail": "……",
           "entities": ["总部信息化管理部", "数据机房"], "category": "标准"}}
       ],
       "relations": [
         {{"from": "总部信息化管理部", "to": "公司数据机房建设或租赁", "relation": "统一管理"}}
       ]}}
  ]
}}
```

### D. 文档状态列表

```http
GET {api}/documents?status=&share=&page=1&page_size=50
GET {api}/documents/stats
```

`/documents` 返回监控范围内文档的清单与状态（`done`/`pending`/`failed`/`skipped`）、
块数、知识点数。`/documents/stats` 是汇总。

## 五、知识图谱

| 接口 | 用途 |
|---|---|
| `GET {api}/graph/search?q=机房&limit=20` | 按名字搜节点，拿到 `node id` |
| `GET {api}/graph/neighbors?node=<id>&hops=1` | 某节点的邻居（返回 nodes + edges） |
| `GET {api}/graph/subgraph?node=<id>&hops=2&types=entity,knowledge_point` | 以某节点为中心的子图 |

`types` 可选 `entity`（实体）/ `knowledge_point`（知识点）/ `category`（分类），逗号分隔。

## 六、其它接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `{api}/me` | 当前密钥对应的用户、角色、可见目录 |
| GET | `{api}/models` | 可用模型列表 |
| GET | `{api}/agents` | 本密钥名下已注册的智能体 |
| POST | `{api}/agents` | 注册智能体 `{{"name":"...","kind":"workbuddy","client_id":"..."}}` |
| GET | `{base_url}/health` | 存活探针 |

## 七、错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 | 检查请求体字段名与类型 |
| 401 | 密钥无效 / 已吊销 / 过期 | 让管理员重新签发 |
| 403 | 超出可见范围，或文档位于涉密目录 | 告知需要管理员授权；涉密原文请登录平台查看 |
| 404 | 资源不存在或不可见 | 不要反复重试（不区分"不存在"与"不可见"） |
| 409 | 内容涉密但没有可用的内网模型 | 告知平台暂不可用 |
| 429 | 超出配额 | 稍后重试，不要密集调用 |
| 502 / 503 | 模型或服务不可用 | 退避重试；连续失败则告知用户稍后再试 |

## 八、给 AI 智能体的调用规则（重要，请严格遵守）

1. **优先用 `/query`**，它内部已完成"检索 + 生成"，一次调用即可。
2. **`[片段N]` 与 `citations[N-1]` 一一对应**。回答里标注了片段编号时，
   请把对应的 `citations` 条目（文件名 + 位置）一并呈现给用户，方便核对原文。
3. **`citations` 为空 = 知识库里没有相关内容**。必须明确回答
   "知识库中未检索到相关文档"，**绝对不要**用通用知识或猜测来填补。
4. **`restricted` 为 `true` 表示命中了涉密目录**（党建/审计/HR 薪酬等）。
   平台已自动切换到内网模型且**不回传涉密原文**。
   请**只转述 `answer`**，不要尝试用 `/documents/*/content`、`/documents/*/download`
   或 `include_text` 去取涉密原文（会返回 403），也不要把片段内容转发给外部模型。
5. **不要把 `citations[].snippet` 整段转发给外部大模型**——那是公司内部文档原文。
   正确做法是转述 `answer`；需要核对时提示用户去知识库平台查看。
6. **一次问答只调一次 `/query`**。不要为了"多找点资料"反复调用，平台有配额
   （默认每密钥每日 500 次）。
7. **需要通读整篇时**，用 `citations[].content_url` 取全文（一次，不要逐片段拼）；
   需要原格式（表格、版式）时用 `download_url`。
8. **回答里保留原文中的数字、金额、时限、比例**，这些是制度的关键约束，
   不要改写成近似说法。

## 九、平台部署信息

> 这部分是给**运维/接手的人**看的；纯调用方可以跳过。

| 项 | 值 |
|---|---|
| 部署形态 | Windows 计划任务托管的**监督进程**（不是 Windows 服务） |
| 任务 A | `OpenCode-KB-Platform` —— 登录时启动，动作是 `tools/run-supervisor.ps1` |
| 任务 B | `OpenCode-KB-Watchdog` —— 每 1 分钟检查，监督进程不在就拉起 |
| 监听端口 | **8000**（Web + 对外 API 同端口） |
| 根目录 | `{dp.get("root", "D:/opencode")}` |
| 数据目录 | `{dp.get("data_dir", "D:/opencode/kb/data")}`（数据库 / FTS 索引 / 向量） |
| 日志目录 | `{dp.get("log_dir", "D:/opencode/kb/logs")}` |
| 配置文件 | `config.toml`（监控目录、轮询间隔、路径、限流、对外地址） |
| 数据库 | SQLite（`kb.db` 平台库 + `fts.db` 全文索引 + `vectors/` 向量） |
| 大模型 | Ollama（本机 `qwen3.5-9b-64k`；远端 `192.0.2.77` `qwen3.5:9b`） |

### 进程结构

```
计划任务 OpenCode-KB-Platform
  └─ tools/run-supervisor.ps1
       └─ 监督进程 kbplatform.supervisor        (写心跳 supervisor-heartbeat.json)
            ├── uvicorn kbplatform.service:app  (Web + API)
            └── kbplatform.pipeline             (摄入流水线)
```

### 数据流

```
NAS 监控目录  →  ①扫描建目录索引  →  ②分块 + 全文索引 + 向量化（Tier 1）
                                          ↓
                                   ③LLM 提炼 + 建图（Tier 2）
                                          ↓
                          全文检索 + 向量检索 + 图谱扩展 + 人工校正层
                                          ↓
                                   融合后交给大模型生成带引用的答案
```

### 运维命令

```powershell
# 启停
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\start-platform.ps1 -Stop

# 重新注册计划任务（改了配置或脚本后）
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\install-task.ps1

# 看状态
Get-ScheduledTask | Where-Object TaskName -like 'OpenCode-KB*' | Get-ScheduledTaskInfo
Get-Content D:\opencode\kb\logs\supervisor-heartbeat.json   # 心跳与重启次数
Get-Content D:\opencode\kb\logs\watchdog.log -Tail 20         # 看门狗记录
Get-Content D:\opencode\kb\logs\supervisor.out.log -Tail 20   # 监督进程日志
```

### 关键约束（运维须知）

| 约束 | 原因 |
|---|---|
| **必须用计划任务，不能用 `Start-Process` 起** | `Start-Process` 起的进程与调用它的 shell 同属一个 job object，shell 一被回收（会话结束/终端关闭/命令超时被杀）服务就被连带终止，**日志里没有任何异常** |
| **Ollama 远端必须配 `num_ctx`** | runner 默认 `num_ctx=4096`，超长提示**静默截断**（不报错）。本机用 `qwen3.5-9b-64k` 变体；远端用请求级覆盖 |
| **向量化模型必须全局唯一（bge-m3）** | 混用不同 embedding 模型会污染向量空间，检索直接失效 |
| **涉密目录命中 → 强制内网模型 + 不回传原文** | 平台层强制；涉密目录在管理台配置 |
| **不要给写路径的 API 开放 NAS 写权限** | NAS 只读挂载，所有产物落本地 D 盘 |

## 十、当前可用模型

{chr(10).join("- " + m for m in models) if models else "- (无)"}

## 十一、可见范围

调用者的可见目录由**管理员在平台管理台**按共享/子目录逐项勾选。
不在范围内的文档**检索不到、问不到**，这不是故障。调用方可以通过
`GET {api}/me` 查看自己当前能看到哪些目录。
"""

    return md


def build_skill_snippet(api: str, token: str | None = None) -> str:
    """给 WorkBuddy 用的最小技能片段。"""
    tok = token or "<你的API密钥>"
    return f"""# 企业知识库技能（WorkBuddy）

## 何时使用
用户询问公司内部制度、规定、流程、标准、职责，或需要从公司文档中找依据时。

## 调用
Base URL: `{api}`
鉴权: `Authorization: Bearer {tok}`

### 1) 知识问答（首选）
```bash
curl -s -X POST "{api}/query" \\
  -H "Authorization: Bearer {tok}" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"<用户原话>","model":"auto","top_k":8}}'
```
返回 `answer`（带 `[片段N]`）与 `citations`（每条的 file / locator / snippet）。

### 2) 文档检索（要一批片段时）
```bash
curl -s -X POST "{api}/search" \\
  -H "Authorization: Bearer {tok}" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"query":"<关键词>","top_k":20}}'
```

### 3) 取整篇文档（需要通读全文时）
```bash
curl -s "{api}/documents/<document_id>/content" -H "Authorization: Bearer {tok}"
```
`document_id` 取自 `citations[].document_id`。

### 4) 下载原始文件（需要原格式时）
```bash
curl -s -o "文件.pdf" "{api}/documents/<document_id>/download" -H "Authorization: Bearer {tok}"
```

### 5) 取已提炼的知识点
```bash
curl -s "{api}/documents/<document_id>/knowledge" -H "Authorization: Bearer {tok}"
```

## 规则
1. 一次问答只调一次 `/query`；不要反复调用（有配额）。
2. `citations` 为空 -> 明确说"知识库中未检索到相关文档"，**不要编造**。
3. `answer` 里的 `[片段N]` 对应 `citations[N-1]`，把文件名与位置一并呈现。
4. `restricted=true` -> 只转述 `answer`。**不要**去调 `/content` 或 `/download`
   取涉密原文（会 403），也不要把片段内容转发给外部模型。
5. `citations[].snippet` 是公司内部文档原文，**不要整段转发给外部大模型**，
   转述 `answer` 即可。
6. 需要通读整篇时用 `/content` 一次取全文，不要逐片段拼接。
7. 超时设 180 秒。
8. 回答里保留原文的数字、金额、时限、比例。
"""


def build_deploy_info(root: str = "D:/opencode") -> dict:
    return {
        "root": root,
        "data_dir": root + "/kb/data",
        "log_dir": root + "/kb/logs",
    }
