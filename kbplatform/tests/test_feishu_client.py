"""飞书开放平台 API 客户端。"""
import json

import pytest

from kbplatform.feishu.client import FeishuApiError, FeishuClient


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


def _token_resp():
    return _Resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})


def test_tenant_token_is_cached(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _token_resp()

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    c = FeishuClient("cli_x", "sec")
    assert c.tenant_token() == "t-1"
    assert c.tenant_token() == "t-1"
    assert len(calls) == 1


def test_tenant_token_error_raises(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _Resp({"code": 10003, "msg": "app not exist"})

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(FeishuApiError) as e:
        FeishuClient("cli_x", "sec").tenant_token()
    assert e.value.code == 10003


def test_send_text_posts_to_messages_api(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _token_resp()
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["auth"] = req.headers.get("Authorization")
        return _Resp({"code": 0, "data": {"message_id": "om_1"}})

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
            return _token_resp()
        return _Resp({"code": 230001, "msg": "bot not in chat"})

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(FeishuApiError) as e:
        FeishuClient("cli_x", "sec").send_text("oc_1", "hi")
    assert e.value.code == 230001
    assert "bot not in chat" in str(e.value)


def test_get_user_returns_profile(monkeypatch):
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _token_resp()
        return _Resp({"code": 0, "data": {"user": {"name": "张三",
                                                   "department_ids": ["od_1"]}}})

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    info = FeishuClient("cli_x", "sec").get_user("ou_1")
    assert info["name"] == "张三"
    assert info["department_ids"] == ["od_1"]


def test_get_user_handles_flat_data_shape(monkeypatch):
    """有些接口把 user 平铺在 data 下，两种形状都要认。"""
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _token_resp()
        return _Resp({"code": 0, "data": {"name": "李四", "department_ids": []}})

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    assert FeishuClient("cli_x", "sec").get_user("ou_1")["name"] == "李四"


def test_get_user_returns_empty_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _token_resp()
        raise OSError("boom")

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    assert FeishuClient("cli_x", "sec").get_user("ou_1") == {}


def test_bot_open_id_is_cached(monkeypatch):
    n = []

    def fake_urlopen(req, timeout=None):
        if "tenant_access_token" in req.full_url:
            return _token_resp()
        n.append(req.full_url)
        return _Resp({"code": 0, "bot": {"open_id": "ou_bot"}})

    monkeypatch.setattr("kbplatform.feishu.client.urllib.request.urlopen", fake_urlopen)
    c = FeishuClient("cli_x", "sec")
    assert c.bot_open_id() == "ou_bot"
    assert c.bot_open_id() == "ou_bot"
    assert len(n) == 1
