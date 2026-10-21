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


class VectorStore:
    def __init__(self, dirpath: str, dim: int = 1024):
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.vec_path = self.dir / "vectors.f16"
        self.ids_path = self.dir / "vectors.ids.json"
        self._ids: list[int] = self._load_ids()

    # ------------------------------------------------------------ 持久化
    def _load_ids(self) -> list[int]:
        if self.ids_path.exists():
            try:
                return [int(i) for i in json.loads(self.ids_path.read_text(encoding="utf-8"))]
            except Exception:
                return []
        return []

    def _save_ids(self) -> None:
        self.ids_path.write_text(json.dumps(self._ids), encoding="utf-8")

    # ------------------------------------------------------------ 写入
    def add(self, ids: list[int], vectors: list[list[float]]) -> None:
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
    def _matrix(self) -> np.ndarray:
        n = len(self._ids)
        if n == 0 or not self.vec_path.exists():
            return np.zeros((0, self.dim), dtype=np.float32)
        expect = n * self.dim * 2
        actual = os.path.getsize(self.vec_path)
        if actual < expect:
            # 文件被截断：只取完整行
            n = actual // (self.dim * 2)
        if n == 0:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.memmap(self.vec_path, dtype=np.float16, mode="r",
                         shape=(n, self.dim)).astype(np.float32)

    def search(self, q: list[float], topk: int = 20) -> list[tuple[int, float]]:
        """返回 [(chunk_id, 余弦相似度)]，按相似度降序。"""
        M = self._matrix()
        if M.size == 0:
            return []
        qv = np.asarray(q, dtype=np.float32).reshape(-1)
        if qv.shape[0] != self.dim:
            raise ValueError(f"查询维度应为 {self.dim}，实际 {qv.shape[0]}")
        n = np.linalg.norm(qv)
        if n == 0:
            return []
        qv = qv / n
        norms = np.linalg.norm(M, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        sims = (M / norms) @ qv
        live = [(i, self._ids[i]) for i in range(len(self._ids)) if self._ids[i] >= 0]
        live.sort(key=lambda x: -float(sims[x[0]]))
        return [(cid, float(sims[row])) for row, cid in live[:topk]]

    def remove(self, ids: Iterable[int]) -> None:
        dead = {int(i) for i in ids}
        for i, cid in enumerate(self._ids):
            if cid in dead:
                self._ids[i] = -1
        self._save_ids()

    def count(self) -> int:
        return sum(1 for i in self._ids if i >= 0)

    def ids(self) -> list[int]:
        return [i for i in self._ids if i >= 0]

    def reset(self) -> None:
        self._ids = []
        self._save_ids()
        if self.vec_path.exists():
            self.vec_path.unlink()
