"""配置加载。结构见 config.toml。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class Config:
    monitor_roots: list[str] = field(default_factory=list)
    poll_interval_sec: int = 60
    scan_qps: int = 20
    max_workers: int = 1
    data_dir: str = "D:/kb-platform/kb/data"
    db_path: str = ""
    fts_path: str = ""
    vector_dir: str = ""
    drive_map: dict[str, str] = field(default_factory=dict)
    share_names: list[str] = field(default_factory=list)
    llm_model: str = "qwen3.5-9b-64k"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024
    session_secret: str = ""
    public_base_url: str = ""
    # 密钥是否保存可恢复密文（默认 true）。
    # 开启后管理员能在管理台重新查看完整密钥，密钥弄丢不用换密钥，
    # 正在使用旧密钥的智能体不会被打断。详见 kbplatform/auth.py。
    token_recoverable: bool = True
    # 2.0 文件底座
    trash_days: int = 30
    upload_max_mb: int = 2048
    preview_dir: str = ""
    source_scan_interval_sec: int = 300

    def __post_init__(self):
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "kb.db")
        if not self.fts_path:
            self.fts_path = os.path.join(self.data_dir, "fts.db")
        if not self.vector_dir:
            self.vector_dir = os.path.join(self.data_dir, "vectors")
        if not self.preview_dir:
            self.preview_dir = os.path.join(self.data_dir, "preview")


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    mon = raw.get("monitor", {})
    paths = raw.get("paths", {})
    runtime = raw.get("runtime", {})
    models = raw.get("models", {})
    security = raw.get("security", {})
    shares = raw.get("shares", {})
    storage = raw.get("storage", {})
    drives = {k.upper(): v for k, v in raw.get("drives", {}).items()}
    return Config(
        monitor_roots=list(mon.get("roots", [])),
        poll_interval_sec=int(mon.get("poll_interval_sec", 60)),
        scan_qps=int(mon.get("scan_qps", 20)),
        max_workers=int(runtime.get("max_workers", 1)),
        data_dir=paths.get("data_dir", "D:/kb-platform/kb/data"),
        drive_map=drives,
        share_names=list(shares.get("names", [])),
        llm_model=str(models.get("llm", "qwen3.5-9b-64k")),
        embed_model=str(models.get("embed", "bge-m3")),
        embed_dim=int(models.get("embed_dim", 1024)),
        # 会话签名密钥：配置优先，其次环境变量。写死是为了让服务重启后会话不失效。
        session_secret=str(security.get("session_secret")
                           or os.environ.get("KB_SESSION_SECRET", "")),
        # 对外地址：给 API 手册与其它机器用。本机请求会是 127.0.0.1，
        # 但手册要发给局域网里的 AI 用，所以单独配。
        public_base_url=str(security.get("public_base_url", "")
                            or os.environ.get("KB_PUBLIC_BASE_URL", "")),
        # 密钥可恢复密文：默认开启，可在 config.toml 用 token_recoverable=false 关掉。
        token_recoverable=bool(security.get("token_recoverable", True)),
        trash_days=int(storage.get("trash_days", 30)),
        upload_max_mb=int(storage.get("upload_max_mb", 2048)),
        preview_dir=str(storage.get("preview_dir", "")),
        source_scan_interval_sec=int(storage.get("source_scan_interval_sec", 300)),
    )
