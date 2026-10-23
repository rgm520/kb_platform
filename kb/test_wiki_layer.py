import sys, os, json
sys.path.insert(0, "D:/kb-platform")
sys.stdout.reconfigure(encoding="utf-8")
from kb.store import Store
from kb.query import Retriever

DB = "D:/kb-platform/kb/data/kb.db"
WIKI = sys.argv[1]
st = Store(DB)
r = Retriever(DB, wiki_dir=WIKI)

print(f"数据库块: {len([k for k in r.chunks if k < 1000000])}   wiki 页: {len(r.wiki)}")
print("=" * 78)

for q in ["哪些系统不得单独建设？", "数据机房由谁审批", "信息化项目生命周期", "信创集中采购"]:
    ids, stt = r.retrieve(q, topk=6)
    wiki_in = [c for c in ids if c >= 1_000_000]
    print(f"\nQ: {q}")
    print(f"  召回统计: {stt}")
    print(f"  结果中 wiki 页: {len(wiki_in)}")
    for c in ids:
        p, i, t, loc = r.chunks[c]
        tag = "WIKI" if c >= 1_000_000 else "DB  "
        print(f"    [{tag}] {os.path.basename(p)[:45]:<45} @{loc[:12]:<12} {t[:55]}...")

# 手工修正验证：改一个 wiki 页，看能否被检索到并进入答案
target = os.path.join(WIKI, "entities", "数据机房建设或租赁.md")
if os.path.exists(target):
    print("\n" + "=" * 78)
    print("手工校正闭环验证：修改 wiki 页，看是否影响检索")
    orig = open(target, encoding="utf-8").read()
    patched = orig + "\n\n## 人工校正\n> 【人工补充】自 2026 年起，三级及以下子企业经总部特批可申请自建机房。\n"
    open(target, "w", encoding="utf-8").write(patched)
    try:
        r2 = Retriever(DB, wiki_dir=WIKI)
        ids2, stt2 = r2.retrieve("三级及以下子企业能不能自建机房？", topk=6)
        hit = [c for c in ids2 if c >= 1_000_000]
        print(f"  修改后召回 wiki 页: {len(hit)}  统计={stt2}")
        for c in hit:
            p, i, t, loc = r.chunks[c]
            print(f"    -> {os.path.basename(p)}  含人工补充: {'人工补充' in t}")
    finally:
        open(target, "w", encoding="utf-8").write(orig)
        print("  已还原原始 wiki 文件")
