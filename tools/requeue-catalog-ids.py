"""一次性救回：把因「embedding 单次输入超限」而没有向量的文档重新入队。

背景（任务 0.2）：catalog id 3395 / 3397 / 3419 / 3495（3 个管理手册分册 +
1 个考核规定）各含超限块，旧逻辑一次 `llm.embed(全部块)` 失败即整篇向量作废，
且失败被 `if vecs:` 静默跳过——这 4 篇一个向量都没有，126 个块永远搜不到。
任务 0.2 之后 `embed_chunks` 能逐块兜底（二分重试），把这几篇重新排队即可救回向量。

本脚本做的事：
  * catalog     —— status='pending'、status_detail=NULL、tier1_done=0、tier2_done=0；
  * jobs        —— 每个 id 入队一个 kind='index' 任务（复用 jobs.JobQueue 的去重）；
**不碰 NAS 上的文件，也不删现有 chunks/vectors**（重解析时 indexer 自己会清旧产物）。

用法（默认 dry-run，只统计不修改；--apply 才真执行，评审通过后由控制器运行）：
    python tools/requeue-catalog-ids.py --ids 3395,3397,3419,3495
    python tools/requeue-catalog-ids.py --ids 3395,3397,3419,3495 --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kbplatform.jobs import JobQueue


def _load(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT id, path, name, status, status_detail, tier1_done, tier2_done,"
        f"       chunk_count, needs_ocr"
        f"  FROM catalog WHERE id IN ({marks}) ORDER BY id", ids).fetchall()


def _chunk_stats(conn: sqlite3.Connection, cats: list[sqlite3.Row]) -> list[tuple]:
    """每篇的块数与向量表（镜像）行数，用于 dry-run 前后对比。"""
    out = []
    for r in cats:
        n_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE file_path=?", (r["path"],)).fetchone()[0]
        n_vectors = conn.execute(
            "SELECT COUNT(*) FROM vectors v JOIN chunks c ON v.chunk_id=c.id"
            " WHERE c.file_path=?", (r["path"],)).fetchone()[0]
        out.append((n_chunks, n_vectors))
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="重新入队缺向量的 catalog 文档")
    ap.add_argument("--ids", required=True,
                    help="逗号分隔的 catalog id，如 3395,3397,3419,3495")
    ap.add_argument("--db", default=os.path.join("kb", "data", "kb.db"),
                    help="kb.db 路径（默认 kb/data/kb.db）")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行重置与入队；不加则只做 dry-run 统计")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if not ids:
        print("[requeue] --ids 为空", file=sys.stderr)
        return 1
    if not os.path.isfile(args.db):
        print(f"[requeue] 数据库不存在: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        cats = _load(conn, ids)
        if len(cats) != len(ids):
            found = {r["id"] for r in cats}
            print(f"[requeue] 警告: 以下 id 不在 catalog 中: "
                  f"{sorted(set(ids) - found)}", file=sys.stderr)
        stats = _chunk_stats(conn, cats)

        mode = "--apply 实际执行模式" if args.apply else "dry-run 预演模式"
        print(f"[requeue] {mode}  db={args.db}")
        for r, (n_chunks, n_vectors) in zip(cats, stats):
            print(f"[requeue] id={r['id']}  {r['name']}")
            print(f"[requeue]   path={r['path']}")
            print(f"[requeue]   当前: status={r['status']} tier1_done={r['tier1_done']}"
                  f" chunk_count={r['chunk_count']} chunks表={n_chunks}"
                  f" 向量={n_vectors} status_detail={r['status_detail']!r}")

        existing = {int(x[0]) for x in conn.execute(
            f"SELECT DISTINCT catalog_id FROM jobs WHERE kind='index' AND catalog_id"
            f" IN ({','.join('?' * len(cats))}) AND state='queued'",
            [r["id"] for r in cats]).fetchall()}

        if not args.apply:
            print(f"[requeue] 计划: 将置 {len(cats)} 篇 status='pending'、tier1_done=0"
                  f"（tier2_done 同时清零），并入队 {len(cats) - len(existing)}"
                  f" 个 index 任务（已有排队任务的 {len(existing)} 个复用，不重复插）")
            print("[requeue] dry-run 结束，未修改任何数据；"
                  "确认后加 --apply 执行（会触发真实解析与 embedding 调用）。")
            return 0

        for r in cats:
            conn.execute(
                "UPDATE catalog SET status='pending', status_detail=NULL,"
                " tier1_done=0, tier2_done=0, updated_at=COALESCE(updated_at, strftime('%s','now'))"
                " WHERE id=?", (r["id"],))
        conn.commit()
        for r in cats:
            JobQueue(conn).enqueue("index", int(r["id"]), force=True)
        print(f"[requeue] 执行完成: {len(cats)} 篇已重置并重新入队。"
              "跑完后应确认：chunks 向量数上升、/api/v1/search 能搜到这 4 篇正文、"
              "status_detail 干净（或只剩真实仍失败块的数量说明）。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
