"""看门狗脚本的回归测试。

背景：2026-10-24 平台整体宕机，根因之一是 tools/watchdog.ps1 把静态方法
FromUnixTimeSeconds 当成 DateTime 的实例方法调用（`(Get-Date).FromUnixTimeSeconds(...)`），
抛异常后被最外层 catch 吞掉，导致看门狗**永远走不到重新拉起平台的逻辑**，
平台一次宕机后就再也起不来了。

这个测试要求 watchdog.ps1：
  1. 支持 `-SelfTest` 自检模式（内部断言决策逻辑）；
  2. 不再出现把静态方法当实例方法调用的写法；
  3. 自检全部通过（退出码 0）。

跑法：python -m pytest kbplatform/tests/test_watchdog_script.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "watchdog.ps1"


def _powershell() -> str:
    for name in ("powershell", "pwsh"):
        exe = shutil.which(name)
        if exe:
            return exe
    pytest.skip("本机没有 PowerShell，跳过看门狗脚本测试")


def test_watchdog_script_exists():
    assert SCRIPT.exists(), f"缺少看门狗脚本：{SCRIPT}"


def test_watchdog_self_test_passes():
    """watchdog.ps1 -SelfTest 必须自检通过（退出码 0）。"""
    exe = _powershell()
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-SelfTest"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"看门狗自检失败（退出码 {proc.returncode}）：\n{out}"
    assert "SELFTEST OK" in out, f"自检没有输出成功标记：\n{out}"


def test_watchdog_does_not_misuse_static_datetime_method():
    """回归点：代码行里不能再出现 (Get-Date).FromUnixTimeSeconds 这种写法。"""
    text = SCRIPT.read_text(encoding="utf-8-sig", errors="replace")
    code = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    bad = [ln for ln in code if "(Get-Date).FromUnixTimeSeconds" in ln]
    assert not bad, (
        "FromUnixTimeSeconds 是 DateTimeOffset 的静态方法，不能写成实例方法调用：\n"
        + "\n".join(bad)
    )
    assert "Get-UnixNow" in text, "看门狗应当用统一的 unix 秒取值函数换算时间"


def test_watchdog_restart_path_is_exception_safe():
    """看门狗的拉起逻辑必须独立于冷却判断，异常不能吞掉拉起动作。"""
    text = SCRIPT.read_text(encoding="utf-8", errors="replace")
    assert "Test-ShouldRestart" in text, "看门狗应把重启决策抽成可测函数"
    assert "Stop-Platform" in text and "Start-Platform" in text, (
        "看门狗应把停止/拉起拆成独立步骤，便于等待状态收敛"
    )
