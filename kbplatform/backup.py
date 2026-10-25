"""可恢复的备份：SQLite 备份 API 快照 + FTS/向量文件固化 + 保留份数清理。

为什么不能直接复制文件（迭代计划 0.6 背景）：
    生产库 kb.db 是 2.25 GB，而 kb.db-wal 曾涨到 2.49 GB（checkpoint 饥饿，
    W1）——大量已提交数据只躺在 WAL 文件里。直接复制 kb.db 会漏掉 WAL 对应的
    事务，复制出的文件不是任何时刻的一致性快照，恢复时可能直接打不开。
    `sqlite3.Connection.backup()` 是 SQLite 官方备份 API：在线状态下逐页拷贝，
    产物是完整、一致的数据库文件，边写库边备份也不会撕裂。

备份内容（索引的三份状态，恢复缺一不可）：
    kb.db    平台库 + 引擎库（同一文件）—— backup API
    fts.db   FTS5 全文索引（也是 SQLite 库）—— backup API
    vectors/ vectors.f16 + vectors.ids.json —— 普通 fp16/json 文件，
             不是 SQLite，复制是安全的；必须在两个库快照**之后**复制，
             保证向量行序与 kb.db 里的 chunk_id 对得上。

备份目录必须在被监控的 NAS 根之外（Task 0.1 刚修过"备份文件被扫进索引"
这类自吞问题），CLI 里做了检查，落在监控范围内直接报错退出。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime

# 运行中备份产物的时间戳与文件名（kb-YYYYMMDD-HHMM.db，分钟粒度，
# 同一分钟内重复备份会覆盖上一份——正常场景每天只在计划任务里跑一次）。
_TS_FMT = "%Y%m%d-%H%M"

# 备份产物名：kb/fts 的 .db 与 vectors 的 .f16/.ids.json，共享同一种时间戳段
_BACKUP_NAME_RE = re.compile(r"^(kb|fts|vectors)-(\d{8}-\d{4})\.(db|f16|ids\.json)$")


def _backup_one(src: sqlite3.Connection, dest_path: str) -> str:
    """用 SQLite 备份 API 把 src 完整快照到 dest_path（覆盖同名旧文件）。"""
    if os.path.exists(dest_path):
        # 覆盖旧文件：backup API 对已有库是整库重写，但先删掉更干净，
        # 也避免撞上损坏的残文件。
        os.remove(dest_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
    return dest_path


def prune_old(dest_dir: str, keep: int = 7, prefix: str = "kb") -> list[str]:
    """只保留 dest_dir 里最近 keep 组备份，返回保留的文件路径列表。

    一"组"同一时间戳的全部文件：kb-<ts>.db 是 1 个文件，
    vectors-<ts>.f16 + vectors-<ts>.ids.json 是 2 个——必须按组删，
    否则会出现 f16 留下而对应 ids.json 被清掉的残废对。
    """
    groups: dict[str, list[str]] = {}
    for name in os.listdir(dest_dir):
        m = _BACKUP_NAME_RE.match(name)
        if not m or m.group(1) != prefix:
            continue
        groups.setdefault(m.group(2), []).append(name)

    kept: list[str] = []
    for ts in sorted(groups, reverse=True)[: max(0, keep)]:
        for name in sorted(groups[ts]):
            kept.append(os.path.join(dest_dir, name))
    drop = [name for ts in sorted(groups, reverse=True)[max(0, keep):]
            for name in sorted(groups[ts])]
    for name in drop:
        try:
            os.remove(os.path.join(dest_dir, name))
        except OSError as e:
            print(f"[backup] 清理旧备份失败 {name}: {e}", flush=True)
    return kept


def backup_db(conn: sqlite3.Connection, dest_dir: str, keep: int = 7) -> list[str]:
    """用 SQLite 备份 API 做一致性快照（WAL 模式下不能直接复制文件）。

    产出 dest_dir/kb-YYYYMMDD-HHMM.db，并清掉只保留最近 keep 份，
    返回保留的备份文件路径列表（最新的在最后）。
    """
    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime(_TS_FMT)
    _backup_one(conn, os.path.join(dest_dir, f"kb-{ts}.db"))
    return prune_old(dest_dir, keep=keep, prefix="kb")


def _stat_vectors(vector_dir: str, name: str) -> tuple | None:
    try:
        st = os.stat(os.path.join(vector_dir, name))
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _copy_vectors(cfg, ts: str, dest_dir: str) -> list[str]:
    """复制 vectors.f16 / vectors.ids.json 到备份目录，要求"同一时刻"的成对。

    为什么不能只 copyfile 一遍：流水线随时可能往 vectors.f16 追加、改写
    ids.json —— 若复制间隙恰好有写入，f16 与 ids 会来自不同时刻
    （行数对不上，恢复即废）。办法：复制完再对一次源文件 stat，
    变了就重来（最多 3 次），仍不稳就报错让人停摄入。
    """
    copied: list[str] = []
    for attempt in range(1, 4):
        stat_before = {n: _stat_vectors(cfg.vector_dir, n)
                       for n in ("vectors.f16", "vectors.ids.json")}
        copied = []
        for name in ("vectors.f16", "vectors.ids.json"):
            if stat_before[name] is None:
                continue                  # 源头本来就没有，略过这一件
            sp = os.path.join(cfg.vector_dir, name)
            target = os.path.join(dest_dir, f"vectors-{ts}." + name.split(".", 1)[1])
            shutil.copyfile(sp, target)
            copied.append(target)
        stat_after = {n: _stat_vectors(cfg.vector_dir, n)
                      for n in ("vectors.f16", "vectors.ids.json")}
        if stat_before == stat_after:
            return copied
    raise RuntimeError("vectors/ 在备份期间持续被写入，3 次都没拷到稳定快照；"
                       "请停摄入后重试")


def backup_all(cfg, dest_dir: str, keep: int = 7) -> list[str]:
    """完整备份一次：kb.db + fts.db（backup API）+ vectors（文件复制）。

    返回全部保留的备份文件路径。三种产物共享同一个时间戳，恢复时配对使用。
    主库 → FTS → 向量文件顺序固定：向量行序对应 kb.db 里的 chunk_id，
    必须等两个库快照落袋后再复制；fts.db 与 kb.db 同属一次 run 的状态。
    """
    from kbplatform.db import connect

    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime(_TS_FMT)
    out: list[str] = []

    # 1) 主库：现有 connect() 只设置连接 pragma，不做业务写入
    src = connect(cfg.db_path)
    try:
        out.append(_backup_one(src, os.path.join(dest_dir, f"kb-{ts}.db")))
    finally:
        src.close()

    # 2) FTS 库同属 SQLite，也走备份 API
    if os.path.exists(cfg.fts_path):
        fts_src = sqlite3.connect(cfg.fts_path, timeout=30)
        try:
            out.append(_backup_one(fts_src, os.path.join(dest_dir, f"fts-{ts}.db")))
        finally:
            fts_src.close()

    # 3) 向量文件（要求 f16 / ids 成对来自同一时刻）
    out.extend(_copy_vectors(cfg, ts, dest_dir))

    # 4) 逐前缀清旧份，只保留本次产物与最近 keep 份
    kept: set[str] = set()
    for prefix in ("kb", "fts", "vectors"):
        kept.update(prune_old(dest_dir, keep=keep, prefix=prefix))
    return [p for p in out if p in kept]


def _default_dest(cfg) -> str:
    """默认备份目录：data_dir（…/kb/data）旁边的 kb/backups，本地盘。

    不进 config.toml 也够用；要改就往 --dest 里传（见 runbook）。
    """
    data_parent = os.path.dirname(os.path.abspath(cfg.data_dir))
    return os.path.join(data_parent, "backups")


def _log(cfg, message: str) -> None:
    """把备份结果写进 kb/logs/backup.log（计划任务排障的入口）。

    日志由 Python 写（UTF-8、io.open append），不交给 PowerShell 管道
    —— PS 5.1 解码原生进程输出按控制台代码页，中文容易变问号。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_dir = os.path.join(os.path.dirname(os.path.abspath(cfg.data_dir)), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "backup.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError as e:
        print(f"[backup] 写日志失败: {e}", flush=True)


def main(argv: list[str] | None = None) -> int:
    from kbplatform.config import load_config
    from kbplatform.paths import is_under_root

    ap = argparse.ArgumentParser(
        description="企业知识平台备份（SQLite 备份 API + 向量文件）")
    ap.add_argument("--config", default="D:/kb-platform/config.toml")
    ap.add_argument("--dest", default="", help="备份目录（默认本地盘 kb/backups）")
    ap.add_argument("--keep", type=int, default=7, help="保留最近几份（默认 7）")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    dest = args.dest or _default_dest(cfg)
    # 安全闸门：备份落在被监控的 NAS 根之下会被平台自己扫进索引
    if is_under_root(dest, cfg.monitor_roots):
        print(f"[backup] 备份目录在监控范围内，会污染索引，拒绝执行: {dest}", flush=True)
        return 2
    if args.keep < 1:
        print("[backup] --keep 至少为 1", flush=True)
        return 2

    t0 = time.time()
    try:
        out = backup_all(cfg, dest, keep=args.keep)
    except Exception as e:
        _log(cfg, f"备份失败 (keep={args.keep}): {type(e).__name__}: {e}")
        print(f"[backup] 备份失败: {type(e).__name__}: {e}", flush=True)
        return 1
    dur = time.time() - t0
    parts = ", ".join(
        f"{os.path.basename(p)} {os.path.getsize(p):,}B" for p in out)
    print(f"[backup] 备份完成 ({dur:.1f}s) -> {dest}", flush=True)
    for p in out:
        print(f"[backup]   {os.path.basename(p)}  {os.path.getsize(p):,} B", flush=True)
    _log(cfg, f"备份完成 {dur:.1f}s keep={args.keep} -> {dest} | {parts}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
