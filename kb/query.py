"""混合检索 + 图谱问答。

召回三路：
  1) 向量检索   bge-m3 语义相似
  2) 关键词检索 BM25（中文用字符二元组，无需额外分词器）
  3) 图谱扩展   命中块的邻居节点（文档 → 知识点 → 实体 → 实体）

融合用 RRF（倒数名次融合），对不同量纲的分数天然鲁棒。

重要设计：图谱不是主检索通道，而是答案组织与补全工具。
"""
import os, sys, json, math, argparse, re
from collections import defaultdict

import numpy as np

from . import llm
from .store import Store

CJK = re.compile(r"[一-鿿]")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")


def tokenize(text):
    """中文按字符二元组 + 单字，英文/数字按词。"""
    toks = []
    for m in WORD.finditer(text.lower()):
        toks.append(m.group())
    cj = CJK.findall(text)
    toks.extend(cj)
    for a, b in zip(cj, cj[1:]):
        toks.append(a + b)
    return toks


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.df = defaultdict(int)
        self.tfs = []
        self.lens = []
        for d in docs:
            tf = defaultdict(int)
            for t in tokenize(d):
                tf[t] += 1
            self.tfs.append(tf)
            self.lens.append(len(d))
            for t in tf:
                self.df[t] += 1
        self.avgdl = (sum(self.lens) / self.N) if self.N else 1.0
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in self.df.items()}

    def search(self, query, topk=20):
        qt = tokenize(query)
        scores = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.lens[i] or 1
            for t in qt:
                f = tf.get(t)
                if not f:
                    continue
                idf = self.idf.get(t, 0)
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return scores[:topk]


def rrf(rank_lists, k=60):
    fused = defaultdict(float)
    for rl in rank_lists:
        for rank, item in enumerate(rl, 1):
            fused[item] += 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


class Retriever:
    def __init__(self, db_path, wiki_dir=None):
        self.store = Store(db_path)
        self.chunks = self.store.chunk_map()          # id -> (path, idx, text, locator)
        self.order = list(self.chunks.keys())
        self.docs = [self.chunks[i][2] for i in self.order]
        self.wiki = []                                 # (path, text) —— 人工可编辑层
        if wiki_dir and os.path.isdir(wiki_dir):
            for root, _, files in os.walk(wiki_dir):
                for fn in sorted(files):
                    if not fn.endswith(".md"):
                        continue
                    p = os.path.join(root, fn)
                    try:
                        txt = open(p, encoding="utf-8").read()
                    except Exception:
                        continue
                    if len(txt.strip()) < 40:
                        continue
                    wid = 1_000_000 + len(self.wiki)
                    self.wiki.append((p, txt))
                    self.chunks[wid] = (p, 0, txt, "wiki")
                    self.order.append(wid)
                    self.docs.append(txt)
        self.mat, self.vids = self.store.load_matrix()
        self.vpos = {cid: i for i, cid in enumerate(self.vids)}
        self.bm25 = BM25(self.docs) if self.docs else None
        self._adj = None
        self._wiki_vecs = None

    def wiki_vector_index(self):
        """wiki 页面按需向量化（懒加载，只算一次）。"""
        if self._wiki_vecs is not None or not self.wiki:
            return self._wiki_vecs
        vecs = llm.embed([t for _, t in self.wiki])
        M = np.asarray(vecs, dtype=np.float32)
        M /= np.linalg.norm(M, axis=1, keepdims=True).clip(1e-9)
        self._wiki_vecs = (M, [1_000_000 + i for i in range(len(self.wiki))])
        return self._wiki_vecs

    def adjacency(self):
        if self._adj is None:
            adj = defaultdict(set)
            for s, d, r, fp, cid in self.store.db.execute(
                    "SELECT src,dst,rel,file_path,chunk_id FROM edges"):
                adj[s].add(d)
                adj[d].add(s)
            self._adj = adj
        return self._adj

    def vector_search(self, qvec, topk=20):
        q = np.asarray(qvec, dtype=np.float32)
        q /= max(np.linalg.norm(q), 1e-9)
        hits = []
        if self.mat.size:
            sims = self.mat @ q
            idx = np.argsort(-sims)[:topk]
            hits += [(self.vids[i], float(sims[i])) for i in idx if sims[i] > 0.01]
        wv = self.wiki_vector_index()
        if wv is not None:
            M, ids = wv
            sims = M @ q
            idx = np.argsort(-sims)[:min(topk, 8)]
            hits += [(ids[i], float(sims[i])) for i in idx if sims[i] > 0.30]
        hits.sort(key=lambda x: -x[1])
        return hits[:topk]

    def keyword_search(self, q, topk=20):
        if not self.bm25:
            return []
        return [(self.order[i], s) for s, i in self.bm25.search(q, topk)]

    def graph_expand(self, chunk_ids, hops=1, limit=40):
        """把命中的块关联到节点，再拉邻居知识点的原文块。"""
        adj = self.adjacency()
        node_ids = set()
        for cid in chunk_ids:
            for nid, rel, fp, c in self.store.db.execute(
                    "SELECT id,rel,file_path,chunk_id FROM edges WHERE chunk_id=?", (cid,)):
                node_ids.add(nid)
        out = set()
        frontier = set(node_ids)
        for _ in range(hops):
            nxt = set()
            for nid in frontier:
                for m in adj.get(nid, ()):  # noqa
                    if m not in node_ids:
                        nxt.add(m)
            out |= nxt
            frontier = nxt
            if len(out) > limit:
                break
        extra = []
        for nid in list(out)[:limit]:
            for cid in self.store.db.execute(
                    "SELECT chunk_id FROM edges WHERE src=? OR dst=?", (nid, nid)):
                if cid[0] not in chunk_ids:
                    extra.append(cid[0])
        return list(dict.fromkeys(extra))

    def retrieve(self, query, topk=8):
        qv = llm.embed([query])
        qv = qv[0] if qv else None
        vhits = self.vector_search(qv, topk * 3) if qv else []
        khits = self.keyword_search(query, topk * 3)
        vlist = [cid for cid, _ in vhits]
        klist = [cid for cid, _ in khits]
        fused = rrf([vlist, klist])

        # wiki 是人工校正层，给它加权并保证至少进 1 个，
        # 否则人工改过的页面永远检索不到，等于白做。
        boosted = []
        for cid, sc in fused:
            boosted.append((cid, sc * (1.8 if cid >= 1_000_000 else 1.0)))
        boosted.sort(key=lambda x: -x[1])

        base = [cid for cid, _ in boosted[:topk]]
        wiki_hits = [cid for cid, _ in boosted if cid >= 1_000_000]
        if wiki_hits and not any(c >= 1_000_000 for c in base):
            base = base[:max(1, topk - 1)] + [wiki_hits[0]]
        extra = self.graph_expand(base, hops=1, limit=12)
        for cid in extra:
            if len(base) >= topk + len(extra):
                break
            base.append(cid)
        return base, {"vector": len(vlist), "keyword": len(klist),
                      "graph_extra": len(extra),
                      "wiki": sum(1 for c in base if c >= 1_000_000)}


ANSWER_PROMPT = """你是企业知识库问答助手。基于下面的资料回答问题。

要求：
1. 答案必须严格基于资料，资料里没有的不要编造；
2. 若资料不足，明确说明缺什么；
3. 引用时用 [片段N] 标注依据；
4. 若资料之间存在冲突或不一致，指出冲突；
5. answer 用中文，直接给结论。

资料：
{ctx}

问题：{q}"""


def answer(store, retriever, query, topk=8, use_graph=True):
    if use_graph:
        ids, stats_ = retriever.retrieve(query, topk)
    else:
        v = retriever.vector_search(llm.embed([query])[0], topk)
        ids = [c for c, _ in v]
        stats_ = {"vector": len(ids), "keyword": 0, "graph_extra": 0}

    ctx_parts, meta = [], []
    for i, cid in enumerate(ids, 1):
        path, idx, text, loc = retriever.chunks[cid]
        ctx_parts.append(f"[片段{i}] 来源: {os.path.basename(path)} 位置: {loc}\n{text}")
        meta.append({"片段": i, "文件": os.path.basename(path), "位置": loc, "chunk": idx})

    ctx = "\n\n".join(ctx_parts)
    obj, llm_meta = llm.chat_json(
        ANSWER_PROMPT.format(ctx=ctx, q=query),
        schema={
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "integer"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer", "citations", "evidence", "gaps"],
        },
        num_predict=3000, temperature=0.1)
    return obj, meta, stats_, llm_meta


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--db", default="D:/kb-platform/kb/data/kb.db")
    ap.add_argument("--wiki", default=None, help="wiki 目录（人工校正层，参与检索）")
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--no-graph", action="store_true")
    a = ap.parse_args()

    r = Retriever(a.db, wiki_dir=a.wiki)
    obj, meta, stats_, lm = answer(r.store, r, a.question, a.topk, use_graph=not a.no_graph)
    print("=" * 78)
    print("问题:", a.question)
    print("=" * 78)
    print("答案:", obj.get("answer"))
    print("\n引用片段:", obj.get("citations"))
    for e in obj.get("evidence", []):
        print("  依据:", e)
    if obj.get("gaps"):
        print("\n资料缺口:")
        for g in obj["gaps"]:
            print("  -", g)
    print(f"\n召回: 向量 {stats_['vector']} / 关键词 {stats_['keyword']} / 图谱补充 {stats_['graph_extra']}"
          f"  | LLM {lm.get('wall', 0):.1f}s")
    print("\n片段明细:")
    for m in meta:
        print(f"  [片段{m['片段']}] {m['文件']} @ {m['位置']}")


if __name__ == "__main__":
    main()
