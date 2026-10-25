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

# 服务明确说"输入过长"的证据（HTTP 413 / 超长提示）。只有这些字样出现，
# 警告里才允许断言"超出 token 上限"；认不出的错误一律中性表述。
_TOO_LONG_MARKERS = ("413", "exceeds the context length", "request entity too large")


def _vectors_ok(vecs, expect: int) -> bool:
    """条数与形状都对才算有效——显式区分「空结果/缺条数」与真正的成功。"""
    if not isinstance(vecs, list) or len(vecs) != expect:
        return False
    return all(isinstance(v, (list, tuple)) and len(v) > 0 for v in vecs)


def _error_kind(exc: Exception) -> str:
    """从异常文本辨认服务的「单次输入过长」证据；认不出归为中性 service。

    生产实测服务返回 HTTP 413（HTTPError 文本含 "413"/"Request Entity Too Large"），
    服务体提示 "the input length exceeds the context length"。
    """
    text = str(exc).lower()
    if any(m in text for m in _TOO_LONG_MARKERS):
        return "too_long"
    return "service"   # 连接失败、超时等传输类错误，无法可靠断言具体原因


def _attempt(texts: list[str], model: str) -> tuple[list[list[float]] | None, str, str]:
    """嵌入一次。成功返回 (向量列表, "", "")；失败返回 (None, kind, 人读摘要)，绝不抛出。

    不挑异常类型：连接失败、HTTP 413、超时……都交由上层降级，判定只看
    「有没有拿到形状正确的向量」。kind 取值：
      too_long — 服务的超长输入证据（HTTP 413 / exceeds the context length）；
      shape    — 返回条数/形状不符（我们自己在调用点量出来的，可可靠分类）；
      service  — 其它异常（原因不明，只能中性转述）。
    """
    try:
        vecs = llm.embed(list(texts), model=model)
    except Exception as e:
        return None, _error_kind(e), f"{type(e).__name__}: {e}"[:_MAX_ERR_CHARS]
    if _vectors_ok(vecs, len(texts)):
        return [list(v) for v in vecs], "", ""
    got = f"{len(vecs)} 条" if isinstance(vecs, list) else type(vecs).__name__
    return None, "shape", f"期望 {len(texts)} 条，得到 {got}"


def _mean_of(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors)]


def _split_embed(text: str, model: str,
                 failures: list[tuple[str, str]]) -> list[list[float] | None]:
    """对一个已确认单条失败的文本二分重试。

    返回长度为 1 的列表：子片段全部成功时是 [合并向量]（子片段向量取均值，
    还原成一整块一个向量），否则 [None]。
    拆分过程中每次失败的 (kind, detail) 都追加进传入的 accumulator——它是
    调用方按位置私有的列表；调用方只在该位置最终仍失败时才把记录并入归因。
    """
    mid = len(text) // 2
    pieces: list[list[float] | None] = []
    for part in (text[:mid], text[mid:]):
        v, kind, detail = _attempt([part], model)
        if v is not None:
            pieces.append(v[0])
            continue
        failures.append((kind, detail))
        if len(part) < MIN_SPLIT_CHARS:
            pieces.append(None)   # 太短没法再切，判此子片段失败
        else:
            pieces.extend(_split_embed(part, model, failures))
    usable = [p for p in pieces if p is not None]
    if len(usable) == len(pieces) and all(len(p) == len(usable[0]) for p in usable):
        return [_mean_of(usable)]
    # 有意取舍：只要有一个子片段失败就整块丢弃（不可只用一半子片段的均值冒充
    # 整块向量），该位置显式为 None 并由 failures 留下真实错误。
    return [None]


def embed_chunks(texts: list[str],
                 model: str = "bge-m3") -> tuple[list[list[float] | None], list[str]]:
    """逐块向量化；对超出单次输入上限的块自动二分重试。

    返回 (vectors, warnings)：
      * vectors 与 texts 等长，救不回来的位置是 None（可查的失败痕迹）；
      * warnings 面向人读（会被拼进 catalog.status_detail），**按真实失败原因
        构造**：全部失败都带服务的超长证据时才断言 token 上限，例如
        "2 个分块向量化失败：单次输入超出 2048 token 上限（embedding 返回：…）"；
        条数/形状不符写明期望与实际；其余一律中性文案（如
        "1 个分块向量化失败：嵌入失败（embedding 返回：HTTPError: …）"），
        不断言没验证过的原因。全部成功时为空列表，调用方可据此保持 status_detail=None。
    """
    vectors: list[list[float] | None] = [None] * len(texts)
    if not texts:
        return vectors, []

    # 1) 先整批——正常文档一次成功，路径与性能和旧行为一致
    whole, _kind, _err = _attempt(list(texts), model)
    if whole is not None:
        return whole, []

    # 2) 整批失败 → 逐条：只有坏块会掉进这里，好块单条即成功
    # 归因规则：只把**最终仍失败**（向量为 None）的位置的错误计入 failures；
    # 靠拆分恢复成功的位置，其过程错误一律丢弃，不能污染"最后一次真实失败"。
    failures: list[tuple[str, str]] = []   # (kind, detail)，按发生顺序，仅最终失败
    for i, text in enumerate(texts):
        here: list[tuple[str, str]] = []   # 本位置的过程错误（含拆分阶段）
        v, kind, detail = _attempt([text], model)
        if v is not None:
            vectors[i] = v[0]
            continue
        here.append((kind, detail))
        # 3) 单条仍失败 → 二分递归。MAX_INPUT_TOKENS*CHARS_PER_TOKEN 只是预判
        #    换算（实测限制按 token 算），所以二分不看长度，只看服务是否仍拒绝。
        if len(text) >= MIN_SPLIT_CHARS:
            sub = _split_embed(text, model, here)
            if sub and sub[0] is not None:
                vectors[i] = sub[0]
        if vectors[i] is None:
            failures.extend(here)

    warnings: list[str] = []
    n_failed = sum(1 for v in vectors if v is None)
    if n_failed:
        warnings.append(_failure_warning(n_failed, failures))
    return vectors, warnings


def _failure_warning(n_failed: int, failures: list[tuple[str, str]]) -> str:
    """从真实失败原因构造人读警告（只含最终失败位置的错误，按发生顺序，
    引用最后一次真实失败）。

    原则：宁可少说、绝不误导——只有**每一条**失败都带着服务的超长证据时才
    说"超出 token 上限"；形状不符写明期望与实际；其余一律中性表述。
    """
    head = f"{n_failed} 个分块向量化失败"
    kinds = {kind for kind, _ in failures}
    last = failures[-1][1] if failures else "原因未知"
    if kinds == {"too_long"}:
        return (f"{head}：单次输入超出 {MAX_INPUT_TOKENS} token 上限"
                f"（embedding 返回：{last}）")
    if kinds == {"shape"}:
        return f"{head}：embedding 返回条数/形状不符（{last}）"
    return f"{head}：嵌入失败（embedding 返回：{last}）"
