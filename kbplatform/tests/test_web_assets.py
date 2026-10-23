from pathlib import Path

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
