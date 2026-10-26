"""登录验证码：服务端生成图片字符验证码。

为什么答案不能交给客户端：
    验证码的作用是挡住"脚本自动试密码"。如果答案以任何机器可读的形式
    （JSON 字段、SVG 文本、文件名…）发到浏览器，脚本读一下就过了，等于没有。
    所以：**服务端生成位图，库里只存答案的哈希**，下发的只有图片。

触发策略：
    连续登录失败达到阈值后才要求验证码，平时登录不受打扰（内部同事每天要
    登录多次）。失败计数按（客户端 IP + 用户名）记 —— 共享机器上一个人的
    连试不该让同机器的其他人也被要求填验证码。成功登录后计数清零。

没有 Pillow 时降级成算术题（图片那套是 python-pptx 的依赖，正常都在；
    降级保证极端情况下登录功能不被搞挂）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import random
import secrets
import time

# 去掉容易看错的字符（I/O/0/1）
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 4
FAIL_THRESHOLD = 3          # 连续失败几次后要验证码
CAPTCHA_TTL = 300           # 验证码有效期（秒）
FAIL_WINDOW = 1800          # 失败计数多久算"连续"（秒）
IMG_W, IMG_H = 150, 48

# 字体候选（Pillow 画字用；系统字体路径因机器而异，逐个试）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\consolab.ttf",
    r"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    r"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


# ---------------------------------------------------------------- 工具
def generate_code(rng: random.Random | None = None) -> str:
    r = rng or random.SystemRandom()
    return "".join(r.choice(ALPHABET) for _ in range(CODE_LEN))


def math_prompt(rng: random.Random | None = None) -> tuple[str, str]:
    """降级用的算术题：(题面, 答案)。"""
    r = rng or random.SystemRandom()
    a, b = r.randint(1, 9), r.randint(1, 9)
    if r.random() < 0.5:
        return f"{a} + {b} = ?", str(a + b)
    return f"{a + b} - {a} = ?", str(b)


def code_hash(code: str, pepper: str = "") -> str:
    return hashlib.sha256((pepper + ":" + str(code)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 图片
def _font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_png(code: str) -> bytes | None:
    """把验证码画成 PNG。Pillow 不可用时返回 None（调用方降级成算术题）。"""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception:
        return None
    try:
        img = Image.new("RGB", (IMG_W, IMG_H), (246, 248, 251))
        d = ImageDraw.Draw(img)
        # 背景噪点
        r = random.SystemRandom()
        for _ in range(420):
            x, y = r.randrange(IMG_W), r.randrange(IMG_H)
            d.point((x, y), fill=(r.randrange(200, 246), r.randrange(200, 246), r.randrange(205, 250)))
        # 干扰线
        for _ in range(4):
            x1, y1 = r.randrange(IMG_W), r.randrange(IMG_H)
            x2, y2 = r.randrange(IMG_W), r.randrange(IMG_H)
            d.line([(x1, y1), (x2, y2)],
                   fill=(r.randrange(120, 200), r.randrange(120, 200), r.randrange(160, 230)),
                   width=1)
        # 字符
        step = IMG_W // (len(code) + 1)
        for i, ch in enumerate(code):
            f = _font(r.randrange(26, 34))
            # 随机基线与旋转
            bbox = d.textbbox((0, 0), ch, font=f)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = step * i + r.randrange(-3, 6)
            y = (IMG_H - h) // 2 + r.randrange(-4, 5)
            color = (r.randrange(20, 110), r.randrange(20, 110), r.randrange(60, 150))
            d.text((x, y), ch, font=f, fill=color)
        img = img.filter(ImageFilter.SMOOTH)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ---------------------------------------------------------------- 计数与签发
def _pepper(conn) -> str:
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key='captcha_pepper'").fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    p = secrets.token_hex(16)
    try:
        conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('captcha_pepper',?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (p, time.time()))
        conn.commit()
    except Exception:
        pass
    return p


def fail_key(client: str, username: str) -> str:
    return f"{client or 'unknown'}|{(username or '').strip().lower()}"


def get_fails(conn, client: str, username: str) -> int:
    key = fail_key(client, username)
    try:
        row = conn.execute(
            "SELECT fails, last_at FROM login_attempts WHERE client_key=?",
            (key,)).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    if time.time() - (row["last_at"] or 0) > FAIL_WINDOW:
        return 0
    return int(row["fails"] or 0)


def add_fail(conn, client: str, username: str) -> int:
    key = fail_key(client, username)
    try:
        conn.execute(
            "INSERT INTO login_attempts(client_key, fails, last_at) VALUES(?,1,?) "
            "ON CONFLICT(client_key) DO UPDATE SET fails=login_attempts.fails+1, "
            "last_at=excluded.last_at", (key, time.time()))
        conn.commit()
    except Exception:
        return 0
    return get_fails(conn, client, username)


def reset_fails(conn, client: str, username: str) -> None:
    try:
        conn.execute("DELETE FROM login_attempts WHERE client_key=?",
                     (fail_key(client, username),))
        conn.commit()
    except Exception:
        pass


def captcha_required(conn, client: str, username: str) -> bool:
    return get_fails(conn, client, username) >= FAIL_THRESHOLD


def issue(conn, data_dir: str, username: str, client: str, code: str | None = None) -> dict:
    """签发一张验证码。

    code 只给测试用（正常流程是随机生成）。答案只以哈希形式落库。
    """
    pepper = _pepper(conn)
    prompt = None
    if code is None:
        # 先按图片模式：生成的字符必须与落库答案一致
        candidate = generate_code()
        png = render_png(candidate)
        if png is None:
            # 没有 Pillow → 降级成算术题，用户输入的是答案
            prompt, code = math_prompt()
            png = None
        else:
            code = candidate
    else:
        png = render_png(code)
        if png is None:
            prompt, _ = math_prompt()
    cid = secrets.token_urlsafe(16)
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO auth_captcha(id, code_hash, created_at, expires_at) VALUES(?,?,?,?)",
            (cid, code_hash(code, pepper), now, now + CAPTCHA_TTL))
        # 顺手清掉过期的
        conn.execute("DELETE FROM auth_captcha WHERE expires_at < ?", (now,))
        conn.commit()
    except Exception:
        pass
    out = {"captcha_id": cid, "image": data_url(png) if png else ""}
    if prompt:
        out["prompt"] = prompt
    return out


def verify(conn, captcha_id: str, code: str, client: str = "") -> bool:
    """校验验证码。通过后立刻作废（一次性）。"""
    if not captcha_id or not code:
        return False
    pepper = _pepper(conn)
    now = time.time()
    try:
        row = conn.execute(
            "SELECT code_hash, expires_at FROM auth_captcha WHERE id=?",
            (captcha_id,)).fetchone()
        if not row:
            return False
        if (row["expires_at"] or 0) < now:
            conn.execute("DELETE FROM auth_captcha WHERE id=?", (captcha_id,))
            conn.commit()
            return False
        ok = secrets.compare_digest(str(row["code_hash"]), code_hash(str(code).strip(), pepper))
    except Exception:
        return False
    # 一次性：不管对错都用掉，避免拿同一张图反复试
    try:
        conn.execute("DELETE FROM auth_captcha WHERE id=?", (captcha_id,))
        conn.commit()
    except Exception:
        pass
    return ok
