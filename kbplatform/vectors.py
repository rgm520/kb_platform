"""fp16 向量库。

约束（spec Global Constraint #4）：**必须 fp16 存盘 + mmap 读，不得物化 fp32 矩阵。**
按真实 chunk 规模外推，fp32 会多占一倍内存（140 万块 × 1024 × 4B = 5.4GB）。

存储布局（vector_dir 下）：
  vectors.f16        N × dim 的 fp16 矩阵，行序即写入序，**只追加不重排**
  vectors.ids.json   行序对应的 chunk_id 列表；-1 表示已删除的墓碑
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import numpy as np

# 分块扫描的行数。16384 × 1024 × 4B ≈ 64MB 峰值——
# 68.8 万块时把整张 fp16 矩阵一次转成 fp32 要 2.6GB，2026-11-11
# 直接把 Web 进程打成 MemoryError，/api/v1/query 全线 500。
SCAN_BLOCK = 16384


class VectorStore:
    def __init__(self, dirpath: str, dim: int = 1024):
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.vec_path = self.dir / "vectors.f16"
        self.ids_path = self.dir / "vectors.ids.json"
        self._ids: list[int] = self._load_ids()
        self._ids_stat = self._stat_key()

    # ------------------------------------------------------------ 持久化
    def _load_ids(self) -> list[int]:
        if self.ids_path.exists():
            try:
                return [int(i) for i in json.loads(self.ids_path.read_text(encoding="utf-8"))]
            except Exception:
                return []
        return []

    def _stat_key(self) -> tuple[int, int] | None:
        try:
            st = self.ids_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """ids 文件变了就重载。

        入库是**独立进程**（kbplatform.pipeline），Web 进程里这个实例在启动时
        只加载一次。不重载的话，新文档的向量要重启 Web 才可见——用户看到的是
        「上传了但语义搜不到」（2026-10-23 实测：新 chunk 在 /search 里
        matched_by 为空，只能靠图谱通道蹭进来）。
        """
        key = self._stat_key()
        if key != self._ids_stat:
            self._ids = self._load_ids()
            self._ids_stat = key

    def _save_ids(self) -> None:
        self.ids_path.write_text(json.dumps(self._ids), encoding="utf-8")
        self._ids_stat = self._stat_key()

    # ------------------------------------------------------------ 写入
    def add(self, ids: list[int], vectors: list[list[float]]) -> None:
        self._refresh()          # 先并上其它进程已写入的行，避免覆盖
        if not ids:
            return
        if len(ids) != len(vectors):
            raise ValueError(f"ids/vectors 长度不一致: {len(ids)} vs {len(vectors)}")
        arr = np.asarray(vectors, dtype=np.float16)
        if arr.ndim != 2 or arr.shape[1] != self.dim:
            raise ValueError(f"向量维度应为 {self.dim}，实际 {arr.shape}")
        with open(self.vec_path, "ab") as f:
            f.write(arr.tobytes())
        self._ids.extend(int(i) for i in ids)
        self._save_ids()

    # ------------------------------------------------------------ 读取
    def _scores(self, qv: np.ndarray, n: int) -> np.ndarray:
        """分块算余弦相似度，绝不物化整张 fp32 矩阵。"""
        sims = np.zeros(n, dtype=np.float32)
        if n == 0 or not self.vec_path.exists():
            return sims
        mm = np.memmap(self.vec_path, dtype=np.float16, mode="r",
                       shape=(n, self.dim))
        for start in range(0, n, SCAN_BLOCK):
            block = np.asarray(mm[start:start + SCAN_BLOCK], dtype=np.float32)
            norms = np.linalg.norm(block, axis=1)
            norms[norms == 0] = 1.0
            sims[start:start + block.shape[0]] = (block @ qv) / norms
        return sims

    def search(self, q: list[float], topk: int = 20) -> list[tuple[int, float]]:
        """返回 [(chunk_id, 余弦相似度)]，按相似度降序。"""
        self._refresh()
        n = len(self._ids)
        if n == 0 or not self.vec_path.exists():
            return []
        # 文件被截断时只取完整行
        n = min(n, os.path.getsize(self.vec_path) // (self.dim * 2))
        if n == 0:
            return []
        qv = np.asarray(q, dtype=np.float32).reshape(-1)
        if qv.shape[0] != self.dim:
            raise ValueError(f"查询维度应为 {self.dim}，实际 {qv.shape[0]}")
        norm = np.linalg.norm(qv)
        if norm == 0:
            return []
        qv = qv / norm

        sims = self._scores(qv, n)
        ids = np.asarray(self._ids[:n], dtype=np.int64)
        live = np.flatnonzero(ids >= 0)
        if live.size == 0:
            return []
        live_sims = sims[live]
        k = min(topk, live.size)
        # argpartition 只挑候选，避免给 68 万行建 tuple 列表再全量排序
        cand = np.argpartition(-live_sims, k - 1)[:k]
        cand = cand[np.argsort(-live_sims[cand], kind="stable")]
        return [(int(ids[live[i]]), float(live_sims[i])) for i in cand]

    def remove(self, ids: Iterable[int]) -> None:
        self._refresh()
        dead = {int(i) for i in ids}
        for i, cid in enumerate(self._ids):
            if cid in dead:
                self._ids[i] = -1
        self._save_ids()

    def count(self) -> int:
        self._refresh()
        return sum(1 for i in self._ids if i >= 0)

    def ids(self) -> list[int]:
        self._refresh()
        return [i for i in self._ids if i >= 0]

    def reset(self) -> None:
        self._ids = []
        self._save_ids()
        if self.vec_path.exists():
            self.vec_path.unlink()
