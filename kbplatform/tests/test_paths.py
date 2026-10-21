import unicodedata

from kbplatform.paths import to_unc, norm_unc, split_share, path_key

DRIVES = {"W:": "\\\\192.0.2.19\\信息化管理"}


def test_drive_letter_maps_to_unc():
    assert to_unc("W:\\AI知识库\\a.pdf", DRIVES) == \
           "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"


def test_unc_input_is_idempotent():
    u = "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf"
    assert to_unc(u, DRIVES) == u


def test_unc_input_without_drive_map_is_still_normalized():
    assert to_unc("\\\\192.0.2.19\\信息化管理\\a.pdf", {}) == \
           "\\\\192.0.2.19\\信息化管理\\a.pdf"


def test_norm_collapses_duplicate_separators():
    assert norm_unc("\\\\192.0.2.19\\\\信息化管理\\\\a.pdf") == \
           "\\\\192.0.2.19\\信息化管理\\a.pdf"


def test_norm_strips_segment_edge_spaces_only():
    assert norm_unc("\\\\192.0.2.19\\ 信息化管理 \\a b.pdf") == \
           "\\\\192.0.2.19\\信息化管理\\a b.pdf"


def test_norm_does_not_unicode_normalize():
    s = "\\\\192.0.2.19\\信息化管理\\AI知识库"
    assert norm_unc(s) == s
    assert unicodedata.normalize("NFC", norm_unc(s)) == norm_unc(s)


def test_split_share_returns_share_and_relpath():
    """共享名取 UNC 的第 2 段，相对路径是共享名之后的全部内容。"""
    share, rel = split_share(
        "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf",
        ["\\\\192.0.2.19\\信息化管理"])
    assert share == "信息化管理"
    assert rel == "AI知识库\\a.pdf"


def test_split_share_ignores_monitor_root_depth():
    """监控根指向共享内的子目录时，共享名仍是共享名。"""
    share, rel = split_share(
        "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf",
        ["\\\\192.0.2.19\\信息化管理\\AI知识库"])
    assert share == "信息化管理"
    assert rel == "AI知识库\\a.pdf"


def test_is_under_root_respects_directory_boundary():
    from kbplatform.paths import is_under_root
    roots = ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert is_under_root("\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf", roots)
    assert is_under_root("\\\\192.0.2.19\\信息化管理\\AI知识库", roots)
    assert not is_under_root("\\\\192.0.2.19\\信息化管理\\AI知识库2\\a.pdf", roots)
    assert not is_under_root("\\\\192.0.2.19\\其他\\a.pdf", roots)


def test_split_share_falls_back_to_first_segment():
    share, rel = split_share("\\\\192.0.2.19\\video\\a.mp4", [])
    assert share == "video"
    assert rel == "a.mp4"


def test_path_key_is_case_insensitive_and_separator_agnostic():
    assert path_key("\\\\192.0.2.19\\Info\\A.PDF") == path_key("\\\\192.0.2.19\\info\\a.pdf")
