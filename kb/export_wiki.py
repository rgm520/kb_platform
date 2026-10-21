"""把图谱导出成可读可编辑的 Wiki。

产物：
  _kb/knowledge.jsonl        机器可读的抽取结果
  _kb/graph.json             节点与边（可直接喂给图可视化工具）
  _kb/wiki/index.md          总索引
  _kb/wiki/entities/*.md     每个实体一页
  _kb/wiki/topics/*.md       每个知识点一页

Wiki 的价值在于「人可读 + 人可编辑」：抽取完先自动生成，人工再修正，
修正后的页面应当能被下一轮抽取当作上下文回灌。
"""
import os, sys, json, re, argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kb.store import Store, norm_name as norm


def safe(name, maxlen=60):
    s = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
    return (s[:maxlen] or "untitled")


def build(store, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    wiki = os.path.join(out_dir, "wiki")
    os.makedirs(os.path.join(wiki, "entities"), exist_ok=True)
    os.makedirs(os.path.join(wiki, "topics"), exist_ok=True)

    # ---------- 读图谱 ----------
    nodes = {}
    for nid, name, ntype, mentions, merged in store.db.execute(
            "SELECT id,name,type,mentions,merged_to FROM nodes"):
        nodes[nid] = dict(id=nid, name=name, type=ntype, mentions=mentions, merged=merged)
    edges = store.db.execute(
        "SELECT src,dst,rel,file_path,chunk_id,weight FROM edges").fetchall()
    adj = defaultdict(list)
    for s, d, rel, fp, cid, w in edges:
        adj[s].append((d, rel))
        adj[d].append((s, rel))

    # ---------- 读知识点 ----------
    topics = []
    for fp, cid, summ, kps, rels in store.db.execute(
            "SELECT file_path,chunk_id,summary,key_points,relations FROM knowledge ORDER BY chunk_id"):
        topics.append(dict(file=fp, chunk=cid, summary=summ,
                           key_points=json.loads(kps), relations=json.loads(rels)))

    # ---------- knowledge.jsonl ----------
    kpath = os.path.join(out_dir, "knowledge.jsonl")
    with open(kpath, "w", encoding="utf-8") as f:
        for t in topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # ---------- graph.json ----------
    gpath = os.path.join(out_dir, "graph.json")
    live = {i: n for i, n in nodes.items() if n["merged"] is None}
    with open(gpath, "w", encoding="utf-8") as f:
        json.dump({
            "nodes": [{"id": i, "name": n["name"], "type": n["type"], "mentions": n["mentions"]}
                      for i, n in live.items()],
            "edges": [{"src": s, "dst": d, "rel": rel, "weight": w}
                      for s, d, rel, fp, cid, w in edges
                      if s in live and d in live],
        }, f, ensure_ascii=False, indent=1)

    # ---------- 知识点页 ----------
    topic_meta = []
    for t in topics:
        src = os.path.basename(t["file"])
        lines = [f"# {t['summary'] or '（无摘要）'}", "",
                 f"> 来源：`{src}` | 文本块 chunk#{t['chunk']}", "",
                 "## 知识点", ""]
        for kp in t["key_points"]:
            lines.append(f"### {kp['title']}  <sub>`{kp.get('category','')}`</sub>")
            lines.append("")
            lines.append(kp.get("detail", ""))
            lines.append("")
            if kp.get("entities"):
                links = []
                for e in kp["entities"]:
                    nid = store.db.execute(
                        "SELECT id FROM nodes WHERE norm=? AND type='entity' AND merged_to IS NULL",
                        (norm(e),)).fetchone()
                    if nid:
                        links.append(f"[{e}]({os.path.relpath(os.path.join(wiki,'entities',safe(e)+'.md'), os.path.join(wiki,'topics')).replace(os.sep,'/')})")
                    else:
                        links.append(e)
                lines.append("- 关联实体：" + " · ".join(links))
                lines.append("")
        if t["relations"]:
            lines += ["## 实体关系", ""]
            for r in t["relations"]:
                lines.append(f"- `{r['from']}` --{r['relation']}--> `{r['to']}`")
            lines.append("")
        head = re.sub(r'\s+', " ", t["summary"] or "")[:28]
        p = os.path.join(wiki, "topics", f"topic-{t['chunk']:03d}-{safe(head, 34)}.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        topic_meta.append((p, t))

    # ---------- 实体页 ----------
    # 实体 -> 涉及的知识点
    ent_topics = defaultdict(list)
    for t in topics:
        for kp in t["key_points"]:
            for e in kp.get("entities", []):
                ent_topics[norm(e)].append((t, kp))
    ent_rels = defaultdict(list)
    ent_inrels = defaultdict(list)
    for s, d, rel, fp, cid, w in edges:
        if s in live and d in live and live[s]["type"] == "entity" and live[d]["type"] == "entity":
            ent_rels[live[s]["name"]].append((rel, live[d]["name"], os.path.basename(fp or "")))
            ent_inrels[live[d]["name"]].append((rel, live[s]["name"], os.path.basename(fp or "")))

    entity_files = []
    for nid, n in live.items():
        if n["type"] != "entity":
            continue
        name = n["name"]
        lines = [f"# {name}", "",
                 f"| 属性 | 值 |", "|---|---|",
                 f"| 类型 | 实体 |", f"| 被提及 | {n['mentions']} 次 |", ""]
        ts = ent_topics.get(norm(name), [])
        if ts:
            lines += ["## 涉及的知识点", ""]
            seen = set()
            for t, kp in ts:
                if kp["title"] in seen:
                    continue
                seen.add(kp["title"])
                lines.append(f"### {kp['title']}  <sub>`{kp.get('category','')}`</sub>")
                lines.append("")
                lines.append(kp.get("detail", ""))
                lines.append("")
        rels = ent_rels.get(name, [])
        if rels:
            lines += ["## 直接关系（本实体 → 其它）", ""]
            for rel, other, src in rels:
                op = os.path.relpath(os.path.join(wiki, "entities", safe(other) + ".md"),
                                     os.path.join(wiki, "entities")).replace(os.sep, "/")
                lines.append(f"- --{rel}--> [{other}]({op})  <sub>来源: {src}</sub>")
            lines.append("")
        inrels = ent_inrels.get(name, [])
        if inrels:
            lines += ["## 被引用的关系（其它 → 本实体）", ""]
            for rel, other, src in inrels:
                op = os.path.relpath(os.path.join(wiki, "entities", safe(other) + ".md"),
                                     os.path.join(wiki, "entities")).replace(os.sep, "/")
                lines.append(f"- [{other}]({op}) --{rel}--> **本实体**  <sub>来源: {src}</sub>")
            lines.append("")
        p = os.path.join(wiki, "entities", safe(name) + ".md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        entity_files.append((p, n))

    # ---------- 索引页 ----------
    lines = ["# 知识库索引", "",
             f"共 {len(live)} 个活跃节点 · {len(edges)} 条边 · "
             f"{len(topics)} 个文本块 · {len(entity_files)} 个实体页 · {len(topic_meta)} 个知识点页", "",
             "## 实体（按提及次数）", ""]
    ents = sorted(entity_files, key=lambda x: -x[1]["mentions"])
    for p, n in ents:
        lines.append(f"- [{n['name']}](entities/{safe(n['name'])}.md) — 提及 {n['mentions']} 次")
    lines += ["", "## 知识点页", ""]
    for p, t in topic_meta:
        lines.append(f"- [{t['summary'] or 'chunk' + str(t['chunk'])}](topics/{os.path.basename(p)})")
    lines += ["", "## 分类", ""]
    cats = defaultdict(int)
    for t in topics:
        for kp in t["key_points"]:
            cats[kp.get("category", "其它")] += 1
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"- {c}: {n} 条")
    with open(os.path.join(wiki, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {"knowledge_jsonl": kpath, "graph_json": gpath, "index": os.path.join(wiki, "index.md"),
            "entity_pages": len(entity_files), "topic_pages": len(topic_meta),
            "nodes": len(live), "edges": len(edges)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="D:/opencode/kb/data/kb.db")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    st = Store(a.db)
    r = build(st, a.out)
    print(json.dumps(r, ensure_ascii=False, indent=2))
