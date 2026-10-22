"""NAS 目录枚举：给授权选择器提供**真实存在**的目录，而不是只有已索引的。

只做一层（共享下的顶层目录），因为：
  * 一次 listdir 很快，不给管理台加负担
  * 实际会用到的涉密目录（党建各支部、审计资料汇集、薪酬与福利）都在顶层
需要更深时，前端可再调 expand()。
"""
from __future__ import annotations

import os
import time

from kbplatform.paths import norm_unc

# 系统目录，不该出现在授权选择器里
SKIP_DIRS = {"#recycle", "#snapshot", "@eaDir", "$RECYCLE.BIN",
             "System Volume Information", "_kb"}


def share_roots(drive_map: dict[str, str], order: list[str] | None = None) -> list[tuple[str, str]]:
    """从盘符映射推出 (共享名, UNC 根)。"""
    out = []
    for _drive, unc in (drive_map or {}).items():
        name = norm_unc(unc).split("\\")[-1]
        if name:
            out.append((name, unc))
    seen = set()
    uniq = []
    for n, u in out:
        if n not in seen:
            seen.add(n)
            uniq.append((n, u))
    if order:
        rank = {n: i for i, n in enumerate(order)}
        uniq.sort(key=lambda x: rank.get(x[0], 999))
    return uniq


def list_subdirs(unc_root: str, limit: int = 400, timeout: float = 8.0) -> list[str]:
    """列出共享根下的顶层子目录名。失败或超时返回空列表（不抛异常）。"""
    t0 = time.time()
    try:
        with os.scandir(unc_root) as it:
            out = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS:
                        out.append(e.name)
                except OSError:
                    continue
                if len(out) >= limit or time.time() - t0 > timeout:
                    break
            return sorted(out)
    except Exception:
        return []


def expand_subdir(unc_root: str, subpath: str, limit: int = 400,
                  timeout: float = 8.0) -> list[str]:
    """列出某子目录下的下一层子目录。"""
    t0 = time.time()
    target = os.path.join(unc_root, subpath) if subpath else unc_root
    try:
        with os.scandir(target) as it:
            out = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        out.append(e.name)
                except OSError:
                    continue
                if len(out) >= limit or time.time() - t0 > timeout:
                    break
            return sorted(out)
    except Exception:
        return []
