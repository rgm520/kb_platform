"""文档看板的文档清单：列宽要固定，短列不许换行。

用户反馈（2026-10-26，截图）：文件名一列占了大半屏宽，而「共享 / 状态 /
两层进度 / 知识点 / 更新时间」这些短列全被挤成两行（"信息化 / 管理"、
"已解 / 析"、"知识 / 点"、日期和时间也断开）。

要点：
  * 表格用 <colgroup> 固定列宽，别让浏览器自己分配；
  * 除文件名列外一律 nowrap；
  * 文件名下面那行显示**所在目录**，不再把完整路径重复一遍（原来
    "附件：xxx.pdf" 下面又是 "AI知识库\\附件：xxx.pdf"，纯属重复且撑爆列宽）。
"""
from pathlib import Path

HTML = Path("web/index.html").read_text(encoding="utf-8")
CSS = Path("web/static/v2.css").read_text(encoding="utf-8")
JS = Path("web/static/app.js").read_text(encoding="utf-8")


def test_board_table_has_fixed_column_widths():
    assert "<colgroup>" in HTML, "文档表格没有 <colgroup>，列宽全靠浏览器瞎分"
    # 九列都要有对应的 col 定义（文件/共享/类型/状态/两层进度/块/知识点/更新时间/操作）
    for cls in ("c-file", "c-share", "c-type", "c-status", "c-progress",
                "c-chunk", "c-kp", "c-time", "c-act"):
        assert cls in HTML, f"列宽定义缺少 {cls}"
    assert "#doc-table col.c-file" in CSS, "没有给文件名列定宽"
    assert "#doc-table col.c-share" in CSS, "没有给共享列定宽"


def test_board_table_short_columns_do_not_wrap():
    assert "#doc-table td" in CSS and "white-space:nowrap" in CSS, \
        "短列必须不换行，否则「共享/状态/知识点」会被拆成两行"


def test_board_row_shows_folder_instead_of_repeated_full_path():
    assert "parentRel" in JS, "没有取所在目录的函数"
    # 文件名那一格：第二行用所在目录，完整路径放 title
    assert "d.rel_path" in JS
    assert "doc-dir" in JS, "所在目录那行没有独立的类名（不好做样式）"
