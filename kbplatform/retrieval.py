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
from kbplatform.paths import split_share
from kbplatform.scopes import (load_scopes, load_restricted, restricted_predicate,
                               unified_predicate)
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
        pred, params = unified_predicate(self.conn, user_id, "c")
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

    def _allowed_paths(self, user_id: int | None) -> set[tuple[str, str]] | None:
        """返回允许的 (share, rel_path) 集合。None 表示不限制（管理员/内部调用）。

        与 _allowed_chunk_ids 用**同一个** unified_predicate（老范围 ∪ grants）：
        wiki 人工校正层的可见性锚点是它的源文件路径（wiki_pages.path → catalog 行），
        所以把判定对象换成 catalog 行本身，语义与其它三路（share/rel_path）一致，
        不另写一套授权判定。
        """
        if user_id is None:
            return None
        pred, params = unified_predicate(self.conn, user_id)
        rows = self.conn.execute(
            f"SELECT c.share, c.rel_path FROM catalog c WHERE {pred}",
            params).fetchall()
        return {(r[0], r[1]) for r in rows}

    def _wiki_ids_for(self, query: str, topk: int,
                      allowed_paths: set[tuple[str, str]] | None = None,
                      acl_dropped: list[int] | None = None) -> list[int]:
        """wiki 人工校正层。候选 id = 1_000_000 + wiki_pages.id（数据库主键）。

        编码/解码两侧必须自洽：解码在 _wiki_meta（``id IN (rid - 1_000_000)``），
        retrieve 里的分段判断（cid >= 1_000_000）不变量须保住——wiki_pages.id
        从 1 起，编码后恒 >= 1_000_001。
        2026-10-25 修复：原来按结果数组序号编码，解码侧却按主键查——第一个
        候选永远查不到 meta（被查成 id=0），靠后的串行，1.8x 加权从未产生过
        可用 hit（wiki 通道因此一直是"死"的）。

        ACL（§7.4.1，四路一个都不能少）：allowed_paths 非 None（普通用户）时，
        只保留源文件路径落在可见范围内的候选；path 在 catalog 里解析不到
        （share/rel_path 无法判定）的一律剔除——不得放宽。管理员（None）不受限。
        在嵌入之前过滤：范围外的候选连向量都不算，也不进 trace。

        acl_dropped 是可选出参：被可见性过滤掉的候选条数（轨迹据此显性化
        "这一路被权限压下去了"，与 k/v/g 三路同等待遇）。
        """
        rows = self.conn.execute(
            """SELECT w.id, w.title, w.body, c.share, c.rel_path
               FROM wiki_pages w LEFT JOIN catalog c ON c.path = w.path""").fetchall()
        if allowed_paths is not None:
            before = len(rows)
            rows = [r for r in rows
                    if r[3] is not None and (r[3], r[4]) in allowed_paths]
            if acl_dropped is not None:
                acl_dropped.append(before - len(rows))
        if not rows:
            return []
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
        return [1_000_000 + int(rows[int(i)][0]) for i in order if sims[i] > 0.30]

    # ------------------------------------------------------------ 融合
    def retrieve(self, query: str, user_id: int | None = None,
                 topk: int = 8, use_graph: bool = True,
                 use_wiki: bool = True,
                 trace: dict | None = None) -> tuple[list[Hit], dict]:
        scopes = load_scopes(self.conn, user_id) if user_id is not None else []
        restricted = load_restricted(self.conn)

        k_ids = self._keyword(query, topk * 4)
        v_ids = self._vector(query, topk * 4)

        # ACL 过滤（每一路都要过）。wiki 路的授权锚点是源文件路径
        # （wiki_pages.path -> catalog 的 share/rel_path），见 _allowed_paths。
        allowed = self._allowed_chunk_ids(user_id)
        allowed_paths = self._allowed_paths(user_id) if use_wiki else None
        acl_filtered = False
        wiki_acl_dropped: list[int] = []
        wiki_ids = (self._wiki_ids_for(query, 4, allowed_paths, wiki_acl_dropped)
                    if use_wiki else [])

        if allowed is not None:
            # 记录有无被过滤掉的召回结果：轨迹里要能看出"是不是被权限压下去了"
            k_before, v_before = len(k_ids), len(v_ids)
            k_ids = [i for i in k_ids if i in allowed]
            v_ids = [i for i in v_ids if i in allowed]
            acl_filtered = len(k_ids) < k_before or len(v_ids) < v_before
        # wiki 候选被可见性过滤同样要在轨迹里显性化："是不是被权限压下去了"
        # （放在 k/v 统计之后：上面是对 acl_filtered 的整体赋值，先置会被覆盖）
        if wiki_acl_dropped and wiki_acl_dropped[0]:
            acl_filtered = True

        ranked_lists = [k_ids, v_ids]
        g_ids: list[int] = []
        if use_graph:
            g_ids = self._graph(k_ids + v_ids)
            # 图谱结果同样必须过 ACL：种子在范围内，扩展出的邻居未必在。
            # 漏掉这一步就是越权路径（chunk_meta 只 join catalog，不查 scope）。
            if allowed is not None:
                g_before = len(g_ids)
                g_ids = [i for i in g_ids if i in allowed]
                if len(g_ids) < g_before:
                    acl_filtered = True
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

        if trace is not None:
            # 轨迹只记 id / 名次 / 分数 / 命中通道，绝不记原文或文件路径。
            # 这是"不准"可度量的原始依据：管理员从这里看这次到底召回了什么。
            # fused 有写死的上限：默认 topk=8，50 条给了"谁把谁压下去"足够
            # 的排查余量，又让每条轨迹的 JSON 体积有界（该表有 30 天保留期，
            # 但不清理体积上限的话一条就能很大，DELETE 也救不回来单条大小）。
            trace["routes"] = {"keyword": list(k_ids), "vector": list(v_ids),
                               "graph": list(g_ids), "wiki": list(wiki_ids)}
            trace["fused"] = [{"chunk_id": int(c), "score": float(s),
                               "routes": matched.get(c, [])} for c, s in scored[:50]]
            trace["acl_filtered"] = acl_filtered

        return hits, {"keyword": len(k_ids), "vector": len(v_ids),
                      "wiki": len(wiki_ids), "graph": len(g_ids)}

    def _wiki_meta(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        real = [i - 1_000_000 for i in ids]       # 解码 = 1_000_000 + wiki_pages.id
        marks = ",".join("?" * len(real))
        rows = self.conn.execute(
            f"""SELECT w.id, w.title, w.body, w.path, w.node_id, c.share, c.rel_path
                FROM wiki_pages w LEFT JOIN catalog c ON c.path = w.path
                WHERE w.id IN ({marks})""", real).fetchall()
        out = {}
        for r in rows:
            path = r[3] or ""
            # share/rel_path 以 catalog 行为权威（与 chunk_meta / 可见性判定同一来源）。
            # 页没有 catalog 行（已下架/未扫描，管理员仍可见）时，才退回现成的
            # UNC 拆解 split_share（此前手工 segs[2] 对
            # \\192.0.2.19\信息化管理\AI知识库\x.md 会拆出 share="AI知识库"，错位）。
            if r[5] is not None:
                share, rel = r[5], r[6]
            else:
                share, rel = split_share(path)
            out[r[0] + 1_000_000] = {"title": r[1], "text": r[2] or "",
                                      "file_path": path, "share": share, "rel_path": rel}
        return out
