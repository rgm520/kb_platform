"""登录验证码：连续失败 3 次后才要求填图片验证码。

用户要求（2026-10-26）：登录页加验证码校验。定的方案：
  * 形式：图片字符验证码（服务端生成，**答案不能出现在发给客户端的数据里**）；
  * 出现时机：连续失败 3 次后才出现，平时登录不受打扰。

要点：
  * 答案在服务端（库里只存哈希），下发的只有图片；
  * 验证码一次性、5 分钟过期；
  * 失败计数按（客户端 IP + 用户名）记 —— 共享机器上一个人的失败不该连累别人；
  * 成功登录后计数清零；
  * 没有 Pillow 时降级成算术题，不能把登录搞挂。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from kbplatform import captcha as cap
from kbplatform.auth import create_user
from kbplatform.service import make_app


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


def _user(app, name="root"):
    create_user(app.state.conn, name, "pw123456", role="admin")


def _wrong(c, username="root", **kw):
    body = {"username": username, "password": "wrong-pass"}
    body.update(kw)
    return c.post("/api/v1/auth/login", json=body)


def _known_captcha(app, code="ABCD", username="root", client="tester"):
    """签一张**答案已知**的验证码（只给测试用；正常流程是随机的）。"""
    return cap.issue(app.state.conn, app.state.cfg.data_dir,
                     username, client, code=code)


def test_captcha_not_required_on_first_login(app):
    c = TestClient(app)
    _user(app)
    r = c.post("/api/v1/auth/login", json={"username": "root", "password": "pw123456"})
    assert r.status_code == 200, r.text


def test_captcha_endpoint_returns_image_only_when_required(app):
    c = TestClient(app)
    _user(app)
    d = c.get("/api/v1/auth/captcha?username=root").json()
    assert d["required"] is False, d

    for _ in range(3):
        _wrong(c)
    d = c.get("/api/v1/auth/captcha?username=root").json()
    assert d["required"] is True, d
    assert d["captcha_id"], d
    assert d["image"], "要求验证码时必须下发图片"
    assert d["image"].startswith("data:image/"), d["image"][:40]


def test_answer_is_never_sent_to_client_and_stored_hashed(app):
    """答案不能出现在下发的数据里，库里也只存哈希。"""
    c = TestClient(app)
    _user(app)
    issued = _known_captcha(app, code="ABCD")
    assert issued["captcha_id"]

    row = app.state.conn.execute(
        "SELECT code_hash FROM auth_captcha WHERE id=?",
        (issued["captcha_id"],)).fetchone()
    assert row is not None
    assert row[0] != "ABCD", "库里存的是明文答案"

    # 下发的 JSON 里也不能有答案字段
    d = c.get("/api/v1/auth/captcha?username=root").json()
    assert "code" not in d and "answer" not in d, d.keys()


def test_login_requires_captcha_after_threshold(app):
    c = TestClient(app)
    _user(app)
    for _ in range(3):
        _wrong(c)
    r = _wrong(c)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"].get("captcha_required") is True, body
    msg = body["detail"].get("message") or ""
    assert "验证码" in msg, body


def test_correct_captcha_lets_login_through(app):
    c = TestClient(app)
    _user(app)
    for _ in range(3):
        _wrong(c)
    issued = _known_captcha(app, code="ABCD")
    # 验证码过了但密码错 → 仍是密码错误（401），不是"要验证码"
    r = _wrong(c, captcha_id=issued["captcha_id"], captcha_code="ABCD")
    assert r.status_code == 401, r.text
    # 验证码是一次性的，上面那次已经用掉了 → 重新签一张再试
    issued2 = _known_captcha(app, code="WXYZ")
    r = c.post("/api/v1/auth/login", json={
        "username": "root", "password": "pw123456",
        "captcha_id": issued2["captcha_id"], "captcha_code": "WXYZ"})
    assert r.status_code == 200, r.text


def test_wrong_captcha_code_is_rejected(app):
    c = TestClient(app)
    _user(app)
    for _ in range(3):
        _wrong(c)
    issued = _known_captcha(app, code="ABCD")
    r = c.post("/api/v1/auth/login", json={
        "username": "root", "password": "pw123456",
        "captcha_id": issued["captcha_id"], "captcha_code": "ZZZZ"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].get("captcha_required") is True


def test_captcha_is_one_shot(app):
    """同一张验证码用第二次就不认（一次性）。"""
    c = TestClient(app)
    _user(app)
    for _ in range(3):
        _wrong(c)
    issued = _known_captcha(app, code="ABCD")
    # 第一次用（密码错）→ 验证码被消费掉
    r = c.post("/api/v1/auth/login", json={
        "username": "root", "password": "wrong-pass",
        "captcha_id": issued["captcha_id"], "captcha_code": "ABCD"})
    assert r.status_code == 401, r.text
    # 第二次拿同一张 → 不认，要求重新填
    r2 = c.post("/api/v1/auth/login", json={
        "username": "root", "password": "pw123456",
        "captcha_id": issued["captcha_id"], "captcha_code": "ABCD"})
    assert r2.status_code == 400, r2.text
    assert r2.json()["detail"]["captcha_required"] is True


def test_expired_captcha_is_rejected(app):
    c = TestClient(app)
    _user(app)
    for _ in range(3):
        _wrong(c)
    issued = _known_captcha(app, code="ABCD")
    app.state.conn.execute(
        "UPDATE auth_captcha SET expires_at=? WHERE id=?",
        (time.time() - 1, issued["captcha_id"]))
    app.state.conn.commit()
    r = c.post("/api/v1/auth/login", json={
        "username": "root", "password": "pw123456",
        "captcha_id": issued["captcha_id"], "captcha_code": "ABCD"})
    assert r.status_code == 400, r.text


def test_successful_login_resets_failure_counter(app):
    c = TestClient(app)
    _user(app)
    for _ in range(2):
        _wrong(c)
    assert c.get("/api/v1/auth/captcha?username=root").json()["required"] is False
    r = c.post("/api/v1/auth/login", json={"username": "root", "password": "pw123456"})
    assert r.status_code == 200
    for _ in range(2):
        _wrong(c)
    assert c.get("/api/v1/auth/captcha?username=root").json()["required"] is False, \
        "成功登录后失败计数应清零"


def test_other_username_is_not_punished(app):
    """共享机器：一个人失败不该让别人的账号也要验证码。"""
    c = TestClient(app)
    _user(app, "root")
    _user(app, "other")
    for _ in range(3):
        _wrong(c, username="root")
    assert c.get("/api/v1/auth/captcha?username=root").json()["required"] is True
    assert c.get("/api/v1/auth/captcha?username=other").json()["required"] is False


def test_code_helpers():
    code = cap.generate_code()
    assert len(code) == cap.CODE_LEN
    assert code.isalnum()
    q, a = cap.math_prompt()
    assert "?" in q and a.isdigit()
