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
    in_parse_pool: bool = False
    pool_weight: float = 1.0


def list_models(conn: sqlite3.Connection) -> list[ModelConfig]:
    rows = conn.execute(
        "SELECT id,name,provider,base_url,model_id,api_key,is_local,is_default,extra,"
        "in_parse_pool,pool_weight FROM models ORDER BY is_local DESC, id").fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r[8]) if r[8] else {}
        except Exception:
            extra = {}
        out.append(ModelConfig(r[0], r[1], r[2], r[3], r[4], r[5],
                               bool(r[6]), bool(r[7]), extra,
                               bool(r[9]), float(r[10] or 1.0)))
    return out


def get_model(conn: sqlite3.Connection, model_id: int) -> ModelConfig | None:
    for m in list_models(conn):
        if m.id == model_id:
            return m
    return None


def add_model(conn: sqlite3.Connection, name: str, provider: str, base_url: str,
              model_id: str, api_key: str | None = None, is_local: bool = False,
              is_default: bool = False, extra: dict | None = None,
              in_parse_pool: bool = False, pool_weight: float = 1.0) -> int:
    cur = conn.execute(
        "INSERT INTO models(name,provider,base_url,model_id,api_key,is_local,is_default,"
        "in_parse_pool,pool_weight,extra,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (name, provider, base_url, model_id, api_key, 1 if is_local else 0,
         1 if is_default else 0, 1 if in_parse_pool else 0, float(pool_weight),
         json.dumps(extra or {}, ensure_ascii=False), time.time()))
    conn.commit()
    return cur.lastrowid


def update_pool_member(conn: sqlite3.Connection, model_id: int,
                       in_parse_pool: bool | None = None,
                       pool_weight: float | None = None) -> ModelConfig | None:
    m = get_model(conn, model_id)
    if m is None:
        return None
    if in_parse_pool is not None:
        conn.execute("UPDATE models SET in_parse_pool=? WHERE id=?",
                     (1 if in_parse_pool else 0, model_id))
    if pool_weight is not None:
        conn.execute("UPDATE models SET pool_weight=? WHERE id=?",
                     (float(pool_weight), model_id))
    conn.commit()
    return get_model(conn, model_id)


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


def model_family(model_id: str) -> str:
    """从模型名推断「族」，用于判断池内模型是否同源。

    qwen3.5:9b 与 qwen3.5-9b-64k 是**同一个模型**（后者只是上下文配置不同），
    必须归为同一族，否则会误报「风格不一致」。
    """
    m = (model_id or "").strip().lower()
    m = m.replace(":", "-")
    # 去掉上下文变体后缀：-64k / -32k / -128k
    for suf in ("-128k", "-64k", "-32k", "-16k", "-8k"):
        if m.endswith(suf):
            m = m[: -len(suf)]
            break
    return m


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


# ================================================================ 解析算力池
class ParsePool:
    """文档解析（知识提炼）的模型池：本地 + 远端多个大模型，由平台调度。

    设计要点：
    * **按文档分派（文件级粘性）**：同一份文档的所有块交给同一个模型，
      知识点风格保持一致；不同文档分给不同模型，吞吐线性叠加。
    * **加权轮转**：按 pool_weight 分配工作量。
    * **每成员独立并发上限**：本地 3060 只能并发 1，远端另算，
      否则会被同一个模型打满。
    * **健康检查**：不可用的成员自动跳过（由 check_usable 判定）。
    * **一致性提醒**：池内模型族不同会产生风格差异，在 status 里提示。
    """

    def __init__(self, conn: sqlite3.Connection, max_concurrency: dict[int, int] | None = None):
        self.conn = conn
        self.max_concurrency = max_concurrency or {}
        self._current: dict[int, float] = {}   # nginx 式平滑加权轮转
        self._inflight: dict[int, int] = {}

    def members(self) -> list[ModelConfig]:
        out = []
        for m in list_models(self.conn):
            if not m.in_parse_pool:
                continue
            st = check_usable(m)
            if not st["usable"]:
                continue
            out.append(m)
        return out

    def limit_of(self, m: ModelConfig) -> int:
        return int(self.max_concurrency.get(m.id, 1))

    def _has_room(self, m: ModelConfig) -> bool:
        return self._inflight.get(m.id, 0) < self.limit_of(m)

    def pick(self) -> ModelConfig | None:
        """挑一个可用且未满载的成员。没有则返回 None（调用方应排队等待）。

        用 nginx 式**平滑加权轮转**：权重 3:1 时分配序列是 A,A,A,B,A,A,A,B，
        既保证比例又不会连续把活全压给同一个成员。
        """
        ms = self.members()
        if not ms:
            return None
        free = [m for m in ms if self._has_room(m)]
        if not free:
            return None
        total = sum(max(0.0, m.pool_weight) for m in free)
        if total <= 0:
            chosen = free[0]
        else:
            for m in free:
                self._current[m.id] = self._current.get(m.id, 0.0) + max(0.0, m.pool_weight)
            chosen = max(free, key=lambda m: self._current.get(m.id, 0.0))
            self._current[chosen.id] = self._current.get(chosen.id, 0.0) - total
        self._inflight[chosen.id] = self._inflight.get(chosen.id, 0) + 1
        return chosen

    def release(self, m: ModelConfig) -> None:
        self._inflight[m.id] = max(0, self._inflight.get(m.id, 1) - 1)

    def status(self) -> dict:
        ms = self.members()
        fams = {}
        for m in ms:
            fams.setdefault(model_family(m.model_id), []).append(m.name)
        return {
            "members": [{"id": m.id, "name": m.name, "base_url": m.base_url,
                         "model_id": m.model_id, "weight": m.pool_weight,
                         "family": model_family(m.model_id),
                         "inflight": self._inflight.get(m.id, 0),
                         "limit": self.limit_of(m),
                         "num_ctx": (m.extra or {}).get("num_ctx")} for m in ms],
            "families": fams,
            "mixed_family": len(fams) > 1,
        }
