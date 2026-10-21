import sys, os, glob
sys.path.insert(0, "D:/opencode")
sys.stdout.reconfigure(encoding="utf-8")
from kb.store import Store
from kb.query import Retriever

DB = "D:/opencode/kb/data/kb.db"
WIKI = sys.argv[1]
st = Store(DB)

cands = glob.glob(os.path.join(WIKI, "entities", "*.md"))
cands = [c for c in cands if os.path.getsize(c) > 500]
print("候选文件数:", len(cands))
if not cands:
    print("没有可用的实体页")
    sys.exit(1)
# 取内容最丰富的实体页做校正测试
target = max(cands, key=lambda p: os.path.getsize(p))
print("选用:", os.path.basename(target))
orig = open(target, encoding="utf-8").read()
print(f"\n目标文件: {os.path.basename(target)}  ({len(orig)} 字符)")
print("--- 原始内容 ---")
print(orig[:600])

PATCH = "\n\n## 人工校正记录\n> 【2026-10-21 补充】经总部批准，三级及以下子企业自 2026 年起可申请特批自建数据机房，审批流程同二级子企业。\n"

Q = "三级及以下子企业能不能自建机房？"

print("\n" + "=" * 78)
print("A. 校正前检索")
r1 = Retriever(DB, wiki_dir=WIKI)
ids1, s1 = r1.retrieve("三级及以下子企业能不能自建机房？", topk=6)
print("  召回:", s1)
print("  含目标页:", target in [r1.chunks[c][0] for c in ids1])
ans1, m1, st1, lm1 = __import__("kb.query", fromlist=["answer"]).answer(st, r1, "三级及以下子企业能不能自建机房？", topk=6)
print("  答案:", ans1.get("answer"))
print("  缺口:", ans1.get("gaps"))

print("\n" + "=" * 78)
print("B. 写入人工校正后检索")
open(target, "w", encoding="utf-8").write(orig + PATCH)
try:
    r2 = Retriever(DB, wiki_dir=WIKI)
    ids2, s2 = r2.retrieve("三级及以下子企业能不能自建机房？", topk=6)
    print("  召回:", s2)
    print("  含目标页:", target in [r2.chunks[c][0] for c in ids2])
    for c in ids2:
        p, i, t, loc = r2.chunks[c]
        if "人工校正" in t:
            print(f"    -> 命中校正内容: {os.path.basename(p)}")
    ans2, m2, st2, lm2 = __import__("kb.query", fromlist=["answer"]).answer(st, r2, "三级及以下子企业能不能自建机房？", topk=6)
    print("  答案:", ans2.get("answer"))
    print("  依据:")
    for e in ans2.get("evidence", []):
        print("    -", e[:180])
finally:
    open(target, "w", encoding="utf-8").write(orig)
    print("\n已还原原始 wiki 文件")
