"""飞书机器人事件主循环。"""
import json
import threading

import pytest

from kbplatform.db import init_db
from kbplatform.feishu.bot import (FeishuBot, PROGRESS_TEXT, should_reply,
                                  split_reply)
from kbplatform.feishu.config import FeishuConfig
from kbplatform.feishu.identity import Identity
from kbplatform.feishu.kbcred import TokenStore


# ---------------------------------------------------------------- 纯函数
def test_split_reply_splits_long_text():
    chunks = split_reply("a" * 7000, 3000)
    assert len(chunks) == 3
    assert "".join(chunks) == "a" * 7000


def test_split_reply_keeps_short_text():
    assert split_reply("短", 3000) == ["短"]


def test_split_reply_prefers_newline_break():
    text = "第一行\n" + "x" * 4000
    chunks = split_reply(text, 3000)
    assert len(chunks) >= 2
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_reply_empty_text():
    assert split_reply("", 3000) == []


def test_should_reply_group_requires_mention():
    assert should_reply("group", [{"id": {"open_id": "ou_bot"}}], "ou_bot") is True
    assert should_reply("group", [{"id": {"open_id": "ou_other"}}], "ou_bot") is False
    assert should_reply("group", [], "ou_bot") is False


def test_should_reply_p2p_always_true():
    assert should_reply("p2p", [], "ou_bot") is True


# ---------------------------------------------------------------- 假实现
class FakeFeishuClient:
    def __init__(self):
        self.sent = []
        self.bot_id = "ou_bot"

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"message_id": f"om_{len(self.sent)}"}

    def bot_open_id(self):
        return self.bot_id

    def get_user(self, open_id):
        return {"name": "测试用户", "department_ids": ["od_1"]}


class FakeOpenCode:
    def __init__(self, result="干完了"):
        self.result = result
        self.sessions = {}
        self.prompts = []
        self.envs = {}
        self.models = {}
        self.permissions = {}
        self._n = 0

    def create_session(self, title, model_ref, directory, permissions=None):
        self._n += 1
        sid = f"ses_{self._n}"
        self.sessions[sid] = model_ref
        self.permissions[sid] = permissions
        return sid

    def prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return {"id": "msg_x", "since": 1000}

    def wait_for_result(self, session_id, since=0, timeout_sec=600):
        self.since = since
        return self.result

    def set_model(self, session_id, model_ref):
        self.models[session_id] = model_ref
        self.sessions[session_id] = model_ref

    def set_environment(self, session_id, variables):
        self.envs[session_id] = variables

    def set_permissions(self, session_id, permissions):
        self.permissions[session_id] = permissions


def _event(message_id="om_1", chat_type="p2p", text="你好", open_id="ou_1",
           chat_id="oc_1", message_type="text", mentions=None):
    return {"message_id": message_id, "chat_id": chat_id, "chat_type": chat_type,
            "sender_open_id": open_id, "message_type": message_type, "text": text,
            "mentions": mentions or []}


def _make_bot(tmp_path, result="干完了"):
    conn = init_db(str(tmp_path / "kb.db"))
    cfg = FeishuConfig(app_id="a", app_secret="b", workspace=str(tmp_path),
                       reply_chunk=3000, max_concurrent_cloud=3, max_concurrent_local=1)
    client = FakeFeishuClient()
    oc = FakeOpenCode(result)
    bot = FeishuBot(conn, cfg, client, oc, TokenStore(conn), workspace=str(tmp_path))
    return bot, client, oc, conn


# ---------------------------------------------------------------- 行为
def test_p2p_message_gets_reply(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    # 首条消息：一条开通提示 + 一条执行结果
    assert len(client.sent) == 2
    assert "干完了" in client.sent[-1][1]
    assert client.sent[-1][0] == "oc_1"


def test_group_message_without_mention_is_silent(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(chat_type="group", mentions=[{"id": {"open_id": "ou_other"}}]))
    assert client.sent == []
    assert oc.prompts == []


def test_group_message_from_unregistered_user_is_silent(tmp_path):
    """没私聊过的人不在群里被自动开号。"""
    bot, client, oc, conn = _make_bot(tmp_path)
    bot.handle(_event(chat_type="group", mentions=[{"id": {"open_id": "ou_bot"}}]))
    assert client.sent == []
    n = conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0]
    assert n == 0


def test_group_message_with_mention_is_answered(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(message_id="om_p", chat_type="p2p"))   # 先私聊建号
    client.sent.clear()
    bot.handle(_event(message_id="om_g", chat_type="group",
                      mentions=[{"id": {"open_id": "ou_bot"}}]))
    assert len(client.sent) == 1
    assert "干完了" in client.sent[0][1]


def test_duplicate_message_id_is_ignored(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    ev = _event()
    bot.handle(ev)
    n_after_first = len(client.sent)
    bot.handle(ev)
    assert len(client.sent) == n_after_first
    assert len(oc.prompts) == 1


def test_first_contact_creates_account_and_still_executes(tmp_path):
    bot, client, oc, conn = _make_bot(tmp_path)
    bot.handle(_event(text="查一下数据中心规定"))
    # 一条是开通提示，一条是执行结果
    assert len(client.sent) == 2
    assert "可见范围为空" in client.sent[0][1]
    assert "干完了" in client.sent[1][1]
    n = conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0]
    assert n == 1


def test_second_message_does_not_repeat_welcome(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(message_id="om_1"))
    bot.handle(_event(message_id="om_2"))
    texts = [t for _, t in client.sent]
    assert sum(1 for t in texts if "可见范围为空" in t) == 1


def test_same_chat_reuses_same_session(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(message_id="om_1"))
    bot.handle(_event(message_id="om_2"))
    sids = {sid for sid, _ in oc.prompts}
    assert len(sids) == 1


def test_different_chat_gets_different_session(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(message_id="om_1", chat_id="oc_1"))
    bot.handle(_event(message_id="om_2", chat_id="oc_2"))
    sids = {sid for sid, _ in oc.prompts}
    assert len(sids) == 2


def test_non_text_message_is_rejected(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(message_type="image", text=""))
    assert len(client.sent) == 1
    assert "只支持文本" in client.sent[0][1]
    assert oc.prompts == []


def test_inactive_user_is_rejected(tmp_path):
    bot, client, oc, conn = _make_bot(tmp_path)
    bot.handle(_event(message_id="om_1"))
    conn.execute("UPDATE users SET active=0")
    conn.commit()
    bot.handle(_event(message_id="om_2"))
    texts = [t for _, t in client.sent]
    assert any("已停用" in t for t in texts)


def test_token_file_is_written_and_env_injected(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    sid = oc.prompts[0][0]
    env = oc.envs[sid]
    assert "KB_TOKEN_FILE" in env
    assert env["KB_TOKEN_FILE"].endswith(".token")
    # agent 上下文里不能出现应用密钥
    assert "FEISHU_APP_SECRET" not in env


def test_long_reply_is_split_into_multiple_messages(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path, result="x" * 7000)
    bot.handle(_event())
    # 1 条开通提示 + 3 段正文
    assert len(client.sent) == 4
    body = "".join(t for _, t in client.sent[1:])
    assert body == "x" * 7000


def test_prompt_includes_kb_usage_directive(tmp_path):
    """agent 必须知道知识库怎么查，否则它只会说「我先看看」。"""
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event(text="数据中心机房规定"))
    sid, prompt = oc.prompts[0]
    assert "tools/kbq.py" in prompt
    assert "KB_TOKEN_FILE" in prompt
    assert "数据中心机房规定" in prompt


def test_prompt_directive_gives_absolute_python_path(tmp_path):
    """必须给绝对路径：Windows 上 `python` 常是拉起 Microsoft Store 的假别名。"""
    import sys
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    sid, prompt = oc.prompts[0]
    assert sys.executable in prompt


def test_prompt_directive_lists_restricted_dirs(tmp_path):
    bot, client, oc, conn = _make_bot(tmp_path)
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('W:','2.党建',1)")
    conn.commit()
    bot.handle(_event())
    sid, prompt = oc.prompts[0]
    assert "W:/2.党建" in prompt


def test_prompt_does_not_contain_token(tmp_path):
    """token 不能进 agent 上下文。"""
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    sid, prompt = oc.prompts[0]
    assert "kb_" not in prompt


def test_audit_row_written(tmp_path):
    bot, client, oc, conn = _make_bot(tmp_path)
    bot.handle(_event(text="查资料"))
    row = conn.execute("SELECT action, question, model_used, token_prefix FROM audit_log"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "feishu"
    assert row["question"] == "查资料"
    assert row["token_prefix"] == "feishu-bot"
    assert row["model_used"]


def test_restricted_user_session_uses_local_model(tmp_path):
    bot, client, oc, conn = _make_bot(tmp_path)
    bot.handle(_event())
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('W:','s',1)")
    conn.commit()
    uid = conn.execute("SELECT user_id FROM feishu_users").fetchone()[0]
    conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES(?, 'W:', 's')", (uid,))
    conn.commit()
    bot.handle(_event(message_id="om_2"))
    sid = oc.prompts[-1][0]
    assert oc.sessions[sid]["providerID"] == "ollama"


def test_agent_error_is_reported_without_traceback(tmp_path):
    from kbplatform.feishu.opencode import OpenCodeError

    class FailingOpenCode(FakeOpenCode):
        def wait_for_result(self, session_id, since=0, timeout_sec=600):
            raise OpenCodeError("模型不可用")

    conn = init_db(str(tmp_path / "kb.db"))
    cfg = FeishuConfig(app_id="a", app_secret="b", workspace=str(tmp_path))
    client = FakeFeishuClient()
    bot = FeishuBot(conn, cfg, client, FailingOpenCode(), TokenStore(conn),
                    workspace=str(tmp_path))
    bot.handle(_event())
    texts = [t for _, t in client.sent]
    assert any("模型不可用" in t for t in texts)
    assert not any("Traceback" in t for t in texts)


def test_bot_passes_turn_marker_to_wait_for_result(tmp_path):
    """必须把本轮的 since 传下去，否则会话里上一轮的 idle 会被当成本轮的结束。"""
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    assert oc.since == 1000


def test_slow_task_sends_progress_notice(tmp_path):
    """任务跑得久时先给一句「处理中」，别让人干等。"""
    import time

    class SlowOpenCode(FakeOpenCode):
        def wait_for_result(self, session_id, since=0, timeout_sec=600):
            time.sleep(0.3)
            return "做完了"

    conn = init_db(str(tmp_path / "kb.db"))
    cfg = FeishuConfig(app_id="a", app_secret="b", workspace=str(tmp_path),
                       progress_notice_sec=0.05)
    client = FakeFeishuClient()
    bot = FeishuBot(conn, cfg, client, SlowOpenCode(), TokenStore(conn),
                    workspace=str(tmp_path))
    bot.handle(_event())
    texts = [t for _, t in client.sent]
    assert any(PROGRESS_TEXT in t for t in texts)
    assert any("做完了" in t for t in texts)


def test_fast_task_sends_no_progress_notice(tmp_path):
    bot, client, oc, _ = _make_bot(tmp_path)
    bot.handle(_event())
    texts = [t for _, t in client.sent]
    assert not any(PROGRESS_TEXT in t for t in texts)


def test_kb_auth_expired_triggers_one_retry(tmp_path):
    class ExpiringOpenCode(FakeOpenCode):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait_for_result(self, session_id, since=0, timeout_sec=600):
            self.calls += 1
            if self.calls == 1:
                return "KB_AUTH_EXPIRED: token 失效"
            return "重试后成功"

    conn = init_db(str(tmp_path / "kb.db"))
    cfg = FeishuConfig(app_id="a", app_secret="b", workspace=str(tmp_path))
    client = FakeFeishuClient()
    oc = ExpiringOpenCode()
    bot = FeishuBot(conn, cfg, client, oc, TokenStore(conn), workspace=str(tmp_path))
    bot.handle(_event())
    assert oc.calls == 2
    assert "重试后成功" in client.sent[-1][1]
