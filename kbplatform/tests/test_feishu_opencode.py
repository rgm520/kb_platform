"""OpenCode HTTP 客户端。"""
import base64
import json

import pytest

from kbplatform.feishu.opencode import (OpenCodeClient, OpenCodeError,
                                         session_env)


class _Resp:
    def __init__(self, payload, status=200):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, handler):
    monkeypatch.setattr("kbplatform.feishu.opencode.urllib.request.urlopen",
                        lambda req, timeout=None: handler(req))


def test_basic_auth_header_is_sent(monkeypatch):
    seen = {}

    def h(req):
        seen["auth"] = req.headers.get("Authorization")
        return _Resp({"data": {"id": "ses_1"}})

    _install(monkeypatch, h)
    OpenCodeClient("http://127.0.0.1:4599", "pw").create_session(
        "t", {"id": "m", "providerID": "opencode", "modelID": "m"}, "D:/opencode")
    assert seen["auth"].startswith("Basic ")
    assert base64.b64decode(seen["auth"].split()[1]).decode() == "opencode:pw"


def test_create_session_returns_data_id(monkeypatch):
    _install(monkeypatch, lambda req: _Resp({"data": {"id": "ses_abc"}}))
    c = OpenCodeClient("http://x", "pw")
    assert c.create_session("t", {"id": "m", "providerID": "p", "modelID": "m"},
                             "D:/x") == "ses_abc"


def test_create_session_sends_location_and_permissions(monkeypatch):
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"data": {"id": "s"}})

    _install(monkeypatch, h)
    perms = [{"action": "read", "resource": "x", "effect": "deny"}]
    OpenCodeClient("http://x", "pw").create_session(
        "t", {"id": "m", "providerID": "p", "modelID": "m"}, "D:/x", permissions=perms)
    assert seen["body"]["location"] == {"directory": "D:/x"}
    assert seen["body"]["permissions"] == perms
    assert seen["body"]["agent"] == "build"


def test_messages_requests_desc_order_with_explicit_limit(monkeypatch):
    """会话消息可能上百条，asc 且不传 limit 只会拿到最旧的 50 条 ——
    idle 结束标记永远看不到，必然超时。必须 desc + 显式 limit。"""
    seen = {}

    def h(req):
        seen["url"] = req.full_url
        return _Resp({"data": []})

    _install(monkeypatch, h)
    OpenCodeClient("http://x", "pw").messages("s", order="desc")
    assert "order=desc" in seen["url"]
    assert "limit=" in seen["url"]


def test_messages_limit_does_not_exceed_api_maximum(monkeypatch):
    """接口拒绝 limit > 200，会直接 400 —— 整条链路就废了。"""
    seen = {}

    def h(req):
        seen["url"] = req.full_url
        return _Resp({"data": []})

    _install(monkeypatch, h)
    OpenCodeClient("http://x", "pw").messages("s")
    limit = int(seen["url"].split("limit=")[1].split("&")[0])
    assert limit <= 200


def test_wait_for_result_ignores_idle_of_previous_turn(monkeypatch):
    """会话是连续的：上一轮遗留的 idle 不能当成本轮的结束标记。"""
    msgs = {"data": [
        {"type": "idle", "outcome": "succeeded", "time": {"created": 1000}},
        {"type": "assistant", "content": [{"type": "text", "text": "上一轮的答案"}]},
        {"type": "user", "text": "新问题", "time": {"created": 2000}},
    ]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    with pytest.raises(OpenCodeError) as e:
        OpenCodeClient("http://x", "pw").wait_for_result("s", since=1500, timeout_sec=1)
    assert "超时" in str(e.value)


def test_wait_for_result_picks_answer_of_current_turn(monkeypatch):
    msgs = {"data": [
        {"type": "idle", "outcome": "succeeded", "time": {"created": 3000}},
        {"type": "assistant", "content": [{"type": "text", "text": "本轮答案"}],
         "time": {"created": 2500}},
        {"type": "assistant", "content": [{"type": "text", "text": "上一轮答案"}],
         "time": {"created": 1500}},
        {"type": "idle", "outcome": "succeeded", "time": {"created": 1000}},
    ]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    got = OpenCodeClient("http://x", "pw").wait_for_result("s", since=2000, timeout_sec=2)
    assert got == "本轮答案"


def test_prompt_returns_turn_marker(monkeypatch):
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"data": {"id": "msg_1", "time": {"created": 4242},
                               "type": "user", "payload": {"text": "hi"}}})

    _install(monkeypatch, h)
    marker = OpenCodeClient("http://x", "pw").prompt("s", "hi")
    assert marker["id"] == "msg_1"
    assert marker["since"] == 4242
    assert seen["body"] == {"text": "hi"}


def test_wait_for_result_waits_for_idle_before_returning(monkeypatch):
    """工具调用前的开场白不是最终答案 —— 必须等到 idle 才返回。"""
    # desc 顺序
    msgs = {"data": [
        {"type": "idle", "outcome": "succeeded"},
        {"type": "assistant", "content": [{"type": "text", "text": "最终答案"}]},
        {"type": "assistant", "content": [{"type": "tool", "text": ""}]},
        {"type": "assistant", "content": [{"type": "text", "text": "我先看看..."}]},
        {"type": "user", "text": "hi"},
    ]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    assert OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2) == "最终答案"


def test_wait_for_result_returns_last_assistant_text(monkeypatch):
    # desc 顺序：最新的在前
    msgs = {"data": [
        {"type": "idle", "outcome": "succeeded"},
        {"type": "assistant", "content": [{"type": "text", "text": "第二段"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "第一段"}]},
    ]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    assert OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2) == "第二段"


def test_wait_for_result_does_not_return_preamble_early(monkeypatch):
    """轮询中途只有开场白时不能提前返回（否则会把「我先看看…」当成结果）。"""
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp({"data": [{"type": "assistant",
                                    "content": [{"type": "text", "text": "我先看看..."}]}]})
        return _Resp({"data": [
            {"type": "idle", "outcome": "succeeded"},
            {"type": "assistant", "content": [{"type": "text", "text": "最终答案"}]},
            {"type": "assistant", "content": [{"type": "text", "text": "我先看看..."}]},
        ]})

    _install(monkeypatch, h)
    assert OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=5) == "最终答案"


def test_wait_for_result_raises_on_interrupted(monkeypatch):
    msgs = {"data": [{"type": "idle", "outcome": "interrupted"}]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    with pytest.raises(OpenCodeError):
        OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2)


def test_wait_for_result_joins_multiple_text_parts(monkeypatch):
    # desc 顺序
    msgs = {"data": [{"type": "idle", "outcome": "succeeded"},
                     {"type": "assistant", "content": [
                         {"type": "reasoning", "text": "想..."},
                         {"type": "text", "text": "第一段"},
                         {"type": "text", "text": "第二段"},
                     ]}]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    assert OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2) == "第一段第二段"


def test_wait_for_result_raises_on_failed_idle(monkeypatch):
    msgs = {"data": [{"type": "idle", "outcome": "failed",
                      "error": {"type": "provider.no-route", "message": "模型不可用"}}]}
    _install(monkeypatch, lambda req: _Resp(msgs))
    with pytest.raises(OpenCodeError) as e:
        OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=2)
    assert "模型不可用" in str(e.value)


def test_wait_for_result_times_out(monkeypatch):
    _install(monkeypatch, lambda req: _Resp({"data": [{"type": "user", "text": "hi"}]}))
    with pytest.raises(OpenCodeError) as e:
        OpenCodeClient("http://x", "pw").wait_for_result("s", timeout_sec=1)
    assert "超时" in str(e.value)


def test_set_environment_uses_put_with_variables(monkeypatch):
    seen = {}

    def h(req):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({})

    _install(monkeypatch, h)
    OpenCodeClient("http://x", "pw").set_environment("s", {"A": "1"})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/api/session/s/environment")
    assert seen["body"] == {"variables": {"A": "1"}}


def test_set_model_uses_post(monkeypatch):
    seen = {}

    def h(req):
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({})

    _install(monkeypatch, h)
    OpenCodeClient("http://x", "pw").set_model("s", {"id": "m", "providerID": "p", "modelID": "m"})
    assert seen["method"] == "POST"
    assert seen["body"] == {"model": {"id": "m", "providerID": "p", "modelID": "m"}}


def test_set_permissions_uses_patch(monkeypatch):
    seen = {}

    def h(req):
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({})

    _install(monkeypatch, h)
    OpenCodeClient("http://x", "pw").set_permissions("s", [{"action": "read"}])
    assert seen["method"] == "PATCH"
    assert seen["body"] == {"permissions": [{"action": "read"}]}


def test_http_error_becomes_opencode_error(monkeypatch):
    import urllib.error

    def h(req):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    _install(monkeypatch, h)
    with pytest.raises(OpenCodeError):
        OpenCodeClient("http://x", "pw").info()


def test_session_env_filters_secrets():
    base = {"PATH": "C:\\x", "SystemRoot": "C:\\Windows", "TEMP": "T",
            "FEISHU_APP_SECRET": "leak", "KB_SESSION_SECRET": "leak2",
            "RANDOM_JUNK": "x"}
    out = session_env(base, {"KB_TOKEN_FILE": "f"})
    assert out["KB_TOKEN_FILE"] == "f"
    assert "C:\\x" in out["PATH"]
    assert out["SystemRoot"] == "C:\\Windows"
    assert "FEISHU_APP_SECRET" not in out
    assert "KB_SESSION_SECRET" not in out
    assert "RANDOM_JUNK" not in out


def test_session_env_extra_overrides_base():
    out = session_env({"PATH": "C:\\old"}, {"PATH": "C:\\new"})
    # extra 覆盖生效，但 Python 目录仍在最前
    assert out["PATH"].endswith("C:\\new")
    assert "C:\\old" not in out["PATH"]


def test_session_env_puts_python_on_path():
    """agent 的 shell 必须能直接调 python。

    Windows 上 `python` 常指向 WindowsApps 的应用执行别名——执行它会打开
    Microsoft Store，退出码 1 且没有任何输出。agent 会因此以为脚本坏了，
    然后满世界找解释器。所以必须把真正的 Python 目录放到 PATH 最前面。
    """
    import os
    import sys
    py_dir = os.path.dirname(sys.executable)
    out = session_env({"PATH": "C:\\Windows\\system32"})
    assert out["PATH"].split(os.pathsep)[0].lower() == py_dir.lower()
    assert py_dir.lower() in out["PATH"].lower()


def test_session_env_does_not_duplicate_python_dir():
    import os
    import sys
    py_dir = os.path.dirname(sys.executable)
    out = session_env({"PATH": py_dir + os.pathsep + "C:\\Windows"})
    assert out["PATH"].lower().count(py_dir.lower()) == 1
