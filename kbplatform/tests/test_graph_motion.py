"""知识图谱：Obsidian 式活体图谱。

用户参照 Obsidian 图谱视图（2026-10-26），要点：
  * **节点位置是动态的** —— 力仿真持续演化，节点缓慢漂移、簇会呼吸，
    不是"布局一次就定死"（cytoscape 的 cose 是一次性布局，做不到）；
  * **一屏要上千个点**，点小、线细、大部分节点不写字（文字淡出阈值）；
  * **右侧有控制面板**：筛选（类型/孤立节点/搜索）、分组（按类型上色）、
    显示（节点大小、连线粗细、文字淡出阈值、箭头）；
  * 动画有护栏：页面不可见时停、系统"减少动态效果"时只做静态。
  * 另加：能导出成 Obsidian 库/画布（用现成工具，不自己造轮子）。

本文件钉住这些行为。
"""
from pathlib import Path

JS = Path("web/static/graph-view.js").read_text(encoding="utf-8")
APP = Path("web/static/app.js").read_text(encoding="utf-8")
HTML = Path("web/index.html").read_text(encoding="utf-8")
CSS = Path("web/static/v2.css").read_text(encoding="utf-8")


def test_graph_is_a_live_force_simulation():
    """位置是持续演化的，不是布局一次就定死。"""
    assert "requestAnimationFrame" in JS, "没有逐帧仿真"
    assert "repulsion" in JS and "spring" in JS, "缺少斥力/弹簧（不是力仿真）"
    assert "function step(" in JS, "没有仿真步进函数"
    assert "damping" in JS, "没有阻尼（会一直加速抖）"
    assert "maxStep" in JS, "每帧位移没有上限（会炸开）"


def test_graph_has_control_panel():
    """右侧面板：筛选 / 分组 / 显示。"""
    assert 'id="g-panel"' in HTML, "图谱页没有右侧控制面板"
    for key in ("gp-q", "gp-size", "gp-link", "gp-fade", "gp-arrows"):
        assert key in JS, f"面板缺少 {key}"
    assert "GROUPS" in JS and "entity" in JS, "缺少分组（按类型上色）"
    assert "applyFilters" in JS, "没有筛选逻辑"
    assert ".g-panel" in CSS or ".gp-sec" in CSS, "缺面板样式"


def test_graph_loads_many_nodes():
    """一屏要上千个点，不是 60 个。"""
    assert "/api/v1/graph/view" in APP, "图谱没有用新的大图接口"
    assert "limit=1000" in APP, "默认节点数太少"


def test_graph_labels_fade_out():
    """大部分节点不写字（Obsidian 的 text fade threshold）。"""
    assert "textFade" in JS and "updateLabels" in JS, "没有文字淡出"
    assert "text-opacity" in JS, "没有用透明度控制文字"


def test_graph_animations_have_guardrails():
    assert "prefers-reduced-motion" in JS, "没有尊重系统的「减少动态效果」"
    assert "document.hidden" in JS, "页面不可见时应该停（别烧后台 CPU）"


def test_graph_export_to_obsidian():
    """用现成工具：能导出成 Obsidian 库与画布。"""
    assert "/api/v1/graph/export/vault" in JS, "没有导出 Obsidian 库"
    assert "/api/v1/graph/export/canvas" in JS, "没有导出 Obsidian 画布"
    backend = Path("kbplatform/graph_export.py")
    assert backend.exists(), "缺导出实现"
    src = backend.read_text(encoding="utf-8")
    assert "[[" in src, "库笔记之间要用 Obsidian 双链"


def test_backend_exposes_big_graph_and_export():
    svc = Path("kbplatform/service.py").read_text(encoding="utf-8")
    assert '"/api/v1/graph/view"' in svc, "缺大图接口"
    assert '"/api/v1/graph/export/vault"' in svc, "缺库导出接口"
    assert '"/api/v1/graph/export/canvas"' in svc, "缺画布导出接口"
