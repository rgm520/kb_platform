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
    data_dir: str = "D:/opencode/kb/data"
    db_path: str = ""
    fts_path: str = ""
    vector_dir: str = ""
    drive_map: dict[str, str] = field(default_factory=dict)
    llm_model: str = "qwen3.5-9b-64k"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024

    def __post_init__(self):
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "kb.db")
        if not self.fts_path:
            self.fts_path = os.path.join(self.data_dir, "fts.db")
        if not self.vector_dir:
            self.vector_dir = os.path.join(self.data_dir, "vectors")


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    mon = raw.get("monitor", {})
    paths = raw.get("paths", {})
    runtime = raw.get("runtime", {})
    models = raw.get("models", {})
    drives = {k.upper(): v for k, v in raw.get("drives", {}).items()}
    return Config(
        monitor_roots=list(mon.get("roots", [])),
        poll_interval_sec=int(mon.get("poll_interval_sec", 60)),
        scan_qps=int(mon.get("scan_qps", 20)),
        max_workers=int(runtime.get("max_workers", 1)),
        data_dir=paths.get("data_dir", "D:/opencode/kb/data"),
        drive_map=drives,
        llm_model=str(models.get("llm", "qwen3.5-9b-64k")),
        embed_model=str(models.get("embed", "bge-m3")),
        embed_dim=int(models.get("embed_dim", 1024)),
    )
