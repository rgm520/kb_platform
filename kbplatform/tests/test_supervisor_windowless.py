"""监督进程"无窗口后台运行"的回归测试。

用户诉求：服务每次启动都会弹一个黑色命令框，要彻底关掉、放到后台跑。
实现约定：
  * 监督进程用 pythonw.exe 启动（没有控制台），stdout/stderr 自己写进日志文件；
  * 子进程（web / ingest）带 CREATE_NO_WINDOW 标志，任何情况下都不再弹窗。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kbplatform import supervisor  # noqa: E402


def test_setup_log_files_redirects_stdout_and_stderr(tmp_path):
    """setup_log_files 之后 print 的内容必须落到日志文件里（pythonw 下 stdout 为 None）。"""
    old_out, old_err = sys.stdout, sys.stderr
    try:
        supervisor.setup_log_files(str(tmp_path))
        print("hello-supervisor")
        sys.stdout.flush()
        sys.stderr.write("boom\n")
        sys.stderr.flush()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    out = (tmp_path / "supervisor.out.log").read_text(encoding="utf-8")
    err = (tmp_path / "supervisor.err.log").read_text(encoding="utf-8")
    assert "hello-supervisor" in out
    assert "boom" in err


def test_child_spawn_uses_create_no_window(tmp_path, monkeypatch):
    """子进程必须带 CREATE_NO_WINDOW，避免 web/ingest 弹出黑色控制台。"""
    captured: dict = {}

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    child = supervisor.Child("web", [sys.executable, "-c", "pass"],
                             str(tmp_path), str(tmp_path))
    child.spawn()
    assert captured.get("creationflags") == subprocess.CREATE_NO_WINDOW


def test_python_exe_prefers_console_python_over_pythonw(monkeypatch):
    """监督进程自己跑在 pythonw 下时，子进程仍要用 python.exe（uvicorn 需要真控制台语义）。"""
    fake = Path("C:/Python312/pythonw.exe")
    monkeypatch.setattr(supervisor.sys, "executable", str(fake))
    monkeypatch.setattr(supervisor.os.path, "exists", lambda p: str(p).endswith("python.exe"))
    assert supervisor.python_exe().lower().endswith("python.exe")
