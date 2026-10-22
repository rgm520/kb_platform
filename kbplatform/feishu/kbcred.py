"""知识库凭证：给 agent 用的 API token。

设计见 `docs/superpowers/specs/2026-10-21-feishu-bot-design.md` §5.3。

要点：
  * **每用户一枚** token（不是每 session 一枚）——api_tokens 表不膨胀，
    平台侧按 token id 计的日配额就等于按人计。
  * 库里只存哈希（沿用平台既有约定），明文只存在于 bot 进程内存 +
    per-session 的 token 文件。
  * token 文件放在 `<workspace>/.feishu/tokens/`，并用 session 权限规则
    禁止 agent 用 read/edit 读它（见 routing.build_permissions）。
"""
from __future__ import annotations

import os
import sqlite3

from kbplatform.auth import issue_token, revoke_token

TOKEN_TTL_DAYS = 90
TOKEN_LABEL = "feishu-bot"
TOKEN_DIR = os.path.join(".feishu", "tokens")


class TokenStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._tokens: dict[int, str] = {}
        self._token_ids: dict[int, int] = {}

    def token_for(self, user_id: int) -> str:
        """取该用户的 token；没有就签发一枚。"""
        cached = self._tokens.get(user_id)
        if cached:
            return cached
        raw, tid = issue_token(self.conn, user_id, TOKEN_LABEL, ttl_days=TOKEN_TTL_DAYS)
        self._tokens[user_id] = raw
        self._token_ids[user_id] = tid
        return raw

    def rotate(self, user_id: int) -> str:
        """吊销旧的并重新签发（token 过期或 bot 重启后调用）。"""
        old_id = self._token_ids.get(user_id)
        if old_id is not None:
            try:
                revoke_token(self.conn, old_id)
            except Exception:
                pass
        self._tokens.pop(user_id, None)
        self._token_ids.pop(user_id, None)
        return self.token_for(user_id)

    def write_session_token(self, session_id: str, user_id: int, root: str) -> str:
        """把 token 写到 per-session 文件，返回文件路径。"""
        directory = os.path.join(root, TOKEN_DIR)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{session_id}.token")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.token_for(user_id))
        return path

    def prune_revoked(self) -> int:
        """删掉已吊销的 bot token，避免 bot 反复轮换把表撑大。

        只动 label='feishu-bot' 且 revoked=1 的行，用户自己签的 token 不动。
        """
        cur = self.conn.execute(
            "DELETE FROM api_tokens WHERE label=? AND revoked=1", (TOKEN_LABEL,))
        self.conn.commit()
        return cur.rowcount
