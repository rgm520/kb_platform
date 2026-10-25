# -*- coding: utf-8 -*-
"""只读体检：某个账号的检索可见范围到底有多大。

背景（2026-10-25 热修）：管理台「目录授权」只写 grants（没有老的
user_scopes 行）时，检索层曾把用户的文档全过滤掉——看板能看到 564 篇，
/api/v1/search 与 /api/v1/query 四路召回却是 0。这个脚本用来在修完后
在线上真实验证：同一个 grants-only 账号，看板总数 vs 检索召回数是否一致。

**只读**：只调用查看类接口（登录、文档看板、搜索）。搜索会照常写
audit_log——那是平台对每次搜索的正常记录，不是本脚本额外做的数据修改。
不建账号、不改授权、不改数据；验证用的临时账号由操作者自己提供。

用法（stdlib，无第三方依赖）：
  python tools/check-retrieval-scope.py --username USER --password PW
  python tools/check-retrieval-scope.py --token kb_xxx
  可选： --base http://127.0.0.1:8000   --query 验收
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"
DEFAULT_QUERY = "验收"


def _cookie_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _request(opener, method: str, url: str, payload: dict | None = None,
             token: str | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json; charset=utf-8",
        **({"Authorization": "Bearer " + token} if token else {}),
    })
    with opener.open(req, timeout=timeout) as r:
        return json.loads((r.read() or b"{}").decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="打印一个账号的检索范围（只读）")
    ap.add_argument("--base", default=os.environ.get("KB_API_BASE", DEFAULT_BASE))
    ap.add_argument("--username", help="登录账号（二选一：--token）")
    ap.add_argument("--password")
    ap.add_argument("--token", default=os.environ.get("KB_TOKEN", "").strip(),
                    help="Bearer 密钥；不传则用 --username/--password 登录")
    ap.add_argument("--query", default=DEFAULT_QUERY)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    opener = _cookie_opener()

    if args.token:
        token = args.token
        print(f"[check] 使用现有 Bearer 密钥（前缀 {token[:12]}…）")
    else:
        if not args.username or not args.password:
            ap.error("需要 --token 或 --username + --password")
        login = _request(opener, "POST", f"{base}/api/v1/auth/login",
                         {"username": args.username, "password": args.password})
        print(f"[check] 登录成功 user={login['user']['username']} role={login['user']['role']} "
              f"scopes={login.get('scopes')}")
        token = None        # 后续请求走会话 cookie

    def call(method: str, path: str, payload: dict | None = None):
        return _request(opener, method, base + path, payload, token=token)

    # 文档看板：同一用户的授权在「看板」维度上能看到多少篇
    board = call("GET", "/api/v1/documents?page=1&page_size=1")
    # 检索：keyword/vector/graph 各路召回了多少（新 bug 表现为全 0）
    search = call("POST", "/api/v1/search", {"query": args.query, "top_k": 8})

    print(f"[check] base={base} query={args.query!r}")
    print(f"[board] /api/v1/documents total = {board['total']}")
    print(f"[search] /api/v1/search total = {search['total']}  stats = {search['stats']}")
    if board["total"] > 0 and search["total"] == 0:
        print("[check] !! 不一致：看板有文档但检索一条都搜不出 —— 检索层的授权过滤有问题")
        return 1
    print("[check] OK：看板与检索的范围一致")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        detail = (e.read().decode("utf-8", "replace") or "")[:300]
        print(f"[check] HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[check] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
