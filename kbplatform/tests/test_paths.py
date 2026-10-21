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
    share, rel = split_share(
        "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf",
        ["\\\\192.0.2.19\\信息化管理"])
    assert share == "信息化管理"
    assert rel == "AI知识库\\a.pdf"


def test_split_share_picks_longest_root():
    share, rel = split_share(
        "\\\\192.0.2.19\\信息化管理\\AI知识库\\a.pdf",
        ["\\\\192.0.2.19", "\\\\192.0.2.19\\信息化管理"])
    assert share == "信息化管理"
    assert rel == "AI知识库\\a.pdf"


def test_split_share_falls_back_to_first_segment():
    share, rel = split_share("\\\\192.0.2.19\\video\\a.mp4", [])
    assert share == "video"
    assert rel == "a.mp4"


def test_path_key_is_case_insensitive_and_separator_agnostic():
    assert path_key("\\\\192.0.2.19\\Info\\A.PDF") == path_key("\\\\192.0.2.19\\info\\a.pdf")
