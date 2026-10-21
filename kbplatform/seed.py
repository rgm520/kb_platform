"""初始化平台数据：管理员、涉密目录、预置模型、默认 scope。

涉密目录初值（spec D13 / Q-B 结论）：精确到子目录。
"""
from __future__ import annotations

import sys

from kbplatform import auth, models as models_mod
from kbplatform.config import load_config
from kbplatform.db import init_db

# 精确到子目录的涉密标记
DEFAULT_RESTRICTED = [
    ("综合系统", "2.党建（党）", "党建党务"),
    ("综合系统", "2.党建（团）", "党建团委"),
    ("综合系统", "2.党建（宣）", "党建宣传"),
    ("综合系统", "2.党建（工）", "党建工会"),
    ("制造物资管理平台开发项目组", "AAA2025年审计资料汇集", "审计资料"),
    ("人力资源共享空间", "4、薪酬与福利（加密）", "薪酬福利"),
    ("人力资源共享空间", "8、涉及审计", "涉及审计"),
    # 整个 HR 共享也是涉密的（HR 制度与薪酬不宜放行给非 HR 角色）
    ("人力资源共享空间", "", "人力资源共享空间（整共享）"),
]

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin12345"


def seed(db_path: str = "D:/opencode/kb/data/kb.db",
         config_path: str = "D:/opencode/config.toml",
         create_admin: bool = True) -> dict:
    conn = init_db(db_path)
    out = {}

    n = models_mod.seed_presets(conn)
    out["models_added"] = n

    added = 0
    for share, subpath, label in DEFAULT_RESTRICTED:
        exists = conn.execute(
            "SELECT 1 FROM dir_scopes WHERE share=? AND subpath=?",
            (share, subpath)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO dir_scopes(share, subpath, restricted, label) VALUES (?,?,1,?)",
            (share, subpath, label))
        added += 1
    conn.commit()
    out["restricted_added"] = added

    if create_admin:
        u = auth.get_user(conn, ADMIN_USER)
        if u is None:
            uid = auth.create_user(conn, ADMIN_USER, ADMIN_PASSWORD, "admin",
                                   "系统管理员")
            out["admin_created"] = True
            out["admin_id"] = uid
            out["admin_password"] = ADMIN_PASSWORD
        else:
            out["admin_created"] = False
            out["admin_id"] = u["id"]
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "D:/opencode/config.toml")
    print(seed(cfg.db_path))
