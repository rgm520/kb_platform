"""平台 HTTP 服务：Web UI + 对外 API。

安全要点（spec §7.5.4 / §7.6 / §7.7）：
  * 检索每一路都过 scope 过滤
  * 命中涉密 -> 自动路由本地模型；涉密片段**默认不回传原文**
  * token 只存哈希；token 调用记审计
  * 限流按 token
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kbplatform import auth, models as models_mod
from kbplatform.config import Config, load_config
from kbplatform.db import init_db
from kbplatform.retrieval import Retriever
from kbplatform.scopes import (load_restricted, load_scopes, describe_scopes,
                               is_restricted)
from kbplatform.vectors import VectorStore

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(APP_DIR), "web")
SESSION_COOKIE = "kb_session"
SESSION_TTL = 8 * 3600
RATE_QPS = 5
RATE_DAILY = 500


# ------------------------------------------------------------ 请求体
# 必须定义在模块级：本文件启用了 from __future__ import annotations，
# 函数体内的局部类注解会变成字符串，FastAPI 解析不到 -> 退化成 query 参数。
class QueryReq(BaseModel):
    question: str
    model: str = "auto"
    top_k: int = 8
    include_text: bool = False


class SearchReq(BaseModel):
    query: str
    top_k: int = 20
    share: Optional[str] = None
    status: Optional[str] = None
    file_type: Optional[list[str]] = None


class LoginReq(BaseModel):
    username: str
    password: str


class UserReq(BaseModel):
    username: str
    password: str
    role: str = "user"
    display_name: Optional[str] = None
    dept: Optional[str] = None
    scopes: list[list[str]] = Field(default_factory=list)


class ScopeReq(BaseModel):
    scopes: list[list[str]] = Field(default_factory=list)


class ActiveReq(BaseModel):
    active: bool


class TokenReq(BaseModel):
    label: Optional[str] = None
    ttl_days: int = 90


class ModelReq(BaseModel):
    name: str
    provider: str
    base_url: str
    model_id: str
    api_key: Optional[str] = None
    is_local: bool = False
    is_default: bool = False


class AgentReq(BaseModel):
    name: str
    kind: str = "workbuddy"
    client_id: Optional[str] = None
    meta: Optional[dict] = None


class RestrictedReq(BaseModel):
    share: str
    subpath: str = ""
    restricted: bool = True
    label: Optional[str] = None


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


def make_app(config_path: str = "D:/opencode/config.toml") -> FastAPI:
    cfg = load_config(config_path)
    conn = init_db(cfg.db_path)
    vs = VectorStore(cfg.vector_dir, dim=cfg.embed_dim)
    retriever = Retriever(conn, cfg.fts_path, vs, embed_model=cfg.embed_model)
    secret = os.environ.get("KB_SESSION_SECRET") or secrets.token_hex(32)

    app = FastAPI(title="企业知识平台", version="1.0")
    app.state.cfg = cfg
    app.state.conn = conn
    app.state.retriever = retriever
    app.state.secret = secret
    app.state.rate = {}

    # ------------------------------------------------------------ 会话
    def make_session(user_id: int) -> str:
        payload = f"{user_id}:{int(time.time() + SESSION_TTL)}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def read_session(raw: str) -> Optional[int]:
        if not raw:
            return None
        try:
            uid, exp, sig = raw.rsplit(":", 2)
            payload = f"{uid}:{exp}"
            expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expect):
                return None
            if int(exp) < time.time():
                return None
            return int(uid)
        except Exception:
            return None

    def current_user(request: Request) -> Optional[dict]:
        uid = read_session(request.cookies.get(SESSION_COOKIE, ""))
        if uid is None:
            return None
        u = auth.get_user_by_id(conn, uid)
        if u is None or not u["active"]:
            return None
        return dict(u)

    def require_user(request: Request) -> dict:
        u = current_user(request)
        if u is None:
            raise HTTPException(401, detail="未登录")
        return u

    def require_admin(request: Request) -> dict:
        u = require_user(request)
        if u["role"] != "admin":
            raise HTTPException(403, detail="需要管理员权限")
        return u

    def token_user(authorization: Optional[str] = Header(None)) -> dict:
        """对外 API 鉴权：Bearer token。"""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, detail="缺少 Authorization: Bearer <token>")
        raw = authorization.split(None, 1)[1].strip()
        row = auth.resolve_token(conn, raw)
        if row is None:
            raise HTTPException(401, detail="token 无效、已吊销或已过期")
        u = auth.get_user_by_id(conn, row["user_id"])
        if u is None or not u["active"]:
            raise HTTPException(401, detail="token 对应账号不可用")
        # 限流
        day = time.strftime("%Y-%m-%d")
        key = (row["id"], day)
        st = app.state.rate.get(key, {"n": 0, "t": time.time()})
        if time.time() - st["t"] > 1.0:
            st["n"] = 0
            st["t"] = time.time()
        st["n"] += 1
        app.state.rate[key] = st
        if st["n"] > RATE_DAILY:
            raise HTTPException(429, detail="超出当日配额")
        return {"user": dict(u), "token": raw, "token_row": dict(row)}

    def actor(request: Request, authorization: Optional[str] = Header(None)) -> dict:
        """统一鉴权：优先 Bearer token，其次会话 cookie（Web UI）。

        两条通路拿到的都是「某个用户」，后续的 scope 过滤逻辑完全一致。
        """
        if authorization and authorization.lower().startswith("bearer "):
            ctx = token_user(authorization)
            ctx["via"] = "token"
            return ctx
        u = current_user(request)
        if u is None:
            raise HTTPException(401, detail="未登录")
        return {"user": u, "token": None, "token_row": None, "via": "session"}

    def scope_user_id(user: dict) -> Optional[int]:
        """需要 scope 过滤时返回 user_id；管理员不受范围限制。"""
        if user["role"] == "admin":
            return None
        return user["id"]

    def audit(user_id, token_prefix, action, question=None, hit_paths=None,
              model_used=None, restricted=False, ip=None):
        conn.execute(
            "INSERT INTO audit_log(user_id, token_prefix, action, question, hit_paths,"
            " model_used, restricted, ip, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, token_prefix, action, question,
             json.dumps(hit_paths or [], ensure_ascii=False), model_used,
             1 if restricted else 0, ip, time.time()))
        conn.commit()

    # ------------------------------------------------------------ 模型
    def pick_model(conn_, model_ref, user_id):
        all_models = models_mod.list_models(conn_)
        if not all_models:
            raise HTTPException(503, detail="尚未配置任何模型")
        chosen = None
        if model_ref and model_ref != "auto":
            for m in all_models:
                if str(m.id) == str(model_ref) or m.model_id == model_ref or m.name == model_ref:
                    chosen = m
                    break
            if chosen is None:
                raise HTTPException(400, detail=f"未知模型: {model_ref}")
        else:
            chosen = next((m for m in all_models if m.is_default), all_models[0])
        return chosen, [m for m in all_models if m.is_local]

    # ============================================================ 认证
    @app.post("/api/v1/auth/login")
    def api_login(req: LoginReq, response: Response):
        u = auth.verify_login(conn, req.username, req.password)
        if u is None:
            raise HTTPException(401, detail="用户名或密码错误")
        response.set_cookie(SESSION_COOKIE, make_session(u["id"]),
                            httponly=True, samesite="lax", max_age=SESSION_TTL)
        return {"user": {"id": u["id"], "username": u["username"],
                         "role": u["role"], "display_name": u["display_name"]},
                "scopes": describe_scopes(load_scopes(conn, u["id"]))}

    @app.post("/api/v1/auth/logout")
    def api_logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.post("/api/v1/auth/change-password")
    def api_change_password(req: ChangePwdReq, request: Request,
                            ctx=Depends(actor)):
        ok, msg = auth.change_password(conn, ctx["user"]["id"],
                                       req.old_password, req.new_password)
        if not ok:
            raise HTTPException(400, detail=msg)
        audit(ctx["user"]["id"], (ctx["token_row"] or {}).get("prefix", "session"),
              "change_password", None, None, None, False,
              request.client.host if request.client else None)
        return {"ok": True, "message": "密码已修改"}

    @app.get("/api/v1/me")
    def api_me(request: Request):
        u = require_user(request)
        return {"user": {"id": u["id"], "username": u["username"], "role": u["role"],
                         "display_name": u["display_name"], "dept": u["dept"]},
                "scopes": describe_scopes(load_scopes(conn, u["id"]))}

    # ============================================================ 问答
    ANSWER_SYSTEM = ("你是企业知识库问答助手。基于给定资料回答问题，"
                     "答案必须严格基于资料，资料不足要明说，不要编造。")

    def build_context(hits):
        parts = []
        for i, h in enumerate(hits, 1):
            parts.append(f"[片段{i}] 来源: {os.path.basename(h.file_path)} 位置: {h.locator}\n{h.text}")
        return "\n\n".join(parts)

    def do_query(req: QueryReq, user_id: int, model_ref: str, token_prefix=None,
                 ip=None, want_text=False):
        hits, stats = retriever.retrieve(req.question, user_id=user_id, topk=req.top_k)
        restricted = any(h.restricted for h in hits)
        requested, local_models = pick_model(conn, model_ref, user_id)
        used, switched = models_mod.route_for(restricted, requested, local_models)

        ctx = build_context(hits)
        prompt = (f"请基于下面的资料回答问题。资料中已有的内容直接引用并标注 [片段N]；"
                  f"资料没有覆盖的部分明确说明。\n\n资料：\n{ctx}\n\n问题：{req.question}")
        try:
            text, usage = models_mod.chat(used, prompt, system=ANSWER_SYSTEM,
                                          temperature=0.1, max_tokens=2000)
        except Exception as e:
            raise HTTPException(502, detail=f"模型调用失败: {type(e).__name__}: {e}")

        citations = []
        for i, h in enumerate(hits, 1):
            c = {"chunk_id": h.chunk_id, "file": os.path.basename(h.file_path),
                 "path": h.file_path, "locator": h.locator,
                 "snippet": "", "text": None,
                 "matched_by": h.matched_by, "restricted": h.restricted}
            # 涉密内容**一律不回传原文**（snippet 与 text 都空）；
            # 非涉密给 200 字摘录，全文只在显式 include_text 时回传。
            if not h.restricted:
                c["snippet"] = h.text[:200]
                if want_text:
                    c["text"] = h.text
            citations.append(c)

        if token_prefix:
            audit(user_id, token_prefix, "query", req.question,
                  [h.file_path for h in hits], used.model_id, restricted, ip)

        resp = {"answer": text, "citations": citations,
                "model_used": used.model_id, "model_requested": requested.model_id,
                "restricted": restricted, "model_switched": switched,
                "usage": usage, "request_id": "req_" + secrets.token_hex(6)}
        if restricted:
            resp["restricted_notice"] = "本次回答命中涉密内容，已锁定本地模型，且未回传片段原文"
        return resp, hits, restricted, used

    @app.post("/api/v1/query")
    def api_query(req: QueryReq, request: Request, ctx=Depends(actor)):
        u = ctx["user"]
        tok = ctx["token_row"]
        resp, _, _, _ = do_query(req, scope_user_id(u), req.model,
                                 token_prefix=(tok["prefix"] if tok else None),
                                 ip=request.client.host if request.client else None,
                                 want_text=req.include_text)
        return resp

    @app.post("/api/v1/search")
    def api_search(req: SearchReq, ctx=Depends(actor)):
        u = ctx["user"]
        hits, stats = retriever.retrieve(req.query, user_id=scope_user_id(u), topk=req.top_k)
        results = []
        for h in hits:
            if req.share and h.share != req.share:
                continue
            results.append({"chunk_id": h.chunk_id, "file": os.path.basename(h.file_path),
                            "path": h.file_path, "locator": h.locator,
                            "snippet": "" if h.restricted else h.text[:200],
                            "score": round(h.score, 5), "matched_by": h.matched_by,
                            "restricted": h.restricted})
        tok = ctx["token_row"]
        audit(u["id"], (tok["prefix"] if tok else "session"), "search", req.query,
              [h.file_path for h in hits], None,
              any(h.restricted for h in hits))
        return {"total": len(results), "results": results, "stats": stats}

    def scope_predicate(user: dict) -> tuple[str, list]:
        """可见范围 SQL 谓词。管理员不受范围限制（普通用户按勾选范围）。"""
        from kbplatform.scopes import visible_predicate
        if user["role"] == "admin":
            return "1=1", []
        scopes = load_scopes(conn, user["id"])
        return visible_predicate(user["id"], "c", scopes)

    # ============================================================ 图谱
    @app.get("/api/v1/graph/search")
    def api_graph_search(q: str = "", limit: int = 20, ctx=Depends(actor)):
        u = ctx["user"]
        rows = conn.execute(
            "SELECT id,name,type,mentions FROM nodes WHERE merged_to IS NULL "
            "AND (name LIKE ? OR norm LIKE ?) ORDER BY mentions DESC LIMIT ?",
            (f"%{q}%", f"%{q.lower()}%", limit)).fetchall()
        pred, params = scope_predicate(u)
        out = []
        for r in rows:
            # 节点可见性 = 其关联的知识记录至少有一条在范围内
            ok = conn.execute(
                f"""SELECT 1 FROM edges e JOIN chunks ch ON ch.id=e.chunk_id
                    JOIN catalog c ON c.path=ch.file_path
                    WHERE (e.src=? OR e.dst=?) AND {pred} LIMIT 1""",
                [r[0], r[0]] + params).fetchone()
            if ok is None and r[2] != "document":
                continue
            out.append({"id": r[0], "name": r[1], "type": r[2], "mentions": r[3]})
        return {"nodes": out}

    @app.get("/api/v1/graph/neighbors")
    def api_graph_neighbors(node: int, hops: int = 1, ctx=Depends(actor)):
        u = ctx["user"]
        pred, params = scope_predicate(u)
        seen = {int(node)}
        frontier = [int(node)]
        nodes_out, edges_out = [], []
        for _ in range(max(1, min(hops, 3))):
            nxt = []
            for nid in frontier:
                rows = conn.execute(
                    f"""SELECT e.src, e.dst, e.rel, ch.id
                        FROM edges e JOIN chunks ch ON ch.id=e.chunk_id
                        JOIN catalog c ON c.path=ch.file_path
                        WHERE (e.src=? OR e.dst=?) AND {pred}""",
                    [nid, nid] + params).fetchall()
                for s, d, rel, cid in rows:
                    edges_out.append({"src": s, "dst": d, "rel": rel})
                    for x in (s, d):
                        if x not in seen:
                            seen.add(x)
                            nxt.append(x)
            frontier = nxt
        for nid in seen:
            r = conn.execute("SELECT id,name,type,mentions FROM nodes WHERE id=?", (nid,)).fetchone()
            if r:
                nodes_out.append({"id": r[0], "name": r[1], "type": r[2], "mentions": r[3]})
        # 去重边
        uniq = []
        seen_e = set()
        for e in edges_out:
            k = (e["src"], e["dst"], e["rel"])
            if k not in seen_e:
                seen_e.add(k)
                uniq.append(e)
        return {"nodes": nodes_out, "edges": uniq}

    # ============================================================ 文档
    @app.get("/api/v1/documents")
    def api_documents(request: Request, status: Optional[str] = None,
                      share: Optional[str] = None, page: int = 1,
                      page_size: int = 50, ctx=Depends(actor)):
        u = ctx["user"]
        pred, params = scope_predicate(u)
        where = [pred]
        if status:
            where.append("c.status=?")
            params.append(status)
        if share:
            where.append("c.share=?")
            params.append(share)
        sql = f"SELECT c.* FROM catalog c WHERE {' AND '.join(where)} ORDER BY c.id LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            # 失败详情仅管理员可见
            if u["role"] != "admin":
                d.pop("status_detail", None)
            items.append(d)
        total = conn.execute(
            f"SELECT COUNT(*) FROM catalog c WHERE {' AND '.join(where)}", params).fetchone()[0]
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    @app.get("/api/v1/documents/stats")
    def api_documents_stats(ctx=Depends(actor)):
        u = ctx["user"]
        pred, params = scope_predicate(u)
        by_status = {}
        for r in conn.execute(
                f"SELECT c.status, COUNT(*) FROM catalog c WHERE {pred} GROUP BY c.status", params):
            by_status[r[0] or "pending"] = r[1]
        total = conn.execute(f"SELECT COUNT(*) FROM catalog c WHERE {pred}", params).fetchone()[0]
        tier1 = conn.execute(f"SELECT COUNT(*) FROM catalog c WHERE {pred} AND tier1_done=1", params).fetchone()[0]
        tier2 = conn.execute(f"SELECT COUNT(*) FROM catalog c WHERE {pred} AND tier2_done=1", params).fetchone()[0]
        non_doc = conn.execute("SELECT SUM(count) FROM catalog_stats").fetchone()[0] or 0
        return {"total": total, "by_status": by_status, "tier1_done": tier1,
                "tier2_done": tier2, "non_document_files": non_doc}

    @app.get("/api/v1/documents/{catalog_id}")
    def api_document(catalog_id: int, ctx=Depends(actor)):
        u = ctx["user"]
        pred, params = scope_predicate(u)
        r = conn.execute(
            f"SELECT c.* FROM catalog c WHERE c.id=? AND {pred}", [catalog_id] + params).fetchone()
        if r is None:
            raise HTTPException(404, detail="文档不存在或不可见")
        d = dict(r)
        kps = conn.execute(
            "SELECT summary, key_points FROM knowledge WHERE file_path=? ORDER BY chunk_id",
            (d["path"],)).fetchall()
        d["knowledge"] = [{"summary": k[0], "key_points": json.loads(k[1])} for k in kps]
        return d

    # ============================================================ 模型
    @app.get("/api/v1/models")
    def api_models(ctx=Depends(actor)):
        return {"models": [{"id": m.id, "name": m.name, "provider": m.provider,
                            "base_url": m.base_url, "model_id": m.model_id,
                            "is_local": m.is_local, "is_default": m.is_default,
                            "has_key": bool(m.api_key)} for m in models_mod.list_models(conn)]}

    # ============================================================ 智能体
    @app.get("/api/v1/agents")
    def api_agents(ctx=Depends(actor)):
        u = ctx["user"]
        rows = conn.execute(
            "SELECT id,name,kind,client_id,created_at,last_seen_at FROM agents "
            "WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
        return {"agents": [dict(r) for r in rows]}

    @app.post("/api/v1/agents")
    def api_agent_register(req: AgentReq, ctx=Depends(actor)):
        u = ctx["user"]
        cur = conn.execute(
            "INSERT INTO agents(user_id,name,kind,client_id,meta,created_at,last_seen_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (u["id"], req.name, req.kind, req.client_id,
             json.dumps(req.meta or {}, ensure_ascii=False), time.time(), time.time()))
        conn.commit()
        return {"id": cur.lastrowid, "user": u["username"], "name": req.name}

    # ============================================================ 管理台
    def _is_restricted_share(conn_, share: str) -> bool:
        """整个共享被标为涉密。"""
        r = conn_.execute(
            "SELECT 1 FROM dir_scopes WHERE share=? AND restricted=1 AND subpath=''",
            (share,)).fetchone()
        return r is not None

    def _is_restricted_sub(conn_, share: str, subpath: str) -> bool:
        """该子目录是否落在某个涉密规则内（自身或其祖先被标记）。"""
        rules = conn_.execute(
            "SELECT subpath FROM dir_scopes WHERE share=? AND restricted=1",
            (share,)).fetchall()
        s = (subpath or "").lower()
        for (rule,) in rules:
            rule = (rule or "").strip("\\").lower()
            if not rule:
                return True
            if s == rule or s.startswith(rule + "\\"):
                return True
        return False

    @app.get("/api/v1/admin/scopes/tree")
    def admin_scope_tree(ctx=Depends(require_admin)):
        """授权目录选择器的数据源。

        共享根来自盘符映射（[drives]），子目录**实时枚举 NAS** 得到，
        而不是只列已索引的——否则未接入监控的共享没法挑具体目录。
        每个子目录附带 catalog 里已索引的文件数。
        """
        from kbplatform import nas as nas_mod
        roots = nas_mod.share_roots(cfg.drive_map, cfg.share_names)
        counts = {}
        for row in conn.execute(
                "SELECT share, rel_path, COUNT(*) n FROM catalog GROUP BY share, rel_path"):
            share, rel, n = row[0], row[1] or "", row[2]
            segs = [s for s in rel.split("\\") if s]
            for depth in range(1, min(len(segs), 4)):
                d = "\\".join(segs[:depth])
                counts[(share, d)] = counts.get((share, d), 0) + n
        share_files = {}
        for (share, _d), n in counts.items():
            share_files[share] = share_files.get(share, 0) + n

        shares = []
        for name, unc in roots:
            dirs = nas_mod.list_subdirs(unc)
            shares.append({
                "name": name,
                "unc": unc,
                "files": share_files.get(name, 0),
                "restricted": _is_restricted_share(conn, name),
                "subdirs": [{"path": d, "files": counts.get((name, d), 0),
                              "restricted": _is_restricted_sub(conn, name, d)}
                             for d in dirs],
            })
        return {"shares": shares}

    @app.get("/api/v1/admin/scopes/expand")
    def admin_scope_expand(share: str = "", subpath: str = "",
                           ctx=Depends(require_admin)):
        """展开某目录的下一层子目录。"""
        from kbplatform import nas as nas_mod
        roots = dict(nas_mod.share_roots(cfg.drive_map, cfg.share_names))
        unc = roots.get(share)
        if not unc:
            raise HTTPException(404, detail="共享不存在")
        return {"subdirs": nas_mod.expand_subdir(unc, subpath)}

    @app.get("/api/v1/admin/users")
    def admin_users(ctx=Depends(require_admin)):
        rows = conn.execute("SELECT id,username,display_name,role,dept,active,created_at"
                            " FROM users ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["scopes"] = describe_scopes(load_scopes(conn, r["id"]))
            out.append(d)
        return {"users": out}

    @app.post("/api/v1/admin/users")
    def admin_create_user(req: UserReq, ctx=Depends(require_admin)):
        if auth.get_user(conn, req.username):
            raise HTTPException(400, detail="用户名已存在")
        uid = auth.create_user(conn, req.username, req.password, req.role,
                               req.display_name, req.dept)
        auth.set_scopes(conn, uid, [(s[0], s[1]) for s in req.scopes])
        return {"id": uid}

    @app.put("/api/v1/admin/users/{user_id}/scopes")
    def admin_set_scopes(user_id: int, req: ScopeReq, ctx=Depends(require_admin)):
        auth.set_scopes(conn, user_id, [(s[0], s[1]) for s in req.scopes])
        return {"ok": True, "scopes": describe_scopes(load_scopes(conn, user_id))}

    @app.put("/api/v1/admin/users/{user_id}/active")
    def admin_set_active(user_id: int, req: ActiveReq, ctx=Depends(require_admin)):
        """启用/停用账号。

        停用后该用户的 token 立刻失效（token_user 会因 active=0 报 401），
        飞书机器人也会拒答——这是停掉一个越权账号的唯一手段。
        收回 scope 不够：scope 只管知识库 API，管不住 agent 的 shell。
        """
        row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="用户不存在")
        conn.execute("UPDATE users SET active=? WHERE id=?",
                     (1 if req.active else 0, user_id))
        conn.commit()
        return {"ok": True, "active": bool(req.active)}

    @app.get("/api/v1/admin/restricted")
    def admin_restricted(ctx=Depends(require_admin)):
        rows = conn.execute("SELECT id,share,subpath,restricted,label FROM dir_scopes"
                            " ORDER BY share, subpath").fetchall()
        return {"dirs": [dict(r) for r in rows]}

    @app.put("/api/v1/admin/restricted")
    def admin_set_restricted(req: RestrictedReq, ctx=Depends(require_admin)):
        conn.execute(
            """INSERT INTO dir_scopes(share,subpath,restricted,label) VALUES (?,?,?,?)
               ON CONFLICT(share,subpath) DO UPDATE SET restricted=excluded.restricted,
                                                      label=excluded.label""",
            (req.share, (req.subpath or "").strip("\\"),
             1 if req.restricted else 0, req.label))
        conn.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/tokens")
    def admin_tokens(ctx=Depends(require_admin)):
        rows = conn.execute(
            "SELECT t.id,t.user_id,u.username,t.prefix,t.label,t.revoked,t.created_at,"
            "t.last_used_at,t.expires_at FROM api_tokens t JOIN users u ON u.id=t.user_id"
            " ORDER BY t.revoked ASC, t.id DESC").fetchall()
        return {"tokens": [dict(r) for r in rows]}

    @app.post("/api/v1/admin/tokens")
    def admin_issue_token(req: TokenReq, user_id: int = 0,
                          ctx=Depends(require_admin)):
        raw, tid = auth.issue_token(conn, user_id, req.label, req.ttl_days)
        return {"id": tid, "token": raw,
                "notice": "这是唯一一次显示明文 token，请立即交给用户"}

    @app.delete("/api/v1/admin/tokens/{token_id}")
    def admin_revoke_token(token_id: int, ctx=Depends(require_admin)):
        """吊销密钥。幂等；已吊销的再调一次不会报错。"""
        row = conn.execute("SELECT revoked FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="密钥不存在")
        auth.revoke_token(conn, token_id)
        return {"ok": True, "revoked": True}

    @app.delete("/api/v1/admin/tokens/{token_id}/purge")
    def admin_purge_token(token_id: int, ctx=Depends(require_admin)):
        """物理删除密钥记录。

        **只允许删除已吊销的**——必须先吊销再删除，避免"点一下就永久消失"
        而管理员自己没意识到这个 token 还在被某个智能体使用。
        """
        row = conn.execute("SELECT revoked, prefix, user_id FROM api_tokens WHERE id=?",
                           (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="密钥不存在")
        if not row["revoked"]:
            raise HTTPException(400, detail="请先吊销该密钥，再执行删除")
        conn.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
        conn.commit()
        return {"ok": True, "deleted": True, "prefix": row["prefix"]}

    @app.post("/api/v1/admin/models")
    def admin_add_model(req: ModelReq, ctx=Depends(require_admin)):
        mid = models_mod.add_model(conn, req.name, req.provider, req.base_url,
                                   req.model_id, req.api_key, req.is_local, req.is_default)
        return {"id": mid}

    @app.delete("/api/v1/admin/models/{model_id}")
    def admin_del_model(model_id: int, ctx=Depends(require_admin)):
        return {"ok": models_mod.delete_model(conn, model_id)}

    @app.get("/api/v1/admin/audit")
    def admin_audit(limit: int = 100, ctx=Depends(require_admin)):
        rows = conn.execute(
            "SELECT a.id,u.username,a.token_prefix,a.action,a.question,a.model_used,"
            "a.restricted,a.created_at FROM audit_log a LEFT JOIN users u ON u.id=a.user_id"
            " ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
        return {"audit": [dict(r) for r in rows]}

    # ============================================================ 健康检查
    @app.get("/health")
    def health():
        """存活探针。监督进程用它判断服务是否需要拉起。"""
        try:
            n_files = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            return {"status": "ok", "files": n_files, "chunks": n_chunks, "nodes": n_nodes}
        except Exception as e:
            return JSONResponse(status_code=503,
                                content={"status": "error", "error": str(e)[:200]})

    # ============================================================ 静态页面
    if os.path.isdir(WEB_DIR):
        class NoCacheStatic(StaticFiles):
            """静态资源禁用强缓存。

            内部工具更新频繁，若让浏览器缓存 app.js，用户会在更新后继续跑
            旧代码（表现为"改了没生效"、"功能对不上"）。
            这里用 no-cache + 每次回源校验（304），保证改动即时生效。
            """
            def file_response(self, *args, **kwargs):
                resp = super().file_response(*args, **kwargs)
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                return resp

        app.mount("/static", NoCacheStatic(directory=os.path.join(WEB_DIR, "static")),
                  name="static")

        @app.get("/")
        def index():
            return FileResponse(os.path.join(WEB_DIR, "index.html"))

        @app.get("/{page:path}")
        def spa(page: str):
            fp = os.path.join(WEB_DIR, page)
            if os.path.isfile(fp):
                return FileResponse(fp)
            return FileResponse(os.path.join(WEB_DIR, "index.html"))

    return app


app = make_app()
