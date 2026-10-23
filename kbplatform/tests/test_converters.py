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


def test_is_ocr_backlog_uses_mtime(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "OCR_BACKLOG_DAYS", 7.0)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    st = p.stat().st_mtime
    assert c.is_ocr_backlog(str(p), now=st + 8 * 86400) is True
    assert c.is_ocr_backlog(str(p), now=st + 1 * 86400) is False


def test_is_ocr_backlog_disabled_when_days_le_zero(tmp_path, monkeypatch):
    import kbplatform.converters as c
    monkeypatch.setattr(c, "OCR_BACKLOG_DAYS", 0.0)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert c.is_ocr_backlog(str(p), now=p.stat().st_mtime + 999 * 86400) is False


def test_converters_do_not_raise_on_broken_input(tmp_path, monkeypatch):
    """降级原则：不可用/出错一律返回 None，绝不抛异常打断流水线。"""
    import kbplatform.converters as c
    monkeypatch.setattr(c, "soffice_path", lambda: "C:/fake/soffice.exe")
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    assert c.convert_legacy(str(p), str(tmp_path / "out")) is None


def test_convert_to_pdf_returns_none_when_unavailable(tmp_path, monkeypatch):
    from kbplatform import converters
    monkeypatch.setattr(converters, "soffice_path", lambda: None)
    assert converters.convert_to_pdf(str(tmp_path / "a.docx"), str(tmp_path)) is None
