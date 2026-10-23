"""文件域 HTTP 接口：浏览、搜索、预览、上传、回收站、存储源管理。"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import time
from urllib.parse import quote

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse

from kbplatform import auth, converters, fsindex, grants as grants_mod
from kbplatform.config import Config
from kbplatform.fsindex import rel_key
from kbplatform.grants import can_read, can_write, filter_entries, user_grants
from kbplatform.storage import (Entry, adapter_for, is_safe_rel, normalize_rel,
                                source_from_row)


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


def register_fs_routes(app, cfg: Config, conn, require_user, require_admin, audit):
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
        entries = filter_entries(gs, source.id, rel, entries)

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
