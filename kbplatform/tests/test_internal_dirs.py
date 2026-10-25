"""平台内部目录 `_kb` 全链路清理的回归测试。

任务 0.1：`_kb` / `_kb_trash` 是平台自己的产物目录（1.0 引擎的 wiki 导出、
2.0 的回收站），不属于用户文件。它们必须：所有扫描器跳过、监控快照不预热、
文件页对所有用户隐藏（管理员也一样）、授权选择器不提供。

真实路径是嵌套的 `AI知识库\\_kb\\wiki\\…`，所以这里的用例同时覆盖
"源根下"与"嵌套在用户目录下"两种情形；名单只有一个来源
`kbplatform.platform_dirs`（平台目录 / 通用忽略目录两个集合）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kbplatform.auth import create_user
from kbplatform.db import init_db
from kbplatform.service import make_app


def _write_cfg(tmp_path, monitor_root: str) -> str:
    """生成一份配置：data_dir 在 tmp 下，monitor_roots 指到 monitor_root。"""
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    lines = [f'[paths]\ndata_dir="{data}"\n']
    if monitor_root:
        # TOML 字面量字符串，反斜杠不用转义
        lines.append(f"[monitor]\nroots=['{monitor_root}']\n")
    lines.append('[storage]\nsource_scan_interval_sec=0\n')
    cfg.write_text("\n".join(lines), encoding="utf-8")
    return str(cfg)


@pytest.fixture()
def app(tmp_path):
    return make_app(_write_cfg(tmp_path, ""))


def test_is_platform_rel():
    """平台目录名是保留名：无论在第几段，命中即视为平台目录。"""
    from kbplatform.platform_dirs import is_platform_rel
    # 源根下的平台目录
    assert is_platform_rel("_kb\\wiki\\entities\\a.md") is True
    assert is_platform_rel("_kb_trash\\20261025\\x.pdf") is True
    # 嵌在用户目录下面的真实 1.0 产物路径（只看第一段会漏）
    assert is_platform_rel("AI知识库\\_kb\\wiki\\entities\\a.md") is True
    assert is_platform_rel("AI知识库\\_kb_trash\\20261025\\x.pdf") is True
    # 纯用户目录不受影响
    assert is_platform_rel("AI知识库\\正常目录\\a.pdf") is False
    assert is_platform_rel("") is False


def test_platform_dir_names_single_source():
    """唯一来源约束：fsindex/catalog/nas/storage 的名单都从 platform_dirs 派生。"""
    import kbplatform.catalog as catalog_mod
    import kbplatform.fsindex as fsindex_mod
    import kbplatform.nas as nas_mod
    import kbplatform.storage as storage_mod
    from kbplatform.platform_dirs import IGNORED_DIR_NAMES, PLATFORM_DIR_NAMES
    for names in (fsindex_mod.SKIP_DIR_NAMES, catalog_mod.SKIP_DIR_NAMES,
                  nas_mod.SKIP_DIRS, storage_mod.SKIP_NAMES):
        assert _superset(names, PLATFORM_DIR_NAMES | IGNORED_DIR_NAMES), names
    # 1.0 扫描器额外的开发工具垃圾目录不能丢（它们不属于平台目录）
    assert {".git", "__pycache__", "node_modules"} <= catalog_mod.SKIP_DIR_NAMES


def _superset(names, want):
    got = {str(n).lower() for n in names}
    return all(w.lower() in got for w in want)


def test_scanner_skips_platform_dirs(tmp_path):
    """`_kb`/`_kb_trash` 不管在不在源根，都不进索引；正常目录照常索引。"""
    from kbplatform.fsindex import scan_source
    from kbplatform.platform_dirs import is_platform_rel
    from kbplatform.storage import LocalAdapter, source_from_row
    root = tmp_path / "src"
    (root / "_kb" / "wiki" / "entities").mkdir(parents=True)
    (root / "_kb" / "wiki" / "entities" / "a.md").write_bytes(b"x")
    (root / "_kb_trash").mkdir()
    (root / "_kb_trash" / "old.docx").write_bytes(b"x")
    (root / "AI知识库" / "_kb" / "wiki" / "entities").mkdir(parents=True)
    (root / "AI知识库" / "_kb" / "wiki" / "entities" / "a.md").write_bytes(b"x")
    (root / "AI知识库" / "正常目录").mkdir()
    (root / "AI知识库" / "正常目录" / "b.txt").write_bytes(b"x")
    conn = init_db(str(tmp_path / "kb.db"))
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,enabled,writable,created_at) "
        "VALUES ('共享','local',?,1,1,0)", (str(root),))
    conn.commit()
    source = source_from_row(conn.execute(
        "SELECT * FROM storage_sources WHERE name='共享'").fetchone())
    scan_source(conn, source, LocalAdapter(str(root)), qps=1000)
    rels = [r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM fs_entries").fetchall()]
    assert rels, "索引不应为空"
    assert not any(is_platform_rel(r) for r in rels), rels
    assert "AI知识库\\正常目录\\b.txt" in rels


def test_catalog_scanner_skips_nested_platform_dirs(tmp_path):
    """1.0 scan_root：嵌套的 `_kb` / `_kb_trash` 也跳过（原来漏 `_kb_trash`）。"""
    from kbplatform.catalog import scan_root
    from kbplatform.platform_dirs import is_platform_rel
    (tmp_path / "AI知识库" / "_kb" / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "AI知识库" / "_kb" / "wiki" / "entities" / "a.md").write_bytes(b"x")
    (tmp_path / "AI知识库" / "_kb_trash").mkdir(parents=True)
    (tmp_path / "AI知识库" / "_kb_trash" / "old.md").write_bytes(b"x")
    (tmp_path / "AI知识库" / "正常").mkdir(parents=True)
    (tmp_path / "AI知识库" / "正常" / "b.md").write_bytes(b"x")
    recs, stats = scan_root(str(tmp_path), {}, [], qps=1000)
    assert stats == {}, "平台目录 / 忽略目录下的东西不该进任何统计"
    names = [r.name for r in recs]
    assert names == ["b.md"], names
    assert all(not is_platform_rel(r.path) for r in recs)


def test_adapter_hides_platform_dirs(tmp_path):
    """适配器层：平台目录与系统目录都不出现在列目录结果里。"""
    from kbplatform.storage import LocalAdapter
    for name in ("_kb", "_kb_trash", "#recycle", "可见"):
        (tmp_path / name).mkdir()
    out = [e.name for e in LocalAdapter(str(tmp_path)).listdir("")]
    assert out == ["可见"]


def test_grant_selector_hides_platform_dirs(tmp_path):
    """授权选择器只列出真实目录，平台目录 / 系统目录不提供。"""
    from kbplatform.nas import list_subdirs
    for name in ("_kb", "_kb_trash", "#recycle", "正常"):
        (tmp_path / name).mkdir()
    assert list_subdirs(str(tmp_path)) == ["正常"]


def test_warm_snapshot_only_monitors_scoped_rows(tmp_path):
    """监控范围之外的 catalog 行（如 1.0 导出的 _kb 产物）不预热快照。"""
    from kbplatform.config import load_config
    from kbplatform.pipeline import Pipeline
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    pipe = Pipeline(load_config(_write_cfg(tmp_path, str(monitor))))
    # 当前实现用整张 catalog 表预热；插进临时库里验证
    conn = init_db(pipe.cfg.db_path)
    inside = str(monitor / "ok.pdf")
    outside = r"\\192.0.2.19\信息化管理\AI知识库\_kb\wiki\entities\a.md"
    for path in (inside, outside):
        conn.execute(
            "INSERT INTO catalog(path, share, rel_path, name, ext, size, mtime, status) "
            "VALUES (?, 'u', 'x', 'x', '.md', 1, 0, 'pending')", (path,))
    conn.commit()
    conn.close()
    pipe._warm_snapshot()
    assert inside in pipe.monitor._snapshot, "范围内的行要预热"
    assert outside not in pipe.monitor._snapshot, "范围外的行不该预热"


def test_fs_list_hides_platform_dirs(app, tmp_path):
    """`_kb` 无论在源根还是嵌套在用户目录下，都不出现在文件页（管理员同样过滤）。"""
    share = tmp_path / "share"
    (share / "_kb" / "wiki" / "entities").mkdir(parents=True)
    (share / "_kb" / "wiki" / "entities" / "a.md").write_bytes(b"x")
    (share / "AI知识库" / "_kb").mkdir(parents=True)
    (share / "AI知识库" / "_kb" / "x.md").write_bytes(b"x")
    (share / "AI知识库" / "正常目录").mkdir()
    (share / "AI知识库" / "正常目录" / "b.txt").write_bytes(b"x")
    conn = app.state.conn
    conn.execute(
        "INSERT INTO storage_sources(name,kind,root,writable,enabled,created_at) "
        "VALUES ('共享','local',?,1,1,0)", (str(share),))
    conn.commit()
    sid = conn.execute("SELECT id FROM storage_sources WHERE name='共享'").fetchone()[0]
    create_user(conn, "admin", "pw123456", role="admin")
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw123456"})
    assert r.status_code == 200, r.text
    names = lambda rel: [e["name"] for e in
                         c.get(f"/api/v1/fs/list?source_id={sid}&rel={rel}").json()["entries"]]
    assert names("") == ["AI知识库"]
    assert names("AI知识库") == ["正常目录"], "嵌套的 _kb 也要隐藏"
