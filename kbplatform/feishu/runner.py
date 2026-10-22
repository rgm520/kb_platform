"""进程入口：拉起并守护 opencode serve，接上飞书长连接。

设计见 `docs/superpowers/specs/2026-10-21-feishu-bot-design.md` §5.10 / §6.3。

bot 是 supervisor：自己 spawn 一个独立的 `opencode serve`，
从 stdout 解析出 url 和密码（密码就打印在 stdout 上），再守护它。
这样 Windows 任务计划只需要登记一条（跑 bot），不用再管 opencode。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request

from kbplatform.feishu.bot import FeishuBot
from kbplatform.feishu.client import FeishuClient
from kbplatform.feishu.config import FeishuConfig, load_feishu_config
from kbplatform.feishu.identity import find_opencode_cli
from kbplatform.feishu.kbcred import TokenStore
from kbplatform.feishu.opencode import OpenCodeClient

log = logging.getLogger("feishu")

_LISTEN_RE = re.compile(r"server listening on (\S+)")
_PASSWORD_RE = re.compile(r"server password (\S+)")


# ---------------------------------------------------------------- 输出解析
def parse_server_output(text: str) -> tuple[str, str]:
    """从 opencode serve 的 stdout 里解析 (url, password)。"""
    url = _LISTEN_RE.search(text or "")
    pw = _PASSWORD_RE.search(text or "")
    if not url or not pw:
        raise ValueError(f"无法从 serve 输出里解析 url/password: {(text or '')[:200]!r}")
    return url.group(1), pw.group(1)


# ---------------------------------------------------------------- 事件归一化
def normalize_event(data) -> dict:
    """把 lark 的 P2ImMessageReceiveV1 转成 bot 认识的 dict。"""
    ev = getattr(data, "event", None)
    msg = getattr(ev, "message", None)
    sender = getattr(ev, "sender", None)
    sender_id = getattr(sender, "sender_id", None)

    text = ""
    content = getattr(msg, "content", "") or ""
    if content:
        try:
            text = (json.loads(content) or {}).get("text", "") or ""
        except (json.JSONDecodeError, AttributeError):
            text = ""

    mentions = []
    for m in (getattr(msg, "mentions", None) or []):
        mid = getattr(m, "id", None)
        mentions.append({"id": {"open_id": getattr(mid, "open_id", "") or ""},
                         "key": getattr(m, "key", "") or "",
                         "name": getattr(m, "name", "") or ""})

    return {
        "message_id": getattr(msg, "message_id", "") or "",
        "chat_id": getattr(msg, "chat_id", "") or "",
        "chat_type": getattr(msg, "chat_type", "") or "p2p",
        "message_type": getattr(msg, "message_type", "") or "text",
        "text": text,
        "mentions": mentions,
        "sender_open_id": getattr(sender_id, "open_id", "") or "",
    }


# ---------------------------------------------------------------- opencode 服务
class OpenCodeServer:
    """opencode serve 子进程的 supervisor。"""

    def __init__(self, cli_path: str, port: int, workdir: str):
        self.cli_path = cli_path
        self.port = port
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.base_url = ""
        self.password = ""

    def start(self) -> None:
        if not self.cli_path or not os.path.isfile(self.cli_path):
            raise FileNotFoundError(f"找不到 opencode-cli.exe: {self.cli_path}")
        cmd = [self.cli_path, "serve", "--port", str(self.port),
               "--hostname", "127.0.0.1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     cwd=self.workdir)
        self._write_pid()
        buf = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    # serve 起不来时真正的错误在输出里（端口占用、参数错等），
                    # 直接抛「启动即退出」等于什么都没说
                    detail = (buf or "").strip()[-500:]
                    raise RuntimeError(f"opencode serve 启动即退出：{detail}")
                continue
            buf += line
            try:
                self.base_url, self.password = parse_server_output(buf)
                log.info("opencode serve 就绪: %s", self.base_url)
                return
            except ValueError:
                continue
        self.stop()
        raise TimeoutError("opencode serve 启动超时")

    def _write_pid(self) -> None:
        """记下 serve 的 PID：机器人被强杀时靠它清理孤儿。"""
        if self.proc is None:
            return
        try:
            directory = os.path.join(self.workdir, ".feishu")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "opencode.pid"), "w",
                      encoding="utf-8") as f:
                f.write(str(self.proc.pid))
        except OSError:
            pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def write_info_file(self) -> str:
        """把 url / 密码写到 `<workdir>/.feishu/opencode.json`。

        测试和排障要能连上机器人自己起的这个 serve —— 密码只出现在 stdout 上，
        不落盘就没法复用。
        """
        directory = os.path.join(self.workdir, ".feishu")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "opencode.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"base_url": self.base_url, "password": self.password,
                       "port": self.port}, f, ensure_ascii=False)
        return path

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            self.proc = None


# ---------------------------------------------------------------- 孤儿清理
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            parts = [p.strip('" ') for p in line.split('","')]
            if len(parts) > 1 and parts[1] == str(pid):
                return parts[0]
    except Exception:
        pass
    return ""


def kill_stale_server(root: str) -> int | None:
    """清掉上次残留的 opencode serve 进程。

    机器人被强杀（任务重启、手动 stop）时，它 spawn 的 serve 子进程不会跟着死，
    会一直占着端口 —— 下次启动 bind 不到，30 秒后超时退出。
    只杀我们自己记录过、且确认是 opencode-cli 的 PID，不误伤别的进程。
    """
    path = os.path.join(root, ".feishu", "opencode.pid")
    try:
        with open(path, "r", encoding="utf-8") as f:
            pid = int((f.read() or "").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    if "opencode" not in (_process_name(pid) or "").lower():
        return None
    try:
        os.kill(pid, 9)
    except OSError:
        return None
    log.info("已清理残留的 opencode serve 进程 PID=%d", pid)
    return pid


def parse_port_owner(netstat_output: str, port: int) -> int | None:
    """从 netstat 输出里找出监听该端口的进程 PID。"""
    for line in (netstat_output or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in parts[3].upper():
            continue
        local = parts[1]
        if not local.rsplit(":", 1)[-1] == str(port):
            continue
        try:
            return int(parts[4])
        except ValueError:
            return None
    return None


def port_owner(port: int) -> int | None:
    """谁占着这个端口。没人占返回 None。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=20)
    except Exception:
        return None
    return parse_port_owner(out.stdout or "", port)


def prepare_port(port: int, root: str) -> None:
    """确保 serve 端口空闲。

    机器人被强杀时它 spawn 的 serve 会变成孤儿继续占端口。启动前先清掉
    自己记录过的那个；端口还被占着的话，如果占的是 opencode-cli（我们的
    残留）就直接清掉，否则明确报出是谁占的。
    """
    kill_stale_server(root)
    owner = port_owner(port)
    if owner is None:
        return
    name = _process_name(owner)
    if "opencode" in (name or "").lower():
        log.info("清理占用端口 %d 的残留 opencode 进程 PID=%d", port, owner)
        try:
            os.kill(owner, 9)
        except OSError:
            pass
        time.sleep(1.5)
        if port_owner(port) is None:
            return
    raise RuntimeError(f"端口 {port} 被 {name or '未知进程'} (PID {owner}) 占用，"
                       f"无法启动 opencode serve")


# ---------------------------------------------------------------- 主流程
def check_kb_health(base_url: str, timeout: int = 8) -> bool:
    """知识库服务是否可达。不可达时 agent 的知识库查询会全部失败。"""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/health",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def read_server_info(root: str) -> dict | None:
    """读机器人自己起的 opencode serve 的连接信息。"""
    path = os.path.join(root, ".feishu", "opencode.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def run_once(config_path: str = "D:/opencode/config.toml") -> FeishuBot | None:
    """装配并启动机器人。返回 None 表示配置不足（调用方应当退出）。"""
    cfg = load_feishu_config(config_path)
    if cfg is None:
        log.error("飞书机器人未启用或缺少凭据（检查 config.toml / secrets.toml / 环境变量）")
        return None

    from kbplatform.config import load_config
    from kbplatform.db import connect

    platform = load_config(config_path)
    conn = connect(platform.db_path)
    tokens = TokenStore(conn)
    tokens.prune_revoked()

    cli = find_opencode_cli(cfg.opencode_cli)
    if not cli:
        log.error("找不到 opencode-cli.exe，请在 config.toml 的 [feishu].opencode_cli 里指定路径")
        return None

    if not check_kb_health(cfg.kb_api_base):
        log.warning("知识库服务 %s 不可达 —— agent 的知识库查询会失败，"
                    "请确认知识库平台已启动（tools/start-platform.ps1）",
                    cfg.kb_api_base)

    # 清掉上次残留的 serve（机器人被强杀时子进程不会跟着死）
    prepare_port(cfg.opencode_port, cfg.workspace)

    server = OpenCodeServer(cli, cfg.opencode_port, cfg.workspace)
    server.start()
    try:
        server.write_info_file()
    except OSError:
        pass

    oc = OpenCodeClient(server.base_url, server.password)
    client = FeishuClient(cfg.app_id, cfg.app_secret)
    bot = FeishuBot(conn, cfg, client, oc, tokens, workspace=cfg.workspace)

    import lark_oapi as lark
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    def on_message(data):
        try:
            bot.handle(normalize_event(data))
        except Exception:
            log.exception("处理飞书消息出错")

    def on_read(data):
        """已读事件：应用订阅了它，但我们用不上。

        不注册的话 SDK 每收一条就报一次 "processor not found"，日志被刷爆。
        """
        return None

    builder = (EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message))
    # 已读事件可选注册（老版本 SDK 没有这个方法）
    if hasattr(builder, "register_p2_im_message_message_read_v1"):
        builder = builder.register_p2_im_message_message_read_v1(on_read)
    handler = builder.build()
    ws_client = lark.ws.Client(cfg.app_id, cfg.app_secret, event_handler=handler)
    try:
        ws_client.start()
    finally:
        server.stop()
    return bot


def setup_logging(log_dir: str | None = None) -> None:
    """配置日志：控制台 + UTF-8 日志文件。

    日志文件由 Python 自己写（UTF-8）。**不要用 PowerShell 的 `>>` 重定向**——
    它写的是 UTF-16，和别处的 UTF-8 内容混在一起就废了。
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(os.path.join(log_dir, "feishu-bot.log"),
                                      encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
            # 飞书 SDK 的连接日志也进同一个文件
            lark_logger = logging.getLogger("Lark")
            lark_logger.setLevel(logging.INFO)
            lark_logger.addHandler(fh)
        except OSError:
            pass


def main() -> int:
    setup_logging(os.path.join("D:/opencode", "kb", "logs"))
    config_path = sys.argv[1] if len(sys.argv) > 1 else "D:/opencode/config.toml"
    try:
        bot = run_once(config_path)
    except Exception:
        log.exception("机器人启动失败")
        return 1
    if bot is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
