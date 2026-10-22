# 飞书机器人

把飞书变成企业知识平台的入口：在飞书里给机器人下指令，它在这台机器上干活
（查知识库、读写文件、跑脚本），完成后把结果发回飞书。

设计文档：`docs/superpowers/specs/2026-10-21-feishu-bot-design.md`
实现计划：`docs/superpowers/plans/2026-10-21-feishu-bot.md`

---

## 快速开始

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
cd D:\opencode

# 前台跑（看日志）
& $py -m kbplatform.feishu.runner D:\opencode\config.toml
```

日志默认写到 `kb\logs\feishu-bot.log`（用任务计划跑时）。

---

## 飞书开发者后台要配的（**没配收不到消息**）

| 项 | 值 |
|---|---|
| 能力 | 开启「机器人」 |
| 事件订阅 | 订阅方式选 **「长连接」** |
| 事件 | 添加「接收消息 `im.message.receive_v1`」 |
| 权限 | `im:message`（发消息）、`im:message:readonly`、`contact:user.base:readonly`（取昵称/部门） |
| 应用可用范围 | 决定谁能用 |

长连接模式**不需要**配置请求地址、Encrypt Key、Verification Token。

连上了长连接只说明凭据有效；**能不能收到消息取决于事件有没有订阅**。

---

## 配置

### 凭据（`D:\opencode\secrets.toml`，已进 .gitignore）

```toml
[feishu]
app_id     = "cli_xxxx"
app_secret = "xxxx"
```

也可用环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（优先级更高）。
**App Secret 已经在本次接入过程中以明文出现过，建议在开发者后台重置一次。**

### 非敏感配置（`D:\opencode\config.toml` 的 `[feishu]` 段）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | — | 必须为 `true` |
| `workspace` | `D:/opencode` | agent 的工作目录 |
| `opencode_port` | 4599 | 机器人自己拉起的 opencode serve 端口 |
| `opencode_cli` | 空 | 留空则自动扫描桌面端安装目录 |
| `cloud_model` | `opencode/minimax-m3.1-flash-free` | 普通用户用的模型 |
| `local_model` | `ollama/qwen3.5-9b-64k:latest` | 有涉密权限的用户用的模型 |
| `kb_api_base` | `http://127.0.0.1:8000` | 知识库服务地址 |
| `max_concurrent_cloud` | 3 | 云端模型并发上限 |
| `max_concurrent_local` | 1 | 本地模型并发上限（12GB 显存放不下两个） |
| `reply_chunk` | 3000 | 回复切段长度 |

---

## 开机自启（Windows 任务计划）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\opencode\tools\install-feishu-task.ps1
```

注册计划任务 `OpenCode-Feishu-Bot`：

| 项 | 值 |
|---|---|
| 触发器 | 登录时 |
| 操作 | `powershell -File D:\opencode\tools\start-feishu-bot.ps1` |
| 条件 | **以登录用户身份运行** |

必须用登录用户：U:/V:/W:/X:/Y: 是持久盘符映射（注册表 `HKCU\Network`），
开机自动重连；知识库平台自己也依赖这些映射。

机器人进程会自己拉起并守护一个独立的 `opencode serve`（端口 4599），
所以任务计划只需要登记这一条。

**注意任务状态码**：长驻任务的 `LastTaskResult` 是 `267009`（0x41301
"任务正在运行中"）才是正常的。出现 `1` 说明进程退出了，看日志。

### 日志

`kb\logs\feishu-bot.log`（UTF-8，由 Python 自己写）。

**不要用 PowerShell 的 `>>` 重定向**——它写 UTF-16，会把日志搞成两种编码混排；
而且 `$ErrorActionPreference=Stop` 时，python 写一行 stderr 就会把脚本打断
（管道一断，python 写 stdout 时就炸 `_readerthread` 异常）。包装脚本里已经规避。

---

## 权限模型

```
飞书用户 ──open_id──> 平台账号(users) ──user_scopes──> 可见目录
                              │                            │
                              └─ 知识库 API 过滤 ───────────┘
```

- **首次私聊**自动建平台账号，可见范围为空（管理员在管理台配 scope 后才查得到东西）。
- **群里**没建号的人 @ 机器人 → 静默忽略；建过号的才能在群里用。
- **有涉密目录可见权限**的用户，整个会话走**本地模型**（涉密内容不出内网）。
- **管理台停用**账号 = 真正的止损手段（token 立刻失效、机器人拒答）。
  收回 scope 不够——scope 只管知识库 API，管不住 agent 的 shell。

---

## 安全边界（务必读完）

1. **飞书下指令 = 远程执行代码。** 任何能私聊机器人的人都能拿到一台能跑 shell 的机器。
   止损靠管理台的停用开关。
2. **agent 能读到 NAS 上的全部文件**（含涉密原文），因为盘符是持久映射。
   三层控制：
   - 框架级：session 权限规则 `external_directory: deny` 挡住文件工具读工作区外的路径；
   - 通道级：知识库只经 `tools/kbq.py` 走 scope 过滤的 API，涉密片段不回传原文；
   - 提示级：每条指令前挂的使用说明里列出禁读目录。
3. **shell 的路径拦截是尽力而为**——opencode 官方文档明说目录推断是 best effort。
   所以「涉密不出内网」的真正保证是第 2 条（不回传原文）+ 涉密用户走本地模型。
4. **应用密钥不进 agent**（session 环境变量走白名单，`FEISHU_APP_SECRET` 被挡掉）。
5. **知识库 token 不进模型上下文**，只在 bot 进程内存 + `.feishu/tokens/<session>.token`。

---

## 排错

| 现象 | 查什么 |
|---|---|
| 收不到消息 | 开发者后台有没有订阅 `im.message.receive_v1` + 订阅方式是不是「长连接」 |
| 日志没出现 `connected to wss://` | 凭据错了，或 `secrets.toml` 没读到 |
| agent 说「没查到相关内容」 | 该用户的 scope 是空的（管理台配一下），或知识库还没索引到那份文档 |
| agent 报 `KB_UNREACHABLE` | 知识库服务没起（`http://127.0.0.1:8000`） |
| agent 报 `KB_AUTH_EXPIRED` | 机器人会自动重签并重试一次；持续出现就查 `api_tokens` 表 |
| 群里的 @ 没反应 | 机器人收到的是 `chat_type=group` 且 mentions 里没有机器人自己的 open_id |
| 任务很久没回 | 现在超过 20 秒会先回一条「收到，正在处理…」。仍超时就是 agent 真卡住了 |
| 日志刷 `processor not found: im.message.message_read_v1` | 老版本 SDK 没注册已读事件处理器（新版已修） |

查看实时日志：

```powershell
Get-Content D:\opencode\kb\logs\feishu-bot.log -Wait
Get-Content D:\opencode\kb\logs\feishu-bot.log.err -Wait
```

---

## 接 opencode API 时踩过的坑（都实测过，改代码前先读）

1. **消息接口默认只返回 50 条。**
   `GET /api/session/{id}/message?order=asc` 不传 `limit` 时只给最旧的 50 条。
   会话动辄上百条，**位于末尾的 `idle` 结束标记永远看不到** —— 调用方只能干等到超时。
   实测踩过：会话 22:28 就成功结束了，机器人却超时到 22:35 才放弃。
   正确做法：`order=desc` + 显式 `limit=500`（最新消息在前，一页就够）。

2. **一轮的结束标志是 `type == "idle"` 的消息**（`outcome` 为
   succeeded/failed/interrupted），不是「出现了 assistant 文本」。
   工具调用前 agent 会先说一句「我先看看…」，那不是结果。

3. **会话是连续的，上一轮的 `idle` 不能当本轮的结束信号。**
   `POST /api/session/{id}/prompt` 的响应里有本轮用户消息的 `time.created`，
   拿它当时间边界，只认 `created >= 边界` 的 `idle`。

4. **`PUT /api/session/{id}/environment` 是全量替换。**
   必须提交一份完整环境，否则 agent 的 shell 会失去 `PATH`。
   而且 `PATH` 里必须有**真正的 Python 目录**——Windows 上 `python` 往往指向
   WindowsApps 的应用执行别名，执行它会拉起 Microsoft Store（退出码 1、零输出），
   agent 会以为脚本坏了然后满世界找解释器（实测白烧十分钟）。

5. **鉴权是 HTTP Basic，用户名固定 `opencode`**，密码是 `opencode serve` 启动时
   打印在 stdout 上的那串。机器人把它落在 `.feishu/opencode.json`（已 gitignore），
   方便测试和排障复用同一个实例。

---


## 代码结构

| 文件 | 职责 |
|---|---|
| `config.py` | 凭据与参数装载、环境变量白名单 |
| `client.py` | 飞书开放平台 API（发消息、取用户、取 bot 自身） |
| `identity.py` | open_id ↔ 平台账号，首次私聊自动建号 |
| `opencode.py` | OpenCode HTTP 客户端（session 生命周期、权限、环境变量） |
| `kbcred.py` | 知识库 token 签发/轮换/per-session 文件 |
| `routing.py` | 模型路由 + session 权限规则生成 |
| `bot.py` | 事件主循环（触发判定、去重、并发、回复分段、审计） |
| `runner.py` | 进程入口：拉起 opencode serve + 飞书长连接 |
| `tools/kbq.py` | agent 侧知识库查询 helper |
