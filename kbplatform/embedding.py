"""带单次输入上限兜底的向量化（Task 0.2）。

背景（实测）：embedding 服务对**单次输入**有 2,048 token 上限——一个 3,156 字符的
块单独就失败，而 12 块 / 20,677 token 的整批却成功（限制是单输入 token 数，不是
请求体大小）。indexer 原先把一篇文件的所有块打包成一次 `llm.embed(texts)`，一个
超限块就让整篇向量作废，且失败被 `if vecs:` 静默跳过。

本模块提供降级路径并**显式失败**：
    整批 → 整批失败时逐条 → 单条仍失败时按字符二分递归重试
    → 二分到最小仍失败则该位置返回 None，并产出面向人读的警告。

二分成功时把子片段向量取均值合并回一个向量，保持「与 texts 等长、失败位 None」
的形状，调用方无需感知分片。
"""
from __future__ import annotations

import kb.llm as llm

MAX_INPUT_TOKENS = 2048   # embedding 服务单次输入上限（实测）
CHARS_PER_TOKEN = 1.6     # 中文为主的粗略估算（≈2048 token ≈ 3276 字符），
                          # 仅供把 token 上限换算成字符做按字符预判，并非硬保证
MIN_SPLIT_CHARS = 8       # 二分到此长度仍失败就放弃（再切会失去语义，几乎必然是服务侧问题）
_MAX_ERR_CHARS = 120      # 警告里附带的原始错误文本截断长度


def _vectors_ok(vecs, expect: int) -> bool:
    """条数与形状都对才算有效——显式区分「空结果/缺条数」与真正的成功。"""
    if not isinstance(vecs, list) or len(vecs) != expect:
        return False
    return all(isinstance(v, (list, tuple)) and len(v) > 0 for v in vecs)


def _attempt(texts: list[str], model: str) -> tuple[list[list[float]] | None, str]:
    """嵌入一次；成功返回 (向量列表, "")，任何失败返回 (None, 错误摘要)。绝不抛出。

    不挑异常类型：连接失败、HTTP 413、超时……都交由上层降级，判定只看
    「有没有拿到形状正确的向量」。
    """
    try:
        vecs = llm.embed(list(texts), model=model)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:_MAX_ERR_CHARS]
    if _vectors_ok(vecs, len(texts)):
        return [list(v) for v in vecs], ""
    got = f"{len(vecs)} 条" if isinstance(vecs, list) else type(vecs).__name__
    return None, f"返回条数/形状不符（期望 {len(texts)} 条，得到 {got}）"


def _mean_of(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors)]


def _split_embed(text: str, model: str) -> list[list[float] | None]:
    """对一个已确认单条失败的文本二分重试。

    返回长度为 1 的列表：子片段全部成功时是 [合并向量]（子片段向量取均值，
    还原成一整块一个向量），否则 [None]。
    """
    mid = len(text) // 2
    pieces: list[list[float] | None] = []
    for part in (text[:mid], text[mid:]):
        v, _err = _attempt([part], model)
        if v is not None:
            pieces.append(v[0])
        elif len(part) < MIN_SPLIT_CHARS:
            pieces.append(None)   # 太短没意义，直接判此子片段失败
        else:
            pieces.extend(_split_embed(part, model))
    usable = [p for p in pieces if p is not None]
    if len(usable) == len(pieces) and all(len(p) == len(usable[0]) for p in usable):
        return [_mean_of(usable)]
    return [None]


def embed_chunks(texts: list[str],
                 model: str = "bge-m3") -> tuple[list[list[float] | None], list[str]]:
    """逐块向量化；对超出单次输入上限的块自动二分重试。

    返回 (vectors, warnings)：
      * vectors 与 texts 等长，救不回来的位置是 None（可查的失败痕迹）；
      * warnings 面向人读（会被拼进 catalog.status_detail），例如
        "2 个分块向量化失败：单次输入超出 2048 token 上限（embedding 返回：…）"。
        全部成功时为空列表，调用方可据此保持 status_detail=None。
    """
    vectors: list[list[float] | None] = [None] * len(texts)
    if not texts:
        return vectors, []

    # 1) 先整批——正常文档一次成功，路径与性能和旧行为一致
    whole, _err = _attempt(list(texts), model)
    if whole is not None:
        return whole, []

    # 2) 整批失败 → 逐条：只有坏块会掉进这里，好块单条即成功
    last_err = ""
    for i, text in enumerate(texts):
        v, err = _attempt([text], model)
        if v is not None:
            vectors[i] = v[0]
            continue
        last_err = err or last_err
        # 3) 单条仍失败 → 二分递归。MAX_INPUT_TOKENS*CHARS_PER_TOKEN 只是预判
        #    换算（实测限制按 token 算），所以二分不看长度，只看服务是否仍拒绝。
        if len(text) < MIN_SPLIT_CHARS:
            continue
        sub = _split_embed(text, model)
        if sub and sub[0] is not None:
            vectors[i] = sub[0]

    warnings: list[str] = []
    n_failed = sum(1 for v in vectors if v is None)
    if n_failed:
        msg = (f"{n_failed} 个分块向量化失败："
               f"单次输入超出 {MAX_INPUT_TOKENS} token 上限")
        if last_err:
            msg += f"（embedding 返回：{last_err}）"
        warnings.append(msg)
    return vectors, warnings
