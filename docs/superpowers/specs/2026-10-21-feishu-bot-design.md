# 飞书机器人接入设计（kbplatform）

日期：2026-10-21
状态：待批准

---

## 1. 目标

给企业知识平台加一个飞书入口：员工在飞书里给机器人下指令，机器人在 `D:\opencode`
工作区干活（读��文件、跑脚本、查知识库），完成后把结果发回飞书。

**机器人是这台机器上 OpenCode 智能体的遥控器**，不是一个独立的关键词问答机器人。

### 非目标（做���就是失败）

- 不做预检索式问答（不预先查知识库再喂给模型）。agent 自己按需调知识库 API。
- 不做飞书卡片交互、审批流、二次确认弹窗。
- 不做 Web 管理台前端改造（管理台已有用户/scope 编辑页，本期复用，不加新页面）。
- 不做 agent 沙箱隔离。安全上限由 agent 的文件权限决定（见 §7）。
- 不做多租户、不做飞书以外 IM 平台。

---

## 2. 已确认的决策

| 决策项 | 结论 | 来源 |
|---|---|---|
| 机器人职责 | 通用指令代理：接收指令 → agent 干活 → 回复结果 | 用户 |
| 事件接入方式 | 长连接（WebSocket），无公网 IP 也能收消息 | 用户 |
| 身份模型 | 飞书 open_id 映射到 kbplatform 平台账号，scope 跟随账号 | 用户 |
| 首次私聊 | 自动建平台账号（默认无可见目录），管理员后续配 scope | 用户 |
| 模型 | 按敏感度自动路由：涉密 → 本地 ollama，公开 → 云端 free 模型 | 用户 |
| OpenCode 服务 | 独立常驻进程，不复用桌面版共享服务（45278） | 用户 |

---

## 3. 整体架构

```
飞书用户（私聊 / 群聊 @机器人）
   │  im.message.receive_v1
   ▼
[feishu-bot 进程]  python -m kbplatform.feishu.runner
   │  ├─ 管理 opencode serve 子进程（supervisor）
   │  ├─ 飞书长连接 client（lark-oapi ws.Client）
   │  ├─ 身份映射 / 自动建号
   │  └─ 会话映射（chat_id + open_id）→ opencode session
   ▼
[opencode serve]  127.0.0.1:4599  （Basic auth: opencode / <password>）
   │  POST   /api/session                    建会话
   │  PUT    /api/session/{id}/environment   注入该用户的 KB 凭证
   │  POST   /api/session/{id}/model         按敏感度切模型
   │  POST   /api/session/{id}/prompt        下指令
   │  GET    /api/session/{id}/message       轮询取结果
   ▼
[知识库]  agent 用 bash 调 http://127.0.0.1:8000/api/v1/query
          Authorization: Bearer <该用户的 API token>
          → 服务端按该用户的 scope 过滤命中，涉密不回传原文
   ▼
飞书回复（超长分段发送）
```

### 为什么 agent 用 HTTP 调知识库，而不是 bot 预先检索

预检索会把「用户问的问题」和「检索结果」硬绑在一起，agent 就没法做多步推理
（先查 A 再决定要不要查 B）。而且预检索天然在 bot 侧做权限过滤，agent 拿到的
片段就固定了。

改成 agent 自主调用后，权限过滤发生在**每次**检索上，用的是**当前用户**的 token，
越权面比预检索更小。代价是 agent 需要知道怎么调——用环境变量 + 一个 helper
脚本解决（§5.3）。

---

## 4. 组件与代码结构

```
kbplatform/feishu/
  __init__.py
  config.py     凭据与参数装载（环境变量 > secrets.toml > config.toml）
  client.py     飞书开放平台 API 客户端（发消息、取用户信息、bot 自身信息）
  identity.py   open_id ↔ 平台账号；首次私聊自动建号
  sessions.py   (chat_id, open_id) → opencode session 的持久化
  opencode.py   OpenCode HTTP 客户端（Basic auth；session/prompt/message/model/env）
  kbcred.py     给 agent 用的知识库凭证签发与轮换
  routing.py    模型路由（按用户可见范围是否含涉密目录）
  bot.py        事件处理主循环（触发判定、去重、并发控制、回复分段）
  runner.py     进程入口：拉起 opencode serve 子进程 → 启动长连接
tools/kbq.py    agent 侧 helper：调知识库 query/search 并格式化输出
```

`kbplatform/feishu` 是新增包，不改动现有 `kbplatform` 模块的既有行为；
`kbplatform/db.py` 只做**追加**（新表），不改既有表结构。
唯一的例外是 `users` 表不加列——飞书身份放在独立的 `feishu_users` 表里，
避免污染平台用户表。

---

## 5. 详细设计

### 5.1 飞书长连接

- `lark-oapi 1.7.5`（已安装）。`lark.ws.Client(app_id, app_secret, event_handler=...)`，
  `auto_reconnect=True`。断线自动重连，SDK 内部处理。
- 事件：`im.message.receive_v1`（`lark_oapi.api.im.v1.P2ImMessageReceiveV1`）。
- 触发判定：
  - `chat_type == "p2p"` → 响应。
  - `chat_type == "group"` → 仅当消息 `mentions` 里含机器人自身的 open_id 才响应。
    机器人 open_id 通过 `GET /open-apis/bot/v3/info` 取一次并缓存。
  - 消息 `message_type != "text"` → 直接回一句「暂只支持文本消息」。
- 幂等：以 `message_id` 为键，进程内 LRU 去重（重启后不重复处理历史消息）。
- 文本提取：`event.message.content` 是 JSON 串 `{"text":"..."}`；
  群聊里 @机器人 的占位符（形如 `@_user_1`）要剥掉。

### 5.2 身份映射与自动建号

新增表：

```sql
CREATE TABLE IF NOT EXISTS feishu_users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_open_id TEXT UNIQUE NOT NULL,
  feishu_union_id TEXT,
  feishu_name    TEXT,
  user_id        INTEGER,          -- kbplatform.users.id
  created_at     REAL,
  last_seen_at   REAL
);

CREATE TABLE IF NOT EXISTS feishu_sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  feishu_user_id  INTEGER NOT NULL,
  chat_id         TEXT NOT NULL,
  chat_type       TEXT,            -- p2p / group
  session_id      TEXT,            -- opencode session id
  model_id        TEXT,            -- 当前 session 用的模型（便于排查）
  created_at      REAL,
  updated_at      REAL,
  UNIQUE(feishu_user_id, chat_id)
);
```

首次私聊（`feishu_users` 里查不到 open_id）：

1. 调 `GET /open-apis/contact/v3/users/{open_id}` 取昵称与部门（失败不阻断，
   昵称降级为「飞书用户」）。
2. 建平台账号：
   - `username` = `fsu_` + `md5(open_id)` 前 10 位（保证唯一且不可枚举）
   - `password_hash` = 随机 32 字节的哈希（**这个账号不能通过 Web 登录**，
     密码无人知晓；平台账号只作为 scope / 审计的归属主体）
   - `role` = `user`，`display_name` = 飞书昵称，`dept` = 飞书部门
   - **不写 user_scopes** → 默认可见范围为空，知识库查不到任何东西
3. 插 `feishu_users`。
4. 回一句：「已为你开通平台账号（可见范围为空）。需要查知识库请联系管理员开通目录权限。」

管理员在**已有的管理台「用户」页**给该账号勾选可见目录（`user_scopes`），
scope 实时生效，无需重启任何进程——因为每次检索都现查 `user_scopes`。

### 5.3 agent 的知识库访问通道

agent 通过 bash 调知识库 API。凭证怎么给它：

1. bot 为该用户签发一枚**短期 API token**：
   `auth.issue_token(conn, user_id, label="feishu-bot", ttl_days=1)`。
   - 明文只在签发时返回一次，**只保留在 bot 进程内存里**，不落盘。
   - bot 重启时重新签发，并吊销旧的（`auth.revoke_token`）。
   - 与平台「token 只存哈希」原则不冲突：库里仍是哈希，明文只在运行内存。
2. 通过 `PUT /api/session/{id}/environment` 注入 session 环境变量：
   - `KB_API_BASE=http://127.0.0.1:8000`
   - `KB_API_TOKEN=<明文 token>`
   - `KB_USER_ID=<user_id>`
3. agent 用 `tools/kbq.py` 调用（脚本读环境变量，不把 token 写进命令行，
   避免出现在 agent 的对话历史里）：
   ```
   python tools/kbq.py query "三级及以下子企业能不能自建数据中心机房？"
   python tools/kbq.py search "数据中心" --top-k 10
   ```

**`PUT /api/session/{id}/environment` 是全量替换，不是追加**（已实测）。
所以 bot 必须提交一份**完整**的环境变量，不能只提交上面两个。
否则 agent 的 shell 会失去 `PATH`，`python`/`curl` 全都找不到（已实测踩到）。

为此 bot 采取的策略：**bot 自己 spawn `opencode serve` 子进程**，
spawn 时用 `env = BASE_ENV`（bot 进程的环境），之后每次 PUT 时提交
`{**BASE_ENV, **KB_VARS}`。这样 agent 的 shell 环境与 server 的环境一致、完整。

### 5.4 模型路由

两套模型命名空间，互不相干：

| 用途 | 配置在哪 | 取值 |
|---|---|---|
| 知识库内部答案生成 | kbplatform `models` 表（`ollama` / `openai_compatible`） | 已有 `route_for()` 涉密自动切本地逻辑，不动 |
| agent 自身 | OpenCode 的 model 列表 | 见下 |

agent 自身模型的选择规则：

```
若 该用户可见范围(user_scopes) 与 任一涉密目录(dir_scopes.restricted=1) 有交集
    → 本地模型  ollama / qwen3.5-9b-64k:latest
否则
    → 云端模型  opencode / minimax-m3.1-flash-free
```

理由：涉密内容的原文虽然**不会**经知识库 API 回传（`do_query` 现有行为），
但 agent 有 bash 和文件工具，**可以直接从磁盘读到涉密文件的原文**。
只要这些原文进过云端模型，就等于出内网了。所以对「有权看涉密」的用户，
整个会话必须在本地模型上跑。

模型在建 session 时定下；若该用户的涉密权限在会话中途被管理员改掉，
bot 在下一次消息时检查，发现需要换模型就 `POST /api/session/{id}/model` 切换。

### 5.5 知识库接口复用

kbplatform 现有 `POST /api/v1/query`、`POST /api/v1/search` 已经做完了
scope 过滤和涉密保护，**不改一行**。bot 侧不需要新增知识库端点。

`tools/kbq.py` 的行为：
- 读 `KB_API_BASE` / `KB_API_TOKEN` / `KB_USER_ID`。
- `query` → 调 `/api/v1/query`，输出答案 + 引用列表（文件名 + 定位 + 是否涉密）。
- `search` → 调 `/api/v1/search`，输出命中片段摘要。
- 涉密片段**不回传原文**（服务端已保证，脚本再兜一层：restricted 的片段
  只输出「该片段涉密，未回传原文」）。

### 5.6 OpenCode 会话管理

- 建会话：`POST /api/session`
  ```json
  {"title": "飞书会话 <chat_id>", "agent": "build",
   "model": {"id":"minimax-m3.1-flash-free","providerID":"opencode","modelID":"minimax-m3.1-flash-free"},
   "location": {"directory": "D:/opencode"}}
  ```
- 下指令：`POST /api/session/{id}/prompt`，body `{"text": "..."}`。
  这是**异步**的——返回的是 inbox 条目，不是结果。
- 取结果：轮询 `GET /api/session/{id}/message?order=asc`。
  - assistant 消息：`content` 数组里 `type=="text"` 的项是正文。
  - user 消息：顶层 `text` 字段。
  - 失败：`type=="idle"` 且 `outcome=="failed"`，`error.message` 是原因。
- 轮询节奏：首 5 秒每 1s，之后每 3s，上限 10 分钟。超时按「超时」回。
- 打断：`POST /api/session/{id}/interrupt`（用户发「停」时调用）。

### 5.7 触发与并发

- 每个 `(chat_id, open_id)` 一把锁，**同一会话的消息串行处理**——
  并发 prompt 同一 session 会导致上下文乱序。
- 全局信号量，最多 3 个 agent 任务同时在跑（CPU/GPU 是共享资源）。
- 群里多人同时 @机器人 → 各自独立 session，互不影响。

### 5.8 回复

- 先回一条「收到，处理中…」（仅当任务超过 20 秒才发，避免刷屏）。
- 结果：文本消息。超过 3000 字按段落切成多条发（飞书单条消息上限约 30KB，
  留足余量）。
- 超长（>30 段）不落文件——本期不做飞书云盘上传，改为只发前 30 段 +
  一句「结果过长已截断，完整内容见服务器 <path>」。完整内容由 bot 写到
  `D:\opencode\.feishu\out\<session_id>-<ts>.md`。
- 失败：回错误摘要（agent 的 `error.message`），不把内部堆栈发给用户。

### 5.9 独立 OpenCode 服务

bot 作为 supervisor：

1. spawn `opencode-cli.exe serve --port <port> --hostname 127.0.0.1`，
   stdout 解析两行：
   ```
   server listening on http://127.0.0.1:4599
   server password _9VZ...
   ```
   （已实测：密码就打印在 stdout 上）
2. 用 Basic auth `opencode:<password>` 调 `/api/info` 探活。
3. server 崩了 → 退出码非 0 或探活失败 → 隔 10 秒重启，最多重启 5 次后放弃。
4. bot 退出时关闭子进程。

`opencode-cli.exe` 路径：`%APPDATA%\ai.opencode.desktop\cli\<version>\opencode-cli.exe`。
版本会变，bot 启动时**扫描该目录取最新版本**（已实测当前为 2.1.2-x64）。
找不到就在 `config.toml` 里用 `opencode_cli` 显式指定。

端口默认 4599（避开桌面版的 45278），可配。

---

## 6. 配置

### 6.1 凭据（不入库、不进版本库）

读取优先级：环境变量 > `secrets.toml` > 不启动。

```toml
# secrets.toml（加入 .gitignore）
[feishu]
app_id     = "cli_aaf56e800078dbc9"
app_secret = "MNrlBbYtF2DbBnCJGFYAkhH6J25iJUl1"
```

环境变量名：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`。

**App Secret 已在本次对话中以明文出现**。落地时建议在飞书开发者后台重置一次
Secret，再填进 `secrets.toml`。

### 6.2 非敏感配置（进 `config.toml`）

```toml
[feishu]
enabled        = true
workspace      = "D:/opencode"        # agent 的工作目录
opencode_port  = 4599
cloud_model    = "opencode/minimax-m3.1-flash-free"
local_model    = "ollama/qwen3.5-9b-64k:latest"
kb_api_base    = "http://127.0.0.1:8000"
max_concurrent = 3
reply_chunk    = 3000
```

### 6.3 部署

Windows 任务计划，触发器「计算机启动时」，操作为
`python -m kbplatform.feishu.runner`，以登录用户身份运行（需要能读用户目录）。
单条任务同时负责 opencode serve 的拉起与守护。

---

## 7. 安全边界（明确说明上限，不假装更强）

1. **agent 的文件权限就是上限。** agent 能用 bash 读 `D:\opencode` 和映射的
   NAS 盘符。知识库的 scope 过滤管的是**知识库 API**，管不住 agent 直接读文件。
   所以「涉密不出内网」的保证依赖三件事：
   (a) 涉密内容的知识库 API 回传被关闭（已有行为）；
   (b) 有涉密权限的用户，会话强制走本地模型（§5.4）；
   (c) session 系统提示里明确列出禁读目录。
   做到 (b)，即便 agent 真的读到了涉密原文，也不会出内网。
2. **飞书下指令 = 远程执行代码。** 只有已建号（=已在 `feishu_users` 表里）的
   open_id 能用；未建号的第一次私聊会触发自动建号，之后即可使用。
   **这意味着任何能私聊机器人的人都能拿到一台能跑 shell 的机器。**
   本期不做更细的白名单——如果需要，管理台把该账号 `active=0` 即可停用。
3. **token 不落盘。** 知识库 token 只在 bot 进程内存里，agent 通过环境变量拿到；
   环境变量会随 session 存到 opencode 的库里（opencode 侧）——这是已知泄露面，
   与 opencode 自身的 session 存储同级别。
4. **审计。** 每次飞书交互写 `audit_log`（action=`feishu`），记录 user_id、
   问题、模型、是否涉密。

---

## 8. 飞书开发者后台需要配什么

| 项 | 值 |
|---|---|
| 能力 | 开启「机器人」 |
| 事件订阅 | 订阅方式选**「长连接」** |
| 事件 | 添加「接收消息 `im.message.receive_v1`」 |
| 权限 | `im:message`（发消息）、`im:message:readonly`、`contact:user.base:readonly`（取用户昵称/部门） |
| 应用可用范围 | 决定哪些人能用机器人 |

长连接模式下**不需要**配置请求地址、Encrypt Key、Verification Token。

---

## 9. 错误处理

| 情况 | 行为 |
|---|---|
| 飞书长连接断开 | SDK 自动重连；连不上时每 30s 重试，日志告警 |
| opencode serve 崩 | bot 10s 后重启，最多 5 次；仍失败则 bot 退出（任务计划负责拉起） |
| agent 执行失败 | 回复错误摘要；session 保留，下条消息可继续 |
| agent 超时 10 分钟 | interrupt 该 session，回复「处理超时」 |
| 知识库 API 401 | 说明 token 过期，bot 重新签发并重试一次 |
| 知识库 API 429 | 回复「已达当日配额」 |
| 消息不是文本 | 回复「暂只支持文本消息」 |
| 用户无可见目录 | 知识库返回空；agent 自然会回答「没查到相关内容」 |

---

## 10. 验收标准

1. 私聊机器人发「你好」→ 收到回复。
2. 群里 @机器人 → 有回复；群里不 @ → 机器人完全静默。
3. 首次私聊的用户被自动建号，回复里告知「可见范围为空」。
4. 管理员在管理台给该账号勾选目录后，同一个机器人会话里能问到该目录的内容；
   另一个没配 scope 的用户问同一问题，答案里不含该目录内容。
5. 机器人进程重启后，同一 (chat_id, open_id) 继续用原 opencode session（上下文连续）。
6. 有涉密目录权限的用户，其 session 的 model 字段是 `ollama/qwen3.5-9b-64k:latest`；
   无涉密权限的是 `opencode/minimax-m3.1-flash-free`。
7. agent 主动调用 `tools/kbq.py` 能取到知识库内容，token 不出现在命令行历史里。
8. 超过 3000 字的回复被切成多条发送，内容完整。
9. 飞书断网 3 分钟后恢复，机器人自动重新连上并能继续响应。
10. `kbplatform/tests/` 全绿；新增 `test_feishu_*.py` 覆盖身份映射、会话映射、
    模型路由、回复分段。

---

## 11. 实施顺序

1. **配置与凭据**：`secrets.toml`、`.gitignore`、config.toml 段、config.py 扩展。
2. **飞书客户端**：`client.py`（发消息、取用户、取 bot 信息）+ 单元测试（mock HTTP）。
3. **身份**：`identity.py` + `feishu_users` 表 + 自动建号 + 测试。
4. **OpenCode 客户端**：`opencode.py`（Basic auth、session 生命周期、轮询）+ 测试。
5. **知识库凭证**：`kbcred.py` + `tools/kbq.py` + 测试。
6. **模型路由**：`routing.py` + 测试。
7. **机器人主循环**：`bot.py`（触发判定、去重、并发、分段回复）+ 测试。
8. **入口与部署**：`runner.py`（spawn/守护 opencode serve）、任务计划脚本。
9. **端到端联调**：真机飞书跑一遍 §10 的验收项。
10. **管理台**：复用已有「用户」页配 scope，不加新页面（若需要再加「飞书用户」标签页）。

---

## 12. 留给实现者的问题（我的默认选择已写明）

1. `opencode-cli.exe` 版本目录会变 —— 扫描取最新，还是锁死版本？
   → **扫描取最新，找不到则用 config.toml 的 `opencode_cli` 显式路径**。
2. `tools/kbq.py` 的输出格式 —— 纯文本还是 JSON？
   → **纯文本**（agent 要读），带 `--json` 开关给程序化调用。
3. 私聊的机器人回复要不要区分「这是机器人」标识？
   → **不加**，飞书自己会显示机器人头像。
4. `feishu_sessions` 里的旧 session 要不要清理？
   → **不清理**。opencode 侧有 session 管理，删了会丢上下文。人工在
     opencode 里删。
