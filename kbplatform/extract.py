"""Tier 2：LLM 知识提炼 -> knowledge 表 -> 知识图谱。

约束（spec Global Constraint #5）：
  * 结构化生成必须 **JSON Schema 约束 + think=false + num_predict>=2500**
    （裸输出实测 40% 概率是非法 JSON）
  * 生成模型必须用 qwen3.5-9b-32k / 64k，**不得**用默认的 qwen3.5:9b
    （默认 runner 上下文 4096，会静默截断长文档）
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import kb.graph as pgraph
import kb.llm as llm
from kb.llm import EXTRACTION_SCHEMA
from kb.store import Store

from kbplatform.db import set_status

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


@dataclass
class ExtractResult:
    catalog_id: int
    key_points: int
    relations: int
    ok: bool
    error: str | None


def extract_file(conn, catalog_id: int, unc_path: str, chunk_ids: list[int],
                 model: str = "qwen3.5-9b-64k") -> ExtractResult:
    """对文件的每个块做 LLM 抽取，写入 knowledge 与图谱。"""
    n_kp = n_rel = 0
    for cid in chunk_ids:
        row = conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            obj, _meta = llm.chat_json(
                EXTRACT_PROMPT.format(chunk=row[0]),
                schema=EXTRACTION_SCHEMA,
                model=model,
                num_predict=2500,
                think=False,
                temperature=0.2,
            )
        except Exception as e:
            msg = f"提炼失败: {type(e).__name__}: {e}"
            set_status(conn, catalog_id, "failed", msg)
            return ExtractResult(catalog_id, n_kp, n_rel, False, msg)

        import json as _json
        conn.execute(
            "INSERT INTO knowledge(file_path, chunk_id, summary, key_points, relations, raw, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (unc_path, cid, obj.get("summary", ""),
             _json.dumps(obj.get("key_points", []), ensure_ascii=False),
             _json.dumps(obj.get("relations", []), ensure_ascii=False),
             _json.dumps(obj, ensure_ascii=False), time.time()))
        pgraph.build_from_extraction(Store.from_conn(conn), unc_path, cid, obj)
        n_kp += len(obj.get("key_points", []))
        n_rel += len(obj.get("relations", []))
    conn.commit()
    conn.execute("UPDATE catalog SET tier2_done=1, kp_count=?, updated_at=? WHERE id=?",
                 (n_kp, time.time(), catalog_id))
    conn.commit()
    return ExtractResult(catalog_id, n_kp, n_rel, True, None)


def canonicalize_pending(conn, batch: int = 2000) -> int:
    """分批做实体归一。返回本次处理的实体数。

    禁止把全量实体一次性送进 embed（spec Global Constraint #7）：
    数十万实体的一次性稠密 M@M.T 会到 TB 级内存。
    """
    return pgraph.canonicalize_entities(conn, batch=batch).get("processed", 0)
