"""目录扫描器：只负责「目录里有什么」，不做解析、不做 LLM 调用。

约束（spec Global Constraint #10）：可解析白名单固定，**kb/parsers.py 的
PARSERS 字典不是白名单**（它还含 .dwg/.js/.java 等），只是解析器能力表。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from kbplatform.paths import to_unc, split_share

PARSEABLE_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx",
                  ".doc", ".xls", ".txt", ".md", ".ofd"}

# 平台自身的产物目录不能进 catalog（否则会自我吞噬）
SKIP_DIR_NAMES = {
    "#recycle", "#snapshot", "@eaDir", "$RECYCLE.BIN", "System Volume Information",
    "_kb", ".git", "__pycache__", "node_modules",
}


@dataclass
class FileRecord:
    path: str
    share: str
    rel_path: str
    name: str
    ext: str
    size: int
    mtime: float


def scan_root(root_unc: str, drive_map: dict[str, str], roots: list[str],
              qps: int = 20) -> tuple[list[FileRecord], dict[str, int]]:
    """遍历一个监控根。

    返回 (可解析文件记录列表, 非文档文件的 {扩展名: 数量})。
    """
    recs: list[FileRecord] = []
    stats: dict[str, int] = {}
    interval = (1.0 / qps) if qps and qps > 0 else 0.0
    root = to_unc(root_unc, drive_map)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                st = os.stat(full)
            except OSError:
                continue
            if interval:
                time.sleep(interval)
            if ext not in PARSEABLE_EXT:
                key = ext or "(none)"
                stats[key] = stats.get(key, 0) + 1
                continue
            unc = to_unc(full, drive_map)
            share, rel = split_share(unc, roots)
            recs.append(FileRecord(unc, share, rel, fn, ext, st.st_size, st.st_mtime))
    return recs, stats
