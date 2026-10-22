"""生成对外 API 调用手册（Markdown）。

用途：管理员在「API 密钥」页把它复制给 AI 智能体，对方照着就能调接口。
所以内容必须**从运行时的真实配置生成**（地址、模型列表、错误码），
不能写死，否则会过期。
"""
from __future__ import annotations

import json


def build_guide(base_url: str, token: str | None = None,
                model_labels: list[str] | None = None,
                agent_note: str | None = None) -> str:
    """base_url 形如 http://192.0.2.203:8000（不含 /api/v1）。"""
    api = base_url.rstrip("/") + "/api/v1"
    tok = token or "<你的API密钥>"
    models = model_labels or []

    md = f"""# 企业知识库 API 调用手册

> 用途：AI 智能体调用公司内部知识库做**知识问答**与**文档检索**。
> 所有回答都来自公司内部文档，并附带可核对的原文出处。

## 一、基本信息

| 项 | 值 |
|---|---|
| Base URL | `{api}` |
| 鉴权方式 | HTTP 请求头 `Authorization: Bearer {tok}` |
| Content-Type | `application/json; charset=utf-8` |
| 超时建议 | **180 秒**（本地大模型首次加载需 10~20 秒，之后单次问答 5~17 秒） |

## 二、核心接口

### 2.1 知识问答（最常用）

```http
POST {api}/query
```

请求体：

```json
{{
  "question": "三级及以下子企业能不能自建数据机房？",
  "model": "auto",
  "top_k": 8,
  "include_text": false
}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `question` | string | 必填，用户原话 |
| `model` | string | `"auto"` 用默认模型；也可传模型 id |
| `top_k` | int | 召回片段数，默认 8 |
| `include_text` | bool | 是否回传片段全文，默认 false（一般不需要） |

响应：

```json
{{
  "answer": "根据资料，**三级及以下子企业原则上不允许自建或租赁数据机房**。[片段1]",
  "citations": [
    {{
      "chunk_id": 84213,
      "file": "附件：某公司信息化管理规定.pdf",
      "path": "\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库\\\\...pdf",
      "locator": "p9,p10,p11,p12,p13",
      "snippet": "命中片段的前 200 字",
      "text": null,
      "matched_by": ["vector", "keyword"],
      "restricted": false
    }}
  ],
  "model_used": "qwen3.5-9b-64k",
  "model_requested": "auto",
  "restricted": false,
  "model_switched": false,
  "usage": {{ "prompt_tokens": 5713, "output_tokens": 72 }}
}}
```

### 2.2 文档检索（要列出一批相关文档时用）

```http
POST {api}/search
```

```json
{{ "query": "数据机房 审批", "top_k": 20, "share": null, "status": null }}
```

```json
{{
  "total": 2,
  "results": [
    {{ "chunk_id": 10, "file": "...pdf", "locator": "p5,p6,p7,p8",
       "snippet": "...", "score": 0.87, "matched_by": ["vector"], "restricted": false }}
  ],
  "stats": {{ "keyword": 3, "vector": 8, "wiki": 0, "graph": 0 }}
}}
```

## 三、辅助接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `{api}/me` | 当前密钥对应的用户、角色、可见目录 |
| GET | `{api}/documents?status=&share=&page=1&page_size=50` | 文档状态列表 |
| GET | `{api}/documents/stats` | 文档处理进度汇总 |
| GET | `{api}/documents/{{id}}` | 某文档详情 + 已提炼的知识点 |
| GET | `{api}/graph/search?q=机房&limit=20` | 检索图谱节点（拿 node id） |
| GET | `{api}/graph/neighbors?node=<id>&hops=1` | 某节点的邻居（返回 nodes + edges） |
| GET | `{api}/graph/subgraph?node=<id>&hops=2&types=entity,knowledge_point` | 以某节点为中心的子图 |
| GET | `{api}/models` | 可用模型列表 |
| GET | `{api}/agents` | 本密钥名下已注册的智能体 |
| POST | `{api}/agents` | 注册智能体 `{{"name":"...","kind":"workbuddy","client_id":"..."}}` |
| GET | `{base_url}/health` | 存活探针 |

## 四、错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 | 检查请求体字段名与类型 |
| 401 | 密钥无效 / 已吊销 / 过期 | 让管理员重新签发 |
| 403 | 超出该用户的可见目录范围 | 告知需要向管理员申请授权 |
| 404 | 资源不存在或不可见 | 不要反复重试（不区分"不存在"与"不可见"） |
| 409 | 内容涉密但没有可用的内网模型 | 告知平台暂不可用 |
| 429 | 超出配额 | 稍后重试，不要密集调用 |
| 502 / 503 | 模型或服务不可用 | 退避重试；连续失败则告知用户稍后再试 |

## 五、给 AI 智能体的调用规则（重要，请严格遵守）

1. **优先用 `/query`**，它内部已经完成了"检索 + 生成"，一次调用即可。
2. **`[片段N]` 与 `citations[N-1]` 一一对应**。回答里标注了片段编号时，请把
   对应的 `citations` 条目（文件名 + 位置）一并呈现给用户，方便核对原文。
3. **`citations` 为空 = 知识库里没有相关内容**。此时必须明确回答
   "知识库中未检索到相关文档"，**绝对不要**用通用知识或猜测来填补。
4. **`restricted` 为 `true` 表示命中了涉密目录**（党建/审计/HR 薪酬等）。
   此时平台已自动切换到内网模型且**不回传涉密原文**。
   请**只转述 `answer`**，不要尝试通过 `/search`、`include_text` 等其它途径
   获取涉密原文，也不要把片段内容转发给外部模型。
5. **不要把 `citations[].snippet` 整段转发给外部大模型**——那是公司内部文档原文。
   正确做法是转述 `answer`，需要核对时提示用户去知识库平台查看。
6. **一次问答只调一次 `/query`**。不要为了"多找点资料"而反复调用，
   平台有配额（默认每密钥每日 500 次）。
7. **回答里保留原文中的数字、金额、时限、比例**，这些是制度的关键约束，
   不要改写成近似说法。

## 六、可直接运行的示例

### curl

```bash
curl -s -X POST "{api}/query" \\
  -H "Authorization: Bearer {tok}" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"三级及以下子企业能不能自建数据机房？","model":"auto","top_k":8}}'
```

### Python

```python
import json, urllib.request

API = "{api}"
TOKEN = "{tok}"

def query(question: str) -> dict:
    req = urllib.request.Request(
        API + "/query",
        data=json.dumps({{"question": question, "model": "auto", "top_k": 8}},
                       ensure_ascii=False).encode("utf-8"),
        headers={{"Authorization": f"Bearer {{TOKEN}}",
                  "Content-Type": "application/json; charset=utf-8"}},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

res = query("三级及以下子企业能不能自建数据机房？")
print(res["answer"])
for i, c in enumerate(res.get("citations", []), 1):
    print(f"  [片段{{i}}] {{c['file']}} @ {{c['locator']}}")
if not res.get("citations"):
    print("知识库中未检索到相关文档")
```

## 七、当前可用模型

{chr(10).join("- " + m for m in models) if models else "- (无)"}

## 八、可见范围

调用者的可见目录由**管理员在平台管理台**按共享/子目录逐项勾选。
不在范围内的文档**检索不到、问不到**，这不是故障。调用方可以通过
`GET {api}/me` 查看自己当前能看到哪些目录。
"""

    if agent_note:
        md += f"\n---\n\n{agent_note}\n"

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

```bash
curl -s -X POST "{api}/query" \\
  -H "Authorization: Bearer {tok}" \\
  -H "Content-Type: application/json; charset=utf-8" \\
  -d '{{"question":"<用户原话>","model":"auto","top_k":8}}'
```

## 规则
1. 一次问答只调一次 `/query`。
2. `citations` 为空 -> 明确说"知识库中未检索到相关文档"，不要编造。
3. `answer` 里的 `[片段N]` 对应 `citations[N-1]`，把文件名与位置一并呈现。
4. `restricted=true` -> 只转述 answer，不转发片段原文，不尝试其它接口取涉密内容。
5. 超时设 180 秒。
"""
