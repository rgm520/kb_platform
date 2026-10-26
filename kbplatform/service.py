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
import urllib.parse
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, Body
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kbplatform import auth, models as models_mod
from kbplatform import branding
from kbplatform import captcha
from kbplatform import grants as grants_mod
from kbplatform import hashing, shortcut
from kbplatform import fs_service
from kbplatform.config import Config, load_config
from kbplatform.db import init_db, ThreadLocalConn, purge_expired_trace_data, write_with_retry
from kbplatform.fs_service import provision_sources_from_config, register_fs_routes
from kbplatform.retrieval import Retriever
from kbplatform.scanner import SourceScanner
from kbplatform.scopes import (load_restricted, load_scopes, describe_scopes,
                               is_restricted, unified_predicate)
from kbplatform.vectors import VectorStore

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(APP_DIR), "web")
SESSION_COOKIE = "kb_session"
SESSION_TTL = 8 * 3600
RATE_QPS = 5
RATE_DAILY = 500

# 密钥失效时给出的提示语。写成**可执行**的指令，让 AI 读了就知道下一步做什么，
# 而不是只丢一句"无效"让调用方反复重试。
_AUTH_FAIL_HINT = {
    "prefix": ("你发来的是密钥前缀（前 12 位），不是密钥本身。"
               "完整密钥约 35 位，形如 kb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx。"
               "请让知识库管理员把完整密钥发给你，不要用前缀重试。"),
    "revoked": ("该密钥已被管理员吊销，不能再使用。"
                "请改用最新的一枚：如果管理员已在本机或配置里为你放好了密钥文件，"
                "直接读取它，不要复用历史对话/旧配置里的旧密钥；"
                "确实找不到时，才联系管理员重新签发（完整密钥约 35 位）。"),
    "expired": ("该密钥已过期，不能再使用。"
                "请让管理员重新签发一枚，并把完整密钥（约 35 位）发给你。"),
    "unknown": ("密钥无效：平台里没有这枚密钥。"
                "请确认拿到的是**完整密钥**（约 35 位）而不是列表里显示的前缀（前 12 位）；"
                "若确认无误，请让管理员重新签发一枚。"),
}


def _client_ip(request: Request) -> str | None:
    """取客户端 IP。反代后面看 X-Forwarded-For，没有就取直连地址。"""
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else None
    except Exception:
        return None


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


class FeedbackReq(BaseModel):
    """答案反馈：rating 三选一，comment 可选（wrong_cite 常带一句说明）。"""
    rating: str                      # up | down | wrong_cite
    comment: Optional[str] = None


class LoginReq(BaseModel):
    username: str
    password: str
    # 连续登录失败后才要求验证码
    captcha_id: Optional[str] = None
    captcha_code: Optional[str] = None


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


class UserUpdateReq(BaseModel):
    """管理台修改用户信息；username 不可改（登录标识，token 绑定）。"""
    display_name: Optional[str] = None
    role: Optional[str] = None
    dept: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None
    username: Optional[str] = None      # 兼容前端传了但忽略


class DuplicateResolveReq(BaseModel):
    """处理一组重复文件：删掉 delete_ids，保留 keep_id 那一份。"""
    keep_id: int
    delete_ids: list[int] = Field(default_factory=list)
    make_shortcut: bool = False      # 是否在被删文件原位置留 .lnk 快捷方式


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


def make_app(config_path: str = "D:/kb-platform/config.toml") -> FastAPI:
    cfg = load_config(config_path)
    init_db(cfg.db_path)                      # 建表 + 补列（幂等）
    # 请求线程各自持有一条连接：共享一条连接时，别的线程 commit 会把
    # 本线程尚未 fetch 的游标重置，表现为随机的 "NoneType is not subscriptable"。
    conn = ThreadLocalConn(cfg.db_path)
    # 配置里已经在监控的目录自动登记为存储源：管理员登录即可看到全部共享文件夹
    try:
        provision_sources_from_config(conn, cfg)
    except Exception as e:  # 自动接入失败不能挡住服务启动
        print(f"[service] 自动接入配置共享失败: {type(e).__name__}: {e}")
    # 老授权同步到 2.0 的目录授权：文件页与文档看板用同一套权限
    try:
        moved = grants_mod.migrate_user_scopes(conn, cfg)
        if moved:
            print(f"[service] 已把 {moved} 条老范围同步为目录授权")
    except Exception as e:
        print(f"[service] 老授权同步失败: {type(e).__name__}: {e}")
    vs = VectorStore(cfg.vector_dir, dim=cfg.embed_dim)
    retriever = Retriever(conn, cfg.fts_path, vs, embed_model=cfg.embed_model)
    secret = os.environ.get("KB_SESSION_SECRET") or secrets.token_hex(32)

    # 密钥密文的加解密主密钥。用 config.toml 的 session_secret 派生，
    # 这样重启服务后仍能解开历史密钥（管理员可以重新查看完整密钥）。
    auth.set_encryption_key(cfg.session_secret or secret, cfg.token_recoverable)

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

    def token_user(request: Request,
                   authorization: Optional[str] = Header(None)) -> dict:
        """对外 API 鉴权：Bearer token。

        失败时做三件事：
          1. 判定失败原因（前缀 / 已吊销 / 已过期 / 不存在）
          2. 落一条审计（action='auth_failed'），记下客户端 IP 与 User-Agent，
             这样"谁的客户端还在用旧密钥"是**看得见**的，而不是只能靠猜
          3. 401 的 detail 写成**可执行**的提示，AI 读了就知道该干什么
        """
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, detail="缺少 Authorization: Bearer <token>")
        raw = authorization.split(None, 1)[1].strip()
        row = auth.resolve_token(conn, raw)
        if row is None:
            reason = auth.diagnose_token(conn, raw)
            detail = _AUTH_FAIL_HINT.get(reason, _AUTH_FAIL_HINT["unknown"])
            try:
                audit(None, raw[:12] or "(空)", "auth_failed",
                      question=f"presented_len={len(raw)} reason={reason}",
                      hit_paths=None, model_used=None, restricted=False,
                      ip=_client_ip(request))
            except Exception:
                pass
            raise HTTPException(401, detail=detail)
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
        if authorization:
            if not authorization.lower().startswith("bearer "):
                raise HTTPException(
                    401, detail="Authorization 头格式不对，应为 "
                               "`Authorization: Bearer <token>`（<token> 是约 35 位的完整密钥）")
            ctx = token_user(request, authorization)
            ctx["via"] = "token"
            return ctx
        u = current_user(request)
        if u is None:
            raise HTTPException(
                401, detail="未登录：缺少 `Authorization: Bearer <token>` 请求头。"
                           "API 调用请带完整密钥（约 35 位，不是前 12 位前缀）；"
                           "浏览器使用请先在平台登录。")
        return {"user": u, "token": None, "token_row": None, "via": "session"}

    def scope_user_id(user: dict) -> Optional[int]:
        """需要 scope 过滤时返回 user_id；管理员不受范围限制。"""
        if user["role"] == "admin":
            return None
        return user["id"]

    def audit(user_id, token_prefix, action, question=None, hit_paths=None,
              model_used=None, restricted=False, ip=None):
        # 审计是旁路：并发入库时数据库可能被写锁占住几十秒。
        # 重试仍失败就丢弃并记一行日志，绝不能把登录/查库变成 500。
        ok = write_with_retry(
            conn,
            "INSERT INTO audit_log(user_id, token_prefix, action, question, hit_paths,"
            " model_used, restricted, ip, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, token_prefix, action, question,
             json.dumps(hit_paths or [], ensure_ascii=False), model_used,
             1 if restricted else 0, ip, time.time()))
        if not ok:
            print(f"[audit] 数据库繁忙，丢弃一条审计记录 action={action}", flush=True)

    # ------------------------------------------------- 观测数据保留期（30 天）
    # 第二重清理保险放在写入点上（pipeline 每轮是第一重）：pipeline 进程没跑、
    # 只有 Web 服务活着时，问答还在发生，写入点顺带清就能兜住。
    # _trace_purge_state 是 make_app 实例内的闭包状态：每个进程各自节流，
    # 每 10 分钟最多真清一次（清失败下轮再来）。
    _trace_purge_state = {"t": 0.0}
    TRACE_PURGE_MIN_INTERVAL = 600.0

    def _maybe_purge_traces():
        now_t = time.time()
        if now_t - _trace_purge_state["t"] < TRACE_PURGE_MIN_INTERVAL:
            return
        _trace_purge_state["t"] = now_t
        purge_expired_trace_data(conn)

    # ------------------------------------------------------------ 2.0 文件域
    app.state.scanner = SourceScanner(conn, cfg)
    register_fs_routes(app, cfg, conn, require_user, require_admin, audit)
    try:
        from kbplatform.fs_service import cleanup_uploads
        cleanup_uploads(cfg, conn)
    except Exception:
        pass
    if cfg.source_scan_interval_sec > 0:
        app.state.scanner.start()

    @app.on_event("shutdown")
    def _stop_scanner():
        try:
            app.state.scanner.stop()
        except Exception:
            pass

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
    @app.get("/api/v1/auth/captcha")
    def api_captcha(username: str = "", request: Request = None):
        """登录验证码：连续失败到阈值后才需要，此时下发一张图片。

        公开接口（登录前就要用）。答案只存在服务端，库里存哈希。
        """
        client = _client_ip(request) if request is not None else ""
        need = captcha.captcha_required(conn, client, username)
        if not need:
            return {"required": False}
        issued = captcha.issue(conn, cfg.data_dir, username, client)
        issued["required"] = True
        return issued

    @app.post("/api/v1/auth/login")
    def api_login(req: LoginReq, request: Request, response: Response):
        client = _client_ip(request)
        # 连续失败到阈值 → 必须带验证码
        if captcha.captcha_required(conn, client, req.username):
            if not captcha.verify(conn, req.captcha_id or "", req.captcha_code or ""):
                raise HTTPException(400, detail={
                    "message": "请输入正确的验证码",
                    "captcha_required": True,
                })
        u = auth.verify_login(conn, req.username, req.password)
        if u is None:
            fails = captcha.add_fail(conn, client, req.username)
            audit(None, "session", "auth_failed",
                  question=f"username={req.username} fails={fails}",
                  hit_paths=None, model_used=None, restricted=False,
                  ip=client)
            if fails >= captcha.FAIL_THRESHOLD:
                raise HTTPException(401, detail={
                    "message": "用户名或密码错误（连续失败多次，请填写验证码）",
                    "captcha_required": True,
                })
            raise HTTPException(401, detail="用户名或密码错误")
        captcha.reset_fails(conn, client, req.username)
        response.set_cookie(SESSION_COOKIE, make_session(u["id"]),
                            httponly=True, samesite="lax", max_age=SESSION_TTL)
        return {"user": {"id": u["id"], "username": u["username"],
                         "role": u["role"], "display_name": u["display_name"]},
                "scopes": _user_scope_labels(u["id"])}

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

    # ------------------------------------------------------------ 品牌（公开读）
    # 登录页在**登录之前**就要显示 logo 与系统名称，所以品牌信息公开可读；
    # 修改只能管理员。
    @app.get("/api/v1/branding")
    def api_branding():
        return branding.branding_state(conn, cfg.data_dir)

    @app.get("/api/v1/branding/logo")
    def api_branding_logo():
        path = branding.logo_path(conn, cfg.data_dir)
        if not path:
            raise HTTPException(404, detail="没有设置 logo")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/v1/admin/branding")
    def admin_branding(user=Depends(require_admin)):
        return branding.branding_state(conn, cfg.data_dir)

    @app.put("/api/v1/admin/branding")
    def admin_set_branding(payload: dict = Body(...), user=Depends(require_admin)):
        try:
            name = branding.set_system_name(conn, payload.get("system_name", ""))
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        audit(user["id"], None, "branding_update", question=name, ip=None)
        return branding.branding_state(conn, cfg.data_dir)

    @app.post("/api/v1/admin/branding/logo")
    async def admin_upload_logo(request: Request, user=Depends(require_admin)):
        form = await request.form()
        up = form.get("file")
        if up is None:
            raise HTTPException(400, detail="缺少文件")
        data = await up.read()
        try:
            branding.save_logo(conn, cfg.data_dir, getattr(up, "filename", ""),
                               getattr(up, "content_type", "") or "", data)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        audit(user["id"], None, "branding_logo", question="上传 logo", ip=None)
        return branding.branding_state(conn, cfg.data_dir)

    @app.delete("/api/v1/admin/branding/logo")
    def admin_remove_logo(user=Depends(require_admin)):
        branding.remove_logo(conn, cfg.data_dir)
        audit(user["id"], None, "branding_logo", question="移除 logo", ip=None)
        return branding.branding_state(conn, cfg.data_dir)

    @app.get("/api/v1/me")
    def api_me(ctx=Depends(actor)):
        """当前身份与可见目录。

        用 actor 而不是 require_user：**token 调用也必须能用**——
        手册把 /me 列为 token 可用接口，AI 智能体要靠它确认自己的可见范围。
        """
        u = ctx["user"]
        return {"user": {"id": u["id"], "username": u["username"], "role": u["role"],
                         "display_name": u["display_name"], "dept": u["dept"]},
                "scopes": _user_scope_labels(u["id"]),
                "via": ctx.get("via", "session")}

    # ============================================================ 问答
    ANSWER_SYSTEM = ("你是企业知识库问答助手。基于给定资料回答问题，"
                     "答案必须严格基于资料，资料不足要明说，不要编造。")

    def build_context(hits):
        parts = []
        for i, h in enumerate(hits, 1):
            parts.append(f"[片段{i}] 来源: {os.path.basename(h.file_path)} 位置: {h.locator}\n{h.text}")
        return "\n\n".join(parts)

    def do_query(req: QueryReq, user_id: int, model_ref: str, token_prefix=None,
                 ip=None, want_text=False, audit_uid=None):
        # user_id  = 可见范围过滤用的用户 id（管理员为 None = 不限）
        # audit_uid= 审计归属用的真实用户 id（管理员也要记上，否则查不到是谁在用）
        trace: dict = {}   # 检索轨迹出参：四路召回 id / 融合名次 / 是否被 ACL 压掉
        hits, stats = retriever.retrieve(req.question, user_id=user_id,
                                         topk=req.top_k, trace=trace)
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
            # 让 AI 能顺着片段找到整篇文档
            c["document_id"] = h.document_id
            c["content_url"] = f"/api/v1/documents/{h.document_id}/content"
            c["download_url"] = f"/api/v1/documents/{h.document_id}/download"
            citations.append(c)

        # 审计：Web 会话（没有 token）也要记，并归属到提问的人 ——
        # 早先只在有 token 时才记，导致浏览器里问的问题查不到，
        # 「每个用户的提问记录」也就无从隔离/审计了。
        audit(audit_uid if audit_uid is not None else user_id,
              token_prefix or "session", "query", req.question,
              [h.file_path for h in hits], used.model_id, restricted, ip)

        resp = {"answer": text, "citations": citations,
                "model_used": used.model_id, "model_requested": requested.model_id,
                "restricted": restricted, "model_switched": switched,
                "usage": usage, "request_id": "req_" + secrets.token_hex(6)}
        if restricted:
            resp["restricted_notice"] = "本次回答命中涉密内容，已锁定本地模型，且未回传片段原文"

        # 检索轨迹：让"不准"变得可度量。只存 chunk id / 分数 / 命中通道，
        # 问题只存哈希前 16 位——轨迹表与审计分开（审计有 question，轨迹没有，
        # 因为轨迹会被更多眼睛看到）。落轨迹失败只记日志，绝不把问答变 500。
        try:
            trace_owner = audit_uid if audit_uid is not None else user_id
            write_with_retry(
                conn,
                "INSERT INTO retrieval_traces(request_id, user_id, question_hash,"
                " route_stats, candidates, top_ids, created_at) VALUES (?,?,?,?,?,?,?)",
                (resp["request_id"], trace_owner,
                 hashlib.sha256(req.question.encode()).hexdigest()[:16],
                 json.dumps(stats, ensure_ascii=False),
                 json.dumps(trace, ensure_ascii=False),
                 json.dumps([h.chunk_id for h in hits]),
                 time.time()))
        except Exception as e:
            print(f"[trace] 轨迹写入异常 request_id={resp['request_id']}: "
                  f"{type(e).__name__}: {e}", flush=True)

        # 30 天保留期（spec §3 A1）的第二重保险：写轨迹的同一个写入点顺带
        # 清理（幂等、走 created_at 索引）。节流到每 10 分钟一次，正常轮里
        # 只是一次空索引查询；失败只记日志，绝不影响响应。
        try:
            _maybe_purge_traces()
        except Exception as e:
            print(f"[trace] 过期轨迹清理失败（忽略）: {type(e).__name__}: {e}",
                  flush=True)
        return resp, hits, restricted, used

    @app.post("/api/v1/query")
    def api_query(req: QueryReq, request: Request, ctx=Depends(actor)):
        u = ctx["user"]
        tok = ctx["token_row"]
        resp, _, _, _ = do_query(req, scope_user_id(u), req.model,
                                 token_prefix=(tok["prefix"] if tok else None),
                                 ip=_client_ip(request),
                                 want_text=req.include_text,
                                 audit_uid=u["id"])
        return resp

    FEEDBACK_RATINGS = ("up", "down", "wrong_cite")

    @app.post("/api/v1/query/{request_id}/feedback")
    def api_query_feedback(request_id: str, req: FeedbackReq, ctx=Depends(actor)):
        """答案反馈：用户点「没用」，这里记下来。

        反馈是旁路数据：写库失败也**绝不能**报错给用户——用户点了"没用"
        却看到 500，比不记录更糟（参考 audit() 的容错风格：写不进去只记
        日志，然后就当成功返回）。
        """
        if req.rating not in FEEDBACK_RATINGS:
            raise HTTPException(400, detail="rating 必须是 up / down / wrong_cite 之一")
        comment = (req.comment or "").strip() or None
        try:
            ok = write_with_retry(
                conn,
                "INSERT INTO query_feedback(request_id, user_id, rating, comment,"
                " created_at) VALUES (?,?,?,?,?)",
                (request_id, ctx["user"]["id"], req.rating, comment, time.time()))
            if not ok:
                print(f"[feedback] 数据库繁忙，丢弃一条反馈 request_id={request_id}",
                      flush=True)
        except Exception as e:
            # no such table / 模式损坏等都走这里：反馈丢失可以接受，
            # 用户点一个按钮换来 500 不可以。
            print(f"[feedback] 反馈写入异常 request_id={request_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

        # 写入点顺带清过期的观测数据（幂等、节流 10 分钟一次；失败不影响本接口）
        try:
            _maybe_purge_traces()
        except Exception as e:
            print(f"[feedback] 过期反馈清理失败（忽略）: {type(e).__name__}: {e}",
                  flush=True)
        # 就算写库失败也返回 ok：反馈丢失可以接受，用户体验断裂不可以
        return {"ok": True}

    @app.get("/api/v1/admin/traces")
    def admin_traces(limit: int = 50, ctx=Depends(require_admin)):
        """检索轨迹列表（仅管理员）：时间 / request_id / 各路召回数 / 反馈情况。

        与审计不同，这里**没有** question 原文——轨迹会被管理员反复查看，
        按红线只保留问题哈希，需要看问题原文去 audit_log 对 request 时间段查。
        """
        rows = conn.execute(
            """SELECT t.id, t.request_id, t.user_id, u.username, t.question_hash,
                      t.route_stats, t.created_at,
                      (SELECT f.rating FROM query_feedback f
                        WHERE f.request_id=t.request_id
                        ORDER BY f.id DESC LIMIT 1) AS feedback_rating,
                      (SELECT f.comment FROM query_feedback f
                        WHERE f.request_id=t.request_id
                        ORDER BY f.id DESC LIMIT 1) AS feedback_comment
               FROM retrieval_traces t LEFT JOIN users u ON u.id=t.user_id
               ORDER BY t.id DESC LIMIT ?""",
            (max(1, min(500, limit)),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["route_stats"] = json.loads(d["route_stats"])
            except Exception:
                d["route_stats"] = {}
            out.append(d)
        return {"traces": out}

    @app.get("/api/v1/admin/traces/{request_id}")
    def admin_trace_detail(request_id: str, ctx=Depends(require_admin)):
        """单条轨迹详情（仅管理员）：四路召回 id、融合名次与命中通道。

        管理员在这里回答"这次到底召回了什么、被哪路压下去了"，
        是后续检索优化的定位依据。
        """
        r = conn.execute(
            "SELECT id, request_id, user_id, question_hash, route_stats,"
            " candidates, top_ids, created_at FROM retrieval_traces"
            " WHERE request_id=?", (request_id,)).fetchone()
        if r is None:
            raise HTTPException(404, detail="没有这条轨迹")
        trace = json.loads(r["candidates"])
        try:
            stats = json.loads(r["route_stats"])
        except Exception:
            stats = {}
        try:
            top_ids = json.loads(r["top_ids"])
        except Exception:
            top_ids = []
        fb = conn.execute(
            "SELECT rating, comment FROM query_feedback WHERE request_id=?"
            " ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
        return {"trace": {"request_id": r["request_id"], "user_id": r["user_id"],
                          "question_hash": r["question_hash"],
                          "created_at": r["created_at"],
                          "route_stats": stats, "top_ids": top_ids,
                          "feedback": dict(fb) if fb else None,
                          **trace}}

    @app.post("/api/v1/search")
    def api_search(req: SearchReq, ctx=Depends(actor)):
        u = ctx["user"]
        hits, stats = retriever.retrieve(req.query, user_id=scope_user_id(u), topk=req.top_k)
        results = []
        for h in hits:
            if req.share and h.share != req.share:
                continue
            results.append({"chunk_id": h.chunk_id, "document_id": h.document_id,
                            "file": os.path.basename(h.file_path),
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
        """可见范围 SQL 谓词：管理员不受限；普通用户按 2.0 目录授权 ∪ 老范围。

        唯一实现在 kbplatform.scopes.unified_predicate：文件页、看板、检索、
        问答共用同一套「grants ∪ 老范围」，改授权语义只改一处。
        """
        if user["role"] == "admin":
            return "1=1", []
        return unified_predicate(conn, user["id"], "c")

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
            # （document 类型也按同一标准判定，避免泄露范围外的文件名）
            ok = conn.execute(
                f"""SELECT 1 FROM edges e JOIN chunks ch ON ch.id=e.chunk_id
                    JOIN catalog c ON c.path=ch.file_path
                    WHERE (e.src=? OR e.dst=?) AND {pred} LIMIT 1""",
                [r[0], r[0]] + params).fetchone()
            if ok is None:
                continue
            out.append({"id": r[0], "name": r[1], "type": r[2], "mentions": r[3]})
        return {"nodes": out}

    @app.get("/api/v1/graph/overview")
    def api_graph_overview(limit: int = 60, ctx=Depends(actor)):
        """图谱页初始视图：范围内被提及最多的节点及它们之间的关系。

        没有这个接口时，图谱页要先搜索才出图，用户打开看到的是空白画布。
        """
        u = ctx["user"]
        pred, params = scope_predicate(u)
        limit = max(10, min(limit, 200))
        rows = conn.execute(
            f"""SELECT n.id, n.name, n.type, n.mentions FROM nodes n
                WHERE n.merged_to IS NULL AND EXISTS (
                  SELECT 1 FROM edges e JOIN chunks ch ON ch.id=e.chunk_id
                  JOIN catalog c ON c.path=ch.file_path
                  WHERE (e.src=n.id OR e.dst=n.id) AND {pred})
                ORDER BY n.mentions DESC LIMIT ?""",
            params + [limit]).fetchall()
        nodes = [{"id": r[0], "name": r[1], "type": r[2], "mentions": r[3]} for r in rows]
        ids = [r[0] for r in rows]
        edges = []
        if ids:
            marks = ",".join("?" * len(ids))
            erows = conn.execute(
                f"""SELECT e.src, e.dst, e.rel FROM edges e
                    JOIN chunks ch ON ch.id=e.chunk_id
                    JOIN catalog c ON c.path=ch.file_path
                    WHERE e.src IN ({marks}) AND e.dst IN ({marks}) AND {pred}
                    GROUP BY e.src, e.dst, e.rel LIMIT 500""",
                ids + ids + params).fetchall()
            edges = [{"src": r[0], "dst": r[1], "rel": r[2]} for r in erows]
        return {"nodes": nodes, "edges": edges}

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
    @app.get("/api/v1/graph/subgraph")
    def api_graph_subgraph(node: int, hops: int = 2, types: str = "",
                           ctx=Depends(actor)):
        """以某节点为中心取 N 跳子图（节点 + 边），供前端渲染。

        types 逗号分隔，筛选节点类型：entity,knowledge_point,category
        """
        u = ctx["user"]
        pred, params = scope_predicate(u)
        want = {t.strip() for t in (types or "").split(",") if t.strip()}
        seen = {int(node)}
        frontier = [int(node)]
        edges_out: list[dict] = []
        for _ in range(max(1, min(hops, 3))):
            nxt: list[int] = []
            for nid in frontier:
                rows = conn.execute(
                    f"""SELECT e.src, e.dst, e.rel FROM edges e
                        JOIN chunks ch ON ch.id=e.chunk_id
                        JOIN catalog c ON c.path=ch.file_path
                        WHERE (e.src=? OR e.dst=?) AND {pred}""",
                    [nid, nid] + params).fetchall()
                for s, d, rel in rows:
                    edges_out.append({"src": s, "dst": d, "rel": rel})
                    for x in (s, d):
                        if x not in seen:
                            seen.add(x)
                            nxt.append(x)
            frontier = nxt
        nodes_out = []
        for nid in seen:
            r = conn.execute("SELECT id,name,type,mentions FROM nodes WHERE id=?",
                             (nid,)).fetchone()
            if not r:
                continue
            if want and r[2] not in want:
                continue
            nodes_out.append({"id": r[0], "name": r[1], "type": r[2], "mentions": r[3]})
        uniq, seen_e = [], set()
        for e in edges_out:
            k = (e["src"], e["dst"], e["rel"])
            if k not in seen_e:
                seen_e.add(k)
                uniq.append(e)
        return {"nodes": nodes_out, "edges": uniq}

    def _mask_restricted_detail(user: dict, restricted: list, d: dict) -> None:
        """status_detail 的涉密脱敏（结构性判断，不做字符串替换）。

        用现成的涉密判定（scopes.is_restricted / load_restricted）判断该文档
        是否命中涉密目录；命中就把整个 status_detail 换成固定文案「（涉密目录）」，
        失败原因里的任何路径都不再出现，也不泄露是哪个涉密目录。
        管理员不受影响；status_detail 缺失/为空时无需脱敏。
        """
        if not d.get("status_detail") or user.get("role") == "admin":
            return
        if is_restricted(restricted, d["share"], d["rel_path"]):
            d["status_detail"] = "（涉密目录）"

    @app.get("/api/v1/documents")
    def api_documents(request: Request, status: Optional[str] = None,
                      share: Optional[str] = None, page: int = 1,
                      page_size: int = 50, tier2: int = 0,
                      ctx=Depends(actor)):
        u = ctx["user"]
        page = max(1, page)
        page_size = min(max(10, page_size), 200)
        pred, params = scope_predicate(u)
        where = [pred]
        if status:
            where.append("c.status=?")
            params.append(status)
        if share:
            where.append("c.share LIKE ?")
            params.append(f"%{share}%")
        if tier2:
            # 看板统计卡片的「已进入知识图谱 (Tier 2)」联动过滤
            where.append("c.tier2_done=1")
        sql = f"SELECT c.* FROM catalog c WHERE {' AND '.join(where)} ORDER BY c.id LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [page_size, (page - 1) * page_size]).fetchall()
        # 失败原因对普通用户也可见了（spec C4：不再只让人看见"坏"）；
        # 命中涉密目录的文档按记录做结构性脱敏，不借助失败原因泄露涉密路径
        restricted = load_restricted(conn) if u["role"] != "admin" else []
        items = []
        for r in rows:
            d = dict(r)
            _mask_restricted_detail(u, restricted, d)
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
        # 非文档文件按扩展名分类统计：普通用户只算自己可见共享的那些
        shares = None
        if u["role"] != "admin":
            shares = set()
            for g in grants_mod.user_grants(conn, u):
                row = conn.execute("SELECT name FROM storage_sources WHERE id=?",
                                   (g.source_id,)).fetchone()
                if row:
                    shares.add(row["name"])
            for s in load_scopes(conn, u["id"]):
                shares.add(s.share)
        by_ext = []
        if shares is not None and not shares:
            shares = set()          # 没有任何可见共享 → 计数为 0
        else:
            sql = "SELECT ext, SUM(count) FROM catalog_stats"
            sparams: list = []
            if shares is not None:
                sql += " WHERE share IN (%s)" % ",".join("?" * len(shares))
                sparams = list(shares)
            sql += " GROUP BY ext ORDER BY SUM(count) DESC"
            for r in conn.execute(sql, sparams):
                by_ext.append({"ext": r[0], "count": r[1] or 0})
        non_doc = sum(x["count"] for x in by_ext)
        # 「已跳过」有两种来源，统计要分开：扫描件待 OCR / 源文件已删除
        skipped_reasons = []
        if u["role"] == "admin":
            rows = conn.execute(
                "SELECT CASE WHEN needs_ocr=1 THEN '扫描件待 OCR' "
                "            WHEN status_detail LIKE '%源文件已删除%' THEN '源文件已删除' "
                "            ELSE COALESCE(NULLIF(status_detail,''),'其它原因') END AS reason, "
                "       COUNT(*) FROM catalog WHERE status='skipped' GROUP BY reason").fetchall()
        else:
            rows = conn.execute(
                f"SELECT CASE WHEN c.needs_ocr=1 THEN '扫描件待 OCR' "
                f"            WHEN c.status_detail LIKE '%源文件已删除%' THEN '源文件已删除' "
                f"            ELSE COALESCE(NULLIF(c.status_detail,''),'其它原因') END AS reason, "
                f"       COUNT(*) FROM catalog c WHERE c.status='skipped' AND {pred} GROUP BY reason",
                params).fetchall()
        for r in rows:
            skipped_reasons.append({"reason": r[0], "count": r[1] or 0})
        # 重复文档（按 MD5 内容哈希判定，不按文件名）
        dup = hashing.duplicate_summary(conn, pred, params)
        return {"total": total, "by_status": by_status, "tier1_done": tier1,
                "tier2_done": tier2, "non_document_files": non_doc,
                "non_doc_by_ext": by_ext, "skipped_reasons": skipped_reasons,
                "duplicates": {"group_count": dup["group_count"],
                               "file_count": dup["file_count"],
                               "wasted_bytes": dup["wasted_bytes"]}}

    def _get_catalog_row(catalog_id: int, user: dict):
        """按可见范围取 catalog 行，取不到一律 404（不区分不存在与不可见）。"""
        pred, params = scope_predicate(user)
        r = conn.execute(
            f"SELECT c.* FROM catalog c WHERE c.id=? AND {pred}",
            [catalog_id] + params).fetchone()
        if r is None:
            raise HTTPException(404, detail="文档不存在或不可见")
        return dict(r)

    @app.get("/api/v1/documents/duplicates")
    def api_document_duplicates(ctx=Depends(actor)):
        """按 MD5 内容哈希找重复文档（不按文件名判断）。

        只返回当前用户可见范围内的重复分组。
        """
        u = ctx["user"]
        pred, params = scope_predicate(u)
        data = hashing.duplicate_summary(conn, pred, params)
        return data

    @app.post("/api/v1/documents/duplicates/resolve")
    def api_document_duplicates_resolve(req: "DuplicateResolveReq", ctx=Depends(actor)):
        """处理一组重复文档：删掉选中的重复件，保留一份。

        可选在**被删文件原位置**留一个 .lnk 快捷方式，指向保留的那份。
        删除走平台回收站（可恢复）。
        """
        u = ctx["user"]
        if u["role"] != "admin":
            raise HTTPException(403, detail="只有管理员可以清理重复文件")
        pred, params = scope_predicate(u)
        keep_id = int(req.keep_id)
        delete_ids = [int(x) for x in (req.delete_ids or []) if int(x) != keep_id]
        if not delete_ids:
            raise HTTPException(400, detail="没有选择要删除的重复文件")
        keep = conn.execute("SELECT * FROM catalog WHERE id=?", (keep_id,)).fetchone()
        if keep is None:
            raise HTTPException(404, detail="要保留的文件不存在")
        if keep["md5"] is None:
            raise HTTPException(400, detail="该文件还没算过哈希，无法判定重复")
        deleted, skipped = [], []
        for did in delete_ids:
            row = conn.execute("SELECT * FROM catalog WHERE id=?", (did,)).fetchone()
            if row is None:
                skipped.append({"id": did, "reason": "文件不存在"})
                continue
            if row["md5"] != keep["md5"]:
                skipped.append({"id": did, "reason": "与保留文件的哈希不同"})
                continue
            # 找到它对应的存储源与相对路径
            src = fs_service.source_for_path(conn, row["path"])
            if src is None:
                skipped.append({"id": did, "reason": "文件不在任何已接入的存储源里"})
                continue
            source, adapter, rel = src
            gs = grants_mod.user_grants(conn, u)
            if not grants_mod.can_write(u, gs, source.id, rel):
                skipped.append({"id": did, "reason": "没有写权限"})
                continue
            trash_id, name, reason = fs_service.move_to_trash(
                conn, cfg, app.state.scanner.vs, source, adapter, rel, u)
            if trash_id is None:
                skipped.append({"id": did, "reason": reason})
                continue
            # 目录行删掉（文件是有意清理的，不是"源文件消失"）
            conn.execute("DELETE FROM catalog WHERE id=?", (did,))
            conn.commit()
            # 快捷方式：放在被删文件的原位置，指向保留的那份
            link_done, link_msg = False, ""
            if req.make_shortcut:
                link_path = row["path"]
                link_done, link_msg = shortcut.create_shortcut(link_path, keep["path"])
            deleted.append({"id": did, "name": name, "trash_id": trash_id,
                            "shortcut": link_done, "shortcut_msg": link_msg})
        audit(u["id"], None, "duplicates_resolve",
              question=f"md5={keep['md5'][:8]} 删除{len(deleted)}份", ip=None)
        return {"ok": True, "deleted": deleted, "skipped": skipped,
                "kept": {"id": keep_id, "name": keep["name"], "path": keep["path"]}}

    @app.get("/api/v1/documents/{catalog_id}")
    def api_document(catalog_id: int, ctx=Depends(actor)):
        u = ctx["user"]
        d = _get_catalog_row(catalog_id, u)
        # 列表接口已放开失败原因；详情接口保持同一规则：
        # 普通用户命中涉密目录的文档同样脱敏，不能从详情弹窗绕过
        if u["role"] != "admin":
            _mask_restricted_detail(u, load_restricted(conn), d)
        kps = conn.execute(
            "SELECT summary, key_points FROM knowledge WHERE file_path=? ORDER BY chunk_id",
            (d["path"],)).fetchall()
        d["knowledge"] = [{"summary": k[0], "key_points": json.loads(k[1])} for k in kps]
        d["content_url"] = f"/api/v1/documents/{catalog_id}/content"
        d["download_url"] = f"/api/v1/documents/{catalog_id}/download"
        d["knowledge_url"] = f"/api/v1/documents/{catalog_id}/knowledge"
        return d

    @app.get("/api/v1/documents/{catalog_id}/file")
    def api_document_file(catalog_id: int, ctx=Depends(actor)):
        """文档对应的实际文件位置（看板右键菜单用）。

        已经在 2.0 索引里的文档 → 给出 (source_id, rel) 与 2.0 的预览/下载地址；
        还没进 2.0 索引的老目录 → 回退到老的 content / download 接口。
        """
        d = _get_catalog_row(catalog_id, ctx["user"])
        fe = conn.execute(
            "SELECT source_id, rel_path, name FROM fs_entries WHERE catalog_id=?",
            (catalog_id,)).fetchone()
        source_id, rel = None, None
        if fe is not None:
            source_id, rel = int(fe["source_id"]), (fe["rel_path"] or "")
        else:
            # 文件还没被 2.0 扫描索引到时，按路径前缀匹配已接入的存储源
            path = str(d["path"] or "")
            best = None
            for s in conn.execute("SELECT id, root FROM storage_sources WHERE enabled=1"):
                root = str(s["root"] or "").rstrip("\\")
                if root and path.lower().startswith(root.lower() + "\\"):
                    if best is None or len(root) > len(best[1]):
                        best = (int(s["id"]), root)
            if best is not None:
                source_id, root = best
                rel = path[len(root) + 1:]
        if source_id is not None:
            qs = urllib.parse.quote((rel or "").replace("\\", "/"), safe="/")
            return {
                "document_id": catalog_id,
                "name": (fe["name"] if fe is not None else d["name"]),
                "path": d["path"],
                "in_fs": True,
                "source_id": source_id,
                "rel": rel,
                "preview_url": f"/api/v1/fs/preview?source_id={source_id}&rel={qs}",
                "download_url": f"/api/v1/fs/download?source_id={source_id}&rel={qs}",
            }
        return {
            "document_id": catalog_id,
            "name": d["name"],
            "path": d["path"],
            "in_fs": False,
            "source_id": None,
            "rel": None,
            "preview_url": f"/api/v1/documents/{catalog_id}/content",
            "download_url": f"/api/v1/documents/{catalog_id}/download",
        }

    @app.get("/api/v1/documents/{catalog_id}/content")
    def api_document_content(catalog_id: int, ctx=Depends(actor)):
        """取文档的**完整纯文本**（所有块按顺序拼接）。

        检索/问答只返回命中的片段；AI 需要通读全文时用这个。
        涉密文档的原文**不回传**（返回 403），与平台的出网管控一致。
        """
        d = _get_catalog_row(catalog_id, ctx["user"])
        if _is_restricted_sub(conn, d["share"], d["rel_path"]):
            raise HTTPException(403, detail="该文档位于涉密目录，不提供原文内容")
        rows = conn.execute(
            "SELECT chunk_idx, text, locator FROM chunks WHERE file_path=? ORDER BY chunk_idx",
            (d["path"],)).fetchall()
        text = "\n\n".join(r[1] or "" for r in rows)
        return {
            "document_id": catalog_id,
            "name": d["name"],
            "path": d["path"],
            "chars": len(text),
            "chunks": len(rows),
            "locators": [r[2] for r in rows],
            "text": text,
        }

    @app.post("/api/v1/documents/reparse-batch")
    def api_document_reparse_batch(ctx=Depends(require_admin)):
        """把「扫描件待 OCR」的文档批量排队重跑（后台队列里慢慢做）。

        扫描件默认不自动 OCR（136 秒/页会堵住队列），这里由管理员统一点名，
        排进 jobs 表由流水线后台执行，不阻塞 Web 服务。
        """
        from kbplatform.jobs import JobQueue
        rows = conn.execute(
            "SELECT id, path FROM catalog WHERE status='skipped' AND needs_ocr=1"
        ).fetchall()
        q = JobQueue(conn)
        queued, missing = 0, 0
        for r in rows:
            if not os.path.exists(r["path"]):
                missing += 1
                continue
            conn.execute(
                "UPDATE catalog SET status='pending', status_detail=NULL, needs_ocr=1, "
                "ocr_requested=1, tier1_done=0, tier2_done=0, updated_at=? WHERE id=?",
                (time.time(), int(r["id"])))
            q.enqueue("index_ocr", int(r["id"]), force=True)
            queued += 1
        conn.commit()
        return {"ok": True, "queued": queued, "missing": missing,
                "notice": f"已把 {queued} 个扫描件排进队列；OCR 约 136 秒/页，"
                          f"在后台慢慢跑（单个文件最多 20 页 / 10 分钟），"
                          f"期间可以继续用平台。进度看「文档看板」的解析状态。"}

    @app.post("/api/v1/documents/{catalog_id}/reparse")
    def api_document_reparse(catalog_id: int, ctx=Depends(actor)):
        """手动重新解析：失败/扫描件跳过的文档在这里重跑。

        扫描件默认"存量不自动 OCR"（单页约 136 秒，批量会堵死队列），
        但管理员点名重跑时这次允许做 OCR（置 ocr_requested=1）。
        """
        from kbplatform.jobs import JobQueue
        d = _get_catalog_row(catalog_id, ctx["user"])
        if not os.path.exists(d["path"]):
            raise HTTPException(404, detail="源文件已不存在，无法重跑")
        conn.execute(
            "UPDATE catalog SET status='pending', status_detail=NULL, needs_ocr=1, "
            "ocr_requested=1, tier1_done=0, tier2_done=0, updated_at=? WHERE id=?",
            (time.time(), catalog_id))
        conn.commit()
        # 低优先级队列：OCR 很慢，不能挡住普通文档的解析
        JobQueue(conn).enqueue("index_ocr", catalog_id, force=True)
        return {"ok": True, "queued": True, "name": d["name"],
                "notice": "已重新排队（低优先级）；扫描件需要 OCR，约 136 秒/页，"
                          "会排在其它文档之后慢慢跑，请稍后刷新看状态。"}

    @app.get("/api/v1/documents/{catalog_id}/knowledge")
    def api_document_knowledge(catalog_id: int, ctx=Depends(actor)):
        """取文档提炼出的知识点与关系（不返回原文，避免涉密外流）。"""
        d = _get_catalog_row(catalog_id, ctx["user"])
        rows = conn.execute(
            "SELECT chunk_id, summary, key_points, relations FROM knowledge "
            "WHERE file_path=? ORDER BY chunk_id", (d["path"],)).fetchall()
        out = []
        for r in rows:
            out.append({
                "chunk_id": r[0], "summary": r[1],
                "key_points": json.loads(r[2] or "[]"),
                "relations": json.loads(r[3] or "[]"),
            })
        n_kp = sum(len(o["key_points"]) for o in out)
        n_rel = sum(len(o["relations"]) for o in out)
        return {"document_id": catalog_id, "name": d["name"],
                "chunks": len(out), "key_points": n_kp, "relations": n_rel,
                "knowledge": out}

    @app.get("/api/v1/documents/{catalog_id}/download")
    def api_document_download(catalog_id: int, ctx=Depends(actor)):
        """下载**原始文件**（从 NAS 流式读取）。

        涉密目录的原文不提供下载。
        """
        d = _get_catalog_row(catalog_id, ctx["user"])
        if _is_restricted_sub(conn, d["share"], d["rel_path"]):
            raise HTTPException(403, detail="该文档位于涉密目录，不提供原文下载")
        path = d["path"]
        if not os.path.exists(path):
            raise HTTPException(404, detail="源文件已不存在")
        fname = d["name"]
        # 文件名含中文/空格时用 RFC 5987 编码，兼容老客户端
        quoted = urllib.parse.quote(fname)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{quoted}",
                "X-Document-Id": str(catalog_id),
            },
        )

    # ============================================================ 模型
    @app.get("/api/v1/models")
    def api_models(ctx=Depends(actor), only_usable: bool = True):
        """模型列表。

        默认只返回**可用**的模型——没配 API Key 的云端模型、连不上的 Ollama
        主机都不可用，列出来只是噪音。需要看全部时传 only_usable=false。
        """
        out = []
        for m in models_mod.list_models(conn):
            st = models_mod.check_usable(m)
            if only_usable and not st["usable"]:
                continue
            if m.provider == "ollama":
                host = m.base_url.replace("http://", "").replace("https://", "")
                host = host.split(":")[0]
                tag = "[本机]" if host in ("localhost", "127.0.0.1") else f"[{host}]"
            else:
                tag = "[云端]"
            out.append({"id": m.id, "name": m.name,
                        "label": f"{m.name} {tag}",
                        "provider": m.provider, "base_url": m.base_url,
                        "model_id": m.model_id,
                        "is_local": m.is_local, "is_default": m.is_default,
                        "has_key": bool(m.api_key),
                        "num_ctx": (m.extra or {}).get("num_ctx"),
                        "usable": st["usable"], "reason": st.get("reason", ""),
                        "latency_ms": st.get("latency_ms")})
        return {"models": out}

    @app.get("/api/v1/admin/models/presets")
    def admin_model_presets(ctx=Depends(require_admin)):
        """添加模型对话框里的预置模板（不落库）。"""
        return {"presets": models_mod.PRESETS}

    @app.get("/api/v1/admin/api-guide")
    def admin_api_guide(request: Request, ctx=Depends(require_user),
                       token_id: Optional[int] = None):
        """对外 API 调用手册（Markdown）。

        token_id 指定时会把该密钥的**明文**替换进示例里，方便管理员直接
        复制给 AI 智能体；不指定则用占位符。

        **普通用户也能取**：这是给自己智能体用的客户端说明书，不含管理侧数据；
        手册开头会附上调用者自己的可见目录，方便 AI 知道"能问到什么"。
        """
        from kbplatform import api_guide as guide_mod
        base = (cfg.public_base_url or str(request.base_url)).rstrip("/")
        # 去掉可能带的 /api/v1 尾巴
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        tok = None
        if token_id:
            row = conn.execute(
                "SELECT user_id, token_hash FROM api_tokens WHERE id=? AND revoked=0",
                (token_id,)).fetchone()
            # 明文不可逆存储，拿不到；用前缀占位并提示
            if row:
                if ctx["role"] != "admin" and int(row["user_id"]) != int(ctx["id"]):
                    raise HTTPException(403, detail="只能使用自己的密钥")
                tok = row["token_hash"][:12] + "...(明文仅签发时显示一次，请手动粘贴)"
        labels = []
        for m in models_mod.list_models(conn):
            st = models_mod.check_usable(m)
            if st["usable"]:
                labels.append(f"`{m.name}` (id={m.id}, model_id={m.model_id})")
        from kbplatform import api_guide as guide_mod
        deploy = guide_mod.build_deploy_info(
            os.path.dirname(APP_DIR).replace("\\", "/"))
        # 调用者自己的可见目录（AI 靠它知道能问到什么）
        scopes = list(describe_scopes(load_scopes(conn, ctx["id"])))
        for g in grants_mod.user_grants(conn, ctx):
            src = conn.execute("SELECT name FROM storage_sources WHERE id=?",
                               (g.source_id,)).fetchone()
            if src:
                label = f"{src['name']}" + (f"\\{g.subpath}" if g.subpath else "")
                if label not in scopes:
                    scopes.append(label)
        scope_text = "、".join(scopes) if scopes else "（暂无授权目录，仅能访问公开接口）"
        guide = guide_mod.build_guide(base, tok, labels, deploy)
        guide = (f"> **你的可见目录**：{scope_text}\n"
                 f"> （超出这个范围的文档，接口会返回空结果；"
                 f"这是正常的权限边界，不是故障。）\n\n") + guide
        return {
            "base_url": base,
            "guide": guide,
            "skill_snippet": guide_mod.build_skill_snippet(base + "/api/v1", tok),
            "scope": scope_text,
            "note": ("「API 密钥」页点「查看密钥」可以随时取出完整密钥（约 35 位），"
                     "把它填进上面的输入框，下载的手册就带着真实密钥，客户端可直接用。"
                     "注意：列表里显示的前缀（前 12 位）不是密钥，填进去会 401。"),
        }

    @app.get("/api/v1/admin/skill-package")
    def admin_skill_package(ctx=Depends(require_user)):
        """打包 WorkBuddy 技能包供下载（SKILL.md + scripts + references）。

        客户端说明书，任何登录用户都能下载（不含管理侧数据）。
        """
        import io
        import zipfile
        src = os.path.join(os.path.dirname(APP_DIR), "workbuddy-zs-kb")
        if not os.path.isdir(src):
            raise HTTPException(404, detail="技能包目录不存在")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _dirs, files in os.walk(src):
                for fn in files:
                    if fn.endswith((".pyc",)) or "__pycache__" in dirpath:
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, os.path.dirname(src))
                    z.write(full, rel.replace("\\", "/"))
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition":
                     'attachment; filename="workbuddy-zs-kb.zip"'})

    @app.get("/api/v1/admin/parse-pool")
    def admin_parse_pool(ctx=Depends(require_admin)):
        """文档解析算力池状态。"""
        pool = models_mod.ParsePool(conn)
        st = pool.status()
        st["note"] = ("向量化模型必须全局唯一（bge-m3），混用会污染向量空间；"
                      "池内只放生成模型。")
        return st

    @app.put("/api/v1/admin/parse-pool")
    def admin_set_parse_pool(ctx=Depends(require_admin),
                             body: dict = Body(...)):
        """设置哪些模型参与文档解析，以及各自的调度权重。"""
        members = body.get("members") or []
        ids = set()
        for m in members:
            mid = int(m.get("id"))
            ids.add(mid)
            models_mod.update_pool_member(conn, mid, True, float(m.get("weight", 1)))
        for m in models_mod.list_models(conn):
            if m.in_parse_pool and m.id not in ids:
                models_mod.update_pool_member(conn, m.id, False, m.pool_weight)
        return models_mod.ParsePool(conn).status()

    @app.post("/api/v1/admin/models/test")
    def admin_test_model(model_id: int, ctx=Depends(require_admin)):
        """测试模型连通性与上下文配置。

        远端 Ollama 最容易踩的坑是 runner 默认 num_ctx=4096 导致长提示被
        **静默截断**——这里用一段较长的提示回显实际处理的 token 数，
        让管理员一眼看出有没有被截。
        """
        m = models_mod.get_model(conn, model_id)
        if m is None:
            raise HTTPException(404, detail="模型不存在")
        probe = "这是一段用于验证上下文长度的中文提示。" * 400   # 约 1600 token
        t0 = time.time()
        try:
            text, usage = models_mod.chat(m, probe + "\n请只回复两个字：好",
                                         system="你是测试助手。",
                                         temperature=0.1, max_tokens=16)
        except Exception as e:
            raise HTTPException(502, detail=f"调用失败: {type(e).__name__}: {e}")
        dt = round(time.time() - t0, 2)
        pt = usage.get("prompt_tokens", 0)
        return {"ok": True, "latency_sec": dt, "prompt_tokens": pt,
                "output_tokens": usage.get("output_tokens", 0),
                "num_ctx": (m.extra or {}).get("num_ctx"),
                "truncated": bool((m.extra or {}).get("num_ctx")
                                  and pt < 1000 and len(probe) > 1000),
                "reply": text[:120]}

    @app.delete("/api/v1/admin/models/unusable")
    def admin_drop_unusable(ctx=Depends(require_admin)):
        """一键清掉不可用的模型（没配 Key 的云端模型、连不上的 Ollama）。"""
        removed = []
        for m in models_mod.list_models(conn):
            st = models_mod.check_usable(m)
            if not st["usable"]:
                models_mod.delete_model(conn, m.id)
                removed.append({"id": m.id, "name": m.name, "reason": st.get("reason", "")})
        return {"removed": removed, "count": len(removed)}

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

    def _user_scope_labels(user_id: int) -> list[str]:
        """用户可见范围的标签：授权（grants）∪ 老范围。

        与实际生效的口径一致（统一授权），只显示老表会让管理员以为"没给权限"。
        """
        out = list(grants_mod.describe_grants(conn, user_id))
        for lab in describe_scopes(load_scopes(conn, user_id)):
            if lab not in out:
                out.append(lab)
        return out

    @app.get("/api/v1/admin/users")
    def admin_users(ctx=Depends(require_admin)):
        rows = conn.execute("SELECT id,username,display_name,role,dept,active,created_at"
                            " FROM users ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["scopes"] = _user_scope_labels(r["id"])
            out.append(d)
        return {"users": out}

    def _apply_user_scopes(user_id: int, legacy: list[tuple[str, str]]) -> dict:
        """把"共享/子目录"范围同时落到 2.0 授权（grants）与老表（user_scopes）。

        文件页按 grants 过滤、看板按 grants ∪ 老范围过滤 —— 所以新范围
        必须**立刻**写进 grants，不能等下次平台启动时的迁移，否则新建的
        用户在文件页要等重启才看得到目录。
        """
        items, pending = [], []
        for share, subpath in legacy:
            sid = grants_mod.find_source_for_share(conn, cfg, share)
            if sid is None:
                pending.append((share, subpath))
            else:
                items.append({"source_id": sid, "subpath": (subpath or "").strip("\\"),
                              "can_write": False})
        if items:
            # 已有 2.0 授权的保留写权限，只覆盖目录范围
            old = {g.source_id: g for g in grants_mod.user_grants(conn, {"id": user_id, "role": "user"})}
            for it in items:
                if it["source_id"] in old and old[it["source_id"]].can_write:
                    it["can_write"] = True
            grants_mod.set_grants(conn, user_id, items)
        conn.execute("DELETE FROM user_scopes WHERE user_id=?", (int(user_id),))
        for share, subpath in pending:
            conn.execute("INSERT INTO user_scopes(user_id, share, subpath) VALUES (?,?,?)",
                         (int(user_id), share, (subpath or "").strip("\\")))
        conn.commit()
        return {"scopes": describe_scopes(load_scopes(conn, user_id)),
                "grants": [{"source_id": g.source_id, "subpath": g.subpath,
                            "can_write": g.can_write}
                           for g in grants_mod.user_grants(conn, {"id": user_id, "role": "user"})]}

    @app.post("/api/v1/admin/users")
    def admin_create_user(req: UserReq, ctx=Depends(require_admin)):
        username = (req.username or "").strip()
        if not username:
            raise HTTPException(400, detail="用户名不能为空")
        if len(username) > 64 or any(ch.isspace() or ord(ch) < 32 for ch in username):
            raise HTTPException(400, detail="用户名不能包含空格或控制字符，且不超过 64 字符")
        if not (req.password or "").strip() or len(req.password) < auth.MIN_PASSWORD_LEN:
            raise HTTPException(400, detail=f"初始密码至少 {auth.MIN_PASSWORD_LEN} 位")
        if auth.get_user(conn, username):
            raise HTTPException(409, detail="用户名已存在")
        dup = conn.execute("SELECT id FROM users WHERE lower(username)=lower(?)",
                           (username,)).fetchone()
        if dup is not None:
            raise HTTPException(409, detail="用户名已存在")
        uid = auth.create_user(conn, username, req.password, req.role,
                               req.display_name, req.dept)
        # 初始可见范围立刻生效（写进 grants），不等下次平台启动的迁移
        _apply_user_scopes(uid, [(s[0], s[1]) for s in req.scopes])
        return {"id": uid}

    @app.put("/api/v1/admin/users/{user_id}")
    def admin_update_user(user_id: int, req: "UserUpdateReq", ctx=Depends(require_admin)):
        """修改用户信息：显示名 / 角色 / 部门 / 启用状态，可选重置密码。

        用户名不可改（它是登录标识，也被 token 绑定）。
        防呆：不能把自己降级或停用，也不能把最后一个管理员降级/停用，
        否则没人能进管理台收拾残局。
        """
        row = auth.get_user_by_id(conn, user_id)
        if row is None:
            raise HTTPException(404, detail="用户不存在")
        actor = ctx["id"]
        data = req.model_dump(exclude_none=True) if hasattr(req, "model_dump") else dict(req)
        new_role = data.get("role", row["role"])
        new_active = data.get("active", bool(row["active"]))
        is_admin_now = row["role"] == "admin" and bool(row["active"])

        if (row["role"] == "admin" and new_role != "admin") or \
           (is_admin_now and new_active is False):
            if int(row["id"]) == int(actor):
                raise HTTPException(400, detail="不能修改自己的角色或停用自己")
        # 变更之后必须至少还剩一个可用的管理员，否则没人能进管理台
        if new_role != "admin" or not new_active:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                (int(row["id"]),)).fetchone()[0]
            if remaining == 0:
                raise HTTPException(400, detail="系统里至少要保留一个可用的管理员账号")
        if new_role not in ("admin", "user"):
            raise HTTPException(400, detail="角色只能是 admin 或 user")

        fields, values = [], []
        if "username" in data:
            # 用户名可改（登录标识），但必须唯一、合法；用户 ID 永远不可改
            name = str(data["username"] or "").strip()
            if not name:
                raise HTTPException(400, detail="用户名不能为空")
            if len(name) > 64 or any(ch.isspace() or ord(ch) < 32 for ch in name):
                raise HTTPException(400, detail="用户名不能包含空格或控制字符，且不超过 64 字符")
            dup = conn.execute(
                "SELECT id FROM users WHERE lower(username)=lower(?) AND id<>?",
                (name, int(user_id))).fetchone()
            if dup is not None:
                raise HTTPException(409, detail=f"用户名 {name} 已被占用")
            if name != row["username"]:
                fields.append("username=?"); values.append(name)
        if "display_name" in data:
            fields.append("display_name=?"); values.append(data["display_name"])
        if "role" in data:
            fields.append("role=?"); values.append(new_role)
        if "dept" in data:
            fields.append("dept=?"); values.append(data["dept"])
        if "active" in data:
            fields.append("active=?"); values.append(1 if new_active else 0)
        if fields:
            values.append(int(user_id))
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
        pwd = data.get("password")
        if pwd:
            if len(str(pwd)) < 8:
                raise HTTPException(400, detail="密码至少 8 位")
            auth.set_password(conn, int(user_id), str(pwd))
        conn.commit()
        fresh = auth.get_user_by_id(conn, user_id)
        audit(ctx["id"], None, "user_update",
              question=f"user={fresh['username']}", ip=None)
        return {"id": int(fresh["id"]), "username": fresh["username"],
                "display_name": fresh["display_name"] or "", "role": fresh["role"],
                "dept": fresh["dept"] or "", "active": bool(fresh["active"])}

    @app.put("/api/v1/admin/users/{user_id}/scopes")
    def admin_set_scopes(user_id: int, req: ScopeReq, ctx=Depends(require_admin)):
        """老的"共享/子目录"授权入口 —— 内部统一落到 2.0 的 grants。

        文件页与文档看板共用一套授权（grants），所以这里写的范围
        在两边同时生效。共享还没接入成存储源时，退回写老表（看板仍可见）。
        """
        legacy = [(s[0], s[1]) for s in req.scopes]
        out = _apply_user_scopes(user_id, legacy)
        out["ok"] = True
        return out

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
    def admin_tokens(ctx=Depends(actor)):
        """密钥列表：管理员看全部，普通用户只看自己的（可以给自己开密钥）。"""
        u = ctx["user"]
        sql = ("SELECT t.id,t.user_id,u.username,t.prefix,t.label,t.revoked,t.created_at,"
               "t.last_used_at,t.expires_at,"
               "(t.token_enc IS NOT NULL) AS recoverable FROM api_tokens t "
               "JOIN users u ON u.id=t.user_id")
        params: list = []
        if u["role"] != "admin":
            sql += " WHERE t.user_id=?"
            params.append(int(u["id"]))
        sql += " ORDER BY t.revoked ASC, t.id DESC"
        rows = conn.execute(sql, params).fetchall()
        return {"tokens": [dict(r) for r in rows]}

    @app.post("/api/v1/admin/tokens")
    def admin_issue_token(req: TokenReq, user_id: int = 0,
                          ctx=Depends(actor)):
        """签发密钥。普通用户只能给自己签发，管理员可以给任意用户签发。"""
        u = ctx["user"]
        target = int(user_id or 0) or int(u["id"])
        if u["role"] != "admin" and target != int(u["id"]):
            raise HTTPException(403, detail="只能给自己签发密钥")
        raw, tid = auth.issue_token(conn, target, req.label, req.ttl_days)
        return {"id": tid, "token": raw,
                "notice": "这是唯一一次显示明文 token，请立即交给用户"}

    @app.get("/api/v1/admin/agents")
    def admin_agents(ctx=Depends(actor)):
        """已接入智能体：管理员看全部；普通用户只看自己账号密钥接入的。"""
        u = ctx["user"]
        sql = ("SELECT a.id,a.user_id,u.username,a.name,a.kind,a.client_id,"
               "a.created_at,a.last_seen_at FROM agents a LEFT JOIN users u ON u.id=a.user_id")
        params: list = []
        if u["role"] != "admin":
            sql += " WHERE a.user_id=?"
            params.append(int(u["id"]))
        sql += " ORDER BY a.id"
        rows = conn.execute(sql, params).fetchall()
        return {"agents": [dict(r) for r in rows]}

    @app.get("/api/v1/admin/tokens/{token_id}/plaintext")
    def admin_token_plaintext(request: Request, token_id: int,
                              ctx=Depends(actor)):
        """重新查看某枚密钥的**完整明文**。

        密钥除了哈希还存了 AES-GCM 密文（密钥由 session_secret 派生），
        所以管理员随时能把完整密钥再拿出来，交给新的客户端。

        为什么需要这个：密钥只在签发那一刻显示一次，丢失就只能重新签发，
        而重新签发会让**所有正在用旧密钥的智能体立刻 401**。
        能重新查看，就不必换密钥，故障可以在原地修好。
        """
        row = conn.execute("SELECT user_id, prefix, revoked FROM api_tokens WHERE id=?",
                           (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="密钥不存在")
        if ctx["user"]["role"] != "admin" and int(row["user_id"]) != int(ctx["user"]["id"]):
            raise HTTPException(403, detail="只能查看自己的密钥")
        plain = auth.token_plaintext(conn, token_id)
        if not plain:
            raise HTTPException(
                404, detail="这枚密钥没有保存可恢复密文（可能是本平台上线前签发的，"
                            "或配置里关掉了 token_recoverable）。请重新签发一枚。")
        # 留痕：谁在什么时候调看了密钥明文
        audit(ctx["id"], row["prefix"], "token_view",
              question=f"token_id={token_id}", hit_paths=None, model_used=None,
              restricted=False, ip=_client_ip(request))
        return {"id": token_id, "token": plain, "prefix": row["prefix"],
                "revoked": bool(row["revoked"]),
                "notice": "密钥明文可再次查看——请直接复制给客户端，不要转述前缀。"}

    @app.delete("/api/v1/admin/tokens/{token_id}")
    def admin_revoke_token(token_id: int, ctx=Depends(actor)):
        """吊销密钥。幂等；已吊销的再调一次不会报错。普通用户只能吊销自己的。"""
        row = conn.execute("SELECT revoked, user_id FROM api_tokens WHERE id=?",
                           (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="密钥不存在")
        if ctx["user"]["role"] != "admin" and int(row["user_id"]) != int(ctx["user"]["id"]):
            raise HTTPException(403, detail="只能吊销自己的密钥")
        auth.revoke_token(conn, token_id)
        return {"ok": True, "revoked": True}

    @app.delete("/api/v1/admin/tokens/{token_id}/purge")
    def admin_purge_token(token_id: int, ctx=Depends(actor)):
        """物理删除密钥记录。

        **只允许删除已吊销的**——必须先吊销再删除，避免"点一下就永久消失"
        而管理员自己没意识到这个 token 还在被某个智能体使用。
        """
        row = conn.execute("SELECT revoked, prefix, user_id FROM api_tokens WHERE id=?",
                           (token_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="密钥不存在")
        if ctx["user"]["role"] != "admin" and int(row["user_id"]) != int(ctx["user"]["id"]):
            raise HTTPException(403, detail="只能删除自己的密钥")
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
    def admin_audit(limit: int = 100, ctx=Depends(actor)):
        """调用审计：管理员看全部；普通用户只看自己账号（密钥/智能体）产生的记录。"""
        u = ctx["user"]
        sql = ("SELECT a.id,u.username,a.token_prefix,a.action,a.question,a.model_used,"
               "a.restricted,a.created_at FROM audit_log a LEFT JOIN users u ON u.id=a.user_id")
        params: list = []
        if u["role"] != "admin":
            sql += " WHERE a.user_id=?"
            params.append(int(u["id"]))
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(max(1, min(1000, limit)))
        rows = conn.execute(sql, params).fetchall()
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
            """首页。静态资源 URL 自动带上文件 mtime 作为版本号。

            浏览器会缓存 app.js，改了前端不换版本号，用户就跑的是旧代码
            （这个坑踩过好几次）。这里用 mtime 自动做版本，无需人工维护。
            """
            fp = os.path.join(WEB_DIR, "index.html")
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    html = f.read()
                for name in ("app.js", "style.css", "cytoscape.min.js"):
                    p = os.path.join(WEB_DIR, "static", name)
                    try:
                        v = int(os.path.getmtime(p))
                    except OSError:
                        v = 0
                    html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
                return Response(content=html, media_type="text/html; charset=utf-8")
            except OSError:
                return FileResponse(fp)

        @app.get("/{page:path}")
        def spa(page: str):
            fp = os.path.join(WEB_DIR, page)
            if os.path.isfile(fp):
                return FileResponse(fp)
            return FileResponse(os.path.join(WEB_DIR, "index.html"))

    return app


app = make_app()
