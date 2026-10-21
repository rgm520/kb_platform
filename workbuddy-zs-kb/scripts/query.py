#!/usr/bin/env python3
"""调用企业知识库的问答接口。

用法：
    python query.py "问题"
    python query.py "问题" --model 1 --include-text --json

环境变量：
    ZB_KB_BASE_URL  知识库服务地址
    ZB_KB_TOKEN     API 密钥
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 中文 Windows 默认输出编码是 GBK，调用方（WorkBuddy / 管道）多半按 UTF-8 解析，
# 会直接抛 UnicodeDecodeError。这里强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def call(base: str, token: str, path: str, body=None, timeout: int = 300):
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
    ap.add_argument("question")
    ap.add_argument("--model", default="auto", help="模型 id / 名称 / auto")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--include-text", action="store_true", help="带回片段全文（涉密内容不会被回传）")
    ap.add_argument("--json", action="store_true", help="只输出原始 JSON")
    a = ap.parse_args()

    base = os.environ.get("ZB_KB_BASE_URL", "").strip()
    token = os.environ.get("ZB_KB_TOKEN", "").strip()
    if not base or not token:
        print("[知识库] 缺少 ZB_KB_BASE_URL 或 ZB_KB_TOKEN 环境变量。"
              "请联系管理员获取服务地址与 API 密钥。", file=sys.stderr)
        sys.exit(4)

    r = call(base, token, "/api/v1/query", {
        "question": a.question,
        "model": a.model,
        "top_k": a.top_k,
        "include_text": a.include_text,
    })

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    print(r.get("answer", "(无输出)"))
    print()
    cits = r.get("citations") or []
    if cits:
        print("--- 出处 ---")
        for i, c in enumerate(cits, 1):
            tag = " [涉密·原文未回传]" if c.get("restricted") else ""
            print(f"[片段{i}] {c.get('file','')}  位置 {c.get('locator','')}{tag}")
            if c.get("snippet"):
                print("    " + c["snippet"].replace("\n", " ")[:160])
    else:
        print("（未检索到相关文档片段）")
    if r.get("restricted"):
        print()
        print("[注意] 本次回答命中内部受限目录，平台已锁定本地模型并未回传原文；"
              "请登录知识库平台查看原文。")
    if r.get("model_switched"):
        print(f"[注意] 原请求模型 {r.get('model_requested')} 因涉密已自动切换为 "
              f"{r.get('model_used')}。")


if __name__ == "__main__":
    main()
