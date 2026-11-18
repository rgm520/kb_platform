import json
import shutil
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"


def test_login_and_desktop_markup_present():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="p-desktop"' in html
    assert 'id="login-form"' in html
    assert 'data-open="files"' in html
    assert 'href="/static/v2.css"' in html
    assert 'src="/static/desktop.js"' in html


def test_file_manager_markup_and_script():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("fm-sources", "fm-crumbs", "fm-search", "fm-list", "fm-preview", "fm-ctx"):
        assert f'id="{i}"' in html
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "KB.files" in js and "/api/v1/fs/list" in js and "/api/v1/fs/search" in js


def test_upload_trash_and_admin_markup():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("fm-upload", "fm-upload-panel", "p-trash", "trash-list"):
        assert f'id="{i}"' in html
    assert 'data-t="sources"' in html
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "/api/v1/fs/upload/init" in js and "/api/v1/fs/delete" in js
    adm = (WEB / "static" / "admin-sources.js").read_text(encoding="utf-8")
    assert "/api/v1/admin/sources" in adm and "/api/v1/admin/users/" in adm


def test_view_switch_sort_and_group_controls_present():
    """文件页要有：图标/列表视图切换、排序方式、分组下拉（Windows 资源管理器式）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("fm-view-icons", "fm-view-list", "fm-sort", "fm-sort-dir", "fm-group"):
        assert f'id="{i}"' in html
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    assert "sortEntries" in js, "文件页要实现排序逻辑"
    assert "groupEntries" in js, "文件页要实现分组逻辑"
    assert "kb.files.view" in js, "视图/排序/分组偏好要按浏览器记住"


def test_board_stat_cards_drive_the_list():
    """看板统计卡片要能点，并联动下方清单（统一过滤状态 + 高亮同步）。"""
    js = (WEB / "static" / "app.js").read_text(encoding="utf-8")
    assert "setDocFilter" in js, "统计卡片点击要通过统一过滤函数"
    assert "markDocFilter" in js, "卡片与状态页签的选中态要同步"
    assert 'data-i="${i}"' in js, "卡片要带索引以便绑定过滤条件"
    assert "tier2" in js, "Tier2 卡片要能单独过滤"
    css = (WEB / "static" / "v2.css").read_text(encoding="utf-8")
    assert ".card-stat.clickable" in css and ".card-stat.active" in css


def test_board_context_menu_and_user_edit_ui():
    """看板右键菜单（查看/定位/下载）与用户信息编辑入口。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="doc-ctx"' in html
    js = (WEB / "static" / "app.js").read_text(encoding="utf-8")
    assert "showDocMenu" in js
    for act in ("view", "locate", "download"):
        assert f'data-act="{act}"' in js, f"右键菜单缺少 {act}"
    assert "/api/v1/documents/" in js and "/file" in js
    assert "window.editUser" in js
    assert "openAt" in (WEB / "static" / "files.js").read_text(encoding="utf-8")


def test_graph_layout_is_stable_and_scales_node_size():
    """图谱：布局要稳定（不因缩放自激重排）、节点随点数自动缩小、标签去重叠。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for i in ("g-fit", "g-layout", "g-zoom-in", "g-zoom-out", "g-rel-label"):
        assert f'id="{i}"' in html
    js = (WEB / "static" / "app.js").read_text(encoding="utf-8")
    assert "graphNodeSize" in js, "节点直径要随节点数自适应"
    assert "enforceMinDistance" in js, "要有最小间距约束，避免节点互相压"
    assert "declutterLabels" in js, "标签要去重叠"
    assert "fitAspectToCanvas" in js, "布局要按画布长宽比铺开"
    import subprocess
    import sys

    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，跳过前端逻辑断言")
    js = (WEB / "static" / "files.js").read_text(encoding="utf-8")
    # 抽出纯函数做断言（用显式标记，避免依赖文件里的函数顺序）
    start = js.index("/* ===== VIEW_LOGIC_START ===== */")
    end = js.index("/* ===== VIEW_LOGIC_END ===== */")
    snippet = js[start:end]
    script = snippet + """
const entries = [
  {name: "b.txt", is_dir: false, size: 10, mtime: 100, ext: ".txt"},
  {name: "A.txt", is_dir: false, size: 300, mtime: 50, ext: ".txt"},
  {name: "z", is_dir: true, size: 0, mtime: 999, ext: ""},
  {name: "c.pdf", is_dir: false, size: 20, mtime: 300, ext: ".pdf"},
];
const byName = sortEntries(entries, "name", "asc").map(e => e.name);
const bySize = sortEntries(entries, "size", "desc").map(e => e.name);
const byTime = sortEntries(entries, "mtime", "desc").map(e => e.name);
const byExt = sortEntries(entries, "ext", "asc").map(e => e.name);
const groups = groupEntries(entries, "name", "name", "asc").map(g => g.label);
console.log(JSON.stringify({byName, bySize, byTime, byExt, groups}));
"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    # 文件夹永远排最前
    assert data["byName"][0] == "z"
    assert data["bySize"][0] == "z"
    assert data["byTime"][0] == "z"
    # 名称升序
    assert data["byName"][1:] == ["A.txt", "b.txt", "c.pdf"]
    # 大小倒序
    assert data["bySize"][1:] == ["A.txt", "c.pdf", "b.txt"]
    # 修改时间倒序
    assert data["byTime"][1:] == ["c.pdf", "b.txt", "A.txt"]
    # 类型升序
    assert data["byExt"][1:] == ["c.pdf", "A.txt", "b.txt"]
    # 按名称分组：中文名按拼音首字母，英文按字母
    assert "A" in data["groups"] and "B" in data["groups"]


def test_modal_card_is_height_capped_and_scrollable():
    """看板详情弹窗：内容再长也不能顶出视口——卡片限高、正文内部滚动。

    实测（2026-11-12）：知识点多的文档详情把 .modal-card 撑到 5599px（视口 886px），
    卡片和「关闭」按钮都在屏幕外，页面又被 fixed 遮罩盖住，既滚不动也翻不了页。
    """
    import re

    css = (WEB / "static" / "style.css").read_text(encoding="utf-8")
    card = re.search(r"\.modal-card\{([^}]*)\}", css)
    assert card, "找不到 .modal-card 规则"
    rules = card.group(1).replace(" ", "").replace("\n", "")
    assert "max-height:calc(100vh" in rules, "卡片必须限高，否则长内容会顶出视口"
    assert "display:flex" in rules and "flex-direction:column" in rules, "卡片要能固定底部按钮"

    body = re.search(r"#modal-body\{([^}]*)\}", css)
    assert body, "#modal-body 要有自己的滚动规则"
    body_rules = body.group(1).replace(" ", "").replace("\n", "")
    assert "overflow-y:auto" in body_rules and "min-height:0" in body_rules, \
        "弹窗正文要内部滚动，min-height:0 才能让 flex 子项真正滚动"

    foot = re.search(r"\.modal-foot\{([^}]*)\}", css)
    assert foot and "flex:00auto" in foot.group(1).replace(" ", ""), "底部按钮不能被正文挤走"


def test_custom_scrollbar_is_themed():
    """滚动条要跟深色主题一致，别用系统默认的浅灰粗条。

    Chromium 走 ::-webkit-scrollbar 伪元素；Firefox 只认标准属性，
    用 @supports 隔离，避免新版 Chrome 因同时声明标准属性而忽略伪元素样式。
    """
    css = (WEB / "static" / "style.css").read_text(encoding="utf-8")
    assert "::-webkit-scrollbar{" in css.replace(" ", "")
    assert "::-webkit-scrollbar-thumb{" in css.replace(" ", "")
    assert "::-webkit-scrollbar-thumb:hover{" in css.replace(" ", "")
    assert "scrollbar-width:thin" in css.replace(" ", "")
    assert "scrollbar-color" in css
    assert "@supports not selector(::-webkit-scrollbar)" in css
