"""文件域 HTTP 接口：浏览、搜索、预览、上传、回收站、存储源管理。"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import sqlite3
import time
import uuid
from urllib.parse import quote

from fastapi import Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from kbplatform import auth, converters, fsindex, grants as grants_mod, indexer
from kbplatform.catalog import PARSEABLE_EXT, FileRecord
from kbplatform.config import Config
from kbplatform.db import upsert_catalog
from kbplatform.fsindex import rel_key
from kbplatform.grants import (can_read, can_write, filter_entries,
                               migrate_user_scopes, set_grants, user_grants)
from kbplatform.paths import split_share
from kbplatform.platform_dirs import is_platform_rel
from kbplatform.storage import (Entry, Source, adapter_for, is_safe_rel, normalize_rel,
                                source_from_row, test_connection)


def _is_admin(user) -> bool:
    return (user or {}).get("role") == "admin"


def _source_row(conn, source_id: int):
    row = conn.execute("SELECT * FROM storage_sources WHERE id=? AND enabled=1",
                       (int(source_id),)).fetchone()
    if row is None:
        raise HTTPException(404, detail="存储源不存在")
    return row


def _adapter(conn, row):
    source = source_from_row(row)
    password = auth.decrypt_secret(row["secret_enc"]) if row["secret_enc"] else None
    return source, adapter_for(source, password)


def _is_ancestor(gs, source_id: int, rel: str) -> bool:
    rel = normalize_rel(rel)
    for g in gs:
        if g.source_id != int(source_id) or not g.subpath:
            continue
        if rel == "" or g.subpath == rel or g.subpath.startswith(rel + "\\"):
            return True
    return False


def _parse_status(conn, source_id: int, rel: str) -> tuple[str, int | None]:
    row = conn.execute(
        "SELECT catalog_id FROM fs_entries WHERE source_id=? AND rel_key=?",
        (source_id, rel_key(rel))).fetchone()
    if not row or not row["catalog_id"]:
        return "", None
    cat = conn.execute("SELECT status FROM catalog WHERE id=?", (row["catalog_id"],)).fetchone()
    return (cat["status"] if cat else ""), int(row["catalog_id"])


def _entry_dict(conn, source_id: int, e, write_here: bool) -> dict:
    status, cid = ("", None) if e.is_dir else _parse_status(conn, source_id, e.rel)
    return {
        "rel": e.rel,
        "name": e.name,
        "is_dir": bool(e.is_dir),
        "size": 0 if e.is_dir else int(e.size or 0),
        "mtime": float(e.mtime or 0),
        "ext": "" if e.is_dir else os.path.splitext(e.name)[1].lower(),
        "parse_status": status,
        "catalog_id": cid,
        "can_write": bool(write_here),
    }


def _breadcrumb(source_name: str, rel: str) -> list[dict]:
    out = [{"name": source_name, "rel": ""}]
    acc = ""
    for part in (normalize_rel(rel).split("\\") if rel else []):
        acc = f"{acc}\\{part}" if acc else part
        out.append({"name": part, "rel": acc})
    return out


def _write_ok(user, gs, source, rel: str) -> bool:
    return bool(source.writable) and can_write(user, gs, source.id, rel)


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".htm", ".ini", ".yaml", ".yml"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".ofd"}
MEDIA_EXT = IMAGE_EXT | {".pdf", ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".m4a", ".mov"}


def _disp(kind: str, name: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(name, safe='')}"


def _iter_file(f, size: int = 65536):
    try:
        while True:
            chunk = f.read(size)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            f.close()
        except Exception:
            pass


def _send(adapter, rel: str, entry, media_type: str | None, download: bool):
    headers = {"Content-Disposition": _disp("attachment" if download else "inline",
                                             entry.name)}
    local = adapter.local_path(rel)
    if local:
        return FileResponse(local, media_type=media_type or "application/octet-stream",
                            headers=headers)
    f = adapter.open_read(rel)
    headers["Content-Length"] = str(int(entry.size or 0))
    return StreamingResponse(_iter_file(f), media_type=media_type or "application/octet-stream",
                             headers=headers)


def _text_preview(adapter, rel: str) -> str:
    with adapter.open_read(rel) as f:
        data = f.read(2 * 1024 * 1024)
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _office_pdf(cfg: Config, adapter, source, rel: str, entry) -> str | None:
    key = hashlib.sha1(
        f"{source.id}|{rel}|{entry.size}|{entry.mtime}".encode("utf-8")).hexdigest()
    cache = os.path.join(cfg.preview_dir, key + ".pdf")
    if os.path.isfile(cache) and os.path.getsize(cache) > 0:
        return cache
    os.makedirs(cfg.preview_dir, exist_ok=True)
    local = adapter.local_path(rel)
    if local is None:
        src_dir = os.path.join(cfg.preview_dir, "_src")
        os.makedirs(src_dir, exist_ok=True)
        local = os.path.join(src_dir, key + (os.path.splitext(entry.name)[1].lower() or ".bin"))
        if not os.path.isfile(local):
            with adapter.open_read(rel) as rf, open(local, "wb") as wf:
                shutil.copyfileobj(rf, wf, length=1024 * 1024)
    return converters.convert_to_pdf(local, cfg.preview_dir, stem=key)


def cleanup_uploads(cfg: Config, conn, max_age_sec: int = 86400) -> int:
    """清理中断超过 24 小时的分片上传临时文件。"""
    now = time.time()
    n = 0
    for r in conn.execute("SELECT id, temp_path, created_at FROM upload_sessions").fetchall():
        if now - (r["created_at"] or 0) < max_age_sec:
            continue
        try:
            if r["temp_path"] and os.path.isfile(r["temp_path"]):
                os.unlink(r["temp_path"])
        except OSError:
            pass
        conn.execute("DELETE FROM upload_sessions WHERE id=?", (r["id"],))
        n += 1
    conn.commit()
    return n


def _source_admin_dict(r) -> dict:
    return {
        "id": int(r["id"]), "name": r["name"], "kind": r["kind"], "root": r["root"],
        "username": r["username"] or "", "domain": r["domain"] or "",
        "writable": bool(r["writable"]), "personal_root": r["personal_root"] or "",
        "enabled": bool(r["enabled"]), "has_password": bool(r["secret_enc"]),
        "scan_state": r["scan_state"], "scan_error": r["scan_error"],
        "last_scan_at": r["last_scan_at"],
    }


def _unc_parts(path: str) -> list[str]:
    p = str(path or "").replace("/", "\\").strip()
    if not p.startswith("\\\\"):
        return []
    return [seg for seg in p[2:].split("\\") if seg]


def config_share_candidates(cfg) -> list[dict]:
    """从老平台配置里推导"可以接入的共享根"。

    配置里的盘符（[drives]）是平台已经在监控的目录，直接可接；
    共享清单（[shares]）是整个共享，范围大，只作为候选项由管理员按需接入。
    """
    out: list[dict] = []
    seen: set[str] = set()
    host = ""
    drives = cfg.drive_map or {}
    for letter, unc in drives.items():
        parts = _unc_parts(unc)
        if len(parts) >= 2 and not host:
            host = parts[0]
    for letter, unc in sorted(drives.items()):
        parts = _unc_parts(unc)
        if len(parts) < 2:
            continue
        root = "\\\\" + "\\".join(parts)
        key = root.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": "\\".join(parts[1:]), "kind": "smb", "root": root,
            "scope": "drive", "origin": f"盘符 {letter}", "auto": True,
        })
    for share in cfg.share_names or []:
        share = str(share).strip()
        if not share or not host:
            continue
        root = f"\\\\{host}\\{share}"
        key = root.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": share, "kind": "smb", "root": root,
            "scope": "share", "origin": "共享清单", "auto": False,
        })
    return out


def provision_sources_from_config(conn, cfg) -> list[dict]:
    """把配置里已经在监控的目录自动登记为存储源（幂等）。

    这样管理员一登录就能在文件页看到全部共享文件夹，不必先手动接入；
    已经存在的源不动，管理员改过的配置不会被覆盖。
    """
    created: list[dict] = []
    existing = {str(r["root"]).lower(): r
                for r in conn.execute("SELECT * FROM storage_sources")}
    used_names = {str(r["name"]) for r in conn.execute("SELECT name FROM storage_sources")}
    for cand in config_share_candidates(cfg):
        if not cand.get("auto"):
            continue
        root = cand["root"]
        if root.lower() in existing:
            continue
        name = cand["name"]
        if name in used_names:
            name = f"{name} ({root})"
            if name in used_names:
                continue
        try:
            conn.execute(
                "INSERT INTO storage_sources(name,kind,root,username,secret_enc,domain,"
                "writable,personal_root,enabled,scan_state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'idle',?)",
                (name, cand["kind"], root, None, None, None, 1, "", 1, time.time()))
        except sqlite3.IntegrityError:
            continue
        used_names.add(name)
        created.append({"name": name, "root": root})
    if created:
        conn.commit()
    return created


def move_to_trash(conn, cfg: Config, vs, source, adapter, rel: str, user) -> tuple[int | None, str, str]:
    """把一个文件移进平台回收站。

    返回 (trash_id 或 None, 文件名, 失败原因)。成功时 trash_id 非空。
    重复文件清理也走这里，保证删除可恢复。
    """
    rel = normalize_rel(rel)
    e = adapter.stat(rel)
    if e is None:
        return None, "", "文件不存在"
    day = time.strftime("%Y%m%d")
    base = f"_kb_trash\\{day}"
    trash_rel = normalize_rel(f"{base}\\{uuid.uuid4().hex}_{e.name}")
    try:
        adapter.mkdirs(base)
        adapter.rename(rel, trash_rel)
    except Exception as ex:
        return None, e.name, f"移入回收站失败: {ex}"
    now = time.time()
    cur = conn.execute(
        "INSERT INTO trash_entries(source_id,original_rel,trash_rel,name,is_dir,"
        "size,deleted_by,deleted_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (source.id, rel, trash_rel, e.name, 1 if e.is_dir else 0,
         0 if e.is_dir else e.size, user["id"], now,
         now + cfg.trash_days * 86400))
    removed = fsindex.delete_subtree(conn, source.id, rel)
    for r in removed:
        path = fsindex.entry_path(source, r)
        row = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()
        if row:
            indexer.remove_file(conn, cfg.fts_path, vs, int(row[0]))
    conn.commit()
    return int(cur.lastrowid), e.name, ""


def source_for_path(conn, path: str):
    """按文件路径找到它属于哪个存储源，返回 (source, adapter, rel)。

    匹配规则：存储源的根是路径前缀（最长优先）。
    """
    p = str(path or "").replace("/", "\\").rstrip("\\")
    best = None
    for row in conn.execute("SELECT * FROM storage_sources WHERE enabled=1").fetchall():
        root = str(row["root"] or "").replace("/", "\\").rstrip("\\")
        if not root:
            continue
        if p.lower() == root.lower() or p.lower().startswith(root.lower() + "\\"):
            if best is None or len(root) > len(best[1]):
                best = (row, root)
    if best is None:
        return None
    row, root = best
    source = source_from_row(row)
    rel = p[len(root):].strip("\\")
    try:
        adapter = adapter_for(source, auth.decrypt_secret(row["secret_enc"])
                              if row["secret_enc"] else None)
    except Exception:
        return None
    return source, adapter, rel


def register_fs_routes(app, cfg: Config, conn, require_user, require_admin, audit):
    uploads_dir = os.path.join(cfg.data_dir, "uploads")

    def _upload_session(upload_id: str, user) -> dict:
        row = conn.execute("SELECT * FROM upload_sessions WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail="上传会话不存在")
        if int(row["user_id"]) != int(user["id"]) and not _is_admin(user):
            raise HTTPException(404, detail="上传会话不存在")
        return dict(row)

    # ------------------------------------------------------------ 浏览
    @app.get("/api/v1/fs/sources")
    def fs_sources(user=Depends(require_user)):
        gs = user_grants(conn, user)
        allowed = {g.source_id for g in gs}
        out = []
        for r in conn.execute("SELECT * FROM storage_sources WHERE enabled=1 ORDER BY name"):
            if not _is_admin(user) and int(r["id"]) not in allowed:
                continue
            out.append({
                "id": int(r["id"]), "name": r["name"], "kind": r["kind"],
                "writable": bool(r["writable"]),
                "can_write": bool(r["writable"]) and (
                    _is_admin(user) or any(g.source_id == int(r["id"]) and g.can_write
                                           for g in gs)),
                "personal": bool(r["personal_root"]),
            })
        return {"sources": out}

    @app.get("/api/v1/fs/list")
    def fs_list(source_id: int, rel: str = "", user=Depends(require_user)):
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise HTTPException(400, detail="非法路径")
        row = _source_row(conn, source_id)
        source = source_from_row(row)
        gs = user_grants(conn, user)
        if not can_read(user, gs, source.id, rel):
            raise HTTPException(404, detail="目录不存在")
        _, adapter = _adapter(conn, row)
        try:
            entries = adapter.listdir(rel)
        except Exception as e:
            if _is_ancestor(gs, source.id, rel):
                entries = []
            else:
                raise HTTPException(404, detail=f"目录不可读: {e}")
        entries = filter_entries(gs, source.id, rel, entries) if not _is_admin(user) else list(entries)
        # 平台内部目录（_kb / _kb_trash）不展示给任何人，对管理员也一样
        entries = [e for e in entries if not is_platform_rel(e.rel)]

        # 授权路径比真实目录更深时，补出「下一段」导航占位
        have = {normalize_rel(e.rel) for e in entries}
        for g in gs:
            if g.source_id != source.id:
                continue
            nx = grants_mod.next_component(rel, g.subpath)
            if nx and nx not in have:
                entries.append(Entry(nx, nx.split("\\")[-1], True, 0, 0))
                have.add(nx)
        entries.sort(key=lambda x: (not x.is_dir, x.name.lower()))

        write_here = _write_ok(user, gs, source, rel)
        return {
            "source": {"id": source.id, "name": source.name, "kind": source.kind},
            "rel": rel,
            "breadcrumb": _breadcrumb(source.name, rel),
            "can_write": write_here,
            "entries": [_entry_dict(conn, source.id, e, write_here and e.is_dir)
                        for e in entries],
        }

    @app.get("/api/v1/fs/stat")
    def fs_stat(source_id: int, rel: str, user=Depends(require_user)):
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise HTTPException(400, detail="非法路径")
        row = _source_row(conn, source_id)
        source, adapter = _adapter(conn, row)
        gs = user_grants(conn, user)
        if not rel or not can_read(user, gs, source.id, rel):
            raise HTTPException(404, detail="文件不存在")
        e = adapter.stat(rel)
        if e is None:
            raise HTTPException(404, detail="文件不存在")
        return {"source": {"id": source.id, "name": source.name},
                **(_entry_dict(conn, source.id, e, _write_ok(user, gs, source, rel)))}

    @app.get("/api/v1/fs/search")
    def fs_search(q: str = "", limit: int = Query(20, ge=1, le=50),
                  user=Depends(require_user)):
        q = (q or "").strip()
        if not q:
            return {"q": q, "results": []}
        gs = user_grants(conn, user)
        allowed = None if _is_admin(user) else [(g.source_id, g.subpath) for g in gs]
        rows = fsindex.search(conn, q, allowed, limit=limit)
        return {"q": q, "results": rows}

    # ------------------------------------------------------------ 预览 / 下载
    @app.get("/api/v1/fs/preview")
    def fs_preview(source_id: int, rel: str, download: int = 0, user=Depends(require_user)):
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise HTTPException(400, detail="非法路径")
        row = _source_row(conn, source_id)
        source, adapter = _adapter(conn, row)
        gs = user_grants(conn, user)
        if not can_read(user, gs, source.id, rel):
            raise HTTPException(404, detail="文件不存在")
        e = adapter.stat(rel)
        if e is None or e.is_dir:
            raise HTTPException(404, detail="文件不存在")
        ext = os.path.splitext(e.name)[1].lower()
        if download:
            return _send(adapter, rel, e, mimetypes.guess_type(e.name)[0], True)
        if ext in TEXT_EXT:
            return Response(_text_preview(adapter, rel),
                            media_type="text/plain; charset=utf-8")
        if ext in OFFICE_EXT:
            pdf = _office_pdf(cfg, adapter, source, rel, e)
            if not pdf:
                raise HTTPException(415, detail="该文件暂不支持在线预览，请下载")
            return FileResponse(pdf, media_type="application/pdf",
                                headers={"Content-Disposition": _disp("inline", e.name)})
        if ext in MEDIA_EXT:
            return _send(adapter, rel, e, mimetypes.guess_type(e.name)[0], False)
        raise HTTPException(415, detail="该文件暂不支持在线预览，请下载")

    @app.get("/api/v1/fs/download")
    def fs_download(source_id: int, rel: str, user=Depends(require_user)):
        return fs_preview(source_id, rel, download=1, user=user)

    # ------------------------------------------------------------ 上传
    @app.post("/api/v1/fs/upload/init")
    def upload_init(payload: dict = Body(...), user=Depends(require_user)):
        try:
            source_id = int(payload["source_id"])
            size = int(payload.get("size", 0))
        except Exception:
            raise HTTPException(400, detail="source_id/size 不合法")
        name = str(payload.get("name", "")).strip()
        rel = normalize_rel(payload.get("rel", ""))
        overwrite = bool(payload.get("overwrite"))
        if (not name or name in (".", "..") or "/" in name or "\\" in name
                or any(ch in name for ch in '*?"<>|:')):
            raise HTTPException(400, detail="文件名不合法")
        if not is_safe_rel(rel):
            raise HTTPException(400, detail="非法路径")
        if size < 0:
            raise HTTPException(400, detail="文件大小不合法")
        if size > cfg.upload_max_mb * 1024 * 1024:
            raise HTTPException(413, detail=f"文件超过 {cfg.upload_max_mb} MB 上限")
        row = _source_row(conn, source_id)
        source, adapter = _adapter(conn, row)
        gs = user_grants(conn, user)
        if not _write_ok(user, gs, source, rel):
            raise HTTPException(403, detail="目录没有写权限")
        if rel and adapter.stat(rel) is None:
            raise HTTPException(404, detail="目标目录不存在")
        target = normalize_rel(f"{rel}\\{name}" if rel else name)
        existing = adapter.stat(target)
        if existing is not None:
            if existing.is_dir:
                raise HTTPException(409, detail="目标是目录")
            if not overwrite:
                raise HTTPException(409, detail="同名文件已存在")
        os.makedirs(uploads_dir, exist_ok=True)
        upid = uuid.uuid4().hex
        temp = os.path.join(uploads_dir, upid + ".part")
        with open(temp, "wb"):
            pass
        conn.execute(
            "INSERT INTO upload_sessions(id,user_id,source_id,rel,name,size,temp_path,"
            "received,overwrite,created_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
            (upid, user["id"], source.id, rel, name, size, temp, 1 if overwrite else 0,
             time.time()))
        conn.commit()
        return {"upload_id": upid, "chunk_size": 5 * 1024 * 1024}

    @app.put("/api/v1/fs/upload/{upload_id}")
    async def upload_chunk(upload_id: str, request: Request, offset: int = Query(..., ge=0),
                           user=Depends(require_user)):
        sess = _upload_session(upload_id, user)
        if offset != int(sess["received"]):
            raise HTTPException(409, detail=f"分片顺序错误，期望 offset={sess['received']}")
        data = await request.body()
        if int(sess["received"]) + len(data) > int(sess["size"]):
            raise HTTPException(413, detail="上传内容超过声明大小")
        with open(sess["temp_path"], "ab") as f:
            f.write(data)
        received = int(sess["received"]) + len(data)
        conn.execute("UPDATE upload_sessions SET received=? WHERE id=?", (received, upload_id))
        conn.commit()
        return {"received": received}

    @app.post("/api/v1/fs/upload/{upload_id}/complete")
    def upload_complete(upload_id: str, request: Request, user=Depends(require_user)):
        sess = _upload_session(upload_id, user)
        if int(sess["received"]) != int(sess["size"]):
            raise HTTPException(409, detail="上传尚未完成")
        row = _source_row(conn, int(sess["source_id"]))
        source, adapter = _adapter(conn, row)
        rel = normalize_rel(sess["rel"])
        gs = user_grants(conn, user)
        if not _write_ok(user, gs, source, rel):
            raise HTTPException(403, detail="目录没有写权限")
        target = normalize_rel(f"{rel}\\{sess['name']}" if rel else sess["name"])
        existing = adapter.stat(target)
        if existing is not None and not int(sess["overwrite"]):
            raise HTTPException(409, detail="同名文件已存在")
        path = fsindex.entry_path(source, target)
        old = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()
        with open(sess["temp_path"], "rb") as src:
            adapter.write_stream(target, src, int(sess["size"]))
        e = adapter.stat(target)
        if e is None:
            raise HTTPException(500, detail="写入后找不到文件")
        fsindex.upsert_entry(conn, source, e)
        ext = os.path.splitext(e.name)[1].lower()
        parseable = ext in PARSEABLE_EXT
        cid = None
        if parseable:
            cid = upsert_catalog(conn, FileRecord(
                path=path, share=split_share(source.root)[0] or source.name,
                rel_path=target, name=e.name, ext=ext,
                size=e.size, mtime=e.mtime))
        scanner = app.state.scanner
        if parseable and old:
            indexer.clear_artifacts(conn, cfg.fts_path, scanner.vs, path)
            conn.execute("UPDATE catalog SET status='pending', status_detail=NULL, "
                         "tier1_done=0, tier2_done=0 WHERE id=?", (cid,))
            conn.commit()
        if parseable:
            scanner.queue.enqueue("index", cid, force=bool(old))
        if cid:
            conn.execute("UPDATE fs_entries SET catalog_id=? WHERE source_id=? AND rel_key=?",
                         (cid, source.id, rel_key(target)))
        conn.commit()
        try:
            os.unlink(sess["temp_path"])
        except OSError:
            pass
        conn.execute("DELETE FROM upload_sessions WHERE id=?", (upload_id,))
        conn.commit()
        audit(user["id"], None, "upload", question=f"{source.name}:{target}",
              hit_paths=[path], ip=None)
        entry = _entry_dict(conn, source.id, e, False)
        return {"entry": entry, "catalog_id": cid, "queued": bool(parseable)}

    # ------------------------------------------------------------ 回收站
    def _grant_root_blocked(source, rel: str) -> bool:
        rel = normalize_rel(rel)
        if not rel:
            return True
        for g in conn.execute("SELECT subpath FROM grants WHERE source_id=?",
                              (source.id,)).fetchall():
            sub = normalize_rel(g["subpath"])
            if sub and (sub == rel or sub.startswith(rel + "\\")):
                return True
        pr = normalize_rel(source.personal_root)
        return bool(pr) and (pr == rel or pr.startswith(rel + "\\"))

    def _cleanup_deleted(source, rels: list[str]) -> None:
        for rel in rels:
            path = fsindex.entry_path(source, rel)
            row = conn.execute("SELECT id FROM catalog WHERE path=?", (path,)).fetchone()
            if row:
                indexer.remove_file(conn, cfg.fts_path, app.state.scanner.vs, int(row[0]))

    def _move_to_trash(source, adapter, rel: str, user) -> tuple[int | None, str, str]:
        """把一个文件移进平台回收站（模块级函数见 move_to_trash）。"""
        return move_to_trash(conn, cfg, app.state.scanner.vs, source, adapter, rel, user)

    @app.post("/api/v1/fs/delete")
    def fs_delete(payload: dict = Body(...), user=Depends(require_user)):
        try:
            sid = int(payload["source_id"])
        except Exception:
            raise HTTPException(400, detail="source_id 不合法")
        rels = payload.get("rels") or []
        if not isinstance(rels, list):
            raise HTTPException(400, detail="rels 必须是数组")
        row = _source_row(conn, sid)
        source, adapter = _adapter(conn, row)
        gs = user_grants(conn, user)
        deleted, skipped = [], []
        for raw in rels:
            rel = normalize_rel(raw)
            if not rel or not is_safe_rel(rel):
                skipped.append({"rel": rel, "reason": "路径不合法"})
                continue
            if not _write_ok(user, gs, source, rel):
                skipped.append({"rel": rel, "reason": "没有写权限"})
                continue
            if _grant_root_blocked(source, rel):
                skipped.append({"rel": rel, "reason": "授权根或平台目录不可删除"})
                continue
            e = adapter.stat(rel)
            if e is None:
                skipped.append({"rel": rel, "reason": "文件不存在"})
                continue
            trash_id, _name, reason = _move_to_trash(source, adapter, rel, user)
            if trash_id is None:
                skipped.append({"rel": rel, "reason": reason})
                continue
            deleted.append(trash_id)
        audit(user["id"], None, "delete", question=f"{source.name}:{len(deleted)}",
              hit_paths=[normalize_rel(r) for r in rels], ip=None)
        return {"deleted": deleted, "skipped": skipped}

    @app.get("/api/v1/fs/trash")
    def fs_trash(user=Depends(require_user)):
        sql = ("SELECT t.*, s.name AS source_name FROM trash_entries t "
               "LEFT JOIN storage_sources s ON s.id=t.source_id ")
        if _is_admin(user):
            rows = conn.execute(sql + "ORDER BY t.deleted_at DESC").fetchall()
        else:
            rows = conn.execute(sql + "WHERE t.deleted_by=? ORDER BY t.deleted_at DESC",
                                (user["id"],)).fetchall()
        return {"items": [{
            "id": r["id"], "source_id": r["source_id"],
            "source_name": r["source_name"] or "", "original_rel": r["original_rel"],
            "name": r["name"], "is_dir": bool(r["is_dir"]), "size": r["size"] or 0,
            "deleted_at": r["deleted_at"], "expires_at": r["expires_at"],
        } for r in rows]}

    @app.post("/api/v1/fs/trash/{trash_id}/restore")
    def fs_restore(trash_id: int, user=Depends(require_user)):
        t = conn.execute("SELECT * FROM trash_entries WHERE id=?", (trash_id,)).fetchone()
        if t is None or (t["deleted_by"] != user["id"] and not _is_admin(user)):
            raise HTTPException(404, detail="回收站条目不存在")
        row = _source_row(conn, int(t["source_id"]))
        source, adapter = _adapter(conn, row)
        original = normalize_rel(t["original_rel"])
        parent = original.rsplit("\\", 1)[0] if "\\" in original else ""
        if adapter.stat(original) is not None:
            raise HTTPException(409, detail="原位置已有同名文件")
        try:
            if parent:
                adapter.mkdirs(parent)
            adapter.rename(normalize_rel(t["trash_rel"]), original)
        except Exception as e:
            raise HTTPException(500, detail=f"恢复失败: {e}")
        e = adapter.stat(original)
        if e is not None:
            fsindex.upsert_entry(conn, source, e)
            ext = os.path.splitext(e.name)[1].lower()
            if not e.is_dir and ext in PARSEABLE_EXT:
                cid = upsert_catalog(conn, FileRecord(
                    path=fsindex.entry_path(source, original),
                    share=split_share(source.root)[0] or source.name,
                    rel_path=original, name=e.name, ext=ext, size=e.size, mtime=e.mtime))
                app.state.scanner.queue.enqueue("index", cid)
                conn.execute("UPDATE fs_entries SET catalog_id=? WHERE source_id=? AND rel_key=?",
                             (cid, source.id, rel_key(original)))
            if e.is_dir:
                app.state.scanner.trigger(source.id)
        conn.execute("DELETE FROM trash_entries WHERE id=?", (trash_id,))
        conn.commit()
        audit(user["id"], None, "restore", question=f"{source.name}:{original}",
              hit_paths=[original], ip=None)
        return {"restored": original}

    @app.delete("/api/v1/fs/trash/{trash_id}")
    def fs_purge(trash_id: int, user=Depends(require_admin)):
        t = conn.execute("SELECT * FROM trash_entries WHERE id=?", (trash_id,)).fetchone()
        if t is None:
            raise HTTPException(404, detail="回收站条目不存在")
        row = _source_row(conn, int(t["source_id"]))
        source, adapter = _adapter(conn, row)
        try:
            adapter.remove(normalize_rel(t["trash_rel"]))
        except Exception:
            pass
        conn.execute("DELETE FROM trash_entries WHERE id=?", (trash_id,))
        conn.commit()
        audit(user["id"], None, "purge", question=f"{source.name}:{t['original_rel']}",
              hit_paths=[t["original_rel"]], ip=None)
        return {"purged": True}

    # ------------------------------------------------------------ 管理台：存储源
    def _source_any(sid: int):
        row = conn.execute("SELECT * FROM storage_sources WHERE id=?", (int(sid),)).fetchone()
        if row is None:
            raise HTTPException(404, detail="存储源不存在")
        return row

    @app.get("/api/v1/admin/sources")
    def admin_sources(user=Depends(require_admin)):
        rows = conn.execute("SELECT * FROM storage_sources ORDER BY id").fetchall()
        return {"sources": [_source_admin_dict(r) for r in rows]}

    @app.get("/api/v1/admin/sources/candidates")
    def admin_source_candidates(user=Depends(require_admin)):
        """老平台配置里可接入的共享（盘符=已监控目录，共享清单=整个共享）。"""
        existing = {str(r["root"]).lower(): r
                    for r in conn.execute("SELECT * FROM storage_sources")}
        items = []
        for cand in config_share_candidates(cfg):
            hit = existing.get(cand["root"].lower())
            items.append({
                "name": cand["name"], "kind": cand["kind"], "root": cand["root"],
                "scope": cand["scope"], "origin": cand["origin"], "auto": cand["auto"],
                "added": hit is not None,
                "added_id": int(hit["id"]) if hit is not None else None,
            })
        return {"candidates": items}

    @app.post("/api/v1/admin/sources")
    def admin_source_create(payload: dict = Body(...), user=Depends(require_admin)):
        name = str(payload.get("name", "")).strip()
        kind = str(payload.get("kind", "")).strip().lower()
        root = str(payload.get("root", "")).strip()
        if not name or kind not in ("local", "smb") or not root:
            raise HTTPException(400, detail="name/kind/root 必填")
        if kind == "local" and not os.path.isdir(root):
            raise HTTPException(400, detail="本地根目录不存在")
        personal = normalize_rel(payload.get("personal_root", ""))
        if not is_safe_rel(personal) or personal == ".":
            raise HTTPException(400, detail="个人空间目录不合法")
        password = payload.get("password") or ""
        secret_enc = auth.encrypt_secret(str(password)) if password else None
        now = time.time()
        try:
            cur = conn.execute(
                "INSERT INTO storage_sources(name,kind,root,username,secret_enc,domain,"
                "writable,personal_root,enabled,scan_state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'idle',?)",
                (name, kind, root, payload.get("username") or None, secret_enc,
                 payload.get("domain") or None, 1 if payload.get("writable") else 0,
                 personal, 1 if payload.get("enabled", True) else 0, now))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail="存储源名称已存在")
        migrate_user_scopes(conn, cfg)
        row = _source_any(int(cur.lastrowid))
        audit(user["id"], None, "source_create", question=name, ip=None)
        return _source_admin_dict(row)

    @app.put("/api/v1/admin/sources/{source_id}")
    def admin_source_update(source_id: int, payload: dict = Body(...),
                            user=Depends(require_admin)):
        row = _source_any(source_id)
        name = str(payload.get("name", row["name"])).strip()
        kind = str(payload.get("kind", row["kind"])).strip().lower()
        root = str(payload.get("root", row["root"])).strip()
        if not name or kind not in ("local", "smb") or not root:
            raise HTTPException(400, detail="name/kind/root 必填")
        if kind == "local" and not os.path.isdir(root):
            raise HTTPException(400, detail="本地根目录不存在")
        personal = normalize_rel(payload.get("personal_root", row["personal_root"] or ""))
        if not is_safe_rel(personal) or personal == ".":
            raise HTTPException(400, detail="个人空间目录不合法")
        secret_enc = row["secret_enc"]
        if payload.get("password"):
            secret_enc = auth.encrypt_secret(str(payload["password"]))
        try:
            conn.execute(
                "UPDATE storage_sources SET name=?,kind=?,root=?,username=?,secret_enc=?,"
                "domain=?,writable=?,personal_root=?,enabled=? WHERE id=?",
                (name, kind, root, payload.get("username", row["username"]) or None,
                 secret_enc, payload.get("domain", row["domain"]) or None,
                 1 if payload.get("writable", bool(row["writable"])) else 0,
                 personal, 1 if payload.get("enabled", bool(row["enabled"])) else 0,
                 int(source_id)))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail="存储源名称已存在")
        audit(user["id"], None, "source_update", question=name, ip=None)
        return _source_admin_dict(_source_any(source_id))

    @app.delete("/api/v1/admin/sources/{source_id}")
    def admin_source_delete(source_id: int, force: int = 0, user=Depends(require_admin)):
        row = _source_any(source_id)
        sid = int(source_id)
        n_grants = conn.execute("SELECT COUNT(*) FROM grants WHERE source_id=?",
                                (sid,)).fetchone()[0]
        n_trash = conn.execute("SELECT COUNT(*) FROM trash_entries WHERE source_id=?",
                               (sid,)).fetchone()[0]
        if (n_grants or n_trash) and not force:
            raise HTTPException(409, detail=("存储源仍有授权或回收站条目，"
                                            "确认后加 ?force=1 删除"))
        prefix = str(row["root"]).rstrip("\\") + "\\"
        cats = conn.execute("SELECT id,path FROM catalog WHERE path LIKE ?",
                            (prefix + "%",)).fetchall()
        for c in cats:
            indexer.clear_artifacts(conn, cfg.fts_path, app.state.scanner.vs, c["path"])
            conn.execute("DELETE FROM jobs WHERE catalog_id=?", (c["id"],))
            conn.execute("DELETE FROM catalog WHERE id=?", (c["id"],))
        conn.execute("DELETE FROM grants WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM trash_entries WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM upload_sessions WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM fs_entries WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM storage_sources WHERE id=?", (sid,))
        conn.commit()
        audit(user["id"], None, "source_delete", question=row["name"], ip=None)
        return {"deleted": True}

    @app.post("/api/v1/admin/sources/{source_id}/test")
    def admin_source_test(source_id: int, payload: dict | None = Body(default=None),
                          user=Depends(require_admin)):
        payload = payload or {}
        if int(source_id) == 0:
            if not payload.get("root") or not payload.get("kind"):
                raise HTTPException(400, detail="未保存的源需要提供 kind/root")
            row = None
        else:
            row = _source_any(source_id)
        kind = str(payload.get("kind") or (row["kind"] if row else "")).lower()
        root = str(payload.get("root") or (row["root"] if row else ""))
        username = payload.get("username", row["username"] if row else None)
        domain = payload.get("domain", row["domain"] if row else None)
        if payload.get("password"):
            password = str(payload["password"])
        else:
            password = auth.decrypt_secret(row["secret_enc"]) if row and row["secret_enc"] else None
        source = Source(id=int(source_id), name=(row["name"] if row else "test"),
                        kind=kind, root=root, username=username, domain=domain,
                        writable=bool(row["writable"]) if row else False,
                        personal_root=(row["personal_root"] if row else "") or "",
                        enabled=True)
        result = test_connection(source, password)
        audit(user["id"], None, "source_test", question=root,
              hit_paths=[root] if result.get("ok") else [], ip=None)
        return result

    @app.post("/api/v1/admin/sources/{source_id}/scan")
    def admin_source_scan(source_id: int, user=Depends(require_admin)):
        _source_any(source_id)
        app.state.scanner.trigger(int(source_id))
        return {"queued": True}

    @app.get("/api/v1/admin/sources/{source_id}/scan/status")
    def admin_source_scan_status(source_id: int, user=Depends(require_admin)):
        _source_any(source_id)
        st = app.state.scanner.status(int(source_id))
        if st is None:
            raise HTTPException(404, detail="存储源不存在")
        return st

    @app.get("/api/v1/admin/sources/{source_id}/tree")
    def admin_source_tree(source_id: int, rel: str = "", user=Depends(require_admin)):
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise HTTPException(400, detail="非法路径")
        row = _source_any(source_id)
        _, adapter = _adapter(conn, row)
        try:
            entries = adapter.listdir(rel)
        except Exception as e:
            raise HTTPException(502, detail=f"目录不可读: {e}")
        dirs = [{"rel": e.rel, "name": e.name} for e in entries if e.is_dir]
        dirs.sort(key=lambda x: x["name"].lower())
        return {"rel": rel, "dirs": dirs}

    # ------------------------------------------------------------ 管理台：授权
    @app.get("/api/v1/admin/users/{user_id}/grants")
    def admin_user_grants(user_id: int, user=Depends(require_admin)):
        if conn.execute("SELECT 1 FROM users WHERE id=?", (int(user_id),)).fetchone() is None:
            raise HTTPException(404, detail="用户不存在")
        rows = conn.execute(
            "SELECT source_id,subpath,can_write FROM grants WHERE user_id=? "
            "ORDER BY source_id,subpath", (int(user_id),)).fetchall()
        return {"grants": [{"source_id": r["source_id"], "subpath": r["subpath"],
                            "can_write": bool(r["can_write"])} for r in rows]}

    @app.put("/api/v1/admin/users/{user_id}/grants")
    def admin_set_user_grants(user_id: int, payload: dict = Body(...),
                              user=Depends(require_admin)):
        if conn.execute("SELECT 1 FROM users WHERE id=?", (int(user_id),)).fetchone() is None:
            raise HTTPException(404, detail="用户不存在")
        items = payload.get("grants")
        if not isinstance(items, list):
            raise HTTPException(400, detail="grants 必须是数组")
        clean = []
        for it in items:
            try:
                clean.append({"source_id": int(it["source_id"]),
                              "subpath": str(it.get("subpath", "")),
                              "can_write": bool(it.get("can_write"))})
            except Exception:
                raise HTTPException(400, detail="授权项不合法")
        try:
            set_grants(conn, int(user_id), clean)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        audit(user["id"], None, "grants_set", question=f"user={user_id} n={len(clean)}",
              ip=None)
        return {"grants": clean}
