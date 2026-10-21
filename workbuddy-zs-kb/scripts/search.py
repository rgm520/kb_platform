#!/usr/bin/env python3
"""调用企业知识库的文档检索接口。

用法：
    python search.py "数据机房"
    python search.py "数据机房" --top-k 20 --share <共享名>
    python search.py "数据机房" --json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--share", default=None)
    ap.add_argument("--status", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    base = os.environ.get("ZB_KB_BASE_URL", "").strip()
    token = os.environ.get("ZB_KB_TOKEN", "").strip()
    if not base or not token:
        print("[知识库] 缺少 ZB_KB_BASE_URL 或 ZB_KB_TOKEN 环境变量。"
              "请联系管理员获取服务地址与 API 密钥。", file=sys.stderr)
        sys.exit(4)

    body = {"query": a.query, "top_k": a.top_k}
    if a.share:
        body["share"] = a.share
    if a.status:
        body["status"] = a.status
    r = call(base, token, "/api/v1/search", body)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    res = r.get("results") or []
    print(f"命中 {r.get('total', len(res))} 条（向量 {r.get('stats',{}).get('vector',0)} / "
          f"关键词 {r.get('stats',{}).get('keyword',0)}）")
    for i, x in enumerate(res, 1):
        tag = " [涉密·原文未回传]" if x.get("restricted") else ""
        by = ",".join(x.get("matched_by") or [])
        print(f"{i}. {x.get('file','')}  位置 {x.get('locator','')}  命中 {by}{tag}")
        if x.get("snippet"):
            print("   " + x["snippet"].replace("\n", " ")[:160])
    if not res:
        print("（无命中）")


if __name__ == "__main__":
    main()
