"""知识图谱构建与实体归一。

节点类型：
  document       源文档
  knowledge_point 知识点（来自 key_points）
  entity         实体（来自 entities / relations）
  category       分类

边类型：
  document --包含--> knowledge_point
  knowledge_point --涉及--> entity
  knowledge_point --归类--> category
  entity --关系--> entity          （来自 relations，边方向可能不可靠，见 merge_edges）

实体归一：先按归一化名称精确合并，再用 bge-m3 向量做近义聚类（cosine >= 阈值），
把「某 / 某公司」这类简称合并，最后由 LLM 复核候选对。
"""
import json
from collections import defaultdict

from . import llm
from .store import norm_name

ENTITY_TYPES = ("entity",)


def _node(store, name, ntype, props=None):
    return store.upsert_node(name, ntype, props)


def _db(store):
    """兼容 Store 对象与裸 sqlite3.Connection。

    平台层（kbplatform）直接用连接，避免为了调图谱而包一层 Store。
    """
    return getattr(store, "db", store)


def build_from_extraction(store, file_path, chunk_id, obj):
    """把一次抽取结果写入图谱，返回本轮新增的 (src,dst,rel) 三元组列表。"""
    doc_id = _node(store, file_path, "document", {"name": file_path})
    triples = []

    for kp in obj.get("key_points", []):
        title = (kp.get("title") or "").strip()
        if not title:
            continue
        kp_id = _node(store, title, "knowledge_point", {
            "detail": kp.get("detail", ""),
            "category": kp.get("category", ""),
            "source": file_path,
        })
        store.add_edge(doc_id, kp_id, "包含", file_path, chunk_id)

        cat = (kp.get("category") or "").strip()
        if cat:
            c_id = _node(store, cat, "category")
            store.add_edge(kp_id, c_id, "归类", file_path, chunk_id)

        for ent in kp.get("entities", []):
            ent = (ent or "").strip()
            if not ent:
                continue
            e_id = _node(store, ent, "entity", {"source": file_path})
            store.add_edge(kp_id, e_id, "涉及", file_path, chunk_id)
            triples.append((kp_id, e_id, "涉及"))

    for rel in obj.get("relations", []):
        a = (rel.get("from") or "").strip()
        b = (rel.get("to") or "").strip()
        r = (rel.get("relation") or "").strip()
        if not (a and b and r):
            continue
        s_id = _node(store, a, "entity", {"source": file_path})
        t_id = _node(store, b, "entity", {"source": file_path})
        store.add_edge(s_id, t_id, r, file_path, chunk_id)
        triples.append((s_id, t_id, r))

    return triples


def merge_edges(store):
    """合并重复边（同一 src/dst/rel 只保留一条，权重累加）。

    增量 upsert，不做全表 DELETE+重写——那会在大图上锁表很久。
    必须保留一个代表性的 chunk_id：图谱扩展靠它把「块」和「节点」关联起来，
    置空会让 graph_expand 完全失效。
    """
    rows = _db(store).execute(
        "SELECT src,dst,rel,COUNT(*),SUM(weight),MIN(chunk_id),"
        "GROUP_CONCAT(DISTINCT file_path) "
        "FROM edges GROUP BY src,dst,rel").fetchall()
    _db(store).execute("DELETE FROM edges")
    for src, dst, rel, cnt, wsum, chunk_id, fps in rows:
        fps = (fps or "").split(",")
        _db(store).execute(
            "INSERT INTO edges(src,dst,rel,file_path,chunk_id,weight) VALUES(?,?,?,?,?,?)",
            (src, dst, rel, fps[0] if fps else None, chunk_id, float(wsum or cnt)))
    store.commit()
    return len(rows)


def canonicalize_entities(store, threshold=0.92, use_llm=True, max_pairs=40,
                          batch=2000, offset=0):
    """实体归一：向量近义聚类 -> LLM 复核。

    **分批执行**（spec Global Constraint #7）：每次最多处理 batch 个未归一实体，
    禁止把全量实体一次性送进 llm.embed —— 数十万实体的一次性稠密 M@M.T
    会到 TB 级中间矩阵，直接爆内存。跨批次的合并结果通过 nodes.merged_to
    软链累积。

    返回 {"clusters": [[名称...], ...], "merged": n, "reviewed": n, "processed": n}
    """
    import numpy as np

    rows = _db(store).execute(
        "SELECT id,name,norm,mentions FROM nodes "
        "WHERE type='entity' AND merged_to IS NULL "
        "ORDER BY mentions DESC LIMIT ? OFFSET ?", (batch, offset)).fetchall()
    if len(rows) < 2:
        return {"clusters": [], "merged": 0, "reviewed": 0, "processed": len(rows)}

    names = [r[1] for r in rows]
    vecs = llm.embed(names)
    if not vecs:
        return {"clusters": [], "merged": 0, "reviewed": 0, "processed": len(rows)}
    M = np.asarray(vecs, dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True).clip(1e-9)

    # 精确同名先合并
    by_norm = defaultdict(list)
    for r in rows:
        by_norm[r[2]].append(r)
    merged = 0
    for norm, group in by_norm.items():
        if len(group) > 1:
            keep = group[0]
            for g in group[1:]:
                _db(store).execute("UPDATE nodes SET merged_to=? WHERE id=?", (keep[0], g[0]))
                merged += 1

    # 向量近义候选对（批内计算，batch=2000 时矩阵仅 2000x2000）
    cand = []
    S = M @ M.T
    n = len(rows)
    for i in range(n):
        for j in range(i + 1, n):
            if S[i][j] >= threshold:
                cand.append((float(S[i][j]), i, j))
    cand.sort(reverse=True)

    # LLM 复核
    confirmed = []
    reviewed = 0
    if use_llm and cand:
        pairs = cand[:max_pairs]
        listing = []
        for k, (sim, i, j) in enumerate(pairs):
            listing.append(f"{k+1}. A=\"{rows[i][1]}\"  B=\"{rows[j][1]}\"  相似度={sim:.3f}")
        prompt = ("下面是企业文档中抽取的实体名称对。请判断每一对是否指向同一个实体（是简称/全称/同义）。\n"
                  "只输出 JSON：{\"pairs\":[{\"index\":1,\"same\":true},{\"index\":2,\"same\":false}]}\n\n"
                  + "\n".join(listing))
        try:
            obj, _ = llm.chat_json(prompt, schema={
                "type": "object",
                "properties": {"pairs": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}, "same": {"type": "boolean"}},
                    "required": ["index", "same"]}}},
                "required": ["pairs"],
            }, num_predict=1200, temperature=0.1)
            for p in obj.get("pairs", []):
                reviewed += 1
                if p.get("same"):
                    confirmed.append(p["index"] - 1)
        except Exception as e:
            print(f"    [实体复核 LLM 失败] {e}")

    clusters = []
    for idx in confirmed:
        sim, i, j = cand[idx]
        clusters.append([rows[i][1], rows[j][1]])

    # 应用合并：把 name B 的边迁到 name A
    for a, b in clusters:
        na, nb = norm_name(a), norm_name(b)
        ia = _db(store).execute("SELECT id FROM nodes WHERE norm=? AND type='entity'", (na,)).fetchone()
        ib = _db(store).execute("SELECT id FROM nodes WHERE norm=? AND type='entity'", (nb,)).fetchone()
        if not ia or not ib or ia[0] == ib[0]:
            continue
        keep, drop = ia[0], ib[0]
        _db(store).execute("UPDATE edges SET src=? WHERE src=?", (keep, drop))
        _db(store).execute("UPDATE edges SET dst=? WHERE dst=?", (keep, drop))
        _db(store).execute("UPDATE nodes SET merged_to=? WHERE id=?", (keep, drop))
        merged += 1

    store.commit()
    return {"clusters": clusters, "merged": merged, "reviewed": reviewed,
            "processed": len(rows)}


def prune_orphans(store):
    """清理没有边的孤立节点与指向已删节点的边。

    重跑提炼会产生上一轮的知识点节点（标题微变就是新节点），不清掉会让
    图谱节点数虚高。全库重建的知识点/实体节点同理。
    """
    db = _db(store)
    db.execute("DELETE FROM edges WHERE src NOT IN (SELECT id FROM nodes) "
               "OR dst NOT IN (SELECT id FROM nodes)")
    db.execute("DELETE FROM nodes WHERE type='knowledge_point' "
               "AND id NOT IN (SELECT src FROM edges) AND id NOT IN (SELECT dst FROM edges)")
    db.execute("DELETE FROM nodes WHERE type='entity' "
               "AND id NOT IN (SELECT src FROM edges) AND id NOT IN (SELECT dst FROM edges)")
    db.execute("DELETE FROM nodes WHERE type='category' "
               "AND id NOT IN (SELECT src FROM edges) AND id NOT IN (SELECT dst FROM edges)")
    db.commit()
    return dict(
        nodes=db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        edges=db.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    )


def recount_mentions(store):
    """按 knowledge 表重算实体/知识点提及次数。

    upsert_node 里的 mentions 是累加的，重复摄取或合并实体会让它虚高，
    这里从原始抽取结果重算，保证 mentions 可信。
    """
    from collections import Counter
    cnt = Counter()
    for (kps,) in _db(store).execute("SELECT key_points FROM knowledge"):
        for kp in json.loads(kps):
            for e in kp.get("entities", []):
                cnt[norm_name(e)] += 1
    _db(store).execute("UPDATE nodes SET mentions=0")
    for norm, c in cnt.items():
        _db(store).execute("UPDATE nodes SET mentions=? WHERE norm=? AND type='entity'", (c, norm))
    # 知识点提及次数 = 关联它的实体边数
    _db(store).execute(
        "UPDATE nodes SET mentions=(SELECT COUNT(*) FROM edges WHERE dst=nodes.id OR src=nodes.id) "
        "WHERE type='knowledge_point'")
    store.commit()
    return len(cnt)


def stats(store):
    def one(q):
        return _db(store).execute(q).fetchone()[0]
    return {
        "files": one("SELECT COUNT(*) FROM files"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "knowledge": one("SELECT COUNT(*) FROM knowledge"),
        "nodes": one("SELECT COUNT(*) FROM nodes"),
        "nodes_active": one("SELECT COUNT(*) FROM nodes WHERE merged_to IS NULL"),
        "edges": one("SELECT COUNT(*) FROM edges"),
        "by_type": dict(_db(store).execute(
            "SELECT type, COUNT(*) FROM nodes GROUP BY type").fetchall()),
        "top_rels": _db(store).execute(
            "SELECT rel, COUNT(*) c FROM edges GROUP BY rel ORDER BY c DESC LIMIT 10").fetchall(),
    }
