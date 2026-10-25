"""一次性清理：把平台内部目录 `_kb` / `_kb_trash` 的历史脏数据从库里删掉。

背景（任务 0.1）：1.0 引擎把自己导出的
``_kb\\wiki\\entities\\*.md``（如 ``\\\\192.0.2.19\\信息化管理\\AI知识库\\_kb\\…``）
登进了 catalog、fs_entries 与解析队列，污染看板统计、混进文件页文件树，
还占着排队任务。本脚本清掉这些索引行。

**只删数据库索引行，绝不碰 NAS 上的任何文件**——NAS 上那个 `_kb` 目录
按决策保留不删。

用法（默认 dry-run，只统计不删除）：
    PYTHONPATH=. python tools/clean-internal-entries.py
真正执行（评审确认 dry-run 数字之后）：
    PYTHONPATH=. python tools/clean-internal-entries.py --apply
指定库文件：
    PYTHONPATH=. python tools/clean-internal-entries.py --db path/to/kb.db

清理范围：
  * catalog     —— path 中任一段等于 `_kb` / `_kb_trash` 的行；
  * fs_entries  —— rel_path 中任一段等于平台内部目录的行（fs_fts 随之同步删）；
  * jobs        —— catalog_id 已不在 catalog 中的孤儿任务（含被上面清掉的行）；
  * catalog_stats —— share 中含平台内部目录的历史计数行。

幂等：删除条件是"行存在且属于平台目录/引用已删行"。第二次跑时已没有
这样的行，输出全为 0，结果与第一次执行后的状态一致。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kbplatform.paths import norm_unc
from kbplatform.platform_dirs import PLATFORM_DIR_NAMES

_ORDER = ("catalog", "fs_entries", "fs_fts", "jobs", "catalog_stats")


def _has_platform_dir(path: str) -> bool:
    """路径中任一段等于平台内部目录名（忽略大小写与分隔符形式）。

    为什么按"段"匹配而不是 LIKE '%_kb\\%'：
      SQLite 的 LIKE 里反斜杠并不是转义符，`%` 永远是通配符；而且
      子串匹配会误伤 ``文档_kb\\`` 这类用户自己的目录。按段相等才能
      又删得全（catalog 存的是 ``…\\AI知识库\\_kb\\wiki\\…`` 这样的中段）、
      又删得准（fs_entries 的段判断复用同一份名单，不在脚本里另写字面量）。
    """
    names = {n.lower() for n in PLATFORM_DIR_NAMES}
    return any(seg.lower() in names for seg in norm_unc(path).split("\\"))


def _collect(conn: sqlite3.Connection) -> dict[str, list]:
    """选出所有要删的行（纯 SELECT，不修改任何数据）。"""
    all_cat = conn.execute("SELECT id, path FROM catalog").fetchall()
    cat_ids = [rid for rid, path in all_cat if _has_platform_dir(path)]
    surviving = {rid for rid, _ in all_cat} - set(cat_ids)

    fe_ids = [rid for rid, rel in conn.execute("SELECT id, rel_path FROM fs_entries")
              if _has_platform_dir(rel)]
    fts_ids = []
    if fe_ids:
        ph = ",".join("?" * len(fe_ids))
        fts_ids = [rid for rid, in conn.execute(
            f"SELECT rowid FROM fs_fts WHERE rowid IN ({ph})", fe_ids)]

    # 孤儿任务 = catalog_id 不在任何"执行后仍存在的 catalog 行"里
    job_ids = [rid for rid, cid in conn.execute(
        "SELECT id, catalog_id FROM jobs WHERE catalog_id IS NOT NULL")
        if cid not in surviving]

    stat_rows = [(share, ext) for share, ext in conn.execute(
        "SELECT share, ext FROM catalog_stats") if _has_platform_dir(share)]
    return {"catalog": cat_ids, "fs_entries": fe_ids, "fs_fts": fts_ids,
            "jobs": job_ids, "catalog_stats": stat_rows}


def _apply(conn: sqlite3.Connection, plan: dict[str, list]) -> None:
    """单事务执行删除：catalog -> fs_entries/fs_fts -> 孤儿 jobs -> catalog_stats。"""
    conn.execute("BEGIN")
    if plan["catalog"]:
        ph = ",".join("?" * len(plan["catalog"]))
        conn.execute(f"DELETE FROM catalog WHERE id IN ({ph})", plan["catalog"])
    if plan["fs_entries"]:
        ph = ",".join("?" * len(plan["fs_entries"]))
        conn.execute(f"DELETE FROM fs_fts WHERE rowid IN ({ph})", plan["fs_fts"])
        conn.execute(f"DELETE FROM fs_entries WHERE id IN ({ph})", plan["fs_entries"])
    if plan["jobs"]:
        ph = ",".join("?" * len(plan["jobs"]))
        conn.execute(f"DELETE FROM jobs WHERE id IN ({ph})", plan["jobs"])
    for share, ext in plan["catalog_stats"]:
        conn.execute("DELETE FROM catalog_stats WHERE share=? AND ext=?",
                     (share, ext))
    conn.commit()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="清理平台内部目录 _kb 的历史索引行")
    ap.add_argument("--db", default=os.path.join("kb", "data", "kb.db"),
                    help="kb.db 路径（默认 kb/data/kb.db）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="只统计不删除（默认行为，此参数为显式声明）")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除；不加则只做 dry-run 统计")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"[clean] 数据库不存在: {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _collect(conn)
        mode = "--apply 实删模式" if args.apply else "dry-run 预演模式"
        print(f"[clean] {mode}  db={args.db}")
        for k in _ORDER:
            print(f"[clean] {k:>14}: {len(plan[k])} 行")
        if not args.apply:
            print("[clean] dry-run 结束，未删除任何数据；"
                  "确认数字无误后加 --apply 实际执行。")
            return 0
        _apply(conn, plan)
        print("[clean] 执行完成。再跑一遍应全部为 0（幂等）。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
