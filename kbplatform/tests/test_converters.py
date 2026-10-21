import pytest

from kbplatform.converters import LEGACY_EXT, available, convert_legacy, ocr_pdf, ocr_available


def test_legacy_ext_set_is_exact():
    assert LEGACY_EXT == {".doc", ".ppt", ".xls"}


def test_available_reports_booleans():
    a = available()
    assert set(a) == {"soffice", "ocr"}
    assert all(isinstance(v, bool) for v in a.values())


def test_convert_legacy_returns_none_when_converter_unavailable(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "soffice_path", lambda: None)
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    assert c.convert_legacy(str(p), str(tmp_path / "out")) is None


def test_convert_legacy_returns_none_for_unknown_ext(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "soffice_path", lambda: "C:/fake/soffice.exe")
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert c.convert_legacy(str(p), str(tmp_path / "out")) is None


def test_ocr_returns_none_when_ocr_unavailable(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "ocr_available", lambda: False)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert c.ocr_pdf(str(p), str(tmp_path / "out")) is None


def test_converters_do_not_raise_on_broken_input(tmp_path, monkeypatch):
    """降级原则：不可用/出错一律返回 None，绝不抛异常打断流水线。"""
    import kbplatform.converters as c
    monkeypatch.setattr(c, "soffice_path", lambda: "C:/fake/soffice.exe")
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    assert c.convert_legacy(str(p), str(tmp_path / "out")) is None
