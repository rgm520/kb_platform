"""系统品牌（logo + 系统名称）可在管理台配置。

用户要求（2026-10-26）：登录页的 logo 与系统名称不要写死，要在管理台里能改。

要点：
  * 登录页在**登录之前**就要显示 logo 与名称 → 品牌信息必须是公开可读的；
  * 改设置必须管理员权限（普通用户改不了全站名称）；
  * logo 存盘要防 XSS：SVG 能带脚本，**不收**；只收位图并限大小；
  * 名称与 logo 都要能"恢复默认"（删掉即回退）。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from kbplatform.auth import create_user
from kbplatform.service import make_app

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)   # 只需要头像够用


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config.toml"
    data = str(tmp_path / "data").replace("\\", "/")
    cfg.write_text(f'[paths]\ndata_dir="{data}"\n'
                   '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    a = make_app(str(cfg))
    yield a
    try:
        a.state.scanner.stop()
    except Exception:
        pass


def _login(app, name, role="user"):
    create_user(app.state.conn, name, "pw123456", role=role)
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return c


def test_public_branding_is_readable_before_login(app):
    """登录页在登录之前就要拿到名称 → 品牌信息必须公开可读。"""
    c = TestClient(app)
    r = c.get("/api/v1/branding")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["system_name"], "默认也该有个系统名"
    assert d["has_logo"] is False


def test_admin_can_set_system_name(app):
    admin = _login(app, "root", role="admin")
    r = admin.put("/api/v1/admin/branding", json={"system_name": "某公司知识平台"})
    assert r.status_code == 200, r.text
    assert r.json()["system_name"] == "某公司知识平台"
    # 公开接口立刻生效
    assert TestClient(app).get("/api/v1/branding").json()["system_name"] == "某公司知识平台"


def test_normal_user_cannot_change_branding(app):
    user = _login(app, "bob")
    r = user.put("/api/v1/admin/branding", json={"system_name": "我改了"})
    assert r.status_code == 403, r.text


def test_admin_can_upload_and_serve_logo(app):
    admin = _login(app, "root", role="admin")
    r = admin.post("/api/v1/admin/branding/logo",
                   files={"file": ("logo.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["has_logo"] is True

    # 登录之前也能取到图
    pub = TestClient(app).get("/api/v1/branding/logo")
    assert pub.status_code == 200, pub.text
    assert pub.content.startswith(b"\x89PNG")
    assert TestClient(app).get("/api/v1/branding").json()["has_logo"] is True


def test_logo_upload_rejects_svg_and_oversize(app):
    """SVG 能带脚本 → 不收；体积也要有上限。"""
    admin = _login(app, "root", role="admin")
    bad = admin.post("/api/v1/admin/branding/logo",
                     files={"file": ("x.svg", io.BytesIO(b"<svg onload=alert(1)>"), "image/svg+xml")})
    assert bad.status_code == 400, bad.text

    big = admin.post("/api/v1/admin/branding/logo",
                     files={"file": ("big.png", io.BytesIO(b"\x89PNG" + b"\x00" * (3 * 1024 * 1024)), "image/png")})
    assert big.status_code == 400, big.text


def test_admin_can_remove_logo_and_reset_name(app):
    admin = _login(app, "root", role="admin")
    admin.post("/api/v1/admin/branding/logo",
               files={"file": ("logo.png", io.BytesIO(PNG), "image/png")})
    admin.put("/api/v1/admin/branding", json={"system_name": "自定义"})

    assert admin.delete("/api/v1/admin/branding/logo").status_code == 200
    d = admin.get("/api/v1/admin/branding").json()
    assert d["has_logo"] is False
    assert d["system_name"] == "自定义"


def test_frontend_uses_configured_branding():
    from pathlib import Path
    html = Path("web/index.html").read_text(encoding="utf-8")
    js = Path("web/static/app.js").read_text(encoding="utf-8")
    assert 'id="login-logo"' in html, "登录页没有 logo 位"
    assert 'id="login-title"' in html, "登录页标题没给 id（换不了名称）"
    assert 'id="brand-name"' in html, "顶栏品牌名没给 id"
    assert "/api/v1/branding" in js, "前端没读品牌配置"
    assert "branding" in js, "前端没处理品牌配置"
    # 管理台要有地方改
    assert 'data-t="branding"' in html, "管理台没有「系统设置」页签"
    assert "admin-branding" in js or "branding" in js, "管理台没渲染品牌设置"
