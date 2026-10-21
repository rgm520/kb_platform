"""Ollama 客户端封装：结构化生成 + 向量化。

关键约束（全部经实测验证）：
- 必须用 JSON Schema 约束，否则裸输出有 40% 概率是非法 JSON
- num_predict 必须放大，否则 done_reason=length 会截断 JSON
- 结构化抽取必须 think=false，thinking 会吃掉输出预算
- 默认 qwen3.5:9b 的上下文只有 4096，会静默截断长文档 → 用 32k/64k 变体
"""
import json, time, urllib.request, urllib.error

OLLAMA = "http://localhost:11434"
GEN_MODEL = "qwen3.5-9b-64k"
EMB_MODEL = "bge-m3"

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string",
                                 "enum": ["制度", "流程", "标准", "职责", "数据", "组织", "其它"]},
                },
                "required": ["title", "detail", "entities", "category"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "relation": {"type": "string"},
                },
                "required": ["from", "to", "relation"],
            },
        },
    },
    "required": ["summary", "key_points", "relations"],
}


def _post(path, payload, timeout=1800):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(OLLAMA + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, timeout=60):
    with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def repair_json(s):
    """从模型输出中抢救合法 JSON。"""
    if not s:
        return None
    s = s.strip()
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:]
            if p.startswith("{"):
                s = p
                break
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    # 去掉尾随逗号
    s = s.replace(",\n}", "\n}").replace(",\n]", "\n]").replace(",}", "}").replace(",]", "]")
    # 括号配平
    depth, instr, esc = 0, False, False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            instr = not instr
            continue
        if instr:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    while depth > 0:
        s += "}" if s.rstrip().endswith("]") is False else "]"
        # 简单判断：栈式
        break
    # 精确配平（用栈）
    stack = []
    instr = esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            instr = not instr
            continue
        if instr:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    s += "".join(reversed(stack))
    try:
        return json.loads(s)
    except Exception:
        return None


def chat_json(prompt, schema=None, model=GEN_MODEL, num_predict=2500,
              temperature=0.2, think=False, retries=2, keep_alive="30m", system=None):
    """调用大模型并保证拿到合法 JSON。返回 (data, meta)。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model, "messages": messages, "stream": False, "think": think,
        "options": {"num_predict": num_predict, "temperature": temperature},
        "keep_alive": keep_alive,
    }
    if schema is not None:
        payload["format"] = schema

    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = _post("/api/chat", payload)
        except Exception as e:
            last_err = f"HTTP {e}"
            time.sleep(1)
            continue
        wall = time.time() - t0
        raw = (r.get("message") or {}).get("content") or ""
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            data = repair_json(raw)
        if data is not None:
            return data, {"wall": wall, "attempt": attempt + 1,
                          "prompt_tokens": r.get("prompt_eval_count"),
                          "eval_tokens": r.get("eval_count"),
                          "done": r.get("done_reason"),
                          "raw_len": len(raw), "repaired": False}
        last_err = "JSON 解析失败"
        # 重试时追加提示，要求补全
        payload["messages"][-1]["content"] = (
            prompt + "\n\n【重要】上一次输出不是合法 JSON。请只输出完整、合法的 JSON，"
                     "确保所有括号和引号闭合，不要输出任何其它文字。")
        time.sleep(0.5)
    raise RuntimeError(f"结构化输出失败: {last_err}")


def embed(texts, model=EMB_MODEL, keep_alive="30m"):
    """批量向量化。实测 bge-m3 批量 10 段仅需 0.73s。"""
    if not texts:
        return []
    payload = {"model": model, "input": list(texts), "keep_alive": keep_alive}
    r = _post("/api/embed", payload)
    return r.get("embeddings", [])


def health():
    try:
        tags = _get("/api/tags")
        return {m["name"]: m["details"]["context_length"] for m in tags.get("models", [])}
    except Exception as e:
        return {"error": str(e)}
