"""飞书机器人配置装载。

凭据来源优先级：**环境变量 > secrets.toml**。
非敏感配置在 config.toml 的 `[feishu]` 段。

安全约束：
  * `FEISHU_APP_SECRET` 绝不能进 agent 的 session 环境变量（见 opencode.session_env 白名单）
  * `secrets.toml` 必须进 .gitignore
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = "D:/kb-platform/config.toml"
DEFAULT_SECRETS_NAME = "secrets.toml"

# 允许透传进 agent session 的环境变量白名单。
# 任何不在表里的变量（含 FEISHU_APP_SECRET / KB_SESSION_SECRET）都不会给到 agent。
SESSION_ENV_ALLOWLIST = (
    "PATH", "SystemRoot", "windir", "SystemDrive", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "USERNAME", "USERDOMAIN",
    "HOME", "HOMEDRIVE", "HOMEPATH", "LANG",
)


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    workspace: str = "D:/kb-platform"
    opencode_port: int = 4599
    opencode_cli: str = ""
    cloud_model: str = "opencode/minimax-m3.1-flash-free"
    local_model: str = "ollama/qwen3.5-9b-64k:latest"
    kb_api_base: str = "http://127.0.0.1:8000"
    max_concurrent_cloud: int = 3
    max_concurrent_local: int = 1
    reply_chunk: int = 3000
    agent_timeout_sec: int = 600
    progress_notice_sec: int = 20
    config_path: str = DEFAULT_CONFIG_PATH
    secrets_path: str = ""

    def cloud_model_ref(self) -> dict:
        return parse_model_ref(self.cloud_model)

    def local_model_ref(self) -> dict:
        return parse_model_ref(self.local_model)


def parse_model_ref(ref: str) -> dict:
    """`"provider/model"` -> opencode 的 Model.Ref。

    opencode 的 Model.Ref 需要 {id, providerID, modelID}，id 与 modelID 用
    model 部分（实测：id 写完整 "provider/model" 会被拼成
    "provider/provider/model" 而找不到路由）。
    """
    ref = (ref or "").strip()
    if "/" in ref:
        provider, model = ref.split("/", 1)
    else:
        provider, model = "opencode", ref
    return {"id": model, "providerID": provider, "modelID": model}


def _read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_feishu_config(config_path: str = DEFAULT_CONFIG_PATH,
                       secrets_path: str | None = None) -> FeishuConfig | None:
    """装载飞书机器人配置。返回 None 表示「没启用」或「缺凭据」，调用方应当退出。"""
    if secrets_path is None:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                                    DEFAULT_SECRETS_NAME)
    raw = _read_toml(config_path).get("feishu", {})
    if not raw.get("enabled", False):
        return None

    sec = _read_toml(secrets_path).get("feishu", {})
    app_id = os.environ.get("FEISHU_APP_ID") or sec.get("app_id") or ""
    app_secret = os.environ.get("FEISHU_APP_SECRET") or sec.get("app_secret") or ""
    if not app_id or not app_secret:
        return None

    return FeishuConfig(
        app_id=app_id,
        app_secret=app_secret,
        workspace=str(raw.get("workspace", "D:/kb-platform")),
        opencode_port=int(raw.get("opencode_port", 4599)),
        opencode_cli=str(raw.get("opencode_cli", "")),
        cloud_model=str(raw.get("cloud_model", "opencode/minimax-m3.1-flash-free")),
        local_model=str(raw.get("local_model", "ollama/qwen3.5-9b-64k:latest")),
        kb_api_base=str(raw.get("kb_api_base", "http://127.0.0.1:8000")),
        max_concurrent_cloud=int(raw.get("max_concurrent_cloud", 3)),
        max_concurrent_local=int(raw.get("max_concurrent_local", 1)),
        reply_chunk=int(raw.get("reply_chunk", 3000)),
        agent_timeout_sec=int(raw.get("agent_timeout_sec", 600)),
        progress_notice_sec=int(raw.get("progress_notice_sec", 20)),
        config_path=config_path,
        secrets_path=secrets_path,
    )
