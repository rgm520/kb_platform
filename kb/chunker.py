"""分块策略：按文档结构切分，保留定位信息，控制每块 token 量。

原则：
- 以段落/页/幻灯片为最小语义单元，不跨结构硬切
- 小块合并、大块拆分，目标 2500-4000 字符，重叠 ~15%
- 保留每个块覆盖的原文定位 (p3, slide5, sheet:xxx)，便于答案回溯引用
"""
import re

CHINESE = re.compile(r"[一-鿿]")
TOKEN = re.compile(r"[A-Za-z0-9]+|[一-鿿]|[^\sA-Za-z0-9一-鿿]")


def estimate_tokens(text):
    """粗略 token 估计：中文按 1 字 1 token，西文按 4 字符 1 token。"""
    cjk = len(CHINESE.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 3.2) + 1


def _split_long(text, max_chars):
    """超长段落按句子边界二次切分。"""
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?<=[。；！？;!?\n])", text)
    out, cur = [], ""
    for p in parts:
        if len(cur) + len(p) <= max_chars:
            cur += p
        else:
            if cur:
                out.append(cur)
            while len(p) > max_chars:
                out.append(p[:max_chars])
                p = p[max_chars:]
            cur = p
    if cur:
        out.append(cur)
    return out


def chunk_blocks(blocks, target=3000, max_chars=6000, min_chars=200):
    """blocks: parsers.Block 列表 → chunk dict 列表。"""
    segs = []
    for b in blocks:
        t = (b.text or "").strip()
        if not t:
            continue
        if len(t) > max_chars:
            for i, piece in enumerate(_split_long(t, max_chars)):
                segs.append({"text": piece, "locator": f"{b.locator}#{i}", "kind": b.kind})
        else:
            segs.append({"text": t, "locator": b.locator, "kind": b.kind})

    chunks = []
    cur, cur_len, cur_locs = [], 0, []
    for s in segs:
        if cur_len + len(s["text"]) > max_chars and cur:
            chunks.append(_pack(cur, cur_locs, len(chunks)))
            # 重叠：保留尾部若干块
            keep, kl = [], 0
            for seg in reversed(cur):
                if kl + len(seg["text"]) > target * 0.15:
                    break
                keep.insert(0, seg)
                kl += len(seg["text"])
            cur, cur_len, cur_locs = list(keep), kl, [x["locator"] for x in keep]
        cur.append(s)
        cur_len += len(s["text"])
        cur_locs.append(s["locator"])
        if cur_len >= target:
            chunks.append(_pack(cur, cur_locs, len(chunks)))
            cur, cur_len, cur_locs = [], 0, []
    if cur and cur_len >= min_chars:
        chunks.append(_pack(cur, cur_locs, len(chunks)))
    elif cur:
        if chunks:
            chunks[-1]["text"] += "\n" + "\n".join(x["text"] for x in cur)
            chunks[-1]["locator"] += "," + ",".join(cur_locs)
        else:
            chunks.append(_pack(cur, cur_locs, 0))
    return chunks


def _pack(segs, locs, idx):
    text = "\n".join(s["text"] for s in segs)
    return {"idx": idx, "text": text, "locator": ",".join(locs),
            "tokens": estimate_tokens(text), "nseg": len(segs)}
