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

from kbplatform import models as models_mod
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


def _call_pool_member(member, prompt: str) -> tuple[dict, dict]:
    """用池里的某个成员做结构化生成。

    远端 Ollama 必须带 base（地址）与 num_ctx（上下文覆盖），
    否则请求会打到本机、且长提示被静默截断。
    """
    extra = member.extra or {}
    return llm.chat_json(
        prompt, schema=EXTRACTION_SCHEMA,
        model=member.model_id,
        num_predict=2500, think=False, temperature=0.2,
        base=member.base_url,
        num_ctx=extra.get("num_ctx"),
        keep_alive=extra.get("keep_alive", "30m"),
    )


# 表格类文档按行切块（实测 11,727 篇占全部块的 81%），行数据对问答价值低，
# 默认不参与知识提炼；它们仍然被切块索引，检索/下载不受影响。
SKIP_DETAIL = "按策略跳过知识提炼（表格类）"


def is_skipped_ext(ext: str | None, skip_exts) -> bool:
    if not ext or not skip_exts:
        return False
    e = ext.strip().lower()
    return e in {str(s).strip().lower() for s in skip_exts}


def mark_extract_skipped(conn, catalog_id: int) -> None:
    """标记为「已处理但跳过提炼」。

    不能只是不入队：tier2_done 会一直是 0，看板上这些文档会永远显示
    「待提炼」。置 tier2_done=1 + status_detail 既能收口，也能在详情里解释。
    """
    conn.execute(
        "UPDATE catalog SET tier2_done=1, kp_count=0, status_detail=?, updated_at=?"
        " WHERE id=?", (SKIP_DETAIL, time.time(), catalog_id))
    conn.commit()


def _model_db_id(conn, model_id: str) -> int | None:
    """把配置里的模型名对回 models.id（用量统计按 id 记）。"""
    if not model_id:
        return None
    row = conn.execute("SELECT id FROM models WHERE model_id=? ORDER BY id LIMIT 1",
                       (model_id,)).fetchone()
    return int(row[0]) if row else None


def _record_usage(conn, model_id: int | None, meta: dict) -> None:
    """记一次成功调用。用量是旁路观测，写失败不能把提炼判成失败。"""
    if not model_id:
        return
    try:
        models_mod.record_usage(conn, model_id,
                                prompt_tokens=meta.get("prompt_tokens") or 0,
                                output_tokens=meta.get("eval_tokens") or 0)
    except Exception as e:
        print(f"[extract] 用量记录失败: {type(e).__name__}: {e}", flush=True)


def extract_file(conn, catalog_id: int, unc_path: str, chunk_ids: list[int],
                 model: str = "qwen3.5-9b-64k", pool=None, member=None) -> ExtractResult:
    """对文件的每个块做 LLM 抽取，写入 knowledge 与图谱。

    model   指定模型（单模型模式）。为空且给了 pool 则走**算力池**。
    pool    models_mod.ParsePool 实例；按文档分派（同一份文档固定一个成员），
            不同文档分给不同成员，吞吐叠加且每份文档内部风格一致。
    member  已经占好的池成员。并发执行器先 pick 再派活，避免多个线程
            同时 pick 把名额超发；传进来时由本函数负责在结束时 release。
    """
    if member is None and pool is not None and not model:
        member = pool.pick()
        if member is None:
            return ExtractResult(catalog_id, 0, 0, False, "算力池当前无可用成员（都在忙或不可用）")
    single_id = _model_db_id(conn, model) if (member is None and model) else None

    n_kp = n_rel = 0
    try:
        for cid in chunk_ids:
            row = conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
            if not row or not row[0]:
                continue
            prompt = EXTRACT_PROMPT.format(chunk=row[0])
            try:
                if member is not None:
                    obj, meta = _call_pool_member(member, prompt)
                else:
                    obj, meta = llm.chat_json(
                        prompt, schema=EXTRACTION_SCHEMA, model=model,
                        num_predict=2500, think=False, temperature=0.2)
            except Exception as e:
                msg = f"提炼失败: {type(e).__name__}: {e}"
                set_status(conn, catalog_id, "failed", msg)
                return ExtractResult(catalog_id, n_kp, n_rel, False, msg)

            _record_usage(conn, member.id if member is not None else single_id, meta)

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
            # 立刻提交：下一次循环要调 LLM（可能几十秒），
            # 事务一直开着会把写锁攥住，Web 端的审计/登录
            # 等不到锁就会报 "database is locked"。
            conn.commit()
    finally:
        if member is not None and pool is not None:
            pool.release(member)

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
