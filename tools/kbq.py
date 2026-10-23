#!/usr/bin/env python
"""知识库查询 helper —— agent 通过 shell 调它查知识库。

用法：
    python tools/kbq.py query "三级及以下子企业能不能自建数据中心机房？"
    python tools/kbq.py search "数据中心" --top-k 10
    python tools/kbq.py query "问题" --json

凭证不写在命令行里（命令行会进 agent 的对话记录）：
  * KB_TOKEN_FILE  —— 指向一个只含 token 明文的文件，由 bot 进程写入
  * KB_API_BASE    —— 知识库 HTTP 地址，默认 http://127.0.0.1:8000

退出码（agent 据此区分故障与「没查到」）：
    0  正常
    3  token 无效/过期          -> KB_AUTH_EXPIRED
    4  知识库服务不可达         -> KB_UNREACHABLE
    5  超出配额                 -> KB_QUOTA_EXCEEDED
    2  参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_UNREACHABLE = 4
EXIT_QUOTA = 5

DEFAULT_API_BASE = "http://127.0.0.1:8000"


def _read_token() -> str:
    path = os.environ.get("KB_TOKEN_FILE", "").strip()
    if not path:
        raise FileNotFoundError("环境变量 KB_TOKEN_FILE 未设置")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _post(path: str, payload: dict, base: str, token: str, timeout: int = 180) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("知识库 token 无效或已过期") from e
        if e.code == 429:
            raise LookupError("超出当日配额") from e
        raise RuntimeError(f"知识库返回 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"连不上知识库服务 {base}: {e.reason}") from e
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _format_citations(cites: list) -> str:
    lines = []
    for i, c in enumerate(cites, 1):
        name = c.get("file") or "?"
        loc = c.get("locator") or ""
        if c.get("restricted"):
            lines.append(f"[{i}] {name} {loc} —— 该片段涉密，未回传原文".rstrip())
        else:
            snip = (c.get("snippet") or "").replace("\n", " ").strip()
            lines.append(f"[{i}] {name} {loc}".rstrip() + (f" —— {snip}" if snip else ""))
    return "\n".join(lines)


def _format_search(results: list) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        name = r.get("file") or "?"
        loc = r.get("locator") or ""
        if r.get("restricted"):
            lines.append(f"[{i}] {name} {loc} —— 该片段涉密，未回传原文".rstrip())
        else:
            snip = (r.get("snippet") or "").replace("\n", " ").strip()
            lines.append(f"[{i}] {name} {loc}".rstrip() + (f" —— {snip}" if snip else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kbq.py", description="知识库查询 helper")
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("query", help="问答（检索 + 生成答案）")
    q.add_argument("question")
    q.add_argument("--top-k", type=int, default=8)
    q.add_argument("--include-text", action="store_true",
                   help="带上片段原文（涉密片段永远不会带）")
    q.add_argument("--json", action="store_true")

    s = sub.add_parser("search", help="只检索，返回片段列表")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=20)
    s.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return EXIT_USAGE

    base = os.environ.get("KB_API_BASE", "").strip() or DEFAULT_API_BASE
    try:
        token = _read_token()
    except Exception as e:
        print(f"KB_AUTH_EXPIRED: 读不到知识库凭证（{e}）")
        return EXIT_AUTH
    if not token:
        print("KB_AUTH_EXPIRED: 知识库凭证为空")
        return EXIT_AUTH

    try:
        if args.cmd == "query":
            obj = _post("/api/v1/query",
                        {"question": args.question, "top_k": args.top_k,
                         "include_text": args.include_text},
                        base, token)
        else:
            obj = _post("/api/v1/search",
                        {"query": args.query, "top_k": args.top_k},
                        base, token)
    except PermissionError as e:
        print(f"KB_AUTH_EXPIRED: {e}")
        return EXIT_AUTH
    except LookupError as e:
        print(f"KB_QUOTA_EXCEEDED: {e}")
        return EXIT_QUOTA
    except ConnectionError as e:
        print(f"KB_UNREACHABLE: {e}")
        return EXIT_UNREACHABLE
    except Exception as e:
        print(f"KB_ERROR: {e}")
        return EXIT_UNREACHABLE

    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=1))
        return EXIT_OK

    if args.cmd == "query":
        answer = obj.get("answer") or ""
        print(answer)
        notice = obj.get("restricted_notice")
        if notice:
            print(f"\n（{notice}）")
        cites = obj.get("citations") or []
        if cites:
            print("\n引用：")
            print(_format_citations(cites))
        if not answer:
            print("（知识库没有给出答案）")
    else:
        results = obj.get("results") or []
        print(f"共 {obj.get('total', len(results))} 条：")
        print(_format_search(results))
        if not results:
            print("（没有命中）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
