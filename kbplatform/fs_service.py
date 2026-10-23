"""文件域 HTTP 接口：浏览、搜索、预览、上传、回收站、存储源管理。"""
from __future__ import annotations

import os
import time

from fastapi import Depends, HTTPException, Query

from kbplatform import auth, fsindex, grants as grants_mod
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
