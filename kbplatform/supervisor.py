"""监督进程：把 Web 服务与摄入流水线拉起来，并在它们挂掉时自动重启。

为什么需要它（实测教训）：
  用 `Start-Process` 起的服务，其父 shell 一被回收（会话结束、终端关闭、
  命令超时被杀），子进程会被连带终止，结果就是"服务静默消失"——
  日志里没有任何异常，只是进程没了。
  飞书机器人的 runner 用同样的方式启动却活了下来（它内部有守护逻辑），
  平台没有，于是掉了。

用法：
    python -m kbplatform.supervisor D:/opencode/config.toml

进程模型：
    supervisor  (长驻，由计划任务拉起)
      ├── uvicorn kbplatform.service:app   (Web 服务)
      └── kbplatform.pipeline              (摄入流水线)

任一子进程退出或 Web 服务健康检查连续失败 -> 立即重启该子进程。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from kbplatform.config import load_config

HEALTH_URL = "http://127.0.0.1:8000/health"
HEALTH_TIMEOUT = 8
HEALTH_FAIL_THRESHOLD = 3
RESTART_BACKOFF = 5


class Child:
    def __init__(self, name: str, argv: list[str], cwd: str, log_dir: str,
                 health: str | None = None):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.log_dir = log_dir
        self.health = health
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = 0
        self.fails = 0
        self.last_error = ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def spawn(self) -> None:
        os.makedirs(self.log_dir, exist_ok=True)
        out = open(os.path.join(self.log_dir, f"{self.name}.out.log"), "a",
                   encoding="utf-8", buffering=1)
        err = open(os.path.join(self.log_dir, f"{self.name}.err.log"), "a",
                   encoding="utf-8", buffering=1)
        self.proc = subprocess.Popen(
            self.argv, cwd=self.cwd, stdout=out, stderr=err,
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 "PYTHONUNBUFFERED": "1"})
        self.started_at = time.time()
        self.fails = 0
        print(f"[supervisor] {self.name} 已启动 PID={self.proc.pid}", flush=True)

    def stop(self) -> None:
        if self.alive():
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def check_health(self) -> bool:
        if not self.health:
            return True
        try:
            with urllib.request.urlopen(self.health, timeout=HEALTH_TIMEOUT) as r:
                return r.status == 200
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def ensure(self) -> None:
        if not self.alive():
            code = self.proc.returncode if self.proc else "(未运行)"
            print(f"[supervisor] {self.name} 已退出 (code={code})，正在重启...", flush=True)
            self.stop()
            time.sleep(RESTART_BACKOFF)
            self.restarts += 1
            self.spawn()
            return
        if self.health:
            if self.check_health():
                self.fails = 0
            else:
                self.fails += 1
                print(f"[supervisor] {self.name} 健康检查失败 "
                      f"({self.fails}/{HEALTH_FAIL_THRESHOLD}) {self.last_error}", flush=True)
                if self.fails >= HEALTH_FAIL_THRESHOLD:
                    print(f"[supervisor] {self.name} 连续失败，重启", flush=True)
                    self.stop()
                    time.sleep(RESTART_BACKOFF)
                    self.restarts += 1
                    self.spawn()


def write_heartbeat(path: str, children: list[Child]) -> None:
    data = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "children": [{"name": c.name, "alive": c.alive(),
                      "pid": c.proc.pid if c.proc else None,
                      "uptime_sec": round(time.time() - c.started_at) if c.alive() else 0,
                      "restarts": c.restarts, "last_error": c.last_error}
                     for c in children],
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def acquire_single_instance_lock(lock_path: str):
    """独占文件锁，保证同一时刻只有一个监督进程。

    没有这个锁的话：看门狗重启平台时可能与旧实例并存，两个监督进程
    各自拉起 web/ingest，后者因端口被占用而反复崩溃重启。
    用 msvcrt 的文件锁：进程一死锁自动释放，不留僵尸锁。
    """
    import msvcrt
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fh = open(lock_path, "a+b")
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"0")
        fh.flush()
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode())
    fh.flush()
    return fh


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "D:/opencode/config.toml"
    cfg = load_config(cfg_path)
    root = os.path.dirname(os.path.abspath(cfg_path))
    log_dir = os.path.join(root, "kb", "logs")
    os.makedirs(log_dir, exist_ok=True)

    lock = acquire_single_instance_lock(os.path.join(log_dir, "supervisor.lock"))
    if lock is None:
        print("[supervisor] 已有另一个监督进程在运行，退出。", flush=True)
        return 0

    py = sys.executable
    # 计划任务不继承交互会话的环境变量，会话密钥统一从配置读，
    # 否则服务每次重启会话都会失效。
    if cfg.session_secret:
        os.environ["KB_SESSION_SECRET"] = cfg.session_secret
    children = [
        Child("web", [py, "-m", "uvicorn", "kbplatform.service:app",
                      "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"],
              root, log_dir, health=HEALTH_URL),
        Child("ingest", [py, "-m", "kbplatform.pipeline", cfg_path], root, log_dir),
    ]

    stop = {"flag": False}

    def on_signal(signum, frame):
        stop["flag"] = True
        print(f"[supervisor] 收到信号 {signum}，正在停止子进程...", flush=True)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    for c in children:
        c.spawn()

    hb = os.path.join(log_dir, "supervisor-heartbeat.json")
    print(f"[supervisor] 监督进程就绪 (PID={os.getpid()})，心跳写入 {hb}", flush=True)

    while not stop["flag"]:
        for c in children:
            try:
                c.ensure()
            except Exception as e:
                print(f"[supervisor] 检查 {c.name} 时异常: {type(e).__name__}: {e}", flush=True)
        write_heartbeat(hb, children)
        time.sleep(10)

    for c in children:
        c.stop()
    print("[supervisor] 已全部停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
