"""轮询监控器：检测监控目录里的新增 / 修改 / 删除。

约束（spec §4.6）：**DS218play 的目录 mtime 不可靠**（实测新增文件时仅 1/5
概率变化），所以这里不做「目录 mtime 变化就重扫该子树」的增量，而是
每次轮询对监控根做全量 listdir 快照，再按**文件级 mtime + size** 比对。
监控范围小时这个开销可忽略。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from kbplatform.catalog import scan_root


@dataclass
class Change:
    path: str      # UNC
    kind: str      # added | modified | removed
    size: int
    mtime: float


class Monitor:
    def __init__(self, conn, roots: list[str], drive_map: dict[str, str],
                 qps: int = 20, poll_interval: int = 60):
        self.conn = conn
        self.roots = list(roots)
        self.drive_map = drive_map
        self.qps = qps
        self.poll_interval = poll_interval
        # 上次快照：path -> (size, mtime)
        self._snapshot: dict[str, tuple[int, float]] = {}

    def _current(self) -> dict[str, tuple[int, float]]:
        snap: dict[str, tuple[int, float]] = {}
        for root in self.roots:
            recs, _ = scan_root(root, self.drive_map, self.roots, qps=self.qps)
            for r in recs:
                snap[r.path] = (r.size, r.mtime)
        return snap

    def poll_once(self) -> list[Change]:
        """扫描一次并与上次快照比对。返回变更列表。"""
        cur = self._current()
        changes: list[Change] = []
        for path, (size, mtime) in cur.items():
            old = self._snapshot.get(path)
            if old is None:
                changes.append(Change(path, "added", size, mtime))
            elif old[0] != size or abs(old[1] - mtime) > 1e-6:
                changes.append(Change(path, "modified", size, mtime))
        for path, (size, mtime) in self._snapshot.items():
            if path not in cur:
                changes.append(Change(path, "removed", size, mtime))
        self._snapshot = cur
        return changes

    def run_forever(self, on_change: Callable[[list[Change]], None]) -> None:
        while True:
            try:
                changes = self.poll_once()
                if changes:
                    on_change(changes)
            except Exception as e:  # 监控不能因单次失败而退出
                print(f"[monitor] 轮询失败: {type(e).__name__}: {e}", flush=True)
            time.sleep(self.poll_interval)
