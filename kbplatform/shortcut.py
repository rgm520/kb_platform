"""在 NAS 上生成 Windows 快捷方式（.lnk），指向被保留的那份文件。

用于「删除重复文件」：重复的那份被移进回收站后，在**原位置**留一个
指向保留副本的快捷方式，别人按老路径打开依然能打开（看到的是保留的那份）。

实现：用 WScript.Shell COM 写 .lnk（Windows 自己生成的格式，兼容性最好）。
"""
from __future__ import annotations

import os
import subprocess
import tempfile

_PS = r'''
param([string]$Link, [string]$Target)
$ErrorActionPreference = "Stop"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($Link)
$s.TargetPath = $Target
$s.Save()
'''


def create_shortcut(link_path: str, target_path: str) -> tuple[bool, str]:
    """在 link_path 处写一个指向 target_path 的 .lnk。返回 (是否成功, 说明)。

    注意：Windows 要求快捷方式文件名以 **.lnk** 结尾，所以调用方要传
    "<原文件名>.lnk"（资源管理器里显示的仍然是原名）。
    """
    if not link_path or not target_path:
        return False, "路径为空"
    if not link_path.lower().endswith((".lnk", ".url")):
        link_path = link_path + ".lnk"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as f:
            f.write(_PS)
            script = f.name
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 "-Link", link_path, "-Target", target_path],
                capture_output=True, text=True, timeout=90)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or "PowerShell 失败").strip()[:200]
            if not os.path.exists(link_path):
                return False, "快捷方式未生成"
            return True, f"已创建快捷方式 {os.path.basename(link_path)}"
        finally:
            try:
                os.unlink(script)
            except OSError:
                pass
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
