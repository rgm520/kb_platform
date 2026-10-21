import sys, os, json
sys.path.insert(0, "D:/opencode")
sys.stdout.reconfigure(encoding="utf-8")
from kb.store import Store
from kb.query import Retriever

DB = "D:/opencode/kb/data/kb.db"
st = Store(DB)
r = Retriever(DB)

print("=" * 78)
print("A. 强制 topk=1，检验图谱扩展路径")
print("=" * 78)
q = "各层级单位能否自行建设数据中心机房？"
ids, stats = r.retrieve(q, topk=1)
print(f"  问题: {q}")
print(f"  初始召回 topk=1 -> {len(ids)} 块, 统计={stats}")
for cid in ids:
    p, i, t, loc = r.chunks[cid]
    print(f"    - {os.path.basename(p)}@{loc} :: {t[:80]}...")

extra = r.graph_expand(ids, hops=1, limit=12)
print(f"  图谱扩展命中 {len(extra)} 个新块:")
for cid in extra:
    p, i, t, loc = r.chunks[cid]
    print(f"    - {os.path.basename(p)}@{loc} :: {t[:80]}...")

print()
print("=" * 78)
print("B. 图谱路径：实体 -> 知识点 -> 相关实体")
print("=" * 78)
adj = r.adjacency()
for name in ["总部信息化管理部", "数据机房建设或租赁", "信息化业务伙伴"]:
    row = st.db.execute("SELECT id,type,mentions FROM nodes WHERE name=? AND merged_to IS NULL", (name,)).fetchone()
    if not row:
        print(f"  {name}: 未找到"); continue
    nid, ntype, mentions = row
    print(f"\n  【{name}】type={ntype} mentions={mentions}")
    neigh = list(adj.get(nid, ()))
    for m in neigh[:8]:
        n2 = st.db.execute("SELECT name,type FROM nodes WHERE id=?", (m,)).fetchone()
        rels = [x[0] for x in st.db.execute(
            "SELECT rel FROM edges WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
            (nid, m, m, nid)).fetchall()]
        print(f"     --{rels}--> {n2[0]} ({n2[1]})")

print()
print("=" * 78)
print("C. 对比：仅向量 vs 仅关键词 vs 混合（topk=2）")
print("=" * 78)
for q in ["数据中心机房由谁审批", "软件正版化责任", "首席信息官"]:
    v = [c for c, _ in r.vector_search(r.__class__ and __import__("kb.llm", fromlist=["llm"]).embed([q])[0], 4)]
    k = [c for c, _ in r.keyword_search(q, 4)]
    print(f"\n  Q: {q}")
    print(f"    向量: {[r.chunks[c][3] for c in v]}")
    print(f"    关键词: {[r.chunks[c][3] for c in k]}")
