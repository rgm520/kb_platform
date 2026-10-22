"""生成对外 API 调用手册（Markdown）。

用途：管理员在「API 密钥」页把它下载给 AI 智能体，AI 照着就能连上平台查知识、下文档。

设计原则：手册是写给**第一次看到它的 AI** 看的，必须先说清"这是什么、我该干什么"，
再给连接方式、查询方法、下载方法和可直接照抄的案例。
内容从运行配置实时生成（地址、模型），不会过期。
"""
from __future__ import annotations


def build_guide(base_url: str, token: str | None = None,
                model_labels: list[str] | None = None,
                deploy: dict | None = None) -> str:
    """base_url 形如 http://192.0.2.203:8000（不含 /api/v1）。"""
    api = base_url.rstrip("/") + "/api/v1"
    tok = token or "<你的API密钥>"
    models = model_labels or []
    dp = deploy or {}

    md = f"""# 企业知识库 · API 接入说明

## 这是什么

这是一个**公司内部文档知识库**的 HTTP 接口。里面是公司 NAS 上的真实文档：
制度、规定、流程、标准、职责说明、会议纪要、项目资料等。

你的能力：通过接口**向公司知识库提问**，并**把整份文档下载下来**。

典型用法：

| 用户说的话 | 你该做的 |
|---|---|
| "公司的信息化管理规定里三级单位能不能自建机房？" | 调 `/query` 提问，答案带原文出处 |
| "把那份信息化管理规定完整读一遍" | 调 `/documents/{{id}}/content` 取全文 |
| "这个制度的附件原文件给我" | 调 `/documents/{{id}}/download` 下原文件 |
| "有哪些关于供应商管理的规定？" | 调 `/search` 找出一批相关文档 |
| "帮我总结这份文档的关键条款" | 调 `/documents/{{id}}/knowledge` 拿已提炼的知识点 |

**它不是什么**：不是搜索引擎，不会回答公司文档以外的问题；不是聊天机器人，
它的回答全部来自检索到的公司文档原文。

---

## 一、开始之前：拿到密钥

平台给每个用户发一个专属 API 密钥，形如 `kb_xxxxxxxx`。
没有密钥什么都做不了——**如果用户没给密钥，直接告诉他去找知识库管理员要，不要编造。**

> **注意两件容易踩的事**
>
> 1. 密钥**明文只在管理员签发的那一刻显示一次**，之后平台里只存哈希、找不回来。
> 2. 管理员的密钥列表里显示的是**密钥前缀**（前 12 位，如 `kb_2MNwd0Ais`），
>    **那是用来人工辨认的，不是密钥**。只拿到前缀去调接口，一定返回 **401**。
>
> 所以：请管理员把**完整密钥**（约 35 位，形如 `kb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）
> 直接发给你。如果对方发过来的只有 12 位，就说明拿到的是前缀，
> **不要重试，直接让管理员重新签发一枚并把完整明文发来**。

拿到后存进环境变量（推荐，命令行和脚本里都不会泄露）：

```bash
export ZB_KB_TOKEN="kb_xxxxxxxx"
export ZB_KB_BASE_URL="{api}"
```

Windows PowerShell：

```powershell
$env:ZB_KB_TOKEN = "kb_xxxxxxxx"
$env:ZB_KB_BASE_URL = "{api}"
```

下文所有例子都假设这两个变量已经设好。

> 环境变量设错了会拿到 401。先用下一节的 `/me` 验证一下，别把时间耗在反复重试上。

---

## 二、连上平台

所有请求都用 **POST 或 GET + HTTP 头**，没有会话、没有 Cookie、没有登录步骤。

```
请求头：
  Authorization: Bearer $ZB_KB_TOKEN
  Content-Type: application/json; charset=utf-8
```

`$API` 代表 `{api}`（即 `$ZB_KB_BASE_URL`）。

**先验证连通性**（可选，1 秒内返回）：

```bash
curl -s "$ZB_KB_BASE_URL/me" -H "Authorization: Bearer $ZB_KB_TOKEN"
```

返回里 `scopes` 是你被授权能看的目录，`user` 是你的账号。

> **超时设 180 秒。** 平台的问答要现调大模型，首次调用要加载模型（10~20 秒），
> 之后单次 5~17 秒。设短了会误判失败。

---

## 三、查知识（最常用）

### 方法

```http
POST $API/query
```

请求体：

```json
{{
  "question": "用户的问题原文",
  "model": "auto",
  "top_k": 8
}}
```

| 字段 | 说明 |
|---|---|
| `question` | **用户的原话**，不要改写、不要总结、不要加前缀（改写会降低命中率） |
| `model` | `"auto"` 即可（用平台默认模型） |
| `top_k` | 召回几个片段，默认 8；问题宽泛时调到 12~20 |
| `include_text` | 想要片段全文就设 `true`，默认 false |

响应：

```json
{{
  "answer": "根据资料，**三级及以下子企业原则上不允许自建或租赁数据机房**。[片段1]",
  "citations": [
    {{
      "chunk_id": 84213,
      "document_id": 1,
      "file": "附件：某公司信息化管理规定.pdf",
      "locator": "p9,p10,p11,p12,p13",
      "snippet": "命中片段的前 200 字",
      "text": null,
      "matched_by": ["vector", "keyword"],
      "restricted": false,
      "content_url": "/api/v1/documents/1/content",
      "download_url": "/api/v1/documents/1/download"
    }}
  ],
  "restricted": false,
  "model_used": "qwen3.5-9b-64k",
  "usage": {{ "prompt_tokens": 5713, "output_tokens": 72 }}
}}
```

**怎么读这个响应**：

- `answer` = 可直接呈现给用户的答案。里面的 `[片段1]` 标记指向 `citations[0]`
- `citations[].file` + `citations[].locator` = 出处。**呈现答案时把出处一起给用户**，
  例如"（来源：附件：xxx.pdf 第 9-13 页）"
- `citations[].snippet` = 命中那段的原文摘录（200 字），可以贴给用户核对
- `citations[].document_id` = 文档 id，要下整篇或原文件时用它
- `citations[].restricted` = 该片段是否涉密
- `restricted`（顶层）= 这次回答是否碰到涉密目录

### 案例 A：问一个问题

```bash
curl -s -X POST "$ZB_KB_BASE_URL/query" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"三级及以下子企业能不能自建数据机房？","model":"auto","top_k":8}}'
```

输出：

```
{{
  "answer": "根据资料，**三级及以下子企业原则上不允许自建或租赁数据机房**。[片段1] ...",
  "citations": [ ... ],
  "restricted": false
}}
```

### 案例 B：Python 里问，并把出处整理出来

```python
import json, os, urllib.request

API = os.environ["ZB_KB_BASE_URL"]
TOKEN = os.environ["ZB_KB_TOKEN"]


def kb_query(question: str, top_k: int = 8) -> dict:
    req = urllib.request.Request(
        API + "/query",
        data=json.dumps({{"question": question, "model": "auto", "top_k": top_k}},
                       ensure_ascii=False).encode("utf-8"),
        headers={{"Authorization": f"Bearer {{TOKEN}}",
                  "Content-Type": "application/json; charset=utf-8"}},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


res = kb_query("软件正版化率要求是多少？")

if not res.get("citations"):
    print("知识库中未检索到相关文档")
else:
    print(res["answer"])
    print("\\n出处：")
    for i, c in enumerate(res["citations"], 1):
        print(f"  [片段{{i}}] {{c['file']}}  第 {{c['locator']}} 页")
```

### 案例 C：找出一批相关文档（不急着让模型答）

```bash
curl -s -X POST "$ZB_KB_BASE_URL/search" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"query":"数据机房 审批","top_k":20}}'
```

返回 `results`，每条含 `document_id` / `file` / `locator` / `snippet` / `score`。
`query` 用 2~4 个中文关键词，空格分隔表示"同时包含"。
拿到 `document_id` 后就可以进第四节的下载流程。

---

## 四、下载文档

`/query` 和 `/search` 只给**片段**。要整篇内容，按需要选下面三种方法。

| 我要做的事 | 用哪个 | 拿到什么 |
|---|---|---|
| 通读整篇、做摘要、逐条核对 | `GET $API/documents/{{id}}/content` | 整篇纯文本 |
| 要原文件（表格、版式、附件） | `GET $API/documents/{{id}}/download` | 原始文件字节流 |
| 直接用已提炼好的结论 | `GET $API/documents/{{id}}/knowledge` | 知识点 + 关系的 JSON |

`{{id}}` 就是 `citations[].document_id` 或 `search` 结果里的 `document_id`。

### 方法 A：取整篇纯文本

```http
GET $API/documents/1/content
```

```json
{{
  "document_id": 1,
  "name": "附件：某公司信息化管理规定.pdf",
  "chars": 10763,
  "chunks": 4,
  "locators": ["p1,p2,p3,p4", "p5,p6,p7,p8", "p9,p10,p11,p12,p13", "p14"],
  "text": "1 / 14\\n某公司信息化管理规定\\n1\\n总则\\n..."
}}
```

```bash
curl -s "$ZB_KB_BASE_URL/documents/1/content" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN"
```

**用例 D：通读整篇并总结**

```python
def kb_doc_text(document_id: int) -> str:
    req = urllib.request.Request(
        f"{{API}}/documents/{{document_id}}/content",
        headers={{"Authorization": f"Bearer {{TOKEN}}"}})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8")).get("text", "")

text = kb_doc_text(1)
print(f"共 {{len(text)}} 字符")
# 然后把 text 交给你的模型做摘要/核对
```

### 方法 B：下载原始文件

```http
GET $API/documents/1/download
```

返回原始字节，响应头带文件名：

```
Content-Disposition: attachment; filename*=UTF-8''%E9%99%84%E4%BB%B6...pdf
X-Document-Id: 1
```

```bash
# 存成文件
curl -s -o "信息化管理规定.pdf" "$ZB_KB_BASE_URL/documents/1/download" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN"

# 或者存成 Python 变量
curl -s "$ZB_KB_BASE_URL/documents/1/download" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN" -o doc.pdf
```

**用例 E：把原文件交给用户或存到本地**

```python
def kb_download(document_id: int, filename: str) -> str:
    req = urllib.request.Request(
        f"{{API}}/documents/{{document_id}}/download",
        headers={{"Authorization": f"Bearer {{TOKEN}}"}})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    with open(filename, "wb") as f:
        f.write(data)
    return filename
```

### 方法 C：取已提炼的知识点

```http
GET $API/documents/1/knowledge
```

```json
{{
  "document_id": 1, "name": "...pdf",
  "chunks": 4, "key_points": 40, "relations": 21,
  "knowledge": [
    {{
      "chunk_id": 1,
      "summary": "该片段规定了数据机房建设的分级管控要求……",
      "key_points": [
        {{ "title": "数据机房建设管控",
           "detail": "总部信息化管理部对公司数据机房建设或租赁实行统一管理……",
           "entities": ["总部信息化管理部", "数据机房"],
           "category": "标准" }}
      ],
      "relations": [
        {{ "from": "总部信息化管理部", "to": "公司数据机房建设或租赁", "relation": "统一管理" }}
      ]
    }}
  ]
}}
```

`category` 取值：制度 / 流程 / 标准 / 职责 / 数据 / 组织 / 其它。
这个接口**不返回原文**，只返回结论，适合直接做成结构化清单给人看。

### 文档清单与状态

```
GET $API/documents?status=&share=&page=1&page_size=50   文档清单与状态
GET $API/documents/stats                                 处理进度汇总
```

`status` 取值：`done` 已解析 / `pending` 待处理 / `failed` 失败 / `skipped` 已跳过。

---

## 五、知识图谱（进阶，可选）

```
GET $API/graph/search?q=机房&limit=20                              搜节点，拿到 node id
GET $API/graph/neighbors?node=<id>&hops=1                            某节点的邻居
GET $API/graph/subgraph?node=<id>&hops=2&types=entity,knowledge_point   中心子图
```

`types` 可选 `entity`（实体）/ `knowledge_point`（知识点）/ `category`（分类）。

节点：`{{"id": 715, "name": "数据机房建设租赁审批权限", "type": "knowledge_point", "mentions": 5}}`
边：`{{"src": 715, "dst": 44, "rel": "归类"}}`

---

## 六、其它接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `$API/me` | 你的账号、角色、能看到的目录 |
| GET | `$API/models` | 平台可用的模型 |
| GET | `$API/agents` | 本密钥名下已注册的智能体 |
| POST | `$API/agents` | 注册智能体 `{{"name":"...","kind":"...","client_id":"..."}}` |
| GET | `{base_url}/health` | 存活探针（不在 `/api/v1` 下） |

---

## 七、出错了怎么办

| 状态码 | 意思 | 你该做什么 |
|---|---|---|
| 400 | 参数写错了 | 检查 JSON 字段名和类型 |
| 401 | 密钥无效 / 已吊销 / 过期 / **只拿到了前缀** | 让管理员重新签发并把**完整明文**发来；不要重试 |
| 403 | 超出你能看的目录，或文档在涉密目录 | 告知需要管理员授权；涉密原文请让用户登录平台网页端看 |
| 404 | 资源不存在或不可见 | 别反复重试（平台不区分"不存在"和"不可见"） |
| 429 | 超出调用配额 | 稍后再试，别密集调用 |
| 502 / 503 | 模型或服务暂时不可用 | 退避重试；连续失败就告诉用户稍后再试 |

---

## 八、必须遵守的规则

1. **检索不到就说检索不到。**
   `citations` 为空 = 知识库里没有相关内容。此时必须明确回答
   "知识库中未检索到相关文档"，**绝对不要**用通用知识或猜测来补。

2. **涉密内容不能外传。**
   顶层 `restricted=true` 表示这次回答碰到了涉密目录（党建、审计、HR 薪酬等）。
   平台已经自动把模型切换到内网模型，也**不回传涉密原文**。你要做的：
   - 只转述 `answer`
   - **不要**去调 `/content`、`/download` 或用 `include_text` 取涉密原文（会 403）
   - **不要**把片段内容转发给外部模型
   - 提示用户"该内容属内部受限范围，请登录知识库平台查看原文"

3. **原文别整段转发给外部模型。**
   `citations[].snippet` 是公司内部文档原文。正确做法是转述 `answer`，
   需要核对时提示用户去平台看。

4. **一次问答只调一次 `/query`。**
   平台有配额（默认每密钥每日 500 次）。不要为了"多找点资料"反复调。

5. **要通读整篇就调一次 `/content`。**
   不要按片段逐个拼——那会又慢又容易踩配额。

6. **保留原文里的数字、金额、时限、比例。**
   这些是制度的关键约束，不要改写成"大约""左右"。

7. **问题原文照传。**
   `question` 传用户说的话，不要自己改写或加前缀。

8. **超时 180 秒。**
   设短了会把正常调用误判成失败。

---

## 九、当前可用模型

{chr(10).join("- " + m for m in models) if models else "- (无)"}

`model` 传 `"auto"` 会用默认的；需要指定时传上面的 id。

---

## 十、你能看到哪些目录

平台管理员给你勾选了可见的共享/子目录。没被授权的文档**检索不到、问不到**——
这不是故障。先用 `GET $API/me` 看你当前能看到哪些目录，再判断是不是需要申请授权。

---

## 附录：平台部署信息

> 这一节是给**运维/接手**的人看的。只做问答和下载的 AI 可以跳过。

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
| 大模型 | Ollama（本机 `qwen3.5-9b-64k` / `qwen3.5-9b-32k` / `qwen3.5:9b`；远端 `192.0.2.77` `qwen3.5:9b` 作为可选冗余） |

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
powershell -NoProfile -ExecutionPolicy Bypass -File D:\\opencode\\tools\\start-platform.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File D:\\opencode\\tools\\start-platform.ps1 -Stop

# 重新注册计划任务（改了配置或脚本后）
powershell -NoProfile -ExecutionPolicy Bypass -File D:\\opencode\\tools\\install-task.ps1

# 看状态
Get-ScheduledTask | Where-Object TaskName -like 'OpenCode-KB*' | Get-ScheduledTaskInfo
Get-Content D:\\opencode\\kb\\logs\\supervisor-heartbeat.json   # 心跳与重启次数
Get-Content D:\\opencode\\kb\\logs\\watchdog.log -Tail 20         # 看门狗记录
Get-Content D:\\opencode\\kb\\logs\\supervisor.out.log -Tail 20   # 监督进程日志
```

### 关键约束（运维须知）

| 约束 | 原因 |
|---|---|
| **必须用计划任务，不能用 `Start-Process` 起** | `Start-Process` 起的进程与调用它的 shell 同属一个 job object，shell 一被回收（会话结束/终端关闭/命令超时被杀）服务就被连带终止，**日志里没有任何异常** |
| **Ollama 远端必须配 `num_ctx`** | runner 默认 `num_ctx=4096`，超长提示**静默截断**（不报错）。本机用 `qwen3.5-9b-64k` 变体；远端用请求级覆盖 |
| **向量化模型必须全局唯一（bge-m3）** | 混用不同 embedding 模型会污染向量空间，检索直接失效 |
| **涉密目录命中 → 强制内网模型 + 不回传原文** | 平台层强制；涉密目录在管理台配置 |
| **不要给写路径的 API 开放 NAS 写权限** | NAS 只读挂载，所有产物落本地 D 盘 |
"""
    return md


def build_skill_snippet(api: str, token: str | None = None) -> str:
    """给 WorkBuddy 用的最小技能片段（备用，页面已不提供下载）。"""
    tok = token or "<你的API密钥>"
    return f"""# 企业知识库技能（WorkBuddy）

## 这是什么
公司内部文档知识库的 HTTP 接口。里面是公司 NAS 上的真实文档（制度、规定、流程、标准等）。
你能用它**向公司知识库提问**，并**把整份文档下载下来**。

## 前置
```bash
export ZB_KB_TOKEN="{tok}"
export ZB_KB_BASE_URL="{api}"
```

## 查知识
```bash
curl -s -X POST "$ZB_KB_BASE_URL/query" \\
  -H "Authorization: Bearer $ZB_KB_TOKEN" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"<用户原话>","model":"auto","top_k":8}}'
```
返回 `answer`（带 `[片段N]`）与 `citations`（file / locator / snippet / document_id）。

## 下载文档
```bash
# 整篇纯文本
curl -s "$ZB_KB_BASE_URL/documents/<document_id>/content" -H "Authorization: Bearer $ZB_KB_TOKEN"
# 原始文件
curl -s -o "文件.pdf" "$ZB_KB_BASE_URL/documents/<document_id>/download" -H "Authorization: Bearer $ZB_KB_TOKEN"
# 已提炼的知识点
curl -s "$ZB_KB_BASE_URL/documents/<document_id>/knowledge" -H "Authorization: Bearer $ZB_KB_TOKEN"
```

## 规则
1. 一次问答只调一次 `/query`（有配额）。
2. `citations` 为空 -> 明确说"知识库中未检索到相关文档"，**不要编造**。
3. `answer` 里的 `[片段N]` 对应 `citations[N-1]`，把文件名与位置一并呈现。
4. `restricted=true` -> 只转述 `answer`；**不要**调 `/content` `/download` 取涉密原文（403），
   也不要把片段内容转发给外部模型。
5. `snippet` 是公司内部原文，**不要整段转发给外部大模型**。
6. 需要通读整篇时用 `/content` 一次取全文。
7. 超时 180 秒。
"""


def build_deploy_info(root: str = "D:/opencode") -> dict:
    return {
        "root": root,
        "data_dir": root + "/kb/data",
        "log_dir": root + "/kb/logs",
    }
