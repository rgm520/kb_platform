from kbplatform.fsindex import scan_source, search
from kbplatform.storage import LocalAdapter, Source


def _src(tmp_path, sid=1):
    return Source(id=sid, name="本地", kind="local", root=str(tmp_path), writable=True)


def test_scan_indexes_all_files_and_is_incremental(tmp_path, conn):
    # 共享根必须是独立目录：测试库 kb.db 不能落在被扫描的目录里
    share = tmp_path / "share"
    (share / "资料").mkdir(parents=True)
    (share / "资料" / "年度信息化计划.docx").write_text("x", encoding="utf-8")
    (share / "资料" / "照片.jpg").write_bytes(b"jpg")
    src, ad = _src(share), LocalAdapter(str(share))
    r1 = scan_source(conn, src, ad)
    assert (r1["files"], r1["dirs"]) == (2, 1)
    assert conn.execute("SELECT COUNT(*) FROM fs_entries").fetchone()[0] == 3
    assert search(conn, "信息化", None)[0]["name"] == "年度信息化计划.docx"
    assert search(conn, "计划", None)[0]["name"] == "年度信息化计划.docx"
    r2 = scan_source(conn, src, ad)
    assert (r2["added"], r2["updated"]) == (0, 0)
    (share / "资料" / "照片.jpg").unlink()
    r3 = scan_source(conn, src, ad)
    assert r3["removed"] == 1
    assert search(conn, "照片", None) == []


def test_scan_skips_trash_and_lock_files(tmp_path, conn):
    share = tmp_path / "share"
    (share / "_kb_trash").mkdir(parents=True)
    (share / "_kb_trash" / "old.docx").write_text("x", encoding="utf-8")
    (share / "~$lock.docx").write_text("x", encoding="utf-8")
    (share / "real.docx").write_text("x", encoding="utf-8")
    scan_source(conn, _src(share), LocalAdapter(str(share)))
    names = {r[0] for r in conn.execute("SELECT name FROM fs_entries")}
    assert names == {"real.docx"}


def test_search_respects_allowed_scope(conn):
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
                 "VALUES (1,'甲','local','X:/甲',1,0)")
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
                 "VALUES (2,'乙','local','X:/乙',1,0)")
    rows = [("甲部门\\预算.docx", "甲部门", "预算.docx"),
            ("乙部门\\预算.docx", "乙部门", "预算.docx")]
    for i, (rel, parent, name) in enumerate(rows):
        cur = conn.execute(
            "INSERT INTO fs_entries(source_id,rel_path,rel_key,parent_rel,name,is_dir,parseable) "
            "VALUES (?,?,?,?,?,0,1)", (i + 1, rel, rel.lower(), parent, name))
        conn.execute("INSERT INTO fs_fts(rowid,name,rel_path) VALUES (?,?,?)",
                     (cur.lastrowid, name, rel))
    conn.commit()
    assert [r["name"] for r in search(conn, "预算", [(1, "")])] == ["预算.docx"]
    assert [r["name"] for r in search(conn, "预算", [(2, "")])] == ["预算.docx"]
    assert search(conn, "预算", [(3, "")]) == []
    assert [r["rel"] for r in search(conn, "预算", [(1, "甲部门")])] == ["甲部门\\预算.docx"]
    assert search(conn, "预算", [(1, "不存在")]) == []
