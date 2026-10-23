import sys, os, json, time
sys.path.insert(0, "D:/kb-platform")
sys.stdout.reconfigure(encoding="utf-8")
from kb.store import Store
from kb.query import Retriever, answer

DB = "D:/kb-platform/kb/data/kb.db"
WIKI = sys.argv[1] if len(sys.argv) > 1 else None

st = Store(DB)
r = Retriever(DB, wiki_dir=WIKI)
print(f"检索池: {len(r.chunks)} 个文档源 (数据库块 {len([1 for k in r.chunks if k < 1000000])} + wiki 页 {len(r.wiki)})")
print("=" * 78)

QS = [
    "哪些系统不得单独建设，须统一使用？",
    "核心业务信息化具体包括哪些业务领域？",
    "三级及以下子企业能不能自建数据中心机房？",
    "总部信息化管理部与二级单位信息化管理部门分别负责什么？",
    "信创产品采购要走什么流程？",
    "信息化项目全生命周期包含哪些阶段？",
    "软件正版化率要求是多少？",
    "公司对网络安全等级保护有什么要求？",
]
t0 = time.time()
for q in QS:
    obj, meta, stt, lm = answer(st, r, q, topk=6)
    wiki_used = [m for m in meta if m["chunk"] == "wiki"]
    print("\n" + "-" * 78)
    print("Q:", q)
    print("A:", obj.get("answer"))
    print(f"   引用: {obj.get('citations')}  | 召回 向量{stt['vector']}/关键词{stt['keyword']}/图谱+{stt['graph_extra']}"
          f" | wiki命中 {len(wiki_used)} | LLM {lm.get('wall',0):.1f}s")
    for e in obj.get("evidence", [])[:3]:
        print("   依据:", e[:150])
    if obj.get("gaps"):
        for g in obj["gaps"]:
            print("   缺口:", g[:150])
print("\n" + "=" * 78)
print(f"8 问总耗时 {time.time()-t0:.0f}s  平均 {(time.time()-t0)/len(QS):.1f}s/问")
