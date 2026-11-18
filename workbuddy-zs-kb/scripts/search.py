#!/usr/bin/env python3
"""调用企业知识库的文档检索接口。

用法：
    python search.py "数据机房"
    python search.py "数据机房" --top-k 20 --share <共享名>
    python search.py "数据机房" --json
    python search.py "数据机房" --with-snippet   # 连片段原文一起打印

环境变量：
    ZB_KB_BASE_URL  知识库服务地址（带不带 /api/v1 都行）
    ZB_KB_TOKEN     API 密钥

片段原文默认**不打印**：它是公司内部原文，打印出来会随工具输出进
agent 的上下文，agent 用外部模型时等于把原文送出了内网
（SKILL.md 铁律 6）。真要原文时加 --with-snippet。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 强制 UTF-8 输出：中文 Windows 默认 GBK，调用方按 UTF-8 解析会崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def call(base: str, token: str, path: str, body=None, timeout: int = 180):
    url = base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            d = json.loads(detail).get("detail")
            detail = d if isinstance(d, str) else detail
        except Exception:
            pass
        print(f"[知识库] HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[知识库] 请求失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)


def api_base(base: str) -> str:
    """把 base 归一成**服务根**。

    两种写法都在流通：手册（SKILL.md 与管理员下载的 API 手册）让设
    `http://host:8000/api/v1`，而 `docs/接入说明.md` / `README.md` 写的是
    `http://host:8000`。本脚本自己会拼 `/api/v1/search`，所以这里必须先把
    末尾的 `/api/v1` 去掉 —— 否则照手册设就会打到 `/api/v1/api/v1/search`，
    落到 SPA 兜底路由上返回 405（不是 404，更难猜）。
    """
    b = (base or "").strip().rstrip("/")
    if b.lower().endswith("/api/v1"):
        b = b[: -len("/api/v1")]
    return b


def strip_snippets(results: list) -> list:
    """清掉片段原文，只留出处元信息。

    snippet 是公司内部原文，默认不带出去（SKILL.md 铁律 6）。
    """
    out = []
    for r in results or []:
        d = dict(r)
        d["snippet"] = ""
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--share", default=None)
    ap.add_argument("--status", default=None)
    ap.add_argument("--with-snippet", action="store_true",
                    help="同时打印片段原文（默认不打印，避免内部原文进 agent 上下文）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    raw_base = os.environ.get("ZB_KB_BASE_URL", "").strip()
    token = os.environ.get("ZB_KB_TOKEN", "").strip()
    if not raw_base or not token:
        print("[知识库] 缺少 ZB_KB_BASE_URL 或 ZB_KB_TOKEN 环境变量。"
              "请联系管理员获取服务地址与 API 密钥。", file=sys.stderr)
        sys.exit(4)

    base = api_base(raw_base)

    body = {"query": a.query, "top_k": a.top_k}
    if a.share:
        body["share"] = a.share
    if a.status:
        body["status"] = a.status
    r = call(base, token, "/api/v1/search", body)

    if not a.with_snippet:
        r["results"] = strip_snippets(r.get("results") or [])

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    res = r.get("results") or []
    print(f"命中 {r.get('total', len(res))} 条（向量 {r.get('stats',{}).get('vector',0)} / "
          f"关键词 {r.get('stats',{}).get('keyword',0)}）")
    for i, x in enumerate(res, 1):
        by = ",".join(x.get("matched_by") or [])
        print(f"{i}. {x.get('file','')}  位置 {x.get('locator','')}  命中 {by}")
        if x.get("snippet"):
            print("   " + x["snippet"].replace("\n", " ")[:160])
    if not res:
        print("（无命中）")


if __name__ == "__main__":
    main()
