"""模型抽象层：预置厂商 + 自定义配置 + 涉密自动路由。

约束（spec §7.5）：
  * provider 只有两种：**ollama** 与 **openai_compatible**
    （国内主流厂商基本都提供 OpenAI 兼容接口）
  * **命中涉密内容 + 选了云端模型 → 自动路由到本地模型**（不报错、不中断），
    响应标注 restricted / model_switched
  * 生成模型必须是 32k/64k 变体（默认 qwen3.5:9b 上下文 4096 会静默截断）
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

import kb.llm as kllm

# ---------------------------------------------------------------- 预置厂商
# 只作为「添加模型」对话框里的模板，**不预置进库**。
# 没有 API Key 的云端模型是不可用的，列在模型列表里只是噪音。
PRESETS = [
    {"name": "DeepSeek", "provider": "openai_compatible",
     "base_url": "https://api.deepseek.com/v1", "model_id": "deepseek-chat",
     "need_key": True, "note": "国内直连"},
    {"name": "通义千问 (DashScope)", "provider": "openai_compatible",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model_id": "qwen-plus", "need_key": True, "note": "阿里云百炼"},
    {"name": "腾讯混元", "provider": "openai_compatible",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "model_id": "hunyuan-turbo", "need_key": True, "note": "腾讯云"},
    {"name": "智谱 GLM", "provider": "openai_compatible",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model_id": "glm-4-plus", "need_key": True, "note": "bigmodel.cn"},
    {"name": "月之暗面 Kimi", "provider": "openai_compatible",
     "base_url": "https://api.moonshot.cn/v1", "model_id": "moonshot-v1-8k",
     "need_key": True, "note": "Moonshot"},
    {"name": "MiniMax", "provider": "openai_compatible",
     "base_url": "https://api.minimax.chat/v1", "model_id": "abab6.5s-chat",
     "need_key": True, "note": ""},
    {"name": "OpenAI", "provider": "openai_compatible",
     "base_url": "https://api.openai.com/v1", "model_id": "gpt-4o-mini",
     "need_key": True, "note": "需外网"},
    {"name": "本地 / 内网 Ollama", "provider": "ollama",
     "base_url": "http://localhost:11434", "model_id": "qwen3.5-9b-64k",
     "need_key": False, "note": "本机或内网其它机器；需填 num_ctx"},
]

# 可用性检查结果缓存（避免每次列表都去打网络）
_usable_cache: dict[tuple[int, float], dict] = {}
_USABLE_TTL = 30.0


def check_usable(cfg: ModelConfig) -> dict:
    """判断模型是否真的能用。

    ollama   : 实际 GET <base_url>/api/tags 看主机通不通
    云端     : 只看有没有配 API Key（不真调，避免花钱/耗配额）
    """
    # key 必须包含配置身份，不能只用 id：删掉 id=5 再新建一个 id=5 的模型，
    # 会拿到上一个模型的旧判定（不同库之间同 id 也会串）。
    key = (cfg.id, cfg.provider, cfg.base_url, cfg.model_id,
           int(time.time() // _USABLE_TTL))
    hit = _usable_cache.get(key)
    if hit:
        return hit
    res = {"usable": True, "reason": ""}
    if cfg.provider == "ollama":
        import urllib.request
        import urllib.error
        t0 = time.time()
        try:
            with urllib.request.urlopen(cfg.base_url.rstrip("/") + "/api/tags", timeout=5) as r:
                r.read()
            res["latency_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            res = {"usable": False,
                   "reason": f"主机不可达（{type(e).__name__}）", "latency_ms": None}
    else:
        if not (cfg.api_key or "").strip():
            res = {"usable": False, "reason": "未配置 API Key", "latency_ms": None}
    _usable_cache[key] = res
    if len(_usable_cache) > 500:
        _usable_cache.clear()
    return res


def seed_local_models(conn: sqlite3.Connection, base_url: str = "http://localhost:11434",
                       candidates: list[str] | None = None) -> int:
    """只注册**本机确实存在**的 Ollama 模型。

    云端厂商不在这里注册——它们没有 API Key 就是不可用的，
    应该由管理员在「添加模型」时按需选择。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8"))
    except Exception:
        return 0
    have = {m["name"] for m in tags.get("models", [])}
    cands = candidates or ["qwen3.5-9b-64k", "qwen3.5-9b-32k"]
    n = 0
    for mid in cands:
        if mid not in have:
            continue
        if get_model(conn, mid):
            continue
        add_model(conn, f"本地 Ollama ({mid})", "ollama", base_url, mid,
                  is_local=True, is_default=(n == 0))
        n += 1
    return n


@dataclass
class ModelConfig:
    id: int
    name: str
    provider: str
    base_url: str
    model_id: str
    api_key: str | None
    is_local: bool
    is_default: bool
    extra: dict = field(default_factory=dict)


def list_models(conn: sqlite3.Connection) -> list[ModelConfig]:
    rows = conn.execute(
        "SELECT id,name,provider,base_url,model_id,api_key,is_local,is_default,extra"
        " FROM models ORDER BY is_local DESC, id").fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r[8]) if r[8] else {}
        except Exception:
            extra = {}
        out.append(ModelConfig(r[0], r[1], r[2], r[3], r[4], r[5],
                               bool(r[6]), bool(r[7]), extra))
    return out


def get_model(conn: sqlite3.Connection, model_id: int) -> ModelConfig | None:
    for m in list_models(conn):
        if m.id == model_id:
            return m
    return None


def add_model(conn: sqlite3.Connection, name: str, provider: str, base_url: str,
              model_id: str, api_key: str | None = None, is_local: bool = False,
              is_default: bool = False, extra: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO models(name,provider,base_url,model_id,api_key,is_local,is_default,extra,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (name, provider, base_url, model_id, api_key, 1 if is_local else 0,
         1 if is_default else 0, json.dumps(extra or {}, ensure_ascii=False), time.time()))
    conn.commit()
    return cur.lastrowid


def delete_model(conn: sqlite3.Connection, model_id: int) -> bool:
    cur = conn.execute("DELETE FROM models WHERE id=?", (model_id,))
    conn.commit()
    return cur.rowcount > 0


def seed_presets(conn: sqlite3.Connection) -> int:
    """已废弃：不再把云端厂商预置进库。

    保留函数名以兼容旧调用，实际行为改为「什么都不做」。
    """
    return 0


# ---------------------------------------------------------------- 调用
def _chat_openai_compatible(cfg: ModelConfig, prompt: str, system: str | None,
                            temperature: float, max_tokens: int) -> tuple[str, dict]:
    import urllib.request
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    payload = {
        "model": cfg.model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions",
                                 data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=cfg.extra.get("timeout", 300)) as r:
        obj = json.loads(r.read().decode("utf-8"))
    text = obj["choices"][0]["message"]["content"]
    usage = obj.get("usage", {}) or {}
    return text, {"prompt_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)}


def _chat_ollama(cfg: ModelConfig, prompt: str, system: str | None,
                 temperature: float, max_tokens: int) -> tuple[str, dict]:
    """Ollama provider。

    base_url 指向哪台机器就打哪台（远端 Ollama 接入靠这个）。

    **num_ctx 必须显式带上**：Ollama 的 runner 默认 num_ctx=4096，
    超长提示会被**静默截断**（不报错、不提示，只是丢掉后面的内容）。
    实测 49500 字符的提示在默认配置下只处理了 2050 token。
    远端机器无法 SSH 建 64k 变体时，请求级 num_ctx 覆盖是唯一可靠办法。
    """
    options = {"temperature": temperature, "num_predict": max_tokens}
    num_ctx = (cfg.extra or {}).get("num_ctx")
    if num_ctx:
        options["num_ctx"] = int(num_ctx)
    r = kllm._post("/api/chat", {
        "model": cfg.model_id,
        "messages": ([{"role": "system", "content": system}] if system else []) +
                    [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": options,
        "keep_alive": (cfg.extra or {}).get("keep_alive", "30m"),
    }, base=cfg.base_url)
    text = (r.get("message") or {}).get("content") or ""
    return text, {"prompt_tokens": r.get("prompt_eval_count", 0),
                  "output_tokens": r.get("eval_count", 0)}


def chat(cfg: ModelConfig, prompt: str, system: str | None = None,
         temperature: float = 0.2, max_tokens: int = 2000) -> tuple[str, dict]:
    if cfg.provider == "ollama":
        return _chat_ollama(cfg, prompt, system, temperature, max_tokens)
    if cfg.provider == "openai_compatible":
        return _chat_openai_compatible(cfg, prompt, system, temperature, max_tokens)
    raise ValueError(f"未知 provider: {cfg.provider}")


def route_for(restricted: bool, requested: ModelConfig,
              local_candidates: list[ModelConfig]) -> tuple[ModelConfig, bool]:
    """涉密自动路由：命中涉密且选了云端模型 -> 换成本地模型。

    返回 (实际使用的模型, 是否发生切换)。
    """
    if not restricted or requested.is_local:
        return requested, False
    for m in local_candidates:
        if m.id != requested.id:
            return m, True
    return requested, False
