"""检索层：四路召回 + RRF 融合 + 强制 ACL。

四路（spec §7.4）：
  1. 关键词  FTS5（预写入字符二元组）      —— 精确术语、编号、金额
  2. 向量    bge-m3 1024 维 fp16 mmap      —— 语义近似
  3. 图谱    命中块 -> 节点 -> 邻居 -> 关联块 —— 实体关系补全
  4. Wiki    人工校正层，向量加权 1.8x     —— 人工修正过的内容

**每一路都必须过 scope 过滤**（§7.4.1）。漏掉任何一路就是一条泄露路径。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

import kb.llm as llm

from kbplatform import fts
from kbplatform.scopes import (load_scopes, load_restricted, restricted_predicate,
                               visible_predicate)
from kbplatform.vectors import VectorStore


@dataclass
class Hit:
    chunk_id: int
    file_path: str
    share: str
    rel_path: str
    text: str
    locator: str
    score: float
    matched_by: list[str] = field(default_factory=list)
    restricted: bool = False
    document_id: int | None = None


def _rrf(rank_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for rl in rank_lists:
        for rank, item in enumerate(rl, 1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


class Retriever:
    def __init__(self, conn: sqlite3.Connection, fts_path: str, vs: VectorStore,
                 embed_model: str = "bge-m3", wiki_boost: float = 1.8):
        self.conn = conn
        self.fts_path = fts_path
        self.vs = vs
        self.embed_model = embed_model
        self.wiki_boost = wiki_boost
        self._wiki_ids: list[int] | None = None

    # ------------------------------------------------------------ 元数据
    def chunk_meta(self, chunk_ids: list[int]) -> dict[int, dict]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT ch.id, ch.file_path, ch.text, ch.locator,
                       c.share, c.rel_path, c.id
                FROM chunks ch JOIN catalog c ON c.path = ch.file_path
                WHERE ch.id IN ({marks})""", chunk_ids).fetchall()
        return {r[0]: {"file_path": r[1], "text": r[2], "locator": r[3],
                       "share": r[4], "rel_path": r[5], "document_id": r[6]}
                for r in rows}

    def _allowed_chunk_ids(self, user_id: int | None) -> set[int] | None:
        """返回允许的 chunk_id 集合。None 表示不限制（管理员/内部调用）。"""
        if user_id is None:
            return None
        scopes = load_scopes(self.conn, user_id)
        pred, params = visible_predicate(user_id, "c", scopes)
        rows = self.conn.execute(
            f"""SELECT ch.id FROM chunks ch JOIN catalog c ON c.path = ch.file_path
                WHERE {pred}""", params).fetchall()
        return {r[0] for r in rows}

    # ------------------------------------------------------------ 单路召回
    def _keyword(self, query: str, topk: int) -> list[int]:
        if fts.needs_vector_fallback(query):
            return []
        res = fts.search(self.fts_path, query, topk)
        return [cid for cid, _ in res]

    def _vector(self, query: str, topk: int) -> list[int]:
        vecs = llm.embed([query], model=self.embed_model)
        if not vecs:
            return []
        return [cid for cid, _ in self.vs.search(vecs[0], topk)]

    def _graph(self, seed_ids: list[int], hops: int = 1, limit: int = 40) -> list[int]:
        if not seed_ids:
            return []
        marks = ",".join("?" * len(seed_ids))
        nodes = self.conn.execute(
            f"SELECT DISTINCT src FROM edges WHERE chunk_id IN ({marks}) "
            f"UNION SELECT DISTINCT dst FROM edges WHERE chunk_id IN ({marks})",
            seed_ids + seed_ids).fetchall()
        node_ids = [r[0] for r in nodes]
        if not node_ids:
            return []
        nm = ",".join("?" * len(node_ids))
        nbrs = self.conn.execute(
            f"""SELECT DISTINCT src FROM edges WHERE dst IN ({nm})
                UNION SELECT DISTINCT dst FROM edges WHERE src IN ({nm})""",
            node_ids + node_ids).fetchall()
        nbr_ids = [r[0] for r in nbrs if r[0] not in set(node_ids)]
        if not nbr_ids:
            return []
        bm = ",".join("?" * len(nbr_ids))
        # {bm} 在 SQL 里出现两次（src IN / dst IN），参数也必须给两遍，
        # 否则 sqlite 报 "Incorrect number of bindings supplied"（曾导致 /query 随机 500）。
        rows = self.conn.execute(
            f"""SELECT DISTINCT chunk_id FROM edges
                WHERE chunk_id IS NOT NULL AND (src IN ({bm}) OR dst IN ({bm}))
                LIMIT ?""", nbr_ids + nbr_ids + [limit]).fetchall()
        return [r[0] for r in rows]

    def _wiki_ids_for(self, query: str, topk: int) -> list[int]:
        """wiki 人工校正层。wiki_pages 的 id 段位与 chunk id 不重叠（>= 1_000_000）。"""
        rows = self.conn.execute(
            "SELECT id, title, body FROM wiki_pages").fetchall()
        if not rows:
            return []
        import numpy as np
        texts = [f"{r[1] or ''}\n{r[2] or ''}" for r in rows]
        vecs = llm.embed(texts, model=self.embed_model)
        if not vecs:
            return []
        M = np.asarray(vecs, dtype=np.float32)
        M /= np.linalg.norm(M, axis=1, keepdims=True).clip(1e-9)
        qv = np.asarray(llm.embed([query], model=self.embed_model)[0], dtype=np.float32)
        qv /= max(np.linalg.norm(qv), 1e-9)
        sims = M @ qv
        order = np.argsort(-sims)[:topk]
        return [1_000_000 + int(i) for i in order if sims[i] > 0.30]

    # ------------------------------------------------------------ 融合
    def retrieve(self, query: str, user_id: int | None = None,
                 topk: int = 8, use_graph: bool = True,
                 use_wiki: bool = True) -> tuple[list[Hit], dict]:
        scopes = load_scopes(self.conn, user_id) if user_id is not None else []
        restricted = load_restricted(self.conn)

        k_ids = self._keyword(query, topk * 4)
        v_ids = self._vector(query, topk * 4)
        wiki_ids = self._wiki_ids_for(query, 4) if use_wiki else []

        # ACL 过滤（每一路都要过）
        allowed = self._allowed_chunk_ids(user_id)
        if allowed is not None:
            k_ids = [i for i in k_ids if i in allowed]
            v_ids = [i for i in v_ids if i in allowed]

        ranked_lists = [k_ids, v_ids]
        g_ids: list[int] = []
        if use_graph:
            g_ids = self._graph(k_ids + v_ids)
            # 图谱结果同样必须过 ACL：种子在范围内，扩展出的邻居未必在。
            # 漏掉这一步就是越权路径（chunk_meta 只 join catalog，不查 scope）。
            if allowed is not None:
                g_ids = [i for i in g_ids if i in allowed]
            ranked_lists.append(g_ids)
        if wiki_ids:
            ranked_lists.append(wiki_ids)

        fused = _rrf([rl for rl in ranked_lists if rl])

        # wiki 加权：人工校正层优先（§7.4）
        scored = []
        for cid, sc in fused:
            if cid >= 1_000_000:
                sc *= self.wiki_boost
            scored.append((cid, sc))
        scored.sort(key=lambda x: -x[1])

        if wiki_ids and not any(c >= 1_000_000 for c, _ in scored[:topk]):
            scored = scored[:max(1, topk - 1)] + [(wiki_ids[0], 0.0)]

        meta = self.chunk_meta([c for c, _ in scored if c < 1_000_000])
        wiki_meta = self._wiki_meta([c for c, _ in scored if c >= 1_000_000])

        matched = {}
        for name, ids in (("keyword", k_ids), ("vector", v_ids), ("graph", g_ids)):
            for i in ids:
                matched.setdefault(i, []).append(name)
        for cid in wiki_ids:
            matched.setdefault(cid, []).append("wiki")

        hits: list[Hit] = []
        for cid, sc in scored[:topk]:
            if cid >= 1_000_000:
                wm = wiki_meta.get(cid)
                if not wm:
                    continue
                hits.append(Hit(cid, wm["file_path"], wm["share"], wm["rel_path"],
                                wm["text"], "wiki", sc, matched.get(cid, []), False))
                continue
            m = meta.get(cid)
            if not m:
                continue
            rest = any(s.covers(m["share"], m["rel_path"]) for s in restricted)
            hits.append(Hit(cid, m["file_path"], m["share"], m["rel_path"],
                            m["text"], m["locator"], sc, matched.get(cid, []), rest,
                            m.get("document_id")))

        return hits, {"keyword": len(k_ids), "vector": len(v_ids),
                      "wiki": len(wiki_ids), "graph": len(g_ids)}

    def _wiki_meta(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        real = [i - 1_000_000 for i in ids]
        marks = ",".join("?" * len(real))
        rows = self.conn.execute(
            f"SELECT id, title, body, path, node_id FROM wiki_pages WHERE id IN ({marks})",
            real).fetchall()
        out = {}
        for r in rows:
            path = r[3] or ""
            share, rel = ("", "")
            if path:
                segs = [s for s in path.replace("/", "\\").split("\\") if s]
                if len(segs) >= 3:
                    share, rel = segs[2], "\\".join(segs[3:])
            out[r[0] + 1_000_000] = {"title": r[1], "text": r[2] or "",
                                      "file_path": path, "share": share, "rel_path": rel}
        return out
