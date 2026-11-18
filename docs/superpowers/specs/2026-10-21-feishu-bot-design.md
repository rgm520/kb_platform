# 飞书机器人接入设计（kbplatform）

日期：2026-10-21
状态：待批准
修订：v2（吸收 builder check 结论，修正安全模型）

---

## 1. 目标

给企业知识平台加一个飞书入口：员工在飞书里给机器人下指令，机器人在 `D:\kb-platform`
工作区干活（读写文件、跑脚本、查知识库），完成后把结果发回飞书。

**机器人是这台机器上 OpenCode 智能体的遥控器**，不是一个独立的关键词问答机器人。

### 非目标（做不到就是失败）

- 不做预检索式问答（不预先查知识库再喂给模型）。agent 自己按需调知识库。
- 不做飞书卡片交互、审批流、二次确认弹窗。
- 不做 Web 管理台新页面。只允许在既有页面上做必要的**文案修正**和**一个开关按钮**。
- 不做 agent 沙箱容器。安全上限由 opencode 权限规则 + 知识库 API 共同决定（§7）。
- 不做多租户、不做飞书以外 IM 平台。
- 不写 TypeScript / JS 插件。全部用 Python（§5.3 记录了为什么不用插件）。

---

## 2. 已确认的决策

| 决策项 | 结论 |
|---|---|
| 机器人职责 | 通用指令代理：接收指令 → agent 干活 → 回复结果 |
| 事件接入方式 | 长连接（WebSocket），无公网 IP 也能收消息 |
| 身份模型 | 飞书 open_id 映射到 kbplatform 平台账号，scope 跟随账号 |
| 首次私聊 | 自动建平台账号（默认无可见目录），管理员后续配 scope |
| 模型 | 按敏感度自动路由：涉密 → 本地 ollama，公开 → 云端 free 模型 |
| OpenCode 服务 | 独立常驻进程，不复用桌面版共享服务（45278） |

---

## 3. 整体架构

```
飞书用户（私聊 / 群聊 @机器人）
   │  im.message.receive_v1
   ▼
[feishu-bot 进程]  python -m kbplatform.feishu.runner
   │  ├─ spawn 并守护 opencode serve 子进程
   │  ├─ 飞书长连接 client（lark-oapi ws.Client）
   │  ├─ 身份映射 / 自动建号
   │  └─ 会话映射（chat_id + open_id）→ opencode session
   ▼
[opencode serve]  127.0.0.1:4599  （Basic auth: opencode / <password>）
   │  POST   /api/session                     建会话（含权限规则）
   │  POST   /api/session/{id}/prompt         下指令
   │  GET    /api/session/{id}/message        轮询取结果
   │  POST   /api/session/{id}/model          换模型
   │  PATCH  /api/session/{id}                更新权限规则
   ▼
[知识库]  agent 用 shell 调 tools/kbq.py
          kbq.py 读 .feishu/tokens/<session>.token
          → POST http://127.0.0.1:8000/api/v1/query
          → 服务端按该用户的 scope 过滤命中，涉密不回传原文
   ▼
飞书回复（超长分段发送）
```

### 为什么不做「bot 预检索再喂给模型」

预检索会把「问题」和「检索结果」硬绑死，agent 做不了多步推理（先查 A 再决定要不要查 B），
而且预检索的权限过滤发生在 bot 侧一次，agent 拿到的片段就固定了。

改成 agent 自主调用后，权限过滤发生在**每一次**检索上，用的是**当前用户**的 token，
越权面比预检索更小。

---

## 4. 代码结构

```
kbplatform/feishu/
  __init__.py
  config.py     凭据与参数装载（环境变量 > secrets.toml > config.toml）
  client.py     飞书开放平台 API 客户端（发消息、取用户、取 bot 信息）
  identity.py   open_id ↔ 平台账号；首次私聊自动建号
  sessions.py   (chat_id, open_id) → opencode session 的持久化
  opencode.py   OpenCode HTTP 客户端（Basic auth；session/prompt/message/model/permission）
  kbcred.py     知识库 token 签发、轮换、按 session 落文件
  routing.py    模型路由 + session 权限规则生成
  bot.py        事件处理主循环（触发判定、去重、并发、回复分段）
  runner.py     进程入口：拉起 opencode serve → 启动长连接
tools/kbq.py    agent 侧 helper：调知识库 query/search 并格式化输出
```

对现有模块的改动，共三处，**全部是追加**：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `kbplatform/db.py` | `SCHEMA_SQL` 追加两张新表 | 飞书身份与会话映射 |
| `kbplatform/service.py` | 追加 `PUT /api/v1/admin/users/{user_id}/active` | 停用开关（现有没有任何改 `users.active` 的接口） |
| `web/static/app.js` | ① 空 scope 显示「（未授权）」而非「（全部）」；② 用户行加一个启用/停用按钮 | ①现在普通用户空 scope 在界面上显示成「（全部）」，管理员会误判成已有全量权限；②配合上面的停用接口 |

`users` 表**不加列**——飞书身份放独立表，不污染平台用户表。

---

## 5. 详细设计

### 5.1 飞书长连接

- `lark-oapi 1.7.5`（已装）。`lark.ws.Client(app_id, app_secret, event_handler=...)`，
  `auto_reconnect=True`，SDK 内部处理重连。
- 事件：`im.message.receive_v1`（`lark_oapi.api.im.v1.P2ImMessageReceiveV1`，已确认存在）。
- 触发判定：
  - `chat_type == "p2p"` → 响应。
  - `chat_type == "group"` → 仅当消息 `mentions` 含机器人自身的 open_id 才响应。
    机器人 open_id 用 `GET /open-apis/bot/v3/info` 取一次并缓存。
  - `message_type != "text"` → 回「暂只支持文本消息」。
- 幂等：以 `message_id` 为键，进程内 LRU 去重。
- 文本提取：`event.message.content` 是 JSON 串 `{"text":"..."}`；群聊里 @机器人 的
  占位符（形如 `@_user_1`）要剥掉。

### 5.2 身份映射与自动建号

新增表：

```sql
CREATE TABLE IF NOT EXISTS feishu_users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_open_id  TEXT UNIQUE NOT NULL,
  feishu_union_id TEXT,
  feishu_name     TEXT,
  user_id         INTEGER,          -- kbplatform.users.id
  created_at      REAL,
  last_seen_at    REAL
);

CREATE TABLE IF NOT EXISTS feishu_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_user_id INTEGER NOT NULL,
  chat_id        TEXT NOT NULL,
  chat_type      TEXT,              -- p2p / group
  session_id     TEXT,              -- opencode session id
  model_id       TEXT,              -- 当前 session 用的模型（排查用）
  created_at     REAL,
  updated_at     REAL,
  UNIQUE(feishu_user_id, chat_id)
);
```

首次私聊（`feishu_users` 里查不到 open_id）：

1. 调 `GET /open-apis/contact/v3/users/{open_id}` 取昵称与 `department_ids`。
   失败不阻断，昵称降级为「飞书用户」。**只存部门 ID 串，不取部门名**——
   取部门名要额外的部门读取权限，收益低。
2. 建平台账号：
   - `username` = `fsu_` + `md5(open_id)` 前 10 位（唯一且不可枚举）
   - `password_hash` = 随机 32 字节哈希（**该账号无法通过 Web 登录**，密码无人知晓；
     平台账号只作为 scope / 审计的归属主体）
   - `role` = `user`，`display_name` = 飞书昵称，`dept` = 部门 ID 串
   - **不写 user_scopes** → 可见范围为空，知识库查不到任何东西
3. 插 `feishu_users`。
4. **继续执行这条指令**，另外单独发一条提示：
   「已为你开通平台账号（可见范围为空）。需要查知识库请联系管理员开通目录权限。」

群里 @机器人但没建号的人 → **静默忽略**（不在群里暴露机器人存在，也不给陌生人
在群里开号的入口）。

### 5.3 agent 的知识库访问通道

agent 通过 shell 调 `tools/kbq.py`。凭证怎么给它：

1. bot 为该用户签发**一枚长期 API token**：
   `auth.issue_token(conn, user_id, label="feishu-bot", ttl_days=90)`。
   - 明文只在签发时返回一次，**只存在于 bot 进程内存 + 一个 per-session 文件**。
   - 库里仍只存哈希，与平台「token 只存哈希」原则一致。
   - bot 重启时重新签发并吊销旧的（`auth.revoke_token`）。
   - **每用户一枚**（不是每 session 一枚）：`api_tokens` 表不会膨胀，
     `service.py` 里按 token id 计的日配额（`RATE_DAILY=500`）就等于按人计。
2. 每个 session 建一个文件 `.feishu/tokens/<session_id>.token`（内容是明文 token），
   并在 session 环境变量里注入 `KB_TOKEN_FILE=<该文件路径>`。
   - **token 不进模型上下文，不进对话记录**——agent 只看到路径，
     路径本身出现在 transcript 里无害。
   - 文件放在 `.feishu/` 下，并用权限规则禁止 agent 用 `read`/`edit` 读它（§5.4）。
3. agent 调用：
   ```
   python tools/kbq.py query "三级及以下子企业能不能自建数据中心机房？"
   python tools/kbq.py search "数据中心" --top-k 10
   ```
   `kbq.py` 读 `KB_TOKEN_FILE` 指向的文件拿 token，调知识库现有 API
   （`/api/v1/query`、`/api/v1/search`），**不新增知识库端点**。
4. 退出码约定（让 bot 和 agent 都能区分故障）：
   | 码 | 含义 | agent 看到什么 |
   |---|---|---|
   | 0 | 正常 | 结果 |
   | 3 | token 无效/过期 | `KB_AUTH_EXPIRED: <原因>` |
   | 4 | 知识库服务不可达 | `KB_UNREACHABLE: <原因>` |
   | 5 | 超配额 | `KB_QUOTA_EXCEEDED: <原因>` |

   没有这组区分，agent 会把「服务挂了」说成「没查到相关内容」——把故障伪装成空结果。

**为什么不用 opencode 插件做工具**：插件（`ctx.session.hook("context")` 改写
`event.tools`）确实能做到 token 完全不进上下文，但要用 TypeScript 写、要配
`opencode.json`、还要解决「插件怎么拿到 session 对应的用户 token」。
`kbq.py` 方案用纯 Python 达成同样的「token 不进上下文」，代价只是多一个
per-session token 文件。权衡后选后者。

### 5.4 模型路由与 session 权限规则

#### 模型路由

两套模型命名空间，互不相干：

| 用途 | 配置在哪 | 取值 |
|---|---|---|
| 知识库内部答案生成 | kbplatform `models` 表（`ollama` / `openai_compatible`） | 已有 `route_for()` 涉密自动切本地逻辑，不动 |
| agent 自身 | OpenCode 的 model 列表 | 见下 |

agent 自身模型：

```
若 该用户可见范围(user_scopes) 与 任一涉密目录(dir_scopes.restricted=1) 有交集
    → 本地模型  ollama / qwen3.5-9b-64k:latest
否则
    → 云端模型  opencode / minimax-m3.1-flash-free
```

理由见 §7。用户在会话中途被改权限时，bot 在下一条消息时检查，需要换就调
`POST /api/session/{id}/model`。

#### session 权限规则

建 session 时用 `POST /api/session` 的 `permissions` 字段带上规则；涉密目录
清单变化时用 `PATCH /api/session/{id}` 更新。

规则（opencode V2 权限模型，**最后一条匹配者生效**）：

```jsonc
[
  { "action": "external_directory", "resource": "*",                    "effect": "deny" },
  { "action": "read",  "resource": ".feishu/*",                           "effect": "deny" },
  { "action": "edit",  "resource": ".feishu/*",                           "effect": "deny" },
  { "action": "read",  "resource": "<涉密目录1>/*",                       "effect": "deny" },
  { "action": "edit",  "resource": "<涉密目录1>/*",                       "effect": "deny" },
  { "action": "shell", "resource": "*<涉密目录1>*",                       "effect": "deny" },
  { "action": "read",  "resource": "secrets.toml",                        "effect": "deny" },
  { "action": "shell", "resource": "*secrets.toml*",                      "effect": "deny" }
]
```

要点：

- `external_directory` deny 是**框架级**的强控制——按 opencode 权限文档，
  Location（`D:/kb-platform`）和项目 worktree 之外的路径，`read`/`edit` 一律被拦。
  这挡住了 agent 用文件工具直接读 NAS。
- `read`/`edit` 的 resource 是路径，Windows 下反斜杠会被归一化成斜杠、大小写不敏感。
- `shell` 的 resource 是命令文本，官方文档明确写了
  「Directory inference from command text is best effort, so prefer a narrow
  shell allowlist instead of patterns intended to recognize every dangerous command」——
  **shell 的路径拦截是尽力而为，不是强隔离**。规则里那几条 shell deny 是补漏，
  不是保证。
- `grep` 的 resource 是**正则表达式而不是搜索路径**，所以 grep 抓取涉密文件绕不过
  `read` 规则（它最终仍要 read），但无法用路径规则直接拦。

### 5.5 知识库接口复用

kbplatform 现有 `POST /api/v1/query`、`POST /api/v1/search` 已经做完 scope 过滤
和涉密保护，**不改一行**。

`kbq.py` 输出：

- `query` → 答案正文 + 引用列表（文件名、定位、是否涉密）。
- `search` → 命中片段摘要。
- 涉密片段**不回传原文**（服务端已保证，脚本再兜一层：restricted 的片段只输出
  「该片段涉密，未回传原文」）。

### 5.6 OpenCode 会话管理

- 建会话：`POST /api/session`
  ```json
  {"title": "飞书会话 <chat_id>", "agent": "build",
   "model": {"id":"minimax-m3.1-flash-free","providerID":"opencode","modelID":"minimax-m3.1-flash-free"},
   "location": {"directory": "D:/kb-platform"},
   "permissions": [...]}
  ```
  **响应体是 `{"data": {...}}` 包装的**，session id 在 `data.id`。
- 下指令：`POST /api/session/{id}/prompt`，body `{"text": "..."}`。
  **异步**——返回的是 inbox 条目（`data.delivery` 为 `steer`），不是结果。
- 取结果：轮询 `GET /api/session/{id}/message?order=asc`，响应同样是 `{"data":[...]}` 包装。
  - assistant 消息：正文在 `content` 数组里 `type == "text"` 的项。
  - user 消息：正文在顶层 `text` 字段。
  - 失败：`type == "idle"` 且 `outcome == "failed"`，`error.message` 是原因。
- 换模型：`POST /api/session/{id}/model`，body `{"model": {...}}`（**是 POST，不是 PATCH**）。
- 改权限：`PATCH /api/session/{id}`，body `{"permissions": [...]}`。
- 打断：`POST /api/session/{id}/interrupt`。
- 轮询节奏：前 5 秒每 1s，之后每 3s，上限 10 分钟。超时按「超时」回。

### 5.7 环境变量白名单

`PUT /api/session/{id}/environment` 是**全量替换**（已实测），不是追加。
所以必须提交一份完整环境，否则 agent 的 shell 会失去 `PATH`（已实测踩到：
`python` 找不到）。

提交内容 = **白名单**，不是 bot 进程环境的全量透传：

```
PATH, SystemRoot, windir, SystemDrive, TEMP, TMP, USERPROFILE, APPDATA,
LOCALAPPDATA, ProgramData, ProgramFiles, ComSpec, PATHEXT,
NUMBER_OF_PROCESSORS, PROCESSOR_ARCHITECTURE, USERNAME, USERDOMAIN, HOME
+ KB_TOKEN_FILE
```

**不能全量透传**：bot 进程环境里有 `FEISHU_APP_SECRET`（和 `KB_SESSION_SECRET`）。
全量透传等于把应用密钥交到 agent 手里，云端模型一 `echo` 就出去了。

### 5.8 触发与并发

- 每个 `(chat_id, open_id)` 一把锁，**同一会话的消息串行处理**——并发 prompt 同一
  session 会导致上下文乱序。
- **两级并发闸门**（单卡 3060 12GB 的硬约束）：
  - 走**本地**模型的会话最多 **1** 个（qwen3.5-9b-64k 常驻约 7.05GB 显存，
    加上常驻的 bge-m3 约 1.1GB，两个就爆）。
  - 走**云端**模型的会话最多 **3** 个。
  - 走本地的会话在等本地闸门时**不占用**云端闸门，反之亦然。
- 群里多人同时 @机器人 → 各自独立 session，互不影响。

### 5.9 回复

- 任务超过 20 秒才先发一条「收到，处理中…」，避免刷屏。
- 结果按 3000 字一段切成多条发（飞书单条上限约 30KB，留足余量）。
- 超长（>30 段）截断，并提示完整内容在服务器上的路径。完整内容由 bot 写到
  `D:\kb-platform\.feishu\out\<session_id>-<ts>.md`。
- 失败：回错误摘要（agent 的 `error.message`），不把内部堆栈发给用户。

### 5.10 独立 OpenCode 服务

bot 作为 supervisor：

1. spawn `opencode-cli.exe serve --port <port> --hostname 127.0.0.1`，
   stdout 解析两行（已实测格式）：
   ```
   server listening on http://127.0.0.1:4599
   server password _9VZ...
   ```
2. 用 Basic auth `opencode:<password>` 调 `/api/info` 探活。
3. 崩了 → 10 秒后重启，最多 5 次后放弃并退出（交给任务计划拉起）。
4. bot 退出时关闭子进程。

`opencode-cli.exe` 路径：`%APPDATA%\ai.opencode.desktop\cli\<version>\opencode-cli.exe`。
版本会变，bot 启动时**扫描该目录取最新版本**（当前 2.1.2-x64）；找不到则用
config.toml 的 `opencode_cli` 显式路径。

端口默认 4599（避开桌面版的 45278），可配。

**已实测**：独立 `opencode serve` 能正常调用云端 free 模型（凭据取自共享的
auth 存储，14 秒返回），不依赖桌面端开着。

### 5.11 停用开关

新增 `PUT /api/v1/admin/users/{user_id}/active`，body `{"active": true|false}`。
bot 在处理每条消息前检查 `users.active`，为假则回「账号已停用，请联系管理员」。

现在的管理台**没有任何改 `users.active` 的接口**（只有 users CRUD 和 scopes PUT），
所以这条止损路径目前不存在。收回 scope 并不能停用 agent——scope 只管知识库 API，
agent 的 shell 照旧。

---

## 6. 配置

### 6.1 凭据（不入库、不进版本库）

读取优先级：环境变量 > `D:\kb-platform\secrets.toml` > 不启动。

```toml
# D:\kb-platform\secrets.toml（加入 .gitignore）
[feishu]
app_id     = "cli_aaf56e800078dbc9"
app_secret = "MNrlBbYtF2DbBnCJGFYAkhH6J25iJUl1"
```

环境变量名：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`。bot **启动时读一次**，不热重载。

**App Secret 已在本次对话中以明文出现。** 建议在飞书开发者后台重置一次 Secret，
再填进 `secrets.toml`，然后重启 bot。

### 6.2 非敏感配置（进 `config.toml`）

```toml
[feishu]
enabled           = true
workspace         = "D:/kb-platform"
opencode_port     = 4599
opencode_cli      = ""              # 留空则自动扫描
cloud_model       = "opencode/minimax-m3.1-flash-free"
local_model       = "ollama/qwen3.5-9b-64k:latest"
kb_api_base       = "http://127.0.0.1:8000"
max_concurrent_cloud = 3
max_concurrent_local = 1
reply_chunk       = 3000
```

### 6.3 部署

Windows 任务计划，触发器「计算机启动时」，操作为
`python -m kbplatform.feishu.runner`，以登录用户身份运行。

**必须以登录用户身份运行**：U:/V:/W:/X:/Y: 五个 NAS 盘符是**持久映射**
（注册表 `HKCU\Network`），开机自动重连。知识库平台自己也依赖这些映射。

任务计划只登记一条：bot 进程自己拉起并守护 `opencode serve`。

### 6.4 知识库服务依赖

agent 用的知识库 HTTP API 由另一个进程提供（`kbplatform.service:app`，uvicorn）。
平台服务挂掉时，`kbq.py` 会返回 `KB_UNREACHABLE`（退出码 4），而不是让 agent
误以为「没查到」。

bot 启动时检查一次 `:8000` 可达性，不可达则**告警但仍启动**（平台服务可能由别的方式
拉起）。

---

## 7. 安全边界（说清楚上限，不假装更强）

### 7.1 已确认的事实

1. **agent 能读到 NAS 上的全部文件，包括涉密原文。**
   U:/V:/W:/X:/Y: 是持久映射（`HKCU\Network`），开机自动重连，agent 的 bash
   以当前用户权限运行 → 能 `type` 任意 NAS 文件。
2. **知识库 API 不回传涉密原文**（`service.py` 的 `do_query` 现有行为：
   涉密片段 `snippet`/`text` 置空）。
3. **opencode 的 shell 路径拦截是尽力而为**（官方权限文档原话：
   "Directory inference from command text is best effort"）。
4. **`external_directory` deny 是框架级的强控制**——Location 和项目 worktree 之外的
   `read`/`edit` 一律被拦。

### 7.2 三层控制

| 层 | 手段 | 强度 |
|---|---|---|
| ① 框架级 | session 权限规则：`external_directory` deny（挡住文件工具读 NAS）+ `read`/`edit` deny 涉密目录 + `read`/`edit` deny `.feishu/` | **强**（对文件类工具） |
| ② 通道级 | 知识库只经 `kbq.py` 走 scope 过滤的 API；token 不进上下文；涉密片段不回传原文 | **强**（对知识库内容） |
| ③ 提示级 | 每条 prompt 前缀指令：列出禁读目录、说明用 `kbq.py` 查知识库 | 弱（防君子不防小人） |

### 7.3 残余风险（必须写进交付说明）

- **shell 可以绕过 ①。** agent 若被文档里的提示注入诱导执行
  `type "W:\...\涉密目录\xxx.docx"`，路径会出现在命令文本里，规则 ① 的 shell deny
  能挡住**字面路径**；但只要路径被拼接/编码/用脚本间接构造，就挡不住。
- 因此「涉密不出内网」的真正保证是：
  1. 涉密原文**只经知识库 API 流出**，而该 API 受 scope 过滤且不回传涉密原文；
  2. **有涉密可见权限的用户，整个会话强制走本地模型**（§5.4），
     即使 agent 真的读到了涉密原文，也只进本地模型，不出内网。
- **没有涉密权限的用户走云端模型**，理论上仍可能通过 shell 读到涉密原文并送出去。
  这是当前方案的**已知缺口**，见 §9 后续项。

### 7.4 飞书指令 = 远程执行代码

- 只有已建号（= 已在 `feishu_users` 表里）的 open_id 能用；未建号的第一次私聊会
  触发自动建号，之后即可使用。
- **这意味着任何能私聊机器人的人都能拿到一台能跑 shell 的机器。**
  止损手段是 §5.11 的停用开关。
- 若要更严，需要管理员预先录入白名单（本期不做，配置项留口）。

### 7.5 凭证暴露面

- 知识库 token：只存在于 bot 进程内存和 `.feishu/tokens/<session>.token`；
  agent 的 `read`/`edit` 被规则禁止读该目录，但 shell 仍可读（该 token 仅代表
  该用户自己的权限，泄露影响有限）。
- 应用密钥（`FEISHU_APP_SECRET`）：**不进 session 环境变量**（白名单机制，§5.7），
  不进 agent 上下文。
- session 环境变量会被 opencode 自己存进它的库——与 opencode 自身的 session
  存储同级别，已知泄露面。

### 7.6 审计

bot 每次交互写 `audit_log`：

| 列 | 填什么 |
|---|---|
| `action` | `feishu` |
| `user_id` | 平台账号 id |
| `token_prefix` | `feishu-bot`（bot 的 token 只在内存，没有 prefix 可用） |
| `question` | 指令原文 |
| `hit_paths` | `[]`（bot 拿不到命中路径——真实命中记录在知识库服务自己的审计行里） |
| `model_used` | 实际使用的模型 |
| `restricted` | 该用户**是否拥有涉密可见权限**（保守上界，不是「本次是否命中涉密」——后者 bot 无从得知） |

注意：bot 与 FastAPI 服务**并发写同一个 `kb.db`**，靠 WAL + `busy_timeout=15000`
扛。bot 侧必须用 `kbplatform.db.connect()`，不要自己 `sqlite3.connect`。
`service.py` 里的 `audit()` 是 `make_app()` 内的闭包，bot 用不了，bot 自己写
`INSERT INTO audit_log`。

---

## 8. 飞书开发者后台需要配什么

| 项 | 值 |
|---|---|
| 能力 | 开启「机器人」 |
| 事件订阅 | 订阅方式选**「长连接」** |
| 事件 | 添加「接收消息 `im.message.receive_v1`」 |
| 权限 | `im:message`（发消息）、`im:message:readonly`、`contact:user.base:readonly`（取昵称/部门 ID） |
| 应用可用范围 | 决定哪些人能用机器人 |

长连接模式下**不需要**配置请求地址、Encrypt Key、Verification Token。

---

## 9. 错误处理与后续项

| 情况 | 行为 |
|---|---|
| 飞书长连接断开 | SDK 自动重连；连不上时每 30s 重试并告警 |
| opencode serve 崩 | bot 10s 后重启，最多 5 次；仍失败则 bot 退出 |
| agent 执行失败 | 回复错误摘要；session 保留，下条消息可继续 |
| agent 超时 10 分钟 | interrupt 该 session，回复「处理超时」 |
| 知识库 401 | `kbq.py` 退出码 3 + `KB_AUTH_EXPIRED`；bot 重新签发 token、重写 token 文件、重发一次 prompt |
| 知识库 429 | 退出码 5 + `KB_QUOTA_EXCEEDED`；回复「已达当日配额」 |
| 知识库服务不可达 | 退出码 4 + `KB_UNREACHABLE`；bot 回复「知识库服务不可用」 |
| 消息不是文本 | 回复「暂只支持文本消息」 |
| 用户无可见目录 | 知识库返回空；agent 自然回答「没查到相关内容」 |
| 用户被停用 | 回复「账号已停用，请联系管理员」 |

**后续项（本期不做，记录在案）**：

1. 堵住 §7.3 的缺口：给有涉密权限的用户一律本地模型之外，再加一条
   「涉密目录的 NAS 路径对 agent 完全不可见」的硬隔离（需要换用独立 Windows
   账号运行 server，或用容器）。
2. 管理台加「飞书用户」标签页（现在只能靠 `fsu_<md5>` 用户名 + 昵称认人）。
3. 长回复改成发飞书云盘文件。
4. 多飞书应用/多租户。

---

## 10. 验收标准

1. 私聊机器人发「你好」→ 收到回复。
2. 群里 @机器人 → 有回复；群里不 @ → 机器人完全静默。
3. 首次私聊的用户被自动建号，**并且这条指令照常执行**，同时收到一条开通提示。
4. 管理员在管理台给该账号勾选目录后，同一个机器人会话里能问到该目录的内容；
   另一个没配 scope 的用户问同一问题，答案里不含该目录内容。
5. 机器人进程重启后，同一 (chat_id, open_id) 继续用原 opencode session（上下文连续）。
6. 有涉密目录权限的用户，其 session 的 model 字段是 `ollama/qwen3.5-9b-64k:latest`；
   无涉密权限的是 `opencode/minimax-m3.1-flash-free`。
7. agent 调 `tools/kbq.py` 能取到知识库内容；**token 不出现在对话记录里**。
8. session 权限规则生效：agent 用 `read` 读 D:\kb-platform 以外的路径被拒绝。
9. 超过 3000 字的回复被切成多条发送，内容完整。
10. 飞书断网 3 分钟后恢复，机器人自动重新连上并能继续响应。
11. 管理台里飞书账号的 scope 显示为「（未授权）」而不是「（全部）」；能一键停用。
12. `kbplatform/tests/` 全绿；新增 `test_feishu_*.py` 覆盖身份映射、会话映射、
    模型路由、权限规则生成、回复分段、token 生命周期。

---

## 11. 实施顺序

1. **配置与凭据**：`secrets.toml`、`.gitignore`、`config.toml` 段、`config.py` 扩展。
2. **飞书客户端**：`client.py`（发消息、取用户、取 bot 信息）+ 单测（mock HTTP）。
3. **身份**：`identity.py` + 两张新表 + 自动建号 + 测试。
4. **OpenCode 客户端**：`opencode.py`（Basic auth、session 生命周期、轮询、权限规则）+ 测试。
5. **知识库凭证**：`kbcred.py` + `tools/kbq.py` + 测试。
6. **模型路由与权限规则**：`routing.py` + 测试。
7. **机器人主循环**：`bot.py`（触发判定、去重、并发、分段回复）+ 测试。
8. **平台侧小改**：`service.py` 停用接口 + `app.js` 文案与按钮。
9. **入口与部署**：`runner.py`（spawn/守护 opencode serve）+ 任务计划脚本。
10. **端到端联调**：真机飞书跑一遍 §10 的验收项。

---

## 12. 留给实现者的问题（我的默认选择已写明）

1. `opencode-cli.exe` 版本目录会变 —— 扫描取最新，还是锁死版本？
   → **扫描取最新，找不到则用 config.toml 的 `opencode_cli` 显式路径**。
2. `tools/kbq.py` 的输出格式 —— 纯文本还是 JSON？
   → **纯文本**（agent 要读），带 `--json` 开关给程序化调用。
3. 私聊的机器人回复要不要区分「这是机器人」标识？
   → **不加**，飞书自己会显示机器人头像。
4. `feishu_sessions` 里的旧 session 要不要清理？
   → **不清理**。opencode 侧有 session 管理，删了会丢上下文。
5. 涉密目录的 shell deny 规则用哪一级路径（盘符 `W:\...` 还是 UNC `\\192.0.2.19\...`）？
   → **两种都写**。agent 可能用任一种形式，规则里都列上。
