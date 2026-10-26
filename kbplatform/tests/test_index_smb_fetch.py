"""带账号密码的 SMB 共享：解析要走平台自己的 SMB 客户端取文件。

回归（2026-10-26）：tmp_ppt 接入后点扫描，7 个文件全部"解析失败：文件无有效文本"。

根因两层：
  1. 解析时直接用 UNC 路径 `open()` —— 走的是**操作系统重定向器**，而这条共享
     需要账号密码，凭据只存在平台里（smbclient）→ 真实错误是
     `PackageNotFoundError`（文件打不开）。
  2. `kb.parsers.parse_file` 把异常吞成 0 个块，`index_file` 于是报了
     "文件无有效文本"，把"读不到文件"伪装成"文件是空的"。

修法：
  * 带凭据的 SMB 源，先用平台 SMB 客户端把文件取到本地临时文件再解析；
  * 解析器的 error 元数据要如实写进 status_detail，不再吞掉真实原因。
"""
from __future__ import annotations

import io

from kbplatform.catalog import FileRecord
from kbplatform.db import init_db, upsert_catalog
from kbplatform.fts import build_index
from kbplatform.indexer import _fetch_local, index_file
from kbplatform.vectors import VectorStore

BODY = "信创资源池由总部统一规划建设，各单位不得自行采购。\n" * 30


class _FakeAdapter:
    """模拟 smbclient：操作系统读不到的路径，平台自己的适配器能读。"""

    def __init__(self, files):
        self.files = files
        self.reads = []

    def open_read(self, rel):
        key = str(rel).replace("\\", "/")
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(rel)
        return io.BytesIO(self.files[key])


def _setup(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    fts_path = str(tmp_path / "fts.db")
    build_index(fts_path, [])
    vs = VectorStore(str(tmp_path / "vectors"), dim=1024)
    return conn, fts_path, vs


def _add_source(conn, root, username=None, secret_enc=None, name="共享"):
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,username,secret_enc,enabled,created_at) "
        "VALUES (?,?,?,?,?,1,0)", (name, "smb", root, username, secret_enc))
    conn.commit()


def test_index_file_fetches_from_authenticated_smb_source(tmp_path, monkeypatch):
    """OS 读不到的共享：解析前先经 SMB 客户端取到本地，再正常切块。"""
    conn, fts_path, vs = _setup(tmp_path)
    root = r"\\fake-nas\share"
    _add_source(conn, root, username="u")
    path = root + r"\a.txt"
    cid = upsert_catalog(conn, FileRecord(
        path=path, share="share", rel_path="a.txt", name="a.txt",
        ext=".txt", size=len(BODY), mtime=0.0))

    fake = _FakeAdapter({"a.txt": BODY.encode("utf-8")})
    monkeypatch.setattr("kbplatform.indexer.adapter_for", lambda src, pw=None: fake)

    res = index_file(conn, fts_path, vs, cid, path)

    assert res.ok, res.error
    assert fake.reads == ["a.txt"], "应该经平台 SMB 客户端按相对路径取文件"
    assert conn.execute("SELECT chunk_count FROM catalog WHERE id=?",
                        (cid,)).fetchone()[0] > 0
    # 块的归属仍是 NAS 上的原路径，不是临时文件
    assert conn.execute("SELECT file_path FROM chunks LIMIT 1").fetchone()[0] == path


def test_anonymous_smb_source_is_not_fetched(tmp_path, monkeypatch):
    """没有账号密码的共享（如现网 192.0.2.19 那几个）保持原样，不额外下载。"""
    conn, _, _ = _setup(tmp_path)
    _add_source(conn, r"\\fake-nas\anon", name="匿名共享")
    called = []
    monkeypatch.setattr("kbplatform.indexer.adapter_for",
                        lambda *a, **k: called.append(1) or None)

    assert _fetch_local(conn, r"\\fake-nas\anon\x.pdf") is None
    assert called == [], "匿名共享不需要走 SMB 客户端取文件"


def test_fetch_local_ignores_paths_outside_known_sources(tmp_path, monkeypatch):
    conn, _, _ = _setup(tmp_path)
    _add_source(conn, r"\\fake-nas\share", username="u")
    monkeypatch.setattr("kbplatform.indexer.adapter_for",
                        lambda *a, **k: _FakeAdapter({}))
    assert _fetch_local(conn, r"\\other-host\share\x.pdf") is None


def test_parse_error_reports_real_reason(tmp_path):
    """文件打不开/格式坏掉时，不能再报成"无有效文本"。"""
    conn, fts_path, vs = _setup(tmp_path)
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a real pptx")
    cid = upsert_catalog(conn, FileRecord(
        path=str(bad), share="本地", rel_path="bad.pptx", name="bad.pptx",
        ext=".pptx", size=16, mtime=0.0))

    res = index_file(conn, fts_path, vs, cid, str(bad))

    assert res.ok is False
    assert "解析失败" in (res.error or ""), res.error
    assert "无有效文本" not in (res.error or ""), "真实原因被埋成了'无有效文本'"
