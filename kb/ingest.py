"""增量摄取：扫描目录 → 解析 → 分块 → LLM 抽取 → 向量化 → 建图。

增量靠 (路径, mtime, size) 指纹：内容没变就跳过，NAS 上重复运行安全。
"""
import os, sys, json, time, argparse, traceback

from . import parsers, chunker, llm, graph
from .store import Store
from .parsers import file_fingerprint

SKIP_DIRS = {".git", "node_modules", "$recycle", "#recycle", "__pycache__",
             ".svn", "_kb", "@eaDir", "#snapshot", "#recycle"}
EXTS = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".txt", ".md", ".ofd"}


EXTRACT_PROMPT = """你是企业知识库构建助手。阅读下面的文档片段，抽取其中的知识点。

要求：
1. key_points 抽取 5-10 条，覆盖该片段的主要规定、流程、标准、职责；
2. title 是凝练的知识点标题（不超过 30 字），detail 是具体说明（保留数字、金额、比例、时限等关键事实）；
3. entities 只保留文档中真实出现的专有名词（部门、岗位、术语、系统名称、文件名称），不要臆造；
4. category 从 制度/流程/标准/职责/数据/组织/其它 中选最贴切的一个；
5. relations 抽取片段中明确表述的实体关系（from/to 用文档里的原词）。

文档片段：
---
{chunk}
---"""


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXTS:
                yield os.path.join(dirpath, fn)


def ingest_file(store, path, model=llm.GEN_MODEL, skip_graph_merge=False, verbose=True):
    blocks, meta = parsers.parse_file(path)
    fp = file_fingerprint(path)

    if meta.get("type") in ("unsupported", "unsupported_legacy", "error"):
        store.mark_file(path, fp, meta, status="skipped:" + meta.get("type", "err"))
        return {"status": "skipped", "meta": meta}

    if not blocks or sum(len(b.text) for b in blocks) < 30:
        store.mark_file(path, fp, meta, status="skipped:empty")
        return {"status": "empty", "meta": meta}

    chunks = chunker.chunk_blocks(blocks)
    ids = store.replace_chunks(path, chunks)

    # 向量化（块文本 + 知识点标题稍后补充）
    vecs = llm.embed([c["text"] for c in chunks]) if chunks else []
    if vecs:
        store.add_vectors(list(zip(ids, vecs)))

    # LLM 抽取
    n_kp = n_rel = 0
    for cid, c in zip(ids, chunks):
        try:
            obj, meta_llm = llm.chat_json(
                EXTRACT_PROMPT.format(chunk=c["text"]), schema=llm.EXTRACTION_SCHEMA,
                model=model, num_predict=2500)
        except Exception as e:
            if verbose:
                print(f"    [抽取失败] {os.path.basename(path)}#{c['idx']}: {e}")
            continue
        raw = json.dumps(obj, ensure_ascii=False)
        store.add_knowledge(path, cid, obj, raw)
        graph.build_from_extraction(store, path, cid, obj)
        n_kp += len(obj.get("key_points", []))
        n_rel += len(obj.get("relations", []))
        if verbose:
            print(f"    [抽取] {os.path.basename(path)}#{c['idx']} "
                  f"tokens={meta_llm.get('prompt_tokens')}->{meta_llm.get('eval_tokens')} "
                  f"{meta_llm.get('wall', 0):.1f}s  kp={len(obj.get('key_points', []))} "
                  f"rel={len(obj.get('relations', []))}")

    store.mark_file(path, fp, meta, status="ok")
    store.commit()
    return {"status": "ok", "chunks": len(chunks), "key_points": n_kp, "relations": n_rel, "meta": meta}


def run(root, db_path, model=llm.GEN_MODEL, do_canon=True, force=False, verbose=True,
        skip_graph_merge=False):
    store = Store(db_path)
    t0 = time.time()
    files = list(walk(root))
    if verbose:
        print(f"扫描 {root}: 命中 {len(files)} 个可处理文件")
        print(f"模型: {model}   向量: {llm.EMB_MODEL}")
        print("-" * 78)

    done = skipped = failed = 0
    t_start = time.time()
    for i, p in enumerate(files, 1):
        try:
            fp = file_fingerprint(p)
            prev = store.seen(p)
            if prev and prev[0] == fp and prev[1] == "ok" and not force:
                skipped += 1
                if verbose:
                    print(f"[{i}/{len(files)}] 跳过(未变更) {os.path.basename(p)}")
                continue
            r = ingest_file(store, p, model=model, verbose=verbose)
            if r["status"] == "ok":
                done += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(files)}] 失败 {os.path.basename(p)}: {type(e).__name__}: {e}")
            traceback.print_exc()

    if verbose:
        print("-" * 78)
        print(f"处理完成: 成功 {done}  跳过 {skipped}  失败 {failed}  用时 {time.time()-t_start:.0f}s")

    has_edges = store.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    if has_edges and not skip_graph_merge:
        print("\n=== 合并重复边 ===")
        n = graph.merge_edges(store)
        print(f"  合并后边数: {n}")
        if do_canon:
            print("\n=== 实体归一 (向量聚类 + LLM 复核) ===")
            res = graph.canonicalize_entities(store)
            print(f"  合并实体: {res['merged']}  LLM 复核对数: {res['reviewed']}")
            for c in res["clusters"][:10]:
                print(f"    {c[0]}  ==  {c[1]}")
        print("\n=== 重算提及次数 ===")
        graph.recount_mentions(store)

    st = graph.stats(store)
    print("\n=== 图谱统计 ===")
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print(f"\n总耗时 {time.time()-t0:.0f}s")
    return store, st


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--db", default="D:/kb-platform/kb/data/kb.db")
    ap.add_argument("--model", default=llm.GEN_MODEL)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-canon", action="store_true")
    a = ap.parse_args()
    run(a.root, a.db, model=a.model, force=a.force, do_canon=not a.no_canon)
