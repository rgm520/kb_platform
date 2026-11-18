# 飞书机器人接入 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让员工在飞书里给机器人下指令，机器人在 `D:\kb-platform` 工作区干活（查知识库、读写文件、跑脚本），完成后把结果发回飞书。

**Architecture:** 独立的 `feishu-bot` Python 进程用 lark-oapi 长连接接收飞书事件，把飞书用户映射到 kbplatform 平台账号（决定知识库可见范围），把每个 `(chat_id, open_id)` 映射到一个 OpenCode session。bot 同时 spawn 并守护一个独立的 `opencode serve` 子进程。agent 通过 shell 调 `tools/kbq.py` 查知识库，凭证是 per-session 的 token 文件，token 不进模型上下文。

**Tech Stack:** Python 3.12 / lark-oapi 1.7.5 / FastAPI（既有）/ SQLite（既有）/ opencode HTTP API v2.1.2

**Spec:** `docs/superpowers/specs/2026-10-21-feishu-bot-design.md`

## Global Constraints

- 知识库接口**不改一行**：`/api/v1/query`、`/api/v1/search` 复用现有实现。
- 知识库 token **不进模型上下文、不进对话记录**；只在 bot 进程内存 + `.feishu/tokens/<session_id>.token`。
- session 环境变量用**白名单**，不得全量透传 bot 进程环境（`FEISHU_APP_SECRET` 绝不能进 agent 上下文）。
- `PUT /api/session/{id}/environment` 是**全量替换**语义，提交时必须给完整环境。
- 本地模型并发上限 1，云端模型并发上限 3（单卡 3060 12GB）。
- 涉密判定：用户可见范围与 `dir_scopes.restricted=1` 有交集 → 该用户会话走本地模型。
- 对现有模块的改动只允许**追加**：`db.py` 加表、`service.py` 加端点、`app.js` 改文案加按钮。
- 测试用 pytest，路径 `kbplatform/tests/test_*.py`，用 `tmp_path` 建临时库。
- 所有新代码注释与文档用中文（与既有代码一致）。

## Review Focus

- **飞书重复投递同一消息** → 机器人必须只处理一次（`message_id` 去重）。
- **群聊里没 @机器人** → 机器人必须完全静默，不能回复任何东西。
- **同一会话并发发消息** → 上下文不能乱序，必须串行。
- **知识库服务不可达** → 必须报「不可用」，不能回答「没查到相关内容」（把故障伪装成空结果）。
- **用户被管理员停用** → 机器人必须拒答，不能继续执行。
- **token 过期** → agent 侧看到的是明确标记（`KB_AUTH_EXPIRED`），不是「没查到」。
- **回复超过 3000 字** → 必须切成多条，不能被飞书截断。
- **管理员把普通用户的 scope 清空** → 管理台必须显示「（未授权）」而不是「（全部）」。

---

### Task 1: 配置与凭据装载

**Files:**
- Create: `kbplatform/feishu/__init__.py`
- Create: `kbplatform/feishu/config.py`
- Create: `kbplatform/tests/__init__.py`
- Create: `kbplatform/tests/test_feishu_config.py`
- Modify: `D:\kb-platform\.gitignore`
- Create: `D:\kb-platform\secrets.toml`（不入版本库）

**Interfaces:**
- Produces: `FeishuConfig` dataclass，字段 `app_id: str`、`app_secret: str`、`workspace: str`、`opencode_port: int`、`opencode_cli: str`、`cloud_model: str`、`local_model: str`、`kb_api_base: str`、`max_concurrent_cloud: int`、`max_concurrent_local: int`、`reply_chunk: int`、`secrets_path: str`、`config_path: str`。
- Produces: `load_feishu_config(config_path: str = "D:/kb-platform/config.toml", secrets_path: str | None = None) -> FeishuConfig | None`。返回 `None` 表示未启用或缺凭据。
- Produces: `parse_model_ref(ref: str) -> dict`，把 `"provider/model"` 拆成 opencode `Model.Ref` 需要的 `{"id","providerID","modelID"}`。

- [x] **Step 1: 写失败测试**

```python
# kbplatform/tests/test_feishu_config.py
def test_parse_model_ref_splits_provider_and_model():
    assert parse_model_ref("opencode/minimax-m3.1-flash-free") == {
        "id": "minimax-m3.1-flash-free",
        "providerID": "opencode",
        "modelID": "minimax-m3.1-flash-free",
    }

def test_load_returns_none_without_credentials(tmp_path):
    cfg = load_feishu_config(config_path=str(tmp_path / "config.toml"))
    assert cfg is None

def test_load_reads_secret_from_secrets_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    (tmp_path / "config.toml").write_text(
        '[feishu]\nenabled = true\nworkspace = "D:/kb-platform"\n', encoding="utf-8")
    (tmp_path / "secrets.toml").write_text(
        '[feishu]\napp_id = "cli_x"\napp_secret = "s3cr3t"\n', encoding="utf-8")
    cfg = load_feishu_config(str(tmp_path / "config.toml"),
                             str(tmp_path / "secrets.toml"))
    assert cfg.app_id == "cli_x" and cfg.app_secret == "s3cr3t"
    assert cfg.workspace == "D:/kb-platform"

def test_env_vars_take_precedence_over_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
    (tmp_path / "config.toml").write_text("[feishu]\nenabled = true\n", encoding="utf-8")
    (tmp_path / "secrets.toml").write_text(
        '[feishu]\napp_id = "cli_file"\napp_secret = "file_secret"\n', encoding="utf-8")
    cfg = load_feishu_config(str(tmp_path / "config.toml"),
                             str(tmp_path / "secrets.toml"))
    assert cfg.app_id == "cli_env" and cfg.app_secret == "env_secret"
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_model_ref'`

- [x] **Step 3: 实现 `kbplatform/feishu/config.py`**

`FeishuConfig` dataclass + `load_feishu_config()`：
- 先读 `config.toml` 的 `[feishu]` 段（用 `tomllib`），`enabled` 缺省 `False`。
- 凭据来源优先级：环境变量 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` > `secrets.toml` 的 `[feishu]` 段。
- `secrets_path` 缺省 `<config_path 同目录>/secrets.toml`。
- 任何一项缺失（app_id / app_secret / enabled）→ 返回 `None`。
- 其余字段按 spec §6.2 的默认值。

`parse_model_ref(ref)`：按第一个 `/` 切开 provider 和 model；`modelID` 与 `id` 都用 model 部分。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_config.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu .gitignore secrets.toml
git commit -m "feat(feishu): 配置与凭据装载"
```

---

### Task 2: 数据表

**Files:**
- Modify: `kbplatform/db.py`（`SCHEMA_SQL` 追加）
- Create: `kbplatform/tests/test_feishu_schema.py`

**Interfaces:**
- Produces: 表 `feishu_users(id, feishu_open_id UNIQUE, feishu_union_id, feishu_name, user_id, created_at, last_seen_at)`
- Produces: 表 `feishu_sessions(id, feishu_user_id, chat_id, chat_type, session_id, model_id, created_at, updated_at, UNIQUE(feishu_user_id, chat_id))`

- [x] **Step 1: 写失败测试**

```python
def test_feishu_tables_created(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feishu_users)")}
    assert {"feishu_open_id", "feishu_union_id", "feishu_name", "user_id"} <= cols
    cols2 = {r[1] for r in conn.execute("PRAGMA table_info(feishu_sessions)")}
    assert {"feishu_user_id", "chat_id", "chat_type", "session_id", "model_id"} <= cols2

def test_feishu_open_id_is_unique(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")

def test_feishu_session_unique_per_user_and_chat(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO feishu_users(feishu_open_id, created_at) VALUES('ou_1', 1)")
    uid = conn.execute("SELECT id FROM feishu_users WHERE feishu_open_id='ou_1'").fetchone()[0]
    conn.execute("INSERT INTO feishu_sessions(feishu_user_id, chat_id, created_at, updated_at)"
                 " VALUES(?, 'oc_1', 1, 1)", (uid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feishu_sessions(feishu_user_id, chat_id, created_at, updated_at)"
                     " VALUES(?, 'oc_1', 1, 1)", (uid,))
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_schema.py -v`
Expected: FAIL — `no such table: feishu_users`

- [x] **Step 3: 实现**

在 `kbplatform/db.py` 的 `SCHEMA_SQL` 末尾追加两张表（`CREATE TABLE IF NOT EXISTS`）。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_schema.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/db.py kbplatform/tests/test_feishu_schema.py
git commit -m "feat(feishu): 飞书身份与会话映射表"
```

---

### Task 3: 身份映射与自动建号

**Files:**
- Create: `kbplatform/feishu/identity.py`
- Create: `kbplatform/tests/test_feishu_identity.py`

**Interfaces:**
- Consumes: `kbplatform.db.connect()`、`kbplatform.auth.create_user`
- Produces: `platform_username(open_id: str) -> str` → `"fsu_" + md5(open_id).hexdigest()[:10]`
- Produces: `class Identity: __init__(self, conn)`；方法 `get_or_create(open_id, name=None, dept=None) -> tuple[int, bool]` 返回 `(user_id, is_new)`；`user_id_for(open_id) -> int | None`；`is_active(user_id) -> bool`
- Produces: `find_opencode_cli(explicit: str = "") -> str` → 扫描 `%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe` 取版本号最大的一个；找不到返回 `""`

- [x] **Step 1: 写失败测试**

```python
def test_platform_username_is_stable_and_unique():
    a = platform_username("ou_abc")
    b = platform_username("ou_abc")
    c = platform_username("ou_xyz")
    assert a == b and a != c
    assert a.startswith("fsu_") and len(a) == 14

def test_get_or_create_creates_platform_user_once(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid1, new1 = ident.get_or_create("ou_abc", name="张三")
    uid2, new2 = ident.get_or_create("ou_abc", name="张三")
    assert new1 is True and new2 is False
    assert uid1 == uid2
    row = conn.execute("SELECT username, display_name, role, active FROM users WHERE id=?", (uid1,)).fetchone()
    assert row["username"] == platform_username("ou_abc")
    assert row["display_name"] == "张三"
    assert row["role"] == "user" and row["active"] == 1

def test_new_user_has_no_scopes(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid, _ = ident.get_or_create("ou_abc")
    n = conn.execute("SELECT COUNT(*) FROM user_scopes WHERE user_id=?", (uid,)).fetchone()[0]
    assert n == 0

def test_auto_created_user_cannot_log_in_with_known_password(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    ident.get_or_create("ou_abc")
    assert verify_login(conn, platform_username("ou_abc"), "") is None

def test_user_id_for_returns_none_for_unknown(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    assert Identity(conn).user_id_for("ou_nobody") is None

def test_is_active_reflects_users_active_column(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    ident = Identity(conn)
    uid, _ = ident.get_or_create("ou_abc")
    assert ident.is_active(uid) is True
    conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    conn.commit()
    assert ident.is_active(uid) is False

def test_find_opencode_cli_returns_existing_path_or_empty():
    p = find_opencode_cli()
    assert p == "" or (p.endswith("opencode-cli.exe") and os.path.isfile(p))
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_identity.py -v`
Expected: FAIL — `ImportError: cannot import name 'platform_username'`

- [x] **Step 3: 实现 `kbplatform/feishu/identity.py`**

- `platform_username`：`hashlib.md5(open_id.encode()).hexdigest()[:10]`，前缀 `fsu_`。
- `Identity.get_or_create`：查 `feishu_users` → 命中则更新 `last_seen_at` 并返回 `(user_id, False)`；
  未命中则 `create_user(conn, platform_username(open_id), secrets.token_urlsafe(32),
  role="user", display_name=name or "飞书用户", dept=dept)`，插 `feishu_users`，返回 `(uid, True)`。
  `create_user` 的 `password_hash` 用随机密码，**该账号无法通过 Web 登录**。
- `find_opencode_cli`：显式路径存在则返回；否则扫 `%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe`，
  用目录名做版本排序取最大；找不到返回 `""`。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_identity.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu/identity.py kbplatform/tests/test_feishu_identity.py
git commit -m "feat(feishu): 身份映射与首次私聊自动建号"
```

---

### Task 4: 飞书 API 客户端

**Files:**
- Create: `kbplatform/feishu/client.py`
- Create: `kbplatform/tests/test_feishu_client.py`

**Interfaces:**
- Produces: `class FeishuClient: __init__(self, app_id, app_secret, domain="https://open.feishu.cn")`
  - `send_text(chat_id: str, text: str) -> dict`
  - `get_user(open_id: str) -> dict`（返回 `{"name","department_ids"}`，失败返回 `{}`）
  - `bot_open_id() -> str`（缓存）
- Produces: `class FeishuApiError(Exception)` 带 `code` 属性

- [x] **Step 1: 写失败测试**

用 `unittest.mock` 打桩 `urllib.request.urlopen`：

```python
def _resp(payload, status=200):
    import io, json
    raw = json.dumps(payload).encode()
    class R:
        status = status
        def read(self): return raw
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()

def test_tenant_token_is_cached(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    c = FeishuClient("cli_x", "sec")
    c.tenant_token()
    c.tenant_token()
    assert len(calls) == 1

def test_send_text_posts_to_messages_api(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return _resp({"code": 0, "data": {"message_id": "om_1"}})
    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    FeishuClient("cli_x", "sec").send_text("oc_1", "你好")
    assert seen["url"].endswith("/im/v1/messages?receive_id_type=chat_id")
    assert seen["body"]["receive_id"] == "oc_1"
    assert seen["body"]["msg_type"] == "text"
    assert seen["body"]["content"] == json.dumps({"text": "你好"}, ensure_ascii=False)
    assert seen["auth"] == "Bearer t-1"

def test_send_text_raises_on_api_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return _resp({"code": 230001, "msg": "bot not in chat"})
    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(FeishuApiError) as e:
        FeishuClient("cli_x", "sec").send_text("oc_1", "hi")
    assert e.value.code == 230001

def test_get_user_returns_empty_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        raise OSError("boom")
    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    assert FeishuClient("cli_x", "sec").get_user("ou_1") == {}

def test_bot_open_id_is_cached(monkeypatch):
    n = []
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        n.append(req.full_url)
        return _resp({"code": 0, "bot": {"open_id": "ou_bot"}})
    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    c = FeishuClient("cli_x", "sec")
    assert c.bot_open_id() == "ou_bot"
    assert c.bot_open_id() == "ou_bot"
    assert len(n) == 1
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'FeishuClient'`

- [x] **Step 3: 实现 `kbplatform/feishu/client.py`**

- `tenant_token()`：`POST {domain}/open-apis/auth/v3/tenant_access_token/internal`，
  body `{"app_id","app_secret"}`；成功则缓存 `tenant_access_token` 与过期时间
  （提前 5 分钟过期）；`code != 0` 抛 `FeishuApiError`。
- `send_text(chat_id, text)`：`POST {domain}/open-apis/im/v1/messages?receive_id_type=chat_id`，
  header `Authorization: Bearer <token>`，body
  `{"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}`。
  `code != 0` 抛 `FeishuApiError(code, msg)`。
- `get_user(open_id)`：`GET {domain}/open-apis/contact/v3/users/{open_id}`，
  返回 `{"name": data["name"], "department_ids": data.get("department_ids", [])}`；
  任何异常都吞掉返回 `{}`。
- `bot_open_id()`：`GET {domain}/open-apis/bot/v3/info`，取 `data.bot.open_id`，缓存。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_client.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu/client.py kbplatform/tests/test_feishu_client.py
git commit -m "feat(feishu): 飞书开放平台 API 客户端"
```

---

### Task 5: OpenCode HTTP 客户端

**Files:**
- Create: `kbplatform/feishu/opencode.py`
- Create: `kbplatform/tests/test_feishu_opencode.py`

**Interfaces:**
- Produces: `class OpenCodeError(Exception)`
- Produces: `class OpenCodeClient:`
  - `__init__(self, base_url: str, password: str, timeout: int = 60)`
  - `info() -> dict`
  - `create_session(title, model_ref, directory, permissions=None) -> str`（返回 session id）
  - `prompt(session_id, text) -> None`
  - `messages(session_id, order="asc") -> list[dict]`
  - `wait_for_result(session_id, timeout_sec=600, poll=None) -> str`（返回 assistant 文本；失败抛 `OpenCodeError`）
  - `set_model(session_id, model_ref) -> None`
  - `set_permissions(session_id, permissions) -> None`
  - `set_environment(session_id, variables: dict) -> None`
  - `interrupt(session_id) -> None`
- Produces: `session_env(base_env: dict, extra: dict) -> dict` —— 白名单过滤 + extra 覆盖

- [x] **Step 1: 写失败测试**

```python
def _resp(payload, status=200):
    class R:
        status = status
        def read(self): return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()

def _client(monkeypatch, handler):
    monkeypatch.setattr("kbplatform.feishu.opencode.urllib.request.urlopen",
                        lambda req, timeout=None: handler(req))

def test_basic_auth_header_is_sent(monkeypatch):
    seen = {}
    def h(req):
        seen["auth"] = req.headers.get("Authorization")
        return _resp({"data": {"id": "ses_1"}})
    _client(monkeypatch, h)
    c = OpenCodeClient("http://127.0.0.1:4599", "pw")
    c.create_session("t", {"id": "m", "providerID": "opencode", "modelID": "m"}, "D:/kb-platform")
    assert seen["auth"].startswith("Basic ")
    import base64
    assert base64.b64decode(seen["auth"].split()[1]).decode() == "opencode:pw"

def test_create_session_returns_data_id(monkeypatch):
    _client(monkeypatch, lambda req: _resp({"data": {"id": "ses_abc"}}))
    c = OpenCodeClient("http://x", "pw")
    assert c.create_session("t", {"id": "m", "providerID": "p", "modelID": "m"}, "D:/x") == "ses_abc"

def test_create_session_sends_location_and_permissions(monkeypatch):
    seen = {}
    def h(req):
        seen["body"] = json.loads(req.data.decode())
        return _resp({"data": {"id": "s"}})
    _client(monkeypatch, h)
    perms = [{"action": "read", "resource": "x", "effect": "deny"}]
    OpenCodeClient("http://x", "pw").create_session(
        "t", {"id": "m", "providerID": "p", "modelID": "m"}, "D:/x", permissions=perms)
    assert seen["body"]["location"] == {"directory": "D:/x"}
    assert seen["body"]["permissions"] == perms

def test_wait_for_result_returns_assistant_text(monkeypatch):
    msgs = {"data": [
        {"type": "user", "text": "hi"},
        {"type": "assistant", "content": [{"type": "text", "text": "答"}]},
    ]}
    _client(monkeypatch, lambda req: _resp(msgs))
    assert OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2) == "答"

def test_wait_for_result_raises_on_failed_idle(monkeypatch):
    msgs = {"data": [{"type": "idle", "outcome": "failed",
                      "error": {"type": "provider.no-route", "message": "模型不可用"}}]}
    _client(monkeypatch, lambda req: _resp(msgs))
    with pytest.raises(OpenCodeError) as e:
        OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2)
    assert "模型不可用" in str(e.value)

def test_set_environment_posts_variables(monkeypatch):
    seen = {}
    def h(req):
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode())
        return _resp({})
    _client(monkeypatch, h)
    OpenCodeClient("http://x", "pw").set_environment("s", {"A": "1"})
    assert seen["method"] == "PUT"
    assert seen["body"] == {"variables": {"A": "1"}}

def test_session_env_filters_secrets():
    base = {"PATH": "C:\\x", "SystemRoot": "C:\\Windows", "TEMP": "T",
            "FEISHU_APP_SECRET": "leak", "KB_SESSION_SECRET": "leak2",
            "RANDOM_JUNK": "x"}
    out = session_env(base, {"KB_TOKEN_FILE": "f"})
    assert out["KB_TOKEN_FILE"] == "f"
    assert out["PATH"] == "C:\\x"
    assert "FEISHU_APP_SECRET" not in out
    assert "KB_SESSION_SECRET" not in out
    assert "RANDOM_JUNK" not in out
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_opencode.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenCodeClient'`

- [x] **Step 3: 实现 `kbplatform/feishu/opencode.py`**

- `_call(method, path, body=None)`：`urllib.request.Request`，header
  `Authorization: Basic <b64(opencode:password)>` + `Content-Type: application/json`；
  HTTPError → 抛 `OpenCodeError`（含响应体前 300 字）。
- `create_session`：body `{"title","agent":"build","model":model_ref,"location":{"directory":directory}}`，
  有 permissions 时带上；返回 `resp["data"]["id"]`。
- `prompt`：`POST /api/session/{id}/prompt`，body `{"text": text}`。
- `messages`：`GET /api/session/{id}/message?order=asc`，返回 `resp["data"]`。
- `wait_for_result`：轮询 `messages()`，遇 `type=="assistant"` 且 content 里有 `type=="text"`
  就拼接返回；遇 `type=="idle"` 且 `outcome=="failed"` 抛 `OpenCodeError(error.message)`；
  超过 `timeout_sec` 抛 `OpenCodeError("超时")`。
- `set_model`：`POST /api/session/{id}/model`，body `{"model": model_ref}`。
- `set_permissions`：`PATCH /api/session/{id}`，body `{"permissions": permissions}`。
- `set_environment`：`PUT /api/session/{id}/environment`，body `{"variables": variables}`。
- `interrupt`：`POST /api/session/{id}/interrupt`。
- `session_env(base, extra)`：白名单
  `PATH, SystemRoot, windir, SystemDrive, TEMP, TMP, USERPROFILE, APPDATA, LOCALAPPDATA, ProgramData, ProgramFiles, ComSpec, PATHEXT, NUMBER_OF_PROCESSORS, PROCESSOR_ARCHITECTURE, USERNAME, USERDOMAIN, HOME`，
  再用 extra 覆盖。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_opencode.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu/opencode.py kbplatform/tests/test_feishu_opencode.py
git commit -m "feat(feishu): OpenCode HTTP 客户端"
```

---

### Task 6: 知识库凭证与 agent 侧 helper

**Files:**
- Create: `kbplatform/feishu/kbcred.py`
- Create: `tools/kbq.py`
- Create: `kbplatform/tests/test_feishu_kbcred.py`
- Create: `kbplatform/tests/test_feishu_kbq.py`

**Interfaces:**
- Produces: `class TokenStore:`
  - `__init__(self, conn)`
  - `token_for(user_id: int) -> str`（内存缓存；没有就签发；有旧的就复用）
  - `rotate(user_id: int) -> str`（吊销旧的 + 重新签发）
  - `write_session_token(session_id: str, user_id: int, root: str) -> str`（写 `<root>/.feishu/tokens/<session_id>.token`，返回文件路径）
- Produces: `tools/kbq.py` 的 CLI：`python tools/kbq.py query "问题"` / `python tools/kbq.py search "关键词" --top-k N`
  - 读环境变量 `KB_TOKEN_FILE`、`KB_API_BASE`
  - 退出码：0 正常 / 3 token 失效 / 4 服务不可达 / 5 超配额

- [x] **Step 1: 写失败测试**

```python
# test_kbcred.py
def test_token_is_issued_once_per_user(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    a = store.token_for(uid)
    b = store.token_for(uid)
    assert a == b
    n = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (uid,)).fetchone()[0]
    assert n == 1

def test_rotate_revokes_old_token(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    old = store.token_for(uid)
    new = store.rotate(uid)
    assert new != old
    assert resolve_token(conn, old) is None
    assert resolve_token(conn, new) is not None

def test_write_session_token_creates_file(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    store = TokenStore(conn)
    path = store.write_session_token("ses_1", uid, str(tmp_path))
    assert os.path.isfile(path)
    assert open(path, encoding="utf-8").read().strip() == store.token_for(uid)
    assert path.replace("\\", "/").endswith(".feishu/tokens/ses_1.token")


# test_kbq.py —— 用 subprocess 跑真脚本
def _run(args, env=None, token="kb_x", base="http://127.0.0.1:1"):
    e = dict(os.environ)
    e.update({"KB_TOKEN_FILE": str(token), "KB_API_BASE": base})
    e.update(env or {})
    return subprocess.run([sys.executable, "tools/kbq.py"] + args,
                          capture_output=True, text=True, env=e)

def test_query_unreachable_service_exits_4():
    r = _run(["query", "问题"], token=os.devnull)
    assert r.returncode == 4
    assert "KB_UNREACHABLE" in r.stdout + r.stderr

def test_search_missing_token_exits_3(tmp_path):
    p = tmp_path / "missing.token"
    r = _run(["search", "x"], token=str(p))
    assert r.returncode == 3
    assert "KB_AUTH_EXPIRED" in r.stdout + r.stderr
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_kbcred.py kbplatform/tests/test_feishu_kbq.py -v`
Expected: FAIL — `ImportError: cannot import name 'TokenStore'`

- [x] **Step 3: 实现**

`kbplatform/feishu/kbcred.py`：
- `TokenStore.__init__(self, conn)`：`self._tokens: dict[int, str] = {}`、`self._ids: dict[int, int] = {}`。
- `token_for(user_id)`：缓存命中直接返回；否则
  `raw, tid = auth.issue_token(self.conn, user_id, "feishu-bot", ttl_days=90)`，记缓存返回。
- `rotate(user_id)`：有旧 id 就 `auth.revoke_token`，再签发新的并更新缓存。
- `write_session_token(session_id, user_id, root)`：目录 `<root>/.feishu/tokens`，
  写文件（内容为 token，UTF-8），返回路径。

`tools/kbq.py`：
- 读 `KB_TOKEN_FILE`（读不到 → 打印 `KB_AUTH_EXPIRED: 找不到 token 文件`，退出 3）
  和 `KB_API_BASE`（缺省 `http://127.0.0.1:8000`）。
- `query`：POST `{base}/api/v1/query`，body
  `{"question": q, "top_k": 8, "include_text": False}`。
- `search`：POST `{base}/api/v1/search`，body `{"query": q, "top_k": n}`。
- HTTP 连接失败 → 打印 `KB_UNREACHABLE: <原因>`，退出 4。
- HTTP 401 → `KB_AUTH_EXPIRED`，退出 3；429 → `KB_QUOTA_EXCEEDED`，退出 5。
- 输出：答案正文 + 引用列表；涉密片段只输出「该片段涉密，未回传原文」。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_kbcred.py kbplatform/tests/test_feishu_kbq.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu/kbcred.py tools/kbq.py kbplatform/tests/
git commit -m "feat(feishu): 知识库凭证与 agent 侧查询 helper"
```

---

### Task 7: 模型路由与权限规则

**Files:**
- Create: `kbplatform/feishu/routing.py`
- Create: `kbplatform/tests/test_feishu_routing.py`

**Interfaces:**
- Produces: `restricted_dirs(conn) -> list[str]`（`dir_scopes.restricted=1` 的 `share`+`subpath` 拼路径）
- Produces: `user_has_restricted_access(conn, user_id) -> bool`
- Produces: `pick_model(conn, user_id, cloud_ref, local_ref) -> tuple[dict, bool]`（返回 `(model_ref, is_local)`）
- Produces: `build_permissions(restricted_paths: list[str], extra_denied: list[str]) -> list[dict]`

- [x] **Step 1: 写失败测试**

```python
def _mkuser(conn, uid=None):
    return create_user(conn, "u", "pw")

def test_restricted_dirs_listed(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted, label)"
                 " VALUES('W:', '2.党建', 1, '涉密')")
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('U:', 'x', 0)")
    conn.commit()
    assert restricted_dirs(conn) == ["W:/2.党建"]

def test_user_without_scope_has_no_restricted_access(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('W:', '2.党建', 1)")
    conn.commit()
    uid = _mkuser(conn)
    assert user_has_restricted_access(conn, uid) is False

def test_user_with_scope_on_restricted_dir_has_access(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('W:', '2.党建', 1)")
    conn.commit()
    uid = _mkuser(conn)
    set_scopes(conn, uid, [("W:", "2.党建")])
    assert user_has_restricted_access(conn, uid) is True

def test_pick_model_uses_local_for_restricted_user(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('W:', 's', 1)")
    conn.commit()
    uid = _mkuser(conn)
    set_scopes(conn, uid, [("W:", "s")])
    cloud = {"id": "c", "providerID": "opencode", "modelID": "c"}
    local = {"id": "l", "providerID": "ollama", "modelID": "l"}
    ref, is_local = pick_model(conn, uid, cloud, local)
    assert is_local is True and ref == local

def test_pick_model_uses_cloud_otherwise(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = _mkuser(conn)
    cloud = {"id": "c", "providerID": "opencode", "modelID": "c"}
    local = {"id": "l", "providerID": "ollama", "modelID": "l"}
    ref, is_local = pick_model(conn, uid, cloud, local)
    assert is_local is False and ref == cloud

def test_build_permissions_denies_external_directory():
    perms = build_permissions(["W:/2.党建"], [".feishu/*"])
    assert {"action": "external_directory", "resource": "*", "effect": "deny"} in perms
    assert {"action": "read", "resource": ".feishu/*", "effect": "deny"} in perms
    assert {"action": "read", "resource": "W:/2.党建/*", "effect": "deny"} in perms
    assert {"action": "shell", "resource": "*W:/2.党建*", "effect": "deny"} in perms
    assert {"action": "read", "resource": "secrets.toml", "effect": "deny"} in perms
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_routing.py -v`
Expected: FAIL — `ImportError: cannot import name 'restricted_dirs'`

- [x] **Step 3: 实现 `kbplatform/feishu/routing.py`**

- `restricted_dirs(conn)`：`SELECT share, subpath FROM dir_scopes WHERE restricted=1`，
  拼成 `share + "/" + subpath`（`subpath` 为空则只返回 `share`），去掉尾部 `/`。
- `user_has_restricted_access(conn, user_id)`：把用户的 `user_scopes` 与受限目录
  按 `(share, subpath)` 前缀匹配；命中返回 True。管理员（`users.role='admin'`）返回 False
  （管理员不受范围限制，用云端模型即可）。
- `pick_model(conn, user_id, cloud_ref, local_ref)`：管理员或无涉密可见 → `(cloud_ref, False)`；
  否则 `(local_ref, True)`。
- `build_permissions(restricted_paths, extra_denied)`：按 spec §5.4 生成规则列表，
  顺序为：`external_directory` deny → `read`/`edit` deny `extra_denied` →
  `read`/`edit`/`shell` deny 每个受限目录 → `read`/`shell` deny `secrets.toml`。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_routing.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/feishu/routing.py kbplatform/tests/test_feishu_routing.py
git commit -m "feat(feishu): 模型路由与 session 权限规则"
```

---

### Task 8: 机器人主循环

**Files:**
- Create: `kbplatform/feishu/bot.py`
- Create: `kbplatform/tests/test_feishu_bot.py`

**Interfaces:**
- Consumes: Task 3 的 `Identity`、Task 4 的 `FeishuClient`、Task 5 的 `OpenCodeClient`、
  Task 6 的 `TokenStore`、Task 7 的 `pick_model`/`build_permissions`
- Produces: `class FeishuBot:`
  - `__init__(self, conn, cfg, client, oc, tokens, workspace)`
  - `handle(event) -> None` —— 事件主入口
  - `split_reply(text: str, chunk: int) -> list[str]`
  - `should_reply(chat_type: str, mentions: list, bot_open_id: str) -> bool`

- [x] **Step 1: 写失败测试**

```python
def test_split_reply_splits_long_text():
    chunks = split_reply("a" * 7000, 3000)
    assert len(chunks) == 3
    assert "".join(chunks) == "a" * 7000

def test_split_reply_keeps_short_text():
    assert split_reply("短", 3000) == ["短"]

def test_should_reply_group_requires_mention():
    assert should_reply("group", [{"id": {"open_id": "ou_bot"}}], "ou_bot") is True
    assert should_reply("group", [{"id": {"open_id": "ou_other"}}], "ou_bot") is False
    assert should_reply("group", [], "ou_bot") is False

def test_should_reply_p2p_always_true():
    assert should_reply("p2p", [], "ou_bot") is True

def test_duplicate_message_id_is_ignored(tmp_path):
    bot, _ = _make_bot(tmp_path)
    ev = _event(message_id="om_1", chat_type="p2p", text="hi")
    bot.handle(ev)
    bot.handle(ev)          # 同一 message_id 再来一次
    assert bot.client.sent == 1     # 只发了一条
```

`_make_bot` 用假 client（记录发送内容）与假 opencode client（立即返回 "ok"）。

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_bot.py -v`
Expected: FAIL — `ImportError: cannot import name 'FeishuBot'`

- [x] **Step 3: 实现 `kbplatform/feishu/bot.py`**

- `split_reply(text, chunk)`：按 `chunk` 切分，优先在换行处断开；空文本返回 `[]`。
- `should_reply(chat_type, mentions, bot_open_id)`：`p2p` → True；否则检查
  `mentions` 里是否有 `id.open_id == bot_open_id`。
- `FeishuBot.handle(event)`：
  1. 提取 `message_id`、`chat_id`、`chat_type`、`sender_open_id`、文本。
  2. `message_id` 去重（进程内 `set`，上限 1000 条，超了丢最旧的）。
  3. `chat_type == "group"` 且没 @ 机器人 → 直接 return。
  4. `message_type != "text"` → 回复「暂只支持文本消息」后 return。
  5. 查/建平台账号（`Identity.get_or_create`）；建了号则额外发一条开通提示。
  6. `Identity.is_active(user_id)` 为假 → 回复「账号已停用，请联系管理员」后 return。
  7. 取或建 opencode session（按 `(feishu_user_id, chat_id)` 查 `feishu_sessions`）。
  8. 按 `pick_model` 选模型；session 已存在但模型变了 → `oc.set_model`。
  9. 写 token 文件，`oc.set_environment(session_id, session_env(base_env, {"KB_TOKEN_FILE": path}))`。
  10. `oc.prompt(session_id, 指令文本)`，然后 `oc.wait_for_result`。
  11. 结果 `split_reply` 后逐条 `client.send_text`。
  12. 写 `audit_log`（action='feishu'，model_used=实际模型，restricted=该用户是否有涉密可见权限）。
  13. 同一 `(chat_id, open_id)` 用一把锁串行；本地模型并发闸门 1、云端 3（用 `threading.Semaphore`）。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_bot.py -v`
Expected: PASS

- [x] **Step 5: 跑全量测试**

Run: `pytest`
Expected: 全绿

- [x] **Step 6: 提交**

```bash
git add kbplatform/feishu/bot.py kbplatform/tests/test_feishu_bot.py
git commit -m "feat(feishu): 机器人事件主循环"
```

---

### Task 9: 平台侧小改

**Files:**
- Modify: `kbplatform/service.py`（加 `PUT /api/v1/admin/users/{user_id}/active`）
- Modify: `web/static/app.js`（空 scope 文案 + 停用按钮）
- Create: `kbplatform/tests/test_feishu_admin_active.py`

**Interfaces:**
- Produces: `PUT /api/v1/admin/users/{user_id}/active`，body `{"active": bool}`，
  返回 `{"ok": True, "active": bool}`；需要管理员权限。

- [x] **Step 1: 写失败测试**

```python
def test_admin_can_toggle_user_active(tmp_path, monkeypatch):
    app = make_app_with_db(tmp_path)     # 测试里已有的造 app 辅助
    # 登录 admin -> PUT active false -> 该用户 token 立刻失效
```

若 `make_app` 不方便注入测试库，则直接对 `users.active` 做断言式测试：
```python
def test_set_active_endpoint_exists():
    from kbplatform import service
    paths = {r.path for r in service.app.routes}
    assert "/api/v1/admin/users/{user_id}/active" in paths
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_admin_active.py -v`
Expected: FAIL — 路由不存在

- [x] **Step 3: 实现**

`service.py` 在 admin 用户端点附近加：

```python
@app.put("/api/v1/admin/users/{user_id}/active")
def admin_set_active(user_id: int, req: ActiveReq, ctx=Depends(require_admin)):
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if req.active else 0, user_id))
    conn.commit()
    return {"ok": True, "active": bool(req.active)}
```

并加请求体模型 `class ActiveReq(BaseModel): active: bool`。

`app.js` 的 `adminUsers()`：
- scope 显示：`(u.scopes || []).map(...).join("") || (u.role === "admin" ? '<span class="muted">（全部）</span>' : '<span class="muted">（未授权）</span>')`
- 用户行加一个「停用/启用」按钮，调用新端点后 `loadAdmin()` 刷新。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_admin_active.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add kbplatform/service.py web/static/app.js kbplatform/tests/test_feishu_admin_active.py
git commit -m "feat(admin): 用户停用开关与 scope 文案修正"
```

---

### Task 10: 进程入口与部署

**Files:**
- Create: `kbplatform/feishu/runner.py`
- Create: `kbplatform/tests/test_feishu_runner.py`
- Create: `tools/start-feishu-bot.ps1`
- Modify: `D:\kb-platform\.gitignore`

**Interfaces:**
- Produces: `class OpenCodeServer:` —— `__init__(self, cli_path, port, workdir)`；
  `start()`（spawn 并从 stdout 解析 url + password）；`stop()`；`is_alive()`；
  `password` / `base_url` 属性。
- Produces: `parse_server_output(text) -> tuple[str, str]` → `(base_url, password)`，
  从两行输出里解析 `server listening on <url>` 和 `server password <pw>`。
- Produces: `python -m kbplatform.feishu.runner` 进程入口。

- [x] **Step 1: 写失败测试**

```python
def test_parse_server_output():
    out = "server listening on http://127.0.0.1:4599\nserver password _9VZabc\n"
    url, pw = parse_server_output(out)
    assert url == "http://127.0.0.1:4599"
    assert pw == "_9VZabc"

def test_parse_server_output_raises_when_incomplete():
    with pytest.raises(ValueError):
        parse_server_output("nothing here")

def test_runner_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    assert run_once(config_path=str(tmp_path / "config.toml")) is None
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest kbplatform/tests/test_feishu_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_server_output'`

- [x] **Step 3: 实现**

- `parse_server_output(text)`：正则找 `server listening on (\S+)` 和 `server password (\S+)`，
  缺任一抛 `ValueError`。
- `OpenCodeServer.start()`：`subprocess.Popen([cli, "serve", "--port", str(port),
  "--hostname", "127.0.0.1"], stdout=PIPE, stderr=STDOUT, text=True, cwd=workdir)`，
  读两行解析出 url 与 password（解析不出来就把进程杀掉并抛错）。
- `runner.run_once(config_path)`：装载配置（拿不到返回 `None`）→ 起 `OpenCodeServer`
  → 建 `FeishuClient` → 建 `OpenCodeClient` → 注册 lark-oapi 事件处理器 → `ws.Client(...).start()`。
- `tools/start-feishu-bot.ps1`：任务计划用的启动脚本，设置 PATH 后跑
  `python -m kbplatform.feishu.runner`。
- `.gitignore` 追加 `secrets.toml` 与 `.feishu/`。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest kbplatform/tests/test_feishu_runner.py -v`
Expected: PASS

- [x] **Step 5: 跑全量测试**

Run: `pytest`
Expected: 全绿

- [x] **Step 6: 提交**

```bash
git add kbplatform/feishu/runner.py tools/start-feishu-bot.ps1 .gitignore
git commit -m "feat(feishu): 进程入口与部署脚本"
```

---

### Task 11: 端到端联调

**Files:**
- Modify: `D:\kb-platform\secrets.toml`（填真实凭据）
- Modify: `D:\kb-platform\config.toml`（加 `[feishu]` 段）

- [x] **Step 1: 填凭据**

`secrets.toml` 写入 AppID / AppSecret；`config.toml` 追加 `[feishu]` 段（`enabled = true`）。

- [x] **Step 2: 启动知识库服务（若未运行）**

确认 `http://127.0.0.1:8000` 可达；不可达则用既有方式拉起 `kbplatform.service:app`。

- [x] **Step 3: 前台跑 bot**

`python -m kbplatform.feishu.runner`，看日志确认长连接建立、密码解析成功。

- [x] **Step 4: 飞书里私聊机器人发「你好」**

确认收到回复；再问一个知识库里的问题，确认 agent 调了 `kbq.py` 并返回带引用的答案。

- [x] **Step 5: 群里 @机器人**

确认响应；不 @ 时确认静默。

- [x] **Step 6: 重启 bot**

确认同一会话继续原 opencode session（上下文连续）。

- [x] **Step 7: 验收项逐条核对**

对照 spec §10 的 12 条验收标准。

---

## 自检

- **Spec 覆盖**：§5.1→T4/T8；§5.2→T2/T3/T8；§5.3→T5/T6；§5.4→T7/T8；§5.5→T6；§5.6→T5；
  §5.7→T5（`session_env`）；§5.8→T8；§5.9→T8/T10；§5.10→T10；§5.11→T9；
  §6→T1/T10；§7→T7/T8（权限规则 + 审计）；§8→T4；§9→T6/T8；§10→T11。
- **签名一致**：`pick_model` 返回 `(ref, is_local)`；`build_permissions(restricted_paths, extra_denied)`；
  `session_env(base, extra)`；`TokenStore.token_for/rotate/write_session_token`；`split_reply(text, chunk)`；
  `should_reply(chat_type, mentions, bot_open_id)`。
- **比例**：11 个任务，每个 5~6 步，代码块只出现在测试和签名处。
