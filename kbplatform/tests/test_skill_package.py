"""技能包脚本（workbuddy-zs-kb/scripts）的两个坑。

坑 1：base url 口径不一致。
    SKILL.md 与管理员下载的 API 手册都让设 `ZB_KB_BASE_URL=http://host:8000/api/v1`，
    但脚本自己还会拼 `/api/v1/query` —— 照手册设就打到 `/api/v1/api/v1/query`，
    FastAPI 落到 SPA 兜底路由，返回 **405 Method Not Allowed**（不是 404，更难猜）。
    `docs/接入说明.md` / `README.md` 又写的是不带 `/api/v1` 的地址。

坑 2：片段摘录直接打进 stdout。
    SKILL.md 铁律 6 与手册规则 5 都写着「不要把 snippet 整段转发给外部大模型」，
    但脚本每个片段都打印 160 字原文 —— 这些原文会随工具输出进 agent 的上下文，
    agent 用外部模型时，内部原文就出内网了。

这两个坑都会让「照手册接入的新智能体」用不起来或泄漏原文，所以钉死成测试。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "workbuddy-zs-kb"
SNIPPET = "内部机密原文片段XYZ"


def _load(name: str):
    """按文件路径加载技能包里的脚本（它们是独立脚本，不是包模块）。"""
    path = PKG / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"skillpkg_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ 坑 1
@pytest.mark.parametrize("name", ["query", "search"])
def test_api_base_accepts_root_or_api_v1_suffix(name):
    """两种 base 写法都要能归一成服务根，拼出来的 URL 才对。"""
    mod = _load(name)
    assert mod.api_base("http://host:8000") == "http://host:8000"
    assert mod.api_base("http://host:8000/") == "http://host:8000"
    assert mod.api_base("http://host:8000/api/v1") == "http://host:8000"
    assert mod.api_base("http://host:8000/api/v1/") == "http://host:8000"


# ------------------------------------------------------------------ 坑 1（端到端）
class _Handler(BaseHTTPRequestHandler):
    """假知识库服务：记录被请求的路径，回一份带片段摘录的标准响应。"""

    paths: list[str] = []

    def _reply(self, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).paths.append(self.path)
        self._reply({"results": [], "total": 0})

    def do_POST(self):
        type(self).paths.append(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if "search" in self.path:
            self._reply({"results": [{"file": "测试.pdf", "locator": "p1",
                                      "snippet": SNIPPET, "restricted": False,
                                      "matched_by": ["vector"]}],
                         "total": 1, "stats": {}})
        else:
            self._reply({"answer": "这是答案", "citations": [
                {"chunk_id": 1, "file": "测试.pdf", "locator": "p1", "snippet": SNIPPET,
                 "restricted": False, "document_id": 1}],
                "restricted": False, "model_used": "m", "model_requested": "m"})

    def log_message(self, *a):
        pass


@pytest.fixture()
def kb_server():
    _Handler.paths = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, _Handler.paths
    srv.shutdown()
    srv.server_close()


def _run(script: str, base: str, *args) -> subprocess.CompletedProcess:
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows",
           "ZB_KB_BASE_URL": base, "ZB_KB_TOKEN": "kb_test_token",
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(PKG / "scripts" / script), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)


@pytest.mark.parametrize("base_suffix", ["", "/api/v1"])
def test_query_script_hits_the_right_path(kb_server, base_suffix):
    """不管 base 写不写 /api/v1，请求都必须打到 /api/v1/query。"""
    srv, paths = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}{base_suffix}"
    r = _run("query.py", base, "测试问题")
    assert r.returncode == 0, r.stderr
    assert paths == ["/api/v1/query"], paths


@pytest.mark.parametrize("base_suffix", ["", "/api/v1"])
def test_search_script_hits_the_right_path(kb_server, base_suffix):
    srv, paths = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}{base_suffix}"
    r = _run("search.py", base, "测试关键词")
    assert r.returncode == 0, r.stderr
    assert paths == ["/api/v1/search"], paths


# ------------------------------------------------------------------ 坑 2
def test_query_output_omits_snippet_by_default(kb_server):
    """默认不把片段原文打进 stdout（否则原文随工具输出进 agent 上下文）。"""
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("query.py", base, "测试问题")
    assert r.returncode == 0, r.stderr
    assert "测试.pdf" in r.stdout, "来源文件名要给，智能体要靠它标出处"
    assert SNIPPET not in r.stdout, "片段原文不应出现在默认输出里"


def test_query_output_includes_snippet_on_demand(kb_server):
    """真要原文时显式打开开关。"""
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("query.py", base, "测试问题", "--with-snippet")
    assert r.returncode == 0, r.stderr
    assert SNIPPET in r.stdout


def test_query_json_output_omits_snippet_by_default(kb_server):
    """--json 同样不能把原文带出去（它也是工具输出，会进 agent 上下文）。"""
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("query.py", base, "测试问题", "--json")
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    assert obj["citations"][0]["file"] == "测试.pdf"
    assert not obj["citations"][0]["snippet"], "片段原文不应出现在 --json 输出里"


def test_query_json_output_includes_snippet_on_demand(kb_server):
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("query.py", base, "测试问题", "--json", "--with-snippet")
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    assert obj["citations"][0]["snippet"] == SNIPPET


def test_search_output_omits_snippet_by_default(kb_server):
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("search.py", base, "测试关键词")
    assert r.returncode == 0, r.stderr
    assert "测试.pdf" in r.stdout
    assert SNIPPET not in r.stdout


def test_search_output_includes_snippet_on_demand(kb_server):
    srv, _ = kb_server
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    r = _run("search.py", base, "测试关键词", "--with-snippet")
    assert r.returncode == 0, r.stderr
    assert SNIPPET in r.stdout
