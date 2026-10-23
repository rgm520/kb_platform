"""飞书机器人事件主循环。

设计见 `docs/superpowers/specs/2026-10-21-feishu-bot-design.md` §5.1 / §5.3 / §5.4 / §5.8 / §5.9。

事件用**归一化的 dict** 表示，不直接吃 lark SDK 的对象——这样主循环可以脱离
飞书 SDK 单测，SDK 那层只负责把 P2ImMessageReceiveV1 转成这个 dict。

    {
      "message_id":      "om_xxx",
      "chat_id":         "oc_xxx",
      "chat_type":       "p2p" | "group",
      "sender_open_id":  "ou_xxx",
      "message_type":    "text",
      "text":            "正文",
      "mentions":        [{"id": {"open_id": "ou_bot"}}],
    }
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict

from kbplatform.feishu.identity import Identity
from kbplatform.feishu.kbcred import TokenStore
from kbplatform.feishu.opencode import OpenCodeError, session_env
from kbplatform.feishu.routing import (build_permissions, pick_model,
                                       restricted_dirs)

log = logging.getLogger("feishu")

DEDUP_MAX = 2000
WELCOME_TEXT = ("已为你开通平台账号（可见范围为空）。"
                "需要查知识库请联系管理员开通目录权限。")
INACTIVE_TEXT = "账号已停用，请联系管理员。"
NON_TEXT_TEXT = "暂只支持文本消息。"
PROGRESS_TEXT = "收到，正在处理…（任务较久，完成后回复你）"


# ---------------------------------------------------------------- 纯函数
def split_reply(text: str, chunk: int = 3000) -> list[str]:
    """长文本切段。优先在换行处断开，且不丢字符。"""
    text = text or ""
    if not text.strip():
        return []
    chunk = max(1, int(chunk))
    out: list[str] = []
    rest = text
    while len(rest) > chunk:
        window = rest[:chunk]
        cut = window.rfind("\n")
        if cut < chunk // 2:
            cut = chunk
        else:
            cut += 1
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


def should_reply(chat_type: str, mentions: list, bot_open_id: str) -> bool:
    """私聊一律回；群里必须 @ 机器人。"""
    if chat_type == "p2p":
        return True
    for m in mentions or []:
        mid = ((m or {}).get("id") or {}).get("open_id")
        if mid and bot_open_id and mid == bot_open_id:
            return True
    return False


def strip_mentions(text: str) -> str:
    """剥掉群消息里 @机器人 的占位符（形如 @_user_1）。"""
    if not text:
        return ""
    out = []
    for line in text.splitlines():
        parts = [p for p in line.split(" ") if not p.startswith("@_user_")]
        out.append(" ".join(parts))
    return "\n".join(out).strip()


def build_directive(restricted: list[str] | None = None,
                    workspace: str = "D:/kb-platform") -> str:
    """每条指令前面挂的使用说明。

    agent 需要知道：知识库怎么查、哪些目录不能直接读。
    不放 token —— token 在环境变量里，agent 自己读文件。
    """
    py = os.path.abspath(sys.executable)
    lines = [
        "【环境说明】",
        f"- 你在 {workspace} 工作区里，"
        "这是用户的 Windows 机器（PowerShell 环境）。",
        f"- **用绝对路径调 Python**：Windows 上 `python` 往往是个假别名，"
        f"执行它会拉起 Microsoft Store（退出码 1 且无输出）。"
        f"请用 `{py}`。",
        "- 查企业知识库用这个脚本（token 已在环境变量 KB_TOKEN_FILE 里，"
        "不要去读它、不要打印它）：",
        f'    "{py}" tools/kbq.py query "你的问题"',
        f'    "{py}" tools/kbq.py search "关键词" --top-k 10',
        "  脚本退出码：0 正常 / 3 token 失效 / 4 知识库服务不可达 / 5 超配额。",
        "  检索到的片段会带 [来源文件 位置] 引用，回答时请引用这些来源。",
    ]
    if restricted:
        lines.append("- 以下目录是涉密目录，**禁止直接读写**；"
                     "需要里面的内容只能通过知识库接口问：")
        lines.extend(f"    {p}" for p in restricted)
    lines.append("- 回答用中文，先给结论再给依据；办完事直接说结果。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 机器人
class FeishuBot:
    def __init__(self, conn, cfg, client, oc, tokens: TokenStore,
                 workspace: str | None = None):
        self.conn = conn
        self.cfg = cfg
        self.client = client
        self.oc = oc
        self.tokens = tokens
        self.identity = Identity(conn)
        self.workspace = workspace or cfg.workspace
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()
        self._chat_locks: dict[str, threading.Lock] = {}
        self._sem_local = threading.Semaphore(max(1, cfg.max_concurrent_local))
        self._sem_cloud = threading.Semaphore(max(1, cfg.max_concurrent_cloud))

    # ------------------------------------------------------------ 去重
    def _is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = None
            while len(self._seen) > DEDUP_MAX:
                self._seen.popitem(last=False)
            return False

    def _chat_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lk = self._chat_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._chat_locks[key] = lk
            return lk

    # ------------------------------------------------------------ 会话
    def _session_row(self, feishu_user_id: int, chat_id: str):
        return self.conn.execute(
            "SELECT id, session_id, model_id FROM feishu_sessions"
            " WHERE feishu_user_id=? AND chat_id=?", (feishu_user_id, chat_id)).fetchone()

    def _upsert_session(self, feishu_user_id: int, chat_id: str, chat_type: str,
                        session_id: str, model_id: str) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO feishu_sessions(feishu_user_id, chat_id, chat_type,"
            " session_id, model_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(feishu_user_id, chat_id) DO UPDATE SET"
            " session_id=excluded.session_id, model_id=excluded.model_id,"
            " chat_type=excluded.chat_type, updated_at=excluded.updated_at",
            (feishu_user_id, chat_id, chat_type, session_id, model_id, now, now))
        self.conn.commit()

    # ------------------------------------------------------------ 审计
    def _audit(self, user_id: int, question: str, model_id: str, restricted: bool) -> None:
        try:
            self.conn.execute(
                "INSERT INTO audit_log(user_id, token_prefix, action, question,"
                " hit_paths, model_used, restricted, ip, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, "feishu-bot", "feishu", question, "[]", model_id,
                 1 if restricted else 0, "feishu", time.time()))
            self.conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------ 主流程
    def handle(self, event: dict) -> None:
        message_id = event.get("message_id") or ""
        chat_id = event.get("chat_id") or ""
        chat_type = event.get("chat_type") or "p2p"
        open_id = event.get("sender_open_id") or ""
        text = event.get("text") or ""
        mentions = event.get("mentions") or []

        if self._is_duplicate(message_id):
            return
        if not should_reply(chat_type, mentions, self.client.bot_open_id()):
            return
        if (event.get("message_type") or "text") != "text":
            self._reply(chat_id, NON_TEXT_TEXT)
            return

        question = strip_mentions(text)
        if not question:
            return

        log.info("收到消息 chat=%s type=%s user=%s: %s",
                 chat_id, chat_type, open_id, question[:80])
        with self._chat_lock(f"{chat_id}:{open_id}"):
            self._process(chat_id, chat_type, open_id, question)

    def _process(self, chat_id: str, chat_type: str, open_id: str, question: str) -> None:
        # 群里未建号的人 -> 静默忽略（不在群里暴露机器人存在，也不给陌生人开号入口）
        if chat_type != "p2p" and self.identity.user_id_for(open_id) is None:
            return

        # 身份
        profile = self.client.get_user(open_id) or {}
        name = profile.get("name")
        dept = ",".join(profile.get("department_ids") or []) or None
        user_id, is_new = self.identity.get_or_create(open_id, name=name, dept=dept)
        if is_new:
            self._reply(chat_id, WELCOME_TEXT)

        if not self.identity.is_active(user_id):
            self._reply(chat_id, INACTIVE_TEXT)
            return

        # 模型
        cloud_ref = self.cfg.cloud_model_ref()
        local_ref = self.cfg.local_model_ref()
        model_ref, is_local = pick_model(self.conn, user_id, cloud_ref, local_ref)
        model_id = f"{model_ref['providerID']}/{model_ref['modelID']}"

        # 会话
        feishu_user = self.conn.execute(
            "SELECT id FROM feishu_users WHERE feishu_open_id=?", (open_id,)).fetchone()
        fuid = feishu_user[0]
        row = self._session_row(fuid, chat_id)
        restricted = restricted_dirs(self.conn)
        permissions = build_permissions(restricted, [".feishu/*"])

        if row and row["session_id"]:
            session_id = row["session_id"]
            if row["model_id"] and row["model_id"] != model_id:
                try:
                    self.oc.set_model(session_id, model_ref)
                except OpenCodeError:
                    pass
            try:
                self.oc.set_permissions(session_id, permissions)
            except OpenCodeError:
                pass
        else:
            session_id = self.oc.create_session(
                f"飞书会话 {chat_id}", model_ref, self.workspace, permissions)
        self._upsert_session(fuid, chat_id, chat_type, session_id, model_id)

        # 知识库凭证：token 落到 per-session 文件，agent 的 shell 自己去读
        token_path = self.tokens.write_session_token(session_id, user_id, self.workspace)
        base_env = dict(os.environ)
        try:
            self.oc.set_environment(
                session_id, session_env(base_env, {"KB_TOKEN_FILE": token_path,
                                                   "KB_API_BASE": self.cfg.kb_api_base}))
        except OpenCodeError:
            pass

        # 执行
        directive = build_directive(restricted, self.workspace)
        prompt_text = f"{directive}\n\n【用户指令】\n{question}"
        sem = self._sem_local if is_local else self._sem_cloud
        log.info("交给 agent：session=%s model=%s", session_id, model_id)
        with sem:
            try:
                result = self._run_with_notice(chat_id, session_id, prompt_text)
            except OpenCodeError as e:
                log.warning("agent 失败 session=%s: %s", session_id, e)
                self._reply(chat_id, f"处理失败：{e}")
                return
        log.info("agent 返回 %d 字", len(result or ""))

        if "KB_AUTH_EXPIRED" in result:
            # agent 侧的知识库 token 过期了 —— 重签后重试一次
            self.tokens.rotate(user_id)
            token_path = self.tokens.write_session_token(session_id, user_id, self.workspace)
            try:
                self.oc.set_environment(
                    session_id, session_env(base_env, {"KB_TOKEN_FILE": token_path,
                                                       "KB_API_BASE": self.cfg.kb_api_base}))
            except OpenCodeError:
                pass
            with sem:
                try:
                    result = self._run_with_notice(chat_id, session_id, prompt_text)
                except OpenCodeError as e:
                    self._reply(chat_id, f"处理失败：{e}")
                    return

        self._audit(user_id, question, model_id, is_local)
        for part in split_reply(result, self.cfg.reply_chunk):
            self._reply(chat_id, part)

    def _run(self, session_id: str, question: str) -> str:
        marker = self.oc.prompt(session_id, question) or {}
        return self.oc.wait_for_result(session_id, since=marker.get("since", 0),
                                       timeout_sec=self.cfg.agent_timeout_sec)

    def _run_with_notice(self, chat_id: str, session_id: str, question: str) -> str:
        """跑 agent；跑得久就先回一句「处理中」，别让人干等。"""
        delay = max(0.0, float(self.cfg.progress_notice_sec or 0))
        timer = None
        if delay > 0:
            timer = threading.Timer(delay, self._reply, args=(chat_id, PROGRESS_TEXT))
            timer.daemon = True
            timer.start()
        try:
            return self._run(session_id, question)
        finally:
            if timer is not None:
                timer.cancel()
    def _reply(self, chat_id: str, text: str) -> None:
        if not text:
            return
        try:
            self.client.send_text(chat_id, text)
        except Exception:
            pass
