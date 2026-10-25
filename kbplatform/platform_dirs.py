"""平台内部目录与通用忽略目录名单 —— **唯一来源**。

两个集合，语义分开：

* ``PLATFORM_DIR_NAMES`` —— 平台**自己产物**的目录：`_kb` 是 1.0 引擎的
  wiki 导出目录，`_kb_trash` 是 2.0 的平台回收站目录。它们不是用户文件：
  扫描器要跳过（fsindex / catalog）、文件页要隐藏（fs_service）、
  监控快照不预热范围外的旧账（pipeline）、授权选择器不提供（nas）。
* ``IGNORED_DIR_NAMES`` —— 各 NAS / Windows 通用的系统与回收目录
  （`#recycle`、`@eaDir` 等），跟"平台"无关，属于一视同仁的忽略项。
  另有开发者环境的垃圾目录（`.git` / `__pycache__` / `node_modules`），
  只有 1.0 扫描根目录需要跳过，与"平台目录"不是同一类，留在
  `catalog.py` 本地、不进本模块。

任何需要这些名单的地方都从本模块导入，**不要再各自写字面量**——
否则两处名单迟早改得不一样（1.0 catalog 与 2.0 fsindex 一人一份字面量、
fsindex 漏了 `_kb`，就是这么来的；catalog 还漏了 `_kb_trash`）。

保留名取舍：`_kb` / `_kb_trash` 视为平台**保留名**，`is_platform_rel`
按**任意路径段**命中（`AI知识库\\_kb\\wiki\\…` 这样嵌在用户目录下的真实
产物路径也隐藏，与扫描器、清理脚本的粒度一致）。用户真建了个叫
`_kb` 的普通目录时也会被跳过 / 隐藏——这个名字本就会与平台产物冲突，
按保留名处理是可接受的取舍。
"""
from __future__ import annotations

from kbplatform.paths import norm_unc

PLATFORM_DIR_NAMES: set[str] = {"_kb", "_kb_trash"}

IGNORED_DIR_NAMES: set[str] = {"#recycle", "#snapshot", "@eaDir",
                               "$RECYCLE.BIN", "System Volume Information"}


def is_platform_rel(rel: str) -> bool:
    """路径中**任一段**等于平台内部目录名 → True（忽略大小写与分隔符）。

    为什么按段相等而不用子串匹配：`文档_kb\\` 这类用户目录不能误伤。
    """
    names = {n.lower() for n in PLATFORM_DIR_NAMES}
    return any(seg.lower() in names for seg in norm_unc(rel).split("\\"))
