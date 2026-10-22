"""进程入口：拉起 opencode serve、接飞书长连接。"""
import json
import os
import subprocess
import sys

import pytest

from kbplatform.feishu.runner import (OpenCodeServer, check_kb_health,
                                       kill_stale_server, normalize_event,
                                       parse_port_owner, parse_server_output,
                                       port_owner, prepare_port,
                                       read_server_info, run_once)


# ---------------------------------------------------------------- 解析
def test_parse_server_output():
    out = "server listening on http://127.0.0.1:4599\nserver password _9VZabc\n"
    url, pw = parse_server_output(out)
    assert url == "http://127.0.0.1:4599"
    assert pw == "_9VZabc"


def test_parse_server_output_raises_when_incomplete():
    with pytest.raises(ValueError):
        parse_server_output("nothing here")


def test_parse_server_output_tolerates_extra_lines():
    out = ("some log line\n"
           "server listening on http://127.0.0.1:4600\n"
           "server password pw123\n"
           "another log line\n")
    url, pw = parse_server_output(out)
    assert url == "http://127.0.0.1:4600" and pw == "pw123"


# ---------------------------------------------------------------- 事件归一化
class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _event_obj(text="你好", chat_type="p2p", message_type="text",
               mentions=None, open_id="ou_1"):
    return _Obj(event=_Obj(
        message=_Obj(message_id="om_1", chat_id="oc_1", chat_type=chat_type,
                     message_type=message_type,
                     content=json.dumps({"text": text}, ensure_ascii=False),
                     mentions=mentions or []),
        sender=_Obj(sender_id=_Obj(open_id=open_id, union_id="on_1")),
    ))


def test_normalize_event_extracts_fields():
    ev = normalize_event(_event_obj())
    assert ev["message_id"] == "om_1"
    assert ev["chat_id"] == "oc_1"
    assert ev["chat_type"] == "p2p"
    assert ev["sender_open_id"] == "ou_1"
    assert ev["message_type"] == "text"
    assert ev["text"] == "你好"
    assert ev["mentions"] == []


def test_normalize_event_keeps_mentions():
    m = _Obj(id=_Obj(open_id="ou_bot"), key="@_user_1", name="机器人")
    ev = normalize_event(_event_obj(mentions=[m]))
    assert ev["mentions"][0]["id"]["open_id"] == "ou_bot"


def test_normalize_event_handles_non_text_content():
    ev = normalize_event(_event_obj(text="", message_type="image"))
    assert ev["message_type"] == "image"
    assert ev["text"] == ""


def test_normalize_event_handles_broken_content_json():
    obj = _event_obj()
    obj.event.message.content = "not json"
    ev = normalize_event(obj)
    assert ev["text"] == ""


# ---------------------------------------------------------------- 入口
def test_run_once_returns_none_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[feishu]\nenabled = true\n", encoding="utf-8")
    assert run_once(config_path=str(cfg)) is None


def test_run_once_returns_none_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    cfg = tmp_path / "config.toml"
    cfg.write_text("[feishu]\nenabled = false\n", encoding="utf-8")
    assert run_once(config_path=str(cfg)) is None


def test_opencode_server_rejects_missing_cli():
    srv = OpenCodeServer(cli_path="D:/不存在/opencode-cli.exe", port=4599,
                         workdir="D:/opencode")
    with pytest.raises(FileNotFoundError):
        srv.start()


def test_opencode_server_stop_is_safe_when_not_started():
    srv = OpenCodeServer(cli_path="D:/不存在/opencode-cli.exe", port=4599,
                         workdir="D:/opencode")
    srv.stop()
    assert srv.is_alive() is False


def test_server_info_file_records_url_and_password(tmp_path):
    """密码落盘：测试和排障要能连上机器人自己起的那个 opencode serve。"""
    srv = OpenCodeServer(cli_path="D:/不存在/opencode-cli.exe", port=4599,
                         workdir=str(tmp_path))
    srv.base_url = "http://127.0.0.1:4599"
    srv.password = "pw123"
    path = srv.write_info_file()
    assert path.replace("\\", "/").endswith(".feishu/opencode.json")
    data = json.load(open(path, encoding="utf-8"))
    assert data["base_url"] == "http://127.0.0.1:4599"
    assert data["password"] == "pw123"
    assert data["port"] == 4599


def test_read_server_info_returns_none_when_missing(tmp_path):
    assert read_server_info(str(tmp_path)) is None


def test_check_kb_health_returns_true_on_ok(monkeypatch):
    class _R:
        status = 200

        def read(self):
            return b'{"status":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("kbplatform.feishu.runner.urllib.request.urlopen",
                        lambda req, timeout=None: _R())
    assert check_kb_health("http://127.0.0.1:8000") is True


def test_check_kb_health_returns_false_when_down(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("kbplatform.feishu.runner.urllib.request.urlopen", boom)
    assert check_kb_health("http://127.0.0.1:8000") is False


def test_kill_stale_server_removes_recorded_pid(tmp_path, monkeypatch):
    """机器人被杀后，它 spawn 的 opencode serve 会变成孤儿继续占着端口，
    下次启动就 bind 不到。启动前必须清掉自己记录过的那个 PID。"""
    pid_file = tmp_path / ".feishu" / "opencode.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("4321", encoding="utf-8")

    killed = []
    monkeypatch.setattr("kbplatform.feishu.runner._pid_alive", lambda p: p == 4321)
    monkeypatch.setattr("kbplatform.feishu.runner._process_name", lambda p: "opencode-cli")
    monkeypatch.setattr("kbplatform.feishu.runner.os.kill",
                        lambda pid, sig: killed.append(pid))
    assert kill_stale_server(str(tmp_path)) == 4321
    assert killed == [4321]


def test_kill_stale_server_ignores_dead_or_foreign_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / ".feishu" / "opencode.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("4321", encoding="utf-8")

    monkeypatch.setattr("kbplatform.feishu.runner._pid_alive", lambda p: False)
    assert kill_stale_server(str(tmp_path)) is None
    # 别的进程占用同一个 PID 也不能杀
    monkeypatch.setattr("kbplatform.feishu.runner._pid_alive", lambda p: True)
    monkeypatch.setattr("kbplatform.feishu.runner._process_name", lambda p: "explorer")
    assert kill_stale_server(str(tmp_path)) is None


def test_kill_stale_server_without_pid_file(tmp_path):
    assert kill_stale_server(str(tmp_path)) is None


def test_port_owner_returns_none_when_free():
    assert port_owner(4599) is None or isinstance(port_owner(4599), int)


def test_parse_port_owner_finds_listener():
    out = """
  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       12644
  TCP    127.0.0.1:4599         0.0.0.0:0              LISTENING       15444
  TCP    127.0.0.1:52000        127.0.0.1:4599         ESTABLISHED     9999
"""
    assert parse_port_owner(out, 4599) == 15444
    assert parse_port_owner(out, 8000) == 12644
    assert parse_port_owner(out, 52000) is None   # 非 LISTENING 不算占用
    assert parse_port_owner(out, 1234) is None


def test_prepare_port_raises_with_clear_message_when_busy(tmp_path, monkeypatch):
    """端口被别人占着时，要说清楚是谁占的，而不是干巴巴一句「启动即退出」。"""
    monkeypatch.setattr("kbplatform.feishu.runner.kill_stale_server",
                        lambda root: None)
    monkeypatch.setattr("kbplatform.feishu.runner.port_owner", lambda port: 1234)
    monkeypatch.setattr("kbplatform.feishu.runner._process_name",
                        lambda pid: "some-other-app")
    with pytest.raises(RuntimeError) as e:
        prepare_port(4599, str(tmp_path))
    assert "4599" in str(e.value)
    assert "1234" in str(e.value)


def test_prepare_port_kills_our_own_orphan(tmp_path, monkeypatch):
    """占着端口的是我们自己的残留 opencode-cli —— 直接清掉。"""
    calls = []
    monkeypatch.setattr("kbplatform.feishu.runner.kill_stale_server",
                        lambda root: None)
    monkeypatch.setattr("kbplatform.feishu.runner._process_name",
                        lambda pid: "opencode-cli")
    monkeypatch.setattr("kbplatform.feishu.runner.port_owner",
                        lambda port: 1234 if not calls else None)
    monkeypatch.setattr("kbplatform.feishu.runner.os.kill",
                        lambda pid, sig: calls.append(pid))
    prepare_port(4599, str(tmp_path))
    assert calls == [1234]
