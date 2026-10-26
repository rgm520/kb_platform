"""顶栏：右上角是用户头像 + 名字，账户操作收进下拉菜单。

用户要求（2026-10-26）：桌面改版后顶栏只留"返回主菜单"；"修改密码""退出"
两个按钮不再平铺在顶栏上，而是放进**用户图标的下拉菜单**里；用户名标签移到
右上角，并给用户加一个好看的图标。

要点：
  * 顶栏上不能还平铺着「修改密码 / 退出」按钮 —— 它们必须在 #user-drop 里；
  * 必须有用户头像（内联 SVG，不引外部图标）+ 右上角的姓名；
  * 下拉要能开能关（点外面/Esc 关掉）。
"""
from pathlib import Path

HTML = Path("web/index.html").read_text(encoding="utf-8")
CSS = Path("web/static/v2.css").read_text(encoding="utf-8")
JS = Path("web/static/app.js").read_text(encoding="utf-8")
HEADER = HTML[HTML.index('<header'):HTML.index('</header>')]


def test_topbar_has_user_avatar_and_name_at_right():
    assert 'id="user-btn"' in HEADER, "顶栏没有用户头像按钮"
    assert 'id="user-drop"' in HEADER, "没有用户下拉菜单"
    assert 'class="user-avatar"' in HEADER or "user-avatar" in HEADER, "缺用户头像"
    assert HEADER.count("<svg") >= 2, "头像与下拉箭头都该是内联 SVG"
    assert 'id="who"' in HEADER, "用户名标签没了"
    # 用户名和下拉菜单都在 .right 里（顶栏右侧）
    right = HEADER[HEADER.index('class="right"'):]
    assert 'id="who"' in right and 'id="user-drop"' in right, "用户信息没放到顶栏右侧"


def test_account_actions_moved_into_dropdown():
    drop = HEADER[HEADER.index('id="user-drop"'):]
    assert 'id="btn-chpwd"' in drop, "修改密码没进下拉菜单"
    assert 'id="logout"' in drop, "退出没进下拉菜单"
    # 顶栏上不该再有平铺的这两个按钮（它们现在只在 drop 里出现一次）
    assert HEADER.count('id="btn-chpwd"') == 1, "修改密码按钮重复了"
    assert HEADER.count('id="logout"') == 1, "退出按钮重复了"


def test_dropdown_is_wired():
    assert "user-drop" in JS and "user-btn" in JS, "下拉菜单没接线"
    assert ".user-drop" in CSS and ".user-avatar" in CSS, "缺下拉/头像样式"
