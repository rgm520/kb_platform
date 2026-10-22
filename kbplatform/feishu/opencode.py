"""OpenCode HTTP API 客户端（v2.1.2）。

鉴权：HTTP Basic，用户名固定 `opencode`，密码是 `opencode serve` 启动时
打印在 stdout 上的那串（也登记在 `~/.local/state/opencode/service.json`）。

接口要点（均已实测）：
  * `POST /api/session`                     响应是 `{"data": {...}}` 包装
  * `POST /api/session/{id}/prompt`         **异步**，返回 inbox 条目
  * `GET  /api/session/{id}/message`        响应是 `{"data": [...]}` 包装
  * `POST /api/session/{id}/model`          换模型（是 POST，不是 PATCH）
  * `PATCH /api/session/{id}`               改权限规则
  * `PUT  /api/session/{id}/environment`    **全量替换**环境变量
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from kbplatform.feishu.config import SESSION_ENV_ALLOWLIST


class OpenCodeError(Exception):
    """OpenCode 服务返回错误，或会话执行失败。"""


def session_env(base_env: dict, extra: dict | None = None) -> dict:
    """构造要提交给 session 的环境变量。

    **白名单过滤**：只放行 SESSION_ENV_ALLOWLIST 里的变量。
    bot 进程环境里有 FEISHU_APP_SECRET / KB_SESSION_SECRET，
    全量透传等于把应用密钥交到 agent 手里。

    **PATH 修正**：把当前解释器所在目录放到 PATH 最前面。Windows 上
    `python` 往往指向 WindowsApps 的应用执行别名——执行它会拉起 Microsoft
    Store，退出码 1 且没有任何输出，agent 会误以为脚本坏了然后满世界找
    解释器（实测白烧十分钟）。
    """
    out = {k: v for k, v in (base_env or {}).items()
           if k in SESSION_ENV_ALLOWLIST and isinstance(v, str)}
    for k, v in (extra or {}).items():
        out[k] = str(v)
    _put_python_first(out)
    return out


def _put_python_first(env: dict) -> None:
    py_dir = os.path.dirname(os.path.abspath(sys.executable))
    if not py_dir:
        return
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    parts = [p for p in parts if p.lower() != py_dir.lower()]
    env["PATH"] = os.pathsep.join([py_dir] + parts)


class OpenCodeClient:
    def __init__(self, base_url: str, password: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self._auth = "Basic " + token

    # ------------------------------------------------------------ 底层
    def _call(self, method: str, path: str, body: dict | None = None,
              timeout: int | None = None) -> dict:
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": self._auth,
                     "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise OpenCodeError(f"HTTP {e.code} {method} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise OpenCodeError(f"连不上 OpenCode 服务 {url}: {e.reason}") from e
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------ 接口
    def info(self) -> dict:
        return self._call("GET", "/api/info")

    def create_session(self, title: str, model_ref: dict, directory: str,
                       permissions: list | None = None) -> str:
        body = {"title": title, "agent": "build", "model": model_ref,
                "location": {"directory": directory}}
        if permissions is not None:
            body["permissions"] = permissions
        obj = self._call("POST", "/api/session", body)
        sid = (obj.get("data") or {}).get("id")
        if not sid:
            raise OpenCodeError(f"建会话失败: {json.dumps(obj, ensure_ascii=False)[:200]}")
        return sid

    def prompt(self, session_id: str, text: str) -> dict:
        """下指令。返回本轮的标记 {"id": 用户消息 id, "since": 创建时间戳(ms)}。

        `since` 用来把本轮和会话里**更早的轮次**区分开——会话是连续的，
        上一轮遗留的 idle 标记不能被当成本轮的结束信号。
        """
        obj = self._call("POST", f"/api/session/{session_id}/prompt", {"text": text})
        data = obj.get("data") or {}
        return {"id": data.get("id") or "",
                "since": (data.get("time") or {}).get("created") or 0}

    def messages(self, session_id: str, order: str = "desc") -> list:
        """取消息列表。

        **必须带显式 limit**：会话动辄上百条消息，而接口默认只返回 50 条。
        用 order=asc 且不传 limit 的话，拿到的是最旧的 50 条 —— 位于末尾的
        idle 结束标记永远看不到，调用方只能干等到超时（实测踩过：会话成功
        结束，调用方却超时了 8 分钟）。

        **limit 上限 200**：超过会被接口拒（"Expected a value less than or
        equal to 200"）。desc 顺序下最新的 200 条足够覆盖当前这一轮。
        """
        obj = self._call("GET",
                         f"/api/session/{session_id}/message?order={order}&limit=200")
        data = obj.get("data")
        return data if isinstance(data, list) else []

    @staticmethod
    def _created(msg: dict) -> int:
        try:
            return int((msg.get("time") or {}).get("created") or 0)
        except (TypeError, ValueError):
            return 0

    def wait_for_result(self, session_id: str, since: int = 0,
                        timeout_sec: int = 600, poll: float = 0.0) -> str:
        """轮询直到本轮结束，返回**最终**的 assistant 文本。

        关键：工具调用前 agent 会先说一句「我先看看…」，那不是结果。
        一轮结束的标志是出现 `type == "idle"` 的消息（outcome 为
        succeeded/failed/interrupted）。必须等到它，才取本轮最后一条带
        text 的 assistant 消息。

        `since` 是本轮用户消息的时间戳，早于它的 idle 属于上一轮，忽略。

        失败（outcome=failed）抛 OpenCodeError；被中断（interrupted）也抛；
        超时同样抛。
        """
        deadline = time.time() + timeout_sec
        interval = 0.0 if poll else 1.0
        while True:
            msgs = self.messages(session_id, order="desc")
            idle_idx = None
            for i, m in enumerate(msgs):
                if m.get("type") == "idle" and self._created(m) >= since:
                    idle_idx = i
                    break
            if idle_idx is not None:
                idle = msgs[idle_idx]
                outcome = idle.get("outcome") or "succeeded"
                if outcome == "failed":
                    err = self._last_error(session_id) or {}
                    raise OpenCodeError(err.get("message") or "agent 执行失败")
                if outcome == "interrupted":
                    raise OpenCodeError("agent 执行被中断")
                # desc 顺序里，idle 之后（更旧）的消息才是本轮的内容；
                # idle 之前的属于更晚的轮次，不能取。
                text = ""
                for m in msgs[idle_idx + 1:]:
                    if self._created(m) < since:
                        break
                    if m.get("type") == "assistant":
                        parts = [c.get("text", "") for c in (m.get("content") or [])
                                 if isinstance(c, dict) and c.get("type") == "text"]
                        t = "".join(parts).strip()
                        if t:
                            text = t
                            break
                if not text:
                    raise OpenCodeError("agent 没有产出文本结果")
                return text
            if time.time() >= deadline:
                raise OpenCodeError(f"agent 处理超时（{timeout_sec} 秒）")
            time.sleep(interval)
            interval = 3.0 if interval < 3.0 else interval

    def _last_error(self, session_id: str) -> dict:
        for m in reversed(self.messages(session_id)):
            if m.get("type") == "idle" and m.get("error"):
                return m["error"]
        return {}

    def set_model(self, session_id: str, model_ref: dict) -> None:
        self._call("POST", f"/api/session/{session_id}/model", {"model": model_ref})

    def set_permissions(self, session_id: str, permissions: list) -> None:
        self._call("PATCH", f"/api/session/{session_id}", {"permissions": permissions})

    def set_environment(self, session_id: str, variables: dict) -> None:
        self._call("PUT", f"/api/session/{session_id}/environment", {"variables": variables})

    def interrupt(self, session_id: str) -> None:
        self._call("POST", f"/api/session/{session_id}/interrupt")
