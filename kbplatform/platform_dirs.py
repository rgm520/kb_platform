"""平台内部目录名单 —— **唯一来源**。

`_kb` 是 1.0 引擎的 wiki 导出产物目录，`_kb_trash` 是 2.0 平台回收站目录。
它们是平台自己的产物，不属于用户文件：扫描器要把它们跳过（fsindex）、
文件页要把它们隐藏（fs_service）、监控快照不预热范围外的旧账（pipeline）。

任何需要这份名单的地方都从本模块导入，不要另写一份字面量——
否则两处名单迟早改得不一样（fsindex 漏 `_kb` 就是这么来的）。
"""
from __future__ import annotations

from kbplatform.storage import normalize_rel

PLATFORM_DIR_NAMES: set[str] = {"_kb", "_kb_trash"}


def is_platform_rel(rel: str) -> bool:
    """相对路径的第一段是否为平台内部目录。

    `AI知识库\\_kb\\...` 的第一段是 `AI知识库`，是用户目录，返回 False；
    平台目录藏在更深层的场景用「任一段命中」的判断（见 tools 清理脚本）。
    """
    first = normalize_rel(rel).split("\\", 1)[0]
    return first in PLATFORM_DIR_NAMES
