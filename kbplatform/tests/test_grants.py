from kbplatform.config import Config
from kbplatform.grants import (Grant, can_read, can_write, filter_entries,
                               migrate_user_scopes, set_grants, user_grants)
from kbplatform.storage import Entry


def _user(role="user", uid=7, name="u1"):
    return {"id": uid, "role": role, "username": name}


def _e(rel, is_dir=True):
    return Entry(rel=rel, name=rel.split("\\")[-1], is_dir=is_dir, size=0, mtime=0)


def test_prefix_and_write_rules(conn):
    set_grants(conn, 7, [{"source_id": 1, "subpath": "A\\B", "can_write": True}])
    gs = user_grants(conn, _user())
    assert can_read(_user(), gs, 1, "A\\B\\c.txt")
    assert can_write(_user(), gs, 1, "A\\B\\c.txt")
    assert can_read(_user(), gs, 1, "A")          # 祖先可进入
    assert not can_write(_user(), gs, 1, "A")
    assert not can_read(_user(), gs, 1, "A\\C")
    assert not can_read(_user(), gs, 2, "A\\B")


def test_whole_source_grant(conn):
    set_grants(conn, 7, [{"source_id": 1, "subpath": "", "can_write": False}])
    gs = user_grants(conn, _user())
    assert can_read(_user(), gs, 1, "任意\\目录\\a.txt")
    assert not can_write(_user(), gs, 1, "任意\\a.txt")


def test_ancestor_is_visible_but_siblings_are_hidden():
    gs = [Grant(1, "A\\B\\C", False)]
    entries = [_e("A"), _e("A\\B"), _e("A\\X")]
    assert [e.rel for e in filter_entries(gs, 1, "", entries)] == ["A"]
    entries = [_e("A\\B"), _e("A\\X")]
    assert [e.rel for e in filter_entries(gs, 1, "A", entries)] == ["A\\B"]
    entries = [_e("A\\B\\C"), _e("A\\B\\D")]
    assert [e.rel for e in filter_entries(gs, 1, "A\\B", entries)] == ["A\\B\\C"]


def test_admin_sees_everything():
    u = _user("admin")
    assert can_read(u, [], 99, "任意") and can_write(u, [], 99, "任意")


def test_personal_space_is_granted_with_write(conn):
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,personal_root,enabled,created_at) "
                 "VALUES (3,'共享','local','X:/s','_personal',1,0)")
    conn.commit()
    gs = user_grants(conn, _user())
    assert Grant(3, "_personal\\u1", True) in gs
    assert can_read(_user(), gs, 3, "_personal\\u1\\a.txt")
    assert can_write(_user(), gs, 3, "_personal\\u1\\a.txt")
    assert not can_read(_user(), gs, 3, "_personal\\u2\\a.txt")


def test_migrate_legacy_scopes(conn):
    conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES (9,'u9','x','user')")
    conn.execute("INSERT INTO storage_sources(id,name,kind,root,enabled,created_at) "
                 "VALUES (1,'信息化管理','smb','\\\\\\\\nas\\信息化管理\\AI知识库',1,0)")
    conn.execute("INSERT INTO user_scopes(user_id,share,subpath) VALUES (9,'信息化管理','AI知识库\\党建')")
    conn.execute("INSERT INTO user_scopes(user_id,share,subpath) VALUES (9,'其他','X')")
    conn.commit()
    assert migrate_user_scopes(conn, Config()) == 1
    rows = conn.execute("SELECT source_id,subpath,can_write FROM grants WHERE user_id=9").fetchall()
    assert [tuple(r) for r in rows] == [(1, "党建", 0)]
    assert migrate_user_scopes(conn, Config()) == 0
