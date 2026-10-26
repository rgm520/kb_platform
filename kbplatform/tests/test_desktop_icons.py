"""桌面（工作台）改成 DSM 风格：大号彩色图标 + 图标下居中文字 + 顶部搜索框。

用户参照的是群晖 DSM 桌面：图标是有画面的彩色图标，文字在图标下方居中，
顶部居中一个搜索框。原来的实现是「彩色方块 + 单字符字形（▤✦◈▦⚙♻）+
标题 + 副标题」的卡片墙，不像桌面。

要点：
  * 图标必须是**内联 SVG**（手绘渐变），不引外部图标库、不联网，
    也不能用单字符字形 —— 各机器字形不一致；
  * 文字在图标下方、居中；原来的副标题挪到悬停提示里；
  * 顶部搜索框要真的能搜：文件名（/fs/search）+ 正文（/search），
    不是装饰。
"""
from pathlib import Path

HTML = Path("web/index.html").read_text(encoding="utf-8")
CSS = Path("web/static/v2.css").read_text(encoding="utf-8")
JS = Path("web/static/desktop.js").read_text(encoding="utf-8")
DESK = HTML[HTML.index('id="p-desktop"'):HTML.index('id="p-files"')]


def test_desktop_uses_drawn_svg_icons():
    assert DESK.count("<svg") >= 7, "桌面图标要内联 SVG（6 个应用 + 1 个搜索镜）"
    for glyph in ("▤", "✦", "◈", "▦", "♻"):
        assert glyph not in DESK, f"还在用单字符字形 {glyph} 当图标"
    assert DESK.count("<linearGradient") >= 6, "图标要有渐变色，不是灰模"


def test_desktop_label_sits_under_the_icon():
    assert 'class="desk-label"' in DESK, "图标下方的文字没有独立类名"
    assert 'class="desk-icon"' in DESK, "图标容器类名变了？"
    assert "desk-card" in CSS and ".desk-icon svg" in CSS, "缺图标尺寸样式"
    assert ".desk-label" in CSS, "缺文字样式"


def test_desktop_has_working_search_box():
    assert 'id="desk-q"' in DESK, "桌面没有搜索框"
    assert 'id="desk-results"' in DESK, "搜索框没有结果面板"
    assert "/api/v1/fs/search" in JS, "桌面搜索没接文件名搜索"
    assert "/api/v1/search" in JS, "桌面搜索没接正文搜索"
    assert ".desk-search" in CSS and ".desk-results" in CSS, "缺搜索框样式"


def test_topbar_has_no_nav_menu_but_a_home_button():
    """有了应用图标，顶部那排导航就多余了 —— 只留一个「返回主菜单」。"""
    header = HTML[HTML.index('<header'):HTML.index('</header>')]
    assert '<nav id="nav"' not in header, "顶部导航还在：图标已承担导航"
    assert 'id="btn-home"' in header, "顶部没有返回主菜单的按钮"
    assert "主菜单" in header, "按钮上要写清楚是回主菜单"
    assert 'href="#/desktop"' not in header, "不该再有指向各页面的链接"
    # 按钮要真的能回桌面（绑定在 app.js 的顶栏逻辑里）
    app = Path("web/static/app.js").read_text(encoding="utf-8")
    assert "btn-home" in app and "#/desktop" in app, "主菜单按钮没绑定跳转"

