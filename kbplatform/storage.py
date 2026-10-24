"""存储适配器：本地磁盘与 SMB 共享。

为什么不用 Windows 映射盘：
    映射盘/凭据管理器同一台服务器只能稳定保存一套凭据，而产品要求
    「每个共享单独填写账号密码」。这里用 smbprotocol 提供的 smbclient
    直连（每个 Source 传自己的账号密码），同一台 NAS 上不同共享互不影响。

路径约定：
    * `rel` 一律是共享根内的相对路径，反斜杠分隔，空串表示根；
    * 禁止 `..`、绝对路径、盘符与 Windows 非法字符；
    * `rel` 只用于定位，不参与显示。
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import BinaryIO, Iterator

import smbclient

SKIP_NAMES = {"_kb_trash", "#recycle", "#snapshot", "@eaDir", "$RECYCLE.BIN",
              "System Volume Information",
              # Windows 资源管理器默认不显示的系统文件
              "Thumbs.db", "thumbs.db", "desktop.ini", "Desktop.ini"}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_BAD_CHARS = set('*?"<>|:')


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    kind: str                  # local | smb
    root: str
    username: str | None = None
    domain: str | None = None
    writable: bool = False
    personal_root: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class Entry:
    rel: str
    name: str
    is_dir: bool
    size: int
    mtime: float


def source_from_row(row) -> Source:
    return Source(
        id=int(row["id"]),
        name=row["name"],
        kind=row["kind"],
        root=row["root"],
        username=row["username"],
        domain=row["domain"],
        writable=bool(row["writable"]),
        personal_root=row["personal_root"] or "",
        enabled=bool(row["enabled"]),
    )


def normalize_rel(rel: str | None) -> str:
    """统一成反斜杠、去掉首尾与重复分隔符；不做 Unicode 归一。"""
    if not rel:
        return ""
    p = str(rel).replace("/", "\\")
    return "\\".join(seg for seg in p.split("\\") if seg)


def is_safe_rel(rel: str | None) -> bool:
    """拒绝越界与 Windows 非法路径。空串（共享根）是合法的。"""
    if not rel:
        return True
    p = str(rel).replace("/", "\\")
    if p.startswith("\\") or _DRIVE_RE.match(p):
        return False
    for seg in p.split("\\"):
        if seg in ("", ".", ".."):
            continue
        if seg == "..":
            return False
        if any(ch in _BAD_CHARS for ch in seg):
            return False
    return ".." not in p.split("\\")


def _join_rel(base: str, name: str) -> str:
    return f"{base}\\{name}" if base else name


def _skip_name(name: str) -> bool:
    # ~$ 是 Office 打开文件时留下的锁文件，不是用户文件
    return name.startswith(".") or name.startswith("~$") or name in SKIP_NAMES


class LocalAdapter:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.real_root = os.path.realpath(self.root)

    def _abs(self, rel: str) -> str:
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise ValueError(f"非法相对路径: {rel!r}")
        p = os.path.abspath(os.path.join(self.root, rel)) if rel else self.root
        if os.path.realpath(p) != self.real_root and not os.path.realpath(p).startswith(
                self.real_root + os.sep):
            raise ValueError(f"路径越界: {rel!r}")
        return p

    def listdir(self, rel: str = "") -> list[Entry]:
        base = normalize_rel(rel)
        path = self._abs(base)
        out: list[Entry] = []
        with os.scandir(path) as it:
            for e in it:
                if _skip_name(e.name):
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append(Entry(_join_rel(base, e.name), e.name, is_dir,
                                 0 if is_dir else st.st_size, st.st_mtime))
        out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return out

    def stat(self, rel: str) -> Entry | None:
        base = normalize_rel(rel)
        path = self._abs(base)
        try:
            st = os.stat(path)
        except OSError:
            return None
        is_dir = os.path.isdir(path)
        return Entry(base, os.path.basename(base) or os.path.basename(self.root),
                     is_dir, 0 if is_dir else st.st_size, st.st_mtime)

    def open_read(self, rel: str) -> BinaryIO:
        return open(self._abs(rel), "rb")

    def write_stream(self, rel: str, src: BinaryIO, size: int | None = None) -> None:
        path = self._abs(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        written = 0
        with open(path, "wb") as f:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
        if size is not None and written != size:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise ValueError(f"写入大小不符: 期望 {size}，实际 {written}")

    def mkdirs(self, rel: str) -> None:
        os.makedirs(self._abs(rel), exist_ok=True)

    def rename(self, src_rel: str, dst_rel: str) -> None:
        src, dst = self._abs(src_rel), self._abs(dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst) if os.path.isfile(src) else os.rename(src, dst)

    def remove(self, rel: str) -> None:
        path = self._abs(rel)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.unlink(path)

    def local_path(self, rel: str) -> str | None:
        return self._abs(rel)


class SmbAdapter:
    def __init__(self, source: Source, password: str | None = None):
        self.source = source
        self.password = password
        self.root = source.root.replace("/", "\\").rstrip("\\")

    def _kw(self) -> dict:
        kw: dict = {}
        if self.source.username:
            kw["username"] = self.source.username
        if self.password:
            kw["password"] = self.password
        if self.source.domain:
            kw["domain"] = self.source.domain
        return kw

    def _unc(self, rel: str) -> str:
        rel = normalize_rel(rel)
        if not is_safe_rel(rel):
            raise ValueError(f"非法相对路径: {rel!r}")
        return self.root if not rel else self.root + "\\" + rel

    def listdir(self, rel: str = "") -> list[Entry]:
        base = normalize_rel(rel)
        it = smbclient.scandir(self._unc(base), **self._kw())
        out: list[Entry] = []
        try:
            for e in it:
                if _skip_name(e.name):
                    continue
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                out.append(Entry(_join_rel(base, e.name), e.name, is_dir,
                                 0 if is_dir else (st.st_size or 0),
                                 st.st_mtime or 0.0))
        finally:
            close = getattr(it, "close", None)
            if close:
                close()
        out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return out

    def stat(self, rel: str) -> Entry | None:
        base = normalize_rel(rel)
        path = self._unc(base)
        try:
            st = smbclient.stat(path, **self._kw())
        except Exception:
            return None
        is_dir = bool(getattr(st, "st_file_attributes", 0) & 0x10)
        return Entry(base, os.path.basename(base) or os.path.basename(self.root),
                     is_dir, 0 if is_dir else (st.st_size or 0), st.st_mtime or 0.0)

    def open_read(self, rel: str) -> BinaryIO:
        return smbclient.open_file(self._unc(rel), mode="rb", **self._kw())

    def write_stream(self, rel: str, src: BinaryIO, size: int | None = None) -> None:
        written = 0
        with smbclient.open_file(self._unc(rel), mode="wb", **self._kw()) as f:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
        if size is not None and written != size:
            raise ValueError(f"写入大小不符: 期望 {size}，实际 {written}")

    def mkdirs(self, rel: str) -> None:
        smbclient.makedirs(self._unc(rel), exist_ok=True, **self._kw())

    def rename(self, src_rel: str, dst_rel: str) -> None:
        smbclient.rename(self._unc(src_rel), self._unc(dst_rel), **self._kw())

    def remove(self, rel: str) -> None:
        path = self._unc(rel)
        entry = self.stat(rel)
        if entry and entry.is_dir:
            for child in self.listdir(rel):
                self.remove(_join_rel(normalize_rel(rel), child.name))
            smbclient.rmdir(path, **self._kw())
        else:
            smbclient.remove(path, **self._kw())

    def local_path(self, rel: str) -> str | None:
        return None


def adapter_for(source: Source, password: str | None = None):
    if source.kind == "local":
        return LocalAdapter(source.root)
    if source.kind == "smb":
        return SmbAdapter(source, password)
    raise ValueError(f"未知存储类型: {source.kind}")


def test_connection(source: Source, password: str | None, limit: int = 20) -> dict:
    """真实列一次共享根，验证地址与凭据。失败不抛异常。"""
    try:
        if source.kind == "smb":
            # 连接池按服务器复用会话；测试连接必须强制重新认证，
            # 否则旧密码可能被已有会话掩盖，管理员会误以为新密码可用。
            host = source.root.replace("/", "\\").lstrip("\\").split("\\")[0]
            if host:
                try:
                    smbclient.delete_session(host)
                except Exception:
                    pass
        items = adapter_for(source, password).listdir("")
        sample = [e.name for e in items[:limit]]
        return {"ok": True, "count": len(items), "sample": sample, "detail": ""}
    except Exception as e:
        return {"ok": False, "count": 0, "sample": [],
                "detail": f"{type(e).__name__}: {e}"}
