"""系统品牌：名称与 logo（管理台可配，登录页在登录前就要显示）。

为什么单独一个模块：
  * 品牌信息有**公开读**（登录页要用）与**管理员写**两种通路；
  * logo 是二进制文件，存盘要防 XSS（SVG 能带脚本）与超大文件；
  * 名称/图片都可能没有 —— 都要能优雅回退到默认值。

存储：名称等文本进 app_settings 键值表；图片存到 data_dir/branding/logo.<ext>，
文件路径也记在键值表里（换图就换文件名，旧的删掉）。
"""
from __future__ import annotations

import os
import shutil
import time

# 默认名称：没配过时用这个
DEFAULT_SYSTEM_NAME = "企业知识平台"
# 允许的 logo 类型（位图）。**不收 SVG**：SVG 能带脚本，登录页直接渲染有 XSS 风险。
ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_LOGO_BYTES = 2 * 1024 * 1024      # 2MB
BRANDING_DIRNAME = "branding"


def _dir(data_dir: str) -> str:
    return os.path.join(data_dir, BRANDING_DIRNAME)


def get_setting(conn, key: str, default=None):
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except Exception:
        return default
    if not row:
        return default
    return row[0] if row[0] is not None else default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()))
    conn.commit()


def get_system_name(conn) -> str:
    name = (get_setting(conn, "system_name", "") or "").strip()
    return name or DEFAULT_SYSTEM_NAME


def set_system_name(conn, name: str) -> str:
    name = (name or "").strip()
    if not name:
        # 空名等于"恢复默认"
        conn.execute("DELETE FROM app_settings WHERE key='system_name'")
        conn.commit()
        return DEFAULT_SYSTEM_NAME
    if len(name) > 60:
        raise ValueError("系统名称不能超过 60 个字")
    set_setting(conn, "system_name", name)
    return name


def logo_path(conn, data_dir: str) -> str | None:
    """当前 logo 的绝对路径；没有就 None。"""
    rel = get_setting(conn, "logo_file", "") or ""
    if not rel:
        return None
    p = os.path.join(data_dir, rel)
    return p if os.path.isfile(p) else None


def has_logo(conn, data_dir: str) -> bool:
    return logo_path(conn, data_dir) is not None


def save_logo(conn, data_dir: str, filename: str, content_type: str, data: bytes) -> dict:
    """保存 logo。返回落盘信息；类型/大小不合规直接抛 ValueError。"""
    ext = ALLOWED_LOGO_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("只支持 PNG / JPEG / WEBP / GIF 图片")
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("图片不能超过 2MB")
    d = _dir(data_dir)
    os.makedirs(d, exist_ok=True)
    old = get_setting(conn, "logo_file", "") or ""
    # 文件名固定，别用用户提供的原始名（防路径穿越/怪字符）
    name = "logo" + ext
    full = os.path.join(d, name)
    with open(full, "wb") as f:
        f.write(data)
    rel = os.path.join(BRANDING_DIRNAME, name)
    set_setting(conn, "logo_file", rel)
    if old and old != rel:
        try:
            os.remove(os.path.join(data_dir, old))
        except OSError:
            pass
    return {"file": rel, "bytes": len(data), "type": content_type}


def remove_logo(conn, data_dir: str) -> bool:
    old = get_setting(conn, "logo_file", "") or ""
    conn.execute("DELETE FROM app_settings WHERE key='logo_file'")
    conn.commit()
    if old:
        try:
            os.remove(os.path.join(data_dir, old))
        except OSError:
            pass
        return True
    return False


def branding_state(conn, data_dir: str) -> dict:
    """给前端用的一份品牌状态。"""
    return {
        "system_name": get_system_name(conn),
        "has_logo": has_logo(conn, data_dir),
        "logo_url": "/api/v1/branding/logo" if has_logo(conn, data_dir) else "",
    }


def purge_branding_dir(data_dir: str) -> None:
    """只清 logo 目录（平台内部目录，不进索引）。"""
    d = _dir(data_dir)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
