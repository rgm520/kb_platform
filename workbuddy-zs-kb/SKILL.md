---
name: 企业知识库
description: 查询公司 NAS 上的企业知识库（制度、规定、流程、标准、职责、文档内容）。当用户询问公司内部制度、规定、流程、某份文档讲了什么、某个规定的具体条款、某项业务的审批要求，或需要从公司文档中找依据、通读某份文档全文时，使用本技能。提供带原文出处引用的问答、文档检索、文档全文与原文件获取。
---

# 企业知识库

通过公司内部知识库服务查询 NAS 上的文档，回答有出处引用的问题。

## 何时使用

- "公司的信息化管理规定里三级单位能不能自建机房？"
- "采购评审小组由哪些部门组成？"
- "把那份信息化管理规定的第三章完整读一遍"
- "这个制度对供应商有什么要求？"
- 任何需要**从公司文档中找依据**或**通读某份文档**的问题

**不适用**：通用技术问题、编程问题、与公司文档无关的内容。

## 前置配置

两个环境变量：

| 变量 | 说明 |
|---|---|
| `ZB_KB_BASE_URL` | 知识库服务地址，例如 `http://<KB_HOST>:8000/api/v1`（**带不带 `/api/v1` 都行**，脚本自己会补） |
| `ZB_KB_TOKEN` | 管理员签发的 API 密钥（`kb_` 开头） |

**没有 token 就直接告诉用户去找管理员要，不要尝试其它方式绕过。**

## 用技能包脚本查（推荐）

包里的脚本已经把鉴权、URL 拼装、UTF-8 输出都处理好了：

```bash
python scripts/query.py "<用户原话>"              # 问答（一次调用完成检索+生成）
python scripts/search.py "<关键词>" --top-k 20    # 只检索
python scripts/query.py "<用户原话>" --with-snippet   # 连片段原文一起打印（默认不打印）
```

> Windows 上 `python` 有时是 Microsoft Store 的假别名，退出码 1 且无输出；
> 这种情况改用 `py` 或 python.exe 的绝对路径。

## 接口一览

Base URL 记作 `$API`（= `ZB_KB_BASE_URL`），下面所有请求都带：

```
Authorization: Bearer $ZB_KB_TOKEN
Content-Type: application/json; charset=utf-8
```

### 1) 知识问答（首选，一次调用完成检索+生成）

```bash
curl -s -X POST "$API/query" \
  -H "Authorization: Bearer $ZB_KB_TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"question":"<用户原话>","model":"auto","top_k":8}'
```

返回：

```json
{
  "answer": "……带 [片段N] 标注的答案……",
  "citations": [{
    "chunk_id": 84213, "document_id": 1,
    "file": "附件：xxx.pdf", "locator": "p9,p10",
    "snippet": "命中片段前 200 字", "restricted": false,
    "content_url": "/api/v1/documents/1/content",
    "download_url": "/api/v1/documents/1/download"
  }],
  "restricted": false
}
```

### 2) 文档检索（要一批相关片段时）

```bash
curl -s -X POST "$API/search" \
  -H "Authorization: Bearer $ZB_KB_TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"query":"<关键词>","top_k":20}'
```

### 3) 取整篇文档纯文本（要通读全文时）

```bash
curl -s "$API/documents/<document_id>/content" -H "Authorization: Bearer $ZB_KB_TOKEN"
```

`document_id` 取自 `citations[].document_id`。返回 `{"text": "...", "chars": N, "chunks": N, "locators": [...]}`。

### 4) 下载原始文件（需要原格式、表格、版式时）

```bash
curl -s -o "文件名.pdf" "$API/documents/<document_id>/download" \
  -H "Authorization: Bearer $ZB_KB_TOKEN"
```

### 5) 取已提炼的知识点与关系

```bash
curl -s "$API/documents/<document_id>/knowledge" -H "Authorization: Bearer $ZB_KB_TOKEN"
```

返回 `{"key_points": N, "relations": N, "knowledge": [...]}`，每条含 `title/detail/entities/category`。

### 6) 知识图谱

```bash
curl -s "$API/graph/search?q=机房&limit=20" -H "Authorization: Bearer $ZB_KB_TOKEN"
curl -s "$API/graph/neighbors?node=<id>&hops=1" -H "Authorization: Bearer $ZB_KB_TOKEN"
curl -s "$API/graph/subgraph?node=<id>&hops=2&types=entity,knowledge_point" -H "Authorization: Bearer $ZB_KB_TOKEN"
```

### 7) 其它

```
GET  $API/me                    当前身份与可见目录
GET  $API/documents              文档状态列表
GET  $API/documents/stats        处理进度汇总
GET  $API/models                 可用模型
GET  $API/agents                 本密钥名下智能体
POST $API/agents                 注册智能体
```

## 调用规则（重要，请严格遵守）

1. **优先用 `/query`**。它内部已完成"检索 + 生成"，一次调用即可。
2. **一次问答只调一次 `/query`**。不要为了"多找点资料"反复调用，平台有配额（默认每密钥每日 500 次）。
3. **`citations` 为空 = 知识库里没有相关内容**。必须明确回答"知识库中未检索到相关文档"，**绝对不要**用通用知识或猜测来填补。
4. **`[片段N]` 对应 `citations[N-1]`**。回答里标注了片段编号时，把对应的 `citations` 条目（文件名 + 位置）一并呈现给用户，方便核对原文。
5. **`restricted=true` 表示命中了涉密目录**（党建/审计/HR 薪酬等）。平台部署在局域网、模型也在内网，
   该标记**只作审计**：`snippet`、`include_text`、`/documents/*/content`、`/documents/*/download`
   都按普通文档返回，不需要隐藏或改道。
6. **不要把 `citations[].snippet` 整段转发给平台之外的大模型**——那是公司内部文档原文。正确做法是转述 `answer`，需要核对时提示用户去知识库平台查看。
   > 技能包的 `scripts/query.py` / `scripts/search.py` **默认就不打印片段原文**（只给文件名+位置+片段号），正是为了守住这条。
   > 确实要原文时加 `--with-snippet`，或者用 `citations[].content_url` 取整篇——两者都只在你确认需要时才用。
7. **需要通读整篇时**，用 `citations[].content_url` **一次**取全文，不要逐片段拼接。
8. **保留原文中的数字、金额、时限、比例**，这些是制度的关键约束，不要改写成近似说法。
9. **超时设 180 秒**（本地大模型首次加载需 10~20 秒）。
10. `question` 传**用户原话**，不要自己改写（改写会降低检索命中率）。

## 错误处理

| 情况 | 处理 |
|---|---|
| HTTP 401 | 密钥无效/已吊销/过期 → 让用户找管理员重新签发 |
| HTTP 403 | 超出可见范围 → 告知需要管理员授权 |
| HTTP 404 | 资源不存在或不可见 → 不要反复重试 |
| HTTP 429 | 超出配额 → 稍后再试 |
| HTTP 502/503 | 模型或服务不可用 → 退避重试；连续失败则告知用户稍后再试 |

## 完整接口契约

见 `references/api.md`。
