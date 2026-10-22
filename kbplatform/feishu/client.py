"""飞书开放平台 API 客户端。

只依赖标准库 urllib —— 少一个依赖少一份风险。

用到的接口：
  * POST /open-apis/auth/v3/tenant_access_token/internal  取应用身份 token
  * POST /open-apis/im/v1/messages                        发消息
  * GET  /open-apis/contact/v3/users/{open_id}             取用户昵称/部门
  * GET  /open-apis/bot/v3/info                            取机器人自己的 open_id
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_DOMAIN = "https://open.feishu.cn"
# 提前 5 分钟过期，避免边界上用到刚失效的 token
_TOKEN_SKEW = 300


class FeishuApiError(Exception):
    """飞书接口返回了非 0 的 code。"""

    def __init__(self, code: int, msg: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"飞书接口错误 code={code} msg={msg}")


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, domain: str = DEFAULT_DOMAIN):
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain.rstrip("/")
        self._token = ""
        self._token_expire = 0.0
        self._bot_open_id = ""

    # ------------------------------------------------------------ 底层
    def _request(self, method: str, path: str, body: dict | None = None,
                 auth: bool = True, timeout: int = 30) -> dict:
        url = self.domain + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = "Bearer " + self.tenant_token()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise FeishuApiError(e.code, f"HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise FeishuApiError(-1, f"网络错误: {e.reason}") from e
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------ token
    def tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire:
            return self._token
        obj = self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                            {"app_id": self.app_id, "app_secret": self.app_secret},
                            auth=False)
        if obj.get("code") != 0:
            raise FeishuApiError(obj.get("code", -1), obj.get("msg", "取 token 失败"))
        self._token = obj.get("tenant_access_token", "")
        expire = int(obj.get("expire", 7200) or 7200)
        self._token_expire = time.time() + max(60, expire - _TOKEN_SKEW)
        return self._token

    # ------------------------------------------------------------ 业务
    def send_text(self, chat_id: str, text: str) -> dict:
        """往指定会话发一条文本消息。"""
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        obj = self._request("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id",
                            payload)
        if obj.get("code") != 0:
            raise FeishuApiError(obj.get("code", -1), obj.get("msg", "发消息失败"))
        return obj.get("data") or {}

    def get_user(self, open_id: str) -> dict:
        """取用户昵称与部门 ID 串。失败返回空 dict（不阻断主流程）。"""
        try:
            obj = self._request("GET", f"/open-apis/contact/v3/users/{open_id}")
        except Exception:
            return {}
        if obj.get("code") != 0:
            return {}
        data = obj.get("data") or {}
        # contact 接口把用户对象放在 data.user 下，但也认平铺的形状
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        return {"name": user.get("name"),
                "department_ids": user.get("department_ids") or []}

    def bot_open_id(self) -> str:
        """机器人自己的 open_id（用来判断群里是否 @ 了我）。"""
        if self._bot_open_id:
            return self._bot_open_id
        try:
            obj = self._request("GET", "/open-apis/bot/v3/info")
        except FeishuApiError:
            return ""
        if obj.get("code") != 0:
            return ""
        bot = obj.get("bot") or (obj.get("data") or {}).get("bot") or {}
        self._bot_open_id = bot.get("open_id") or ""
        return self._bot_open_id
