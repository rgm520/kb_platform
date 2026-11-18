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
    """用 SQLite 备份 API 把 src 完整快照到 dest_path（覆盖同名旧文件）。

    失败时必须删掉目标半截文件：备份 API 中途挂掉的产物不是完整快照，
    留在盘上会被 prune_old 当成一份"有效"备份，恢复时直接坑。
    """
    if os.path.exists(dest_path):
        # 覆盖旧文件：backup API 对已有库是整库重写，但先删掉更干净，
        # 也避免撞上损坏的残文件。
        os.remove(dest_path)
    ok = False
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
        ok = True
        return dest_path
    finally:
        if not ok and os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError as e:
                print(f"[backup] 清理失败的半截快照失败 {dest_path}: {e}",
                      flush=True)


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


_MAX_TRIES = 3   # 源库还在变就整组重来的次数上限


def _chunk_fingerprint(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """chunks 表的廉价指纹（行数, max(id)），跨产物对齐用。

    kb.db 快照 → fts 快照 → 向量复制是顺序走的，窗口里流水线若写入了
    新 chunk，恢复集就是"旧 kb.db + 新向量"——ids.json 里出现恢复后
    库里根本不存在的 chunk id，而向量库是纯追加无 id 去重（vectors.py），
    同一个 id 会对应两条候选，检索静默错位。所以复制完必须回头看：
    指纹没变才算一个自洽的恢复集。chunks 表不存在时返回 None（没有可
    错位的东西，视为一致）。
    """
    try:
        row = conn.execute("SELECT count(*), max(id) FROM chunks").fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0] or 0, row[1] or 0)


def _snapshot_group(cfg, src: sqlite3.Connection, ts: str,
                    dest_dir: str) -> list[str]:
    """一轮快照：kb.db → fts.db（backup API）→ vectors（成对稳定复制）。"""
    paths: list[str] = []
    # 1) 主库快照（src 由调用方持有并最后统一关闭）
    paths.append(_backup_one(src, os.path.join(dest_dir, f"kb-{ts}.db")))

    # 2) FTS 库同属 SQLite，也走备份 API
    if os.path.exists(cfg.fts_path):
        fts_src = sqlite3.connect(cfg.fts_path, timeout=30)
        try:
            paths.append(_backup_one(fts_src, os.path.join(dest_dir,
                                                           f"fts-{ts}.db")))
        finally:
            fts_src.close()

    # 3) 向量文件（要求 f16 / ids 成对来自同一时刻）
    paths.extend(_copy_vectors(cfg, ts, dest_dir))
    return paths


def _discard_group(paths: list[str]) -> None:
    """整组作废：恢复集要么三样都同一时刻，要么一个不留。"""
    for p in paths:
        try:
            os.remove(p)
        except OSError as e:
            print(f"[backup] 清理作废备份文件失败 {p}: {e}", flush=True)


def backup_all(cfg, dest_dir: str, keep: int = 7) -> list[str]:
    """完整备份一次：kb.db + fts.db（backup API）+ vectors（文件复制）。

    返回全部保留的备份文件路径。三种产物共享同一个时间戳，恢复时配对使用。
    主库 → FTS → 向量文件顺序固定：向量行序对应 kb.db 里的 chunk_id，
    必须等两个库快照落袋后再复制；fts.db 与 kb.db 同属一次 run 的状态。
    顺序执行有窗口：若窗口里流水线写入了新 chunk，"旧 kb.db + 新向量"
    是静默检索损坏。因此每轮快照前后各取一次 chunks 指纹（行数/max(id)），
    变了就整组作废重来，最多 _MAX_TRIES 轮；仍不行就明确失败——
    宁可今晚没有备份，也不留下一个"看着完整、实际错位"的恢复集。
    """
    from kbplatform.db import connect

    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime(_TS_FMT)

    # 源库连接只开一次：现有 connect() 只设置连接 pragma，不做业务写入
    src = connect(cfg.db_path)
    try:
        for attempt in range(1, _MAX_TRIES + 1):
            fp_before = _chunk_fingerprint(src)
            paths = _snapshot_group(cfg, src, ts, dest_dir)
            fp_after = _chunk_fingerprint(src)
            if fp_before == fp_after:
                break            # 恢复集自洽，进入修剪
            # 库在备份窗口里还在变：整组作废，重照一份
            _discard_group(paths)
            if attempt == _MAX_TRIES:
                raise RuntimeError(
                    f"备份窗口内 chunks 持续变化（指纹 {fp_before} -> {fp_after}），"
                    f"{_MAX_TRIES} 轮都没拿到自洽恢复集；请停摄入后重试")
            print(f"[backup] 恢复集不自洽（指纹 {fp_before} -> {fp_after}），"
                  f"第 {attempt}/{_MAX_TRIES} 轮作废重来", flush=True)
    finally:
        src.close()

    # 4) 逐前缀清旧份，只保留本次产物与最近 keep 份
    kept: set[str] = set()
    for prefix in ("kb", "fts", "vectors"):
        kept.update(prune_old(dest_dir, keep=keep, prefix=prefix))
    return sorted(kept)


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
    from kbplatform.config import DEFAULT_CONFIG_PATH, load_config
    from kbplatform.paths import is_under_root

    ap = argparse.ArgumentParser(
        description="企业知识平台备份（SQLite 备份 API + 向量文件）")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH)
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
