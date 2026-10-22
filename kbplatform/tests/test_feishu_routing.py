"""模型路由与 session 权限规则。"""
from kbplatform.auth import create_user, set_scopes
from kbplatform.db import init_db
from kbplatform.feishu.routing import (build_permissions, pick_model,
                                       restricted_dirs,
                                       user_has_restricted_access)

CLOUD = {"id": "c", "providerID": "opencode", "modelID": "c"}
LOCAL = {"id": "l", "providerID": "ollama", "modelID": "l"}


def _restricted(conn, share="W:", subpath="2.党建"):
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES(?,?,1)",
                 (share, subpath))
    conn.commit()


def test_restricted_dirs_listed(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn, "W:", "2.党建")
    conn.execute("INSERT INTO dir_scopes(share, subpath, restricted) VALUES('U:','x',0)")
    conn.commit()
    assert restricted_dirs(conn) == ["W:/2.党建"]


def test_restricted_dirs_with_empty_subpath(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn, "W:", "")
    assert restricted_dirs(conn) == ["W:"]


def test_user_without_scope_has_no_restricted_access(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn)
    uid = create_user(conn, "u", "pw")
    assert user_has_restricted_access(conn, uid) is False


def test_user_with_scope_on_restricted_dir_has_access(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn)
    uid = create_user(conn, "u", "pw")
    set_scopes(conn, uid, [("W:", "2.党建")])
    assert user_has_restricted_access(conn, uid) is True


def test_user_with_scope_on_unrelated_dir_has_no_restricted_access(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn, "W:", "2.党建")
    uid = create_user(conn, "u", "pw")
    set_scopes(conn, uid, [("W:", "别的目录")])
    assert user_has_restricted_access(conn, uid) is False


def test_admin_has_no_restricted_scope_restriction(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn)
    uid = create_user(conn, "a", "pw", role="admin")
    assert user_has_restricted_access(conn, uid) is False


def test_pick_model_uses_local_for_restricted_user(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    _restricted(conn)
    uid = create_user(conn, "u", "pw")
    set_scopes(conn, uid, [("W:", "2.党建")])
    ref, is_local = pick_model(conn, uid, CLOUD, LOCAL)
    assert is_local is True and ref == LOCAL


def test_pick_model_uses_cloud_otherwise(tmp_path):
    conn = init_db(str(tmp_path / "kb.db"))
    uid = create_user(conn, "u", "pw")
    ref, is_local = pick_model(conn, uid, CLOUD, LOCAL)
    assert is_local is False and ref == CLOUD


def test_build_permissions_denies_external_directory():
    perms = build_permissions(["W:/2.党建"], [".feishu/*"])
    assert {"action": "external_directory", "resource": "*", "effect": "deny"} in perms
    assert {"action": "read", "resource": ".feishu/*", "effect": "deny"} in perms
    assert {"action": "edit", "resource": ".feishu/*", "effect": "deny"} in perms
    assert {"action": "read", "resource": "W:/2.党建/*", "effect": "deny"} in perms
    assert {"action": "edit", "resource": "W:/2.党建/*", "effect": "deny"} in perms
    assert {"action": "shell", "resource": "*W:/2.党建*", "effect": "deny"} in perms


def test_build_permissions_denies_secrets():
    perms = build_permissions([], [])
    assert {"action": "read", "resource": "secrets.toml", "effect": "deny"} in perms
    assert {"action": "shell", "resource": "*secrets.toml*", "effect": "deny"} in perms


def test_build_permissions_denies_only_listed_paths():
    perms = build_permissions(["W:/甲"], [])
    reads = {p["resource"] for p in perms if p["action"] == "read"}
    assert "W:/甲/*" in reads
    assert "W:/乙/*" not in reads


def test_build_permissions_last_match_wins_order(tmp_path):
    """规则顺序：宽泛的在前，具体的在后（opencode 是最后匹配者生效）。"""
    perms = build_permissions(["W:/甲"], [".feishu/*"])
    idx_ext = [i for i, p in enumerate(perms)
               if p["action"] == "external_directory"][0]
    idx_secret = max(i for i, p in enumerate(perms)
                      if p["action"] == "read" and p["resource"] == "secrets.toml")
    assert idx_ext < idx_secret
