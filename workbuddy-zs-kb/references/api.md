# 企业知识库 API 完整契约

Base URL: `http://<KB_HOST>:8000/api/v1`（以管理员提供为准）

鉴权: `Authorization: Bearer <API密钥>`
Content-Type: `application/json; charset=utf-8`
超时建议: **180 秒**（本地大模型首次加载需 10~20 秒）
编码: UTF-8

---

## 目录

- [一、核心：知识问答](#一核心知识问答)
- [二、文档检索](#二文档检索)
- [三、文档获取](#三文档获取全文--原文件--知识点)
- [四、知识图谱](#四知识图谱)
- [五、其它接口](#五其它接口)
- [六、错误码](#六错误码)
- [七、给 AI 的调用规则](#七给-ai-智能体的调用规则)
- [八、平台部署信息](#八平台部署信息运维参考)

---

## 一、核心：知识问答

```http
POST /query
```

### 请求

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `question` | string | 是 | 用户原话，不要改写 |
| `model` | string | 否 | `"auto"` 用默认模型；也可传模型 id |
| `top_k` | int | 否 | 召回片段数，默认 8；问题宽泛时 12~20 |
| `include_text` | bool | 否 | 是否回传片段全文，默认 false |

```json
{
  "question": "三级及以下子企业能不能自建数据机房？",
  "model": "auto",
  "top_k": 8,
  "include_text": false
}
```

### 响应

```json
{
  "answer": "根据资料，**三级及以下子企业原则上不允许自建或租赁数据机房**。[片段1]",
  "citations": [
    {
      "chunk_id": 84213,
      "document_id": 1,
      "file": "附件：某公司信息化管理规定.pdf",
      "path": "\\\\<NAS_HOST>\\<share>\\<dir>\\附件：...pdf",
      "locator": "p9,p10,p11,p12,p13",
      "snippet": "命中片段的前 200 字",
      "text": null,
      "matched_by": ["vector", "keyword"],
      "restricted": false,
      "content_url": "/api/v1/documents/1/content",
      "download_url": "/api/v1/documents/1/download"
    }
  ],
  "model_used": "qwen3.5-9b-64k",
  "model_requested": "auto",
  "restricted": false,
  "model_switched": false,
  "usage": { "prompt_tokens": 5713, "output_tokens": 72 }
}
```

| 字段 | 说明 |
|---|---|
| `answer` | 已基于检索到的原文生成；`[片段N]` 对应 `citations[N-1]` |
| `citations[].snippet` | 命中片段前 200 字 |
| `citations[].text` | 片段全文，仅 `include_text=true` 时才有 |
| `citations[].locator` | 原文位置：`p9`=第9页，`sheet:名`=工作表，`slide5`=第5页幻灯片 |
| `citations[].matched_by` | `vector` 语义 / `keyword` 关键词 / `wiki` 人工校正 / `graph` 图谱扩展 |
| `citations[].document_id` | 文档 id，取全文/原文件时用 |
| `citations[].content_url` | 整篇纯文本接口路径 |
| `citations[].download_url` | 原始文件下载路径 |
| `restricted` | 本次回答是否命中涉密目录 |
| `model_switched` | 是否因涉密把云端模型换成了内网模型 |

### curl

```bash
curl -s -X POST "http://<KB_HOST>:8000/api/v1/query" \
  -H "Authorization: Bearer <API密钥>" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"question":"三级及以下子企业能不能自建数据机房？","model":"auto","top_k":8}'
```

### Python

```python
import json, urllib.request

API = "http://<KB_HOST>:8000/api/v1"
TOKEN = "<API密钥>"

def query(question: str, top_k: int = 8) -> dict:
    req = urllib.request.Request(
        API + "/query",
        data=json.dumps({"question": question, "model": "auto", "top_k": top_k},
                       ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

res = query("软件正版化率要求是多少？")
print(res["answer"])
for i, c in enumerate(res.get("citations", []), 1):
    print(f"[片段{i}] {c['file']} @ {c['locator']}")
if not res.get("citations"):
    print("知识库中未检索到相关文档")
```

---

## 二、文档检索

```http
POST /search
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 检索词。空格分隔表示同时包含；中文建议 2~4 字关键词 |
| `top_k` | int | 返回条数，默认 20 |
| `share` | string | 限定共享名，可空 |
| `status` | string | 限定状态，可空 |

```json
{ "query": "数据机房 审批", "top_k": 20, "share": null, "status": null }
```

响应：

```json
{
  "total": 2,
  "results": [
    {
      "chunk_id": 10, "document_id": 1,
      "file": "...pdf", "locator": "p5,p6,p7,p8",
      "snippet": "…", "score": 0.87,
      "matched_by": ["vector"], "restricted": false,
      "content_url": "/api/v1/documents/1/content",
      "download_url": "/api/v1/documents/1/download"
    }
  ],
  "stats": { "keyword": 3, "vector": 8, "wiki": 0, "graph": 0 }
}
```

---

## 三、文档获取（全文 / 原文件 / 知识点）

检索与问答只给**片段**。要通读整篇或取原始文件，用下面三个接口。

### A. 取全文纯文本

```http
GET /documents/{document_id}/content
```

```json
{
  "document_id": 1,
  "name": "附件：某公司信息化管理规定.pdf",
  "path": "\\\\<NAS_HOST>\\<share>\\<dir>\\...pdf",
  "chars": 10763,
  "chunks": 4,
  "locators": ["p1,p2,p3,p4", "p5,p6,p7,p8", "p9,p10,p11,p12,p13", "p14"],
  "text": "第 1 页\n……\n\n第 5 页\n……"
}
```

```bash
curl -s "http://<KB_HOST>:8000/api/v1/documents/1/content" \
  -H "Authorization: Bearer <API密钥>"
```

### B. 下载原始文件

```http
GET /documents/{document_id}/download
```

返回原始字节流，响应头：

```
Content-Disposition: attachment; filename*=UTF-8''%E9%99%84%E4%BB%B6...pdf
X-Document-Id: 1
```

```bash
curl -s -o "文件.pdf" "http://<KB_HOST>:8000/api/v1/documents/1/download" \
  -H "Authorization: Bearer <API密钥>"
```

### C. 取知识点

```http
GET /documents/{document_id}/knowledge
```

```json
{
  "document_id": 1, "name": "...pdf",
  "chunks": 4, "key_points": 40, "relations": 21,
  "knowledge": [
    {
      "chunk_id": 1, "summary": "……",
      "key_points": [
        { "title": "数据机房建设管控", "detail": "……",
          "entities": ["总部信息化管理部", "数据机房"], "category": "标准" }
      ],
      "relations": [
        { "from": "总部信息化管理部", "to": "公司数据机房建设或租赁", "relation": "统一管理" }
      ]
    }
  ]
}
```

`category` 取值：制度 / 流程 / 标准 / 职责 / 数据 / 组织 / 其它。

### D. 文档详情与状态

```http
GET /documents/{id}                    元信息 + 知识点 + 三个获取入口的 URL
GET /documents?status=&share=&page=1&page_size=50
GET /documents/stats
```

`status` 取值：`done` 已解析 / `pending` 待处理 / `failed` 失败 / `skipped` 已跳过。
`/documents/stats` 返回：

```json
{ "total": 1, "by_status": { "done": 1 },
  "tier1_done": 1, "tier2_done": 1, "non_document_files": 0 }
```

> **涉密目录只作标记**：`restricted=true` 仅用于审计/统计，不限制 `/content`、`/download` 与 `include_text`。

---

## 四、知识图谱

| 接口 | 用途 |
|---|---|
| `GET /graph/search?q=机房&limit=20` | 按名字搜节点，拿到 `node id` |
| `GET /graph/neighbors?node=<id>&hops=1` | 某节点的邻居 |
| `GET /graph/subgraph?node=<id>&hops=2&types=entity,knowledge_point` | 以某节点为中心的子图 |

`types` 可选 `entity`（实体）/ `knowledge_point`（知识点）/ `category`（分类），逗号分隔。

节点：`{"id": 715, "name": "数据机房建设租赁审批权限", "type": "knowledge_point", "mentions": 5}`
边：`{"src": 715, "dst": 44, "rel": "归类"}`

---

## 五、其它接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/me` | 当前密钥对应的用户、角色、可见目录 |
| GET | `/models` | 可用模型列表 |
| GET | `/agents` | 本密钥名下已注册的智能体 |
| POST | `/agents` | 注册智能体 `{"name":"...","kind":"workbuddy","client_id":"..."}` |
| GET | `/health` | 存活探针（注意不在 `/api/v1` 下） |

---

## 六、错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 | 检查请求体字段名与类型 |
| 401 | 密钥无效 / 已吊销 / 过期 | 让管理员重新签发 |
| 403 | 超出可见范围 | 告知需要管理员授权 |
| 404 | 资源不存在或不可见 | 不要反复重试（不区分"不存在"与"不可见"） |
| 429 | 超出配额 | 稍后重试，不要密集调用 |
| 502 / 503 | 模型或服务不可用 | 退避重试；连续失败则告知用户稍后再试 |

---

## 七、给 AI 智能体的调用规则

1. **优先用 `/query`**，它内部已完成"检索 + 生成"，一次调用即可。
2. **`[片段N]` 与 `citations[N-1]` 一一对应**，把对应条目的文件名与位置一并呈现给用户。
3. **`citations` 为空 = 知识库里没有相关内容**。必须明确回答"知识库中未检索到相关文档"，
   **绝对不要**用通用知识或猜测来填补。
4. **`restricted=true` 只是标记**：片段原文、`include_text`、`/documents/*/content`、
   `/documents/*/download` 都按普通文档返回，不需要隐藏或改道。
5. **不要把 `citations[].snippet` 整段转发给平台之外的大模型**——那是公司内部文档原文。
6. **一次问答只调一次 `/query`**，平台有配额（默认每密钥每日 500 次）。
7. **需要通读整篇时**用 `citations[].content_url` **一次**取全文，不要逐片段拼接。
8. **保留原文中的数字、金额、时限、比例**。
9. **超时设 180 秒**。
10. `question` 传**用户原话**，不要改写。

---

## 八、平台部署信息（运维参考）

| 项 | 值 |
|---|---|
| 部署形态 | Windows 计划任务托管的**监督进程**（不是 Windows 服务） |
| 任务 A | `OpenCode-KB-Platform` —— 登录时启动，动作是 `tools/run-supervisor.ps1` |
| 任务 B | `OpenCode-KB-Watchdog` —— 每 1 分钟检查，监督进程不在就拉起 |
| 监听端口 | **8000**（Web + 对外 API 同端口） |
| 根目录 | `D:/kb-platform` |
| 数据目录 | `D:/kb-platform/kb/data`（数据库 / FTS 索引 / 向量） |
| 日志目录 | `D:/kb-platform/kb/logs` |
| 配置文件 | `config.toml` |
| 数据库 | SQLite（`kb.db` 平台库 + `fts.db` 全文索引 + `vectors/` 向量） |
| 大模型 | Ollama（本机 `qwen3.5-9b-64k`；远端 `<OLLAMA_HOST>` `qwen3.5:9b`） |

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
powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\start-platform.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\start-platform.ps1 -Stop

# 重新注册计划任务（改了配置或脚本后）
powershell -NoProfile -ExecutionPolicy Bypass -File D:\kb-platform\tools\install-task.ps1

# 看状态
Get-ScheduledTask | Where-Object TaskName -like 'OpenCode-KB*' | Get-ScheduledTaskInfo
Get-Content D:\kb-platform\kb\logs\supervisor-heartbeat.json   # 心跳与重启次数
Get-Content D:\kb-platform\kb\logs\watchdog.log -Tail 20         # 看门狗记录
Get-Content D:\kb-platform\kb\logs\supervisor.out.log -Tail 20   # 监督进程日志
```

### 关键约束（运维须知）

| 约束 | 原因 |
|---|---|
| **必须用计划任务，不能用 `Start-Process` 起** | `Start-Process` 起的进程与调用它的 shell 同属一个 job object，shell 一被回收（会话结束/终端关闭/命令超时被杀）服务就被连带终止，**日志里没有任何异常** |
| **Ollama 远端必须配 `num_ctx`** | runner 默认 `num_ctx=4096`，超长提示**静默截断**（不报错）。本机用 `qwen3.5-9b-64k` 变体；远端用请求级覆盖 |
| **向量化模型必须全局唯一（bge-m3）** | 混用不同 embedding 模型会污染向量空间，检索直接失效 |
| **涉密目录命中 → 自动用内网模型（标记仅审计）** | 平台层路由；涉密目录在管理台配置，不影响原文返回 |
| **不要给写路径的 API 开放 NAS 写权限** | NAS 只读挂载，所有产物落本地 D 盘 |
