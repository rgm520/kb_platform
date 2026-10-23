import sys, os, json
sys.path.insert(0, "D:/kb-platform")
sys.stdout.reconfigure(encoding="utf-8")
from kb.store import Store
from kb.query import Retriever, answer

DB = "D:/kb-platform/kb/data/kb.db"
st = Store(DB)

print("=" * 78)
print("一、抽取出的知识点（全部）")
print("=" * 78)
rows = st.db.execute("SELECT file_path, chunk_id, summary, key_points FROM knowledge ORDER BY chunk_id").fetchall()
for fp, cid, summ, kps in rows:
    print(f"\n--- chunk {cid}  ({os.path.basename(fp)}) ---")
    print(f"摘要: {summ}")
    for i, kp in enumerate(json.loads(kps), 1):
        print(f"  {i:2}. [{kp['category']}] {kp['title']}")
        print(f"      {kp['detail'][:120]}")
        if kp.get("entities"):
            print(f"      实体: {', '.join(kp['entities'])}")

print("\n" + "=" * 78)
print("二、图谱结构")
print("=" * 78)
print("\n[分类分布]")
for t, c in st.db.execute("SELECT type, COUNT(*) FROM nodes WHERE merged_to IS NULL GROUP BY type").fetchall():
    print(f"  {t:<16} {c}")
print("\n[关系类型分布]")
for r, c in st.db.execute("SELECT rel, COUNT(*) c FROM edges GROUP BY rel ORDER BY c DESC LIMIT 15").fetchall():
    print(f"  {r:<32} {c}")

print("\n[实体度数 Top 20]")
q = """
SELECT n.name, n.type,
  (SELECT COUNT(*) FROM edges WHERE src=n.id OR dst=n.id) deg
FROM nodes n WHERE n.merged_to IS NULL AND n.type='entity'
ORDER BY deg DESC LIMIT 20
"""
for name, typ, deg in st.db.execute(q).fetchall():
    print(f"  {deg:>3}  {name}")

print("\n[实体间关系边 (非结构边)]")
q2 = """
SELECT s.name, e.rel, d.name, e.file_path FROM edges e
JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst
WHERE s.type='entity' AND d.type='entity'
"""
for a, rel, b, fp in st.db.execute(q2).fetchall():
    print(f"  {a}  --{rel}-->  {b}")

print("\n[知识点 -> 实体 关系样例]")
q3 = """
SELECT k.name, n.name FROM edges e
JOIN nodes k ON k.id=e.src JOIN nodes n ON n.id=e.dst
WHERE k.type='knowledge_point' AND n.type='entity' LIMIT 25
"""
for k, en in st.db.execute(q3).fetchall():
    print(f"  {k}  ->  {en}")

print("\n" + "=" * 78)
print("三、问答测试")
print("=" * 78)
r = Retriever(DB)
questions = [
    "哪些系统不得单独建设，须统一使用？",
    "核心业务信息化具体包括哪些业务领域？",
    "信息化领导小组和工作小组分别承担什么职责？",
    "二级单位的信息化管理部门负责哪些工作？",
    "供应商或应用系统上线前需要经过哪些审批流程？",
]
for q in questions:
    print("\n" + "-" * 78)
    obj, meta, stt, lm = answer(st, r, q, topk=6)
    print("Q:", q)
    print("A:", obj.get("answer"))
    if obj.get("citations"):
        print("  引用片段:", obj["citations"])
    for e in obj.get("evidence", [])[:4]:
        print("   依据:", e)
    if obj.get("gaps"):
        print("   缺口:", obj["gaps"])
    print(f"  召回: 向量{stt['vector']}/关键词{stt['keyword']}/图谱+{stt['graph_extra']}  LLM {lm.get('wall',0):.1f}s")
