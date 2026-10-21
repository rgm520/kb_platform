# 企业知识库 API 契约

Base URL 由管理员提供，默认形如 `http://<AI服务器内网IP>:8000`。

所有接口：

- 前缀：`/api/v1`
- 鉴权：`Authorization: Bearer <token>`
- 请求/响应：`application/json; charset=utf-8`

---

## POST /api/v1/query — 知识问答

```json
{
  "question": "三级及以下子企业能不能自建数据机房？",
  "model": "auto",
  "top_k": 8,
  "include_text": false
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `question` | string | 必填，问题原文 |
| `model` | string | 模型 id、模型名或 `"auto"`（用该用户默认模型） |
| `top_k` | int | 召回片段数，默认 8 |
| `include_text` | bool | 是否回传片段全文。**涉密片段无论如何都不回传** |

响应：

```json
{
  "answer": "……带 [片段N] 标注的答案……",
  "citations": [
    {
      "chunk_id": 11,
      "file": "附件：xxx.pdf",
      "path": "\\\\<NAS_HOST>\\<share>\\<dir>\\附件：xxx.pdf",
      "locator": "p9,p10,p11,p12,p13",
      "snippet": "命中片段前 200 字（涉密时为空串）",
      "text": null,
      "matched_by": ["vector", "keyword"],
      "restricted": false
    }
  ],
  "model_used": "qwen3.5-9b-64k",
  "model_requested": "deepseek-chat",
  "restricted": false,
  "model_switched": false,
  "usage": {"prompt_tokens": 5713, "output_tokens": 68},
  "request_id": "req_xxx"
}
```

### 涉密语义（务必理解）

| 字段 | 含义 |
|---|---|
| `restricted` | 本次回答是否命中涉密目录 |
| `citations[].restricted` | 该片段是否来自涉密目录 |
| `citations[].snippet` | 涉密 → **空串**；非涉密 → 前 200 字 |
| `citations[].text` | 仅在 `include_text=true` **且**非涉密时才有值 |
| `model_switched` | 是否因涉密把云端模型自动换成了本地模型 |
| `restricted_notice` | 涉密时附带的提示文案 |

**调用方责任**：当 `restricted=true` 时，不得把任何片段内容转发给外部模型，只能转述 `answer`。

---

## POST /api/v1/search — 文档检索

```json
{"query": "数据机房", "top_k": 20, "share": null, "status": null}
```

响应：

```json
{
  "total": 2,
  "results": [
    {"chunk_id": 10, "file": "…pdf", "path": "…", "locator": "p5,p6,p7,p8",
     "snippet": "…", "score": 0.87, "matched_by": ["vector"], "restricted": false}
  ],
  "stats": {"keyword": 3, "vector": 8, "wiki": 0, "graph": 0}
}
```

`matched_by` 说明该片段被哪几路召回：`keyword`（全文）、`vector`（语义）、`wiki`（人工校正层）、`graph`（图谱扩展）。

---

## GET /api/v1/documents — 文档状态

查询参数：`status`、`share`、`page`、`page_size`。

## GET /api/v1/documents/stats — 状态汇总

```json
{"total": 1, "by_status": {"done": 1}, "tier1_done": 1, "tier2_done": 1,
 "non_document_files": 0}
```

## GET /api/v1/graph/search?q=机房&limit=20

```json
{"nodes": [{"id": 715, "name": "数据机房建设租赁审批权限", "type": "knowledge_point", "mentions": 5}]}
```

## GET /api/v1/graph/neighbors?node=715&hops=1

```json
{"nodes": [...], "edges": [{"src": 715, "dst": 44, "rel": "归类"}]}
```

## GET /api/v1/models — 可用模型

```json
{"models": [{"id": 1, "name": "本地 Ollama (qwen3.5-9b-64k)", "provider": "ollama",
             "base_url": "http://localhost:11434", "model_id": "qwen3.5-9b-64k",
             "is_local": true, "is_default": true, "has_key": false}]}
```

## GET /api/v1/agents — 当前 token 名下的智能体

## POST /api/v1/agents — 智能体注册

```json
{"name": "我的WorkBuddy助手", "kind": "workbuddy", "client_id": "wb-001"}
```

---

## 错误码

| 状态码 | 含义 | 调用方处理 |
|---|---|---|
| 400 | 参数错误 | 检查请求体 |
| 401 | token 无效 / 已吊销 / 过期 | 找管理员重新签发 |
| 403 | 超出该用户的可见范围 | 提示用户申请授权 |
| 404 | 资源不存在或不可见 | 不区分「不存在」与「不可见」，避免探测 |
| 409 | 涉密且无可用本地模型 | 告知用户平台暂不可用 |
| 429 | 超出配额 | 稍后重试 |
| 502 | 模型调用失败 | 稍后重试 |
| 503 | 服务不可用 | 稍后重试 |

---

## 权限与可见范围

- 每个 token 绑定一个平台账号，**继承该账号的可见范围**
- 可见范围由管理员按 `(共享, 子目录)` 勾选，例如 `("信息化管理", "AI知识库")`
- **管理员**不受范围限制
- 检索的四路召回（全文 / 向量 / 图谱 / wiki）**每一路都过范围过滤**

## 限流

单 token 每日 500 次调用。响应头 `X-KB-RateLimit-Remaining`（当前未实现，可按需补充）。
