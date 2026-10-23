import io

import pytest

from kbplatform import auth
from kbplatform import storage
from kbplatform.storage import (Entry, LocalAdapter, Source, adapter_for,
                                is_safe_rel, normalize_rel)
from kbplatform.storage import test_connection as probe_connection


@pytest.fixture(autouse=True)
def _restore_key():
    before = auth.get_encryption_key()
    yield
    auth.set_encryption_key(*before)


def test_secret_roundtrip_and_wrong_key():
    auth.set_encryption_key("unit-test-secret")
    blob = auth.encrypt_secret("p@ss")
    assert blob and "p@ss" not in blob
    assert auth.decrypt_secret(blob) == "p@ss"
    auth.set_encryption_key("other")
    assert auth.decrypt_secret(blob) is None


def test_safe_rel_rejects_escape():
    assert is_safe_rel("a/b") and is_safe_rel("a\\b")
    for bad in ("../x", "a/../../x", "\\\\host\\share", "C:\\x", "a/.."):
        assert not is_safe_rel(bad)
    assert normalize_rel("\\a\\b\\") == "a\\b"
    assert normalize_rel("a//b") == "a\\b"


def test_local_adapter_crud(tmp_path):
    root = tmp_path / "share"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    ad = LocalAdapter(str(root))
    assert [e.name for e in ad.listdir("")] == ["docs"]
    assert ad.listdir("docs")[0].name == "a.txt"
    assert ad.stat("docs/a.txt").size == 5
    assert ad.open_read("docs/a.txt").read() == b"hello"
    ad.mkdirs("new/sub")
    ad.write_stream("new/sub/b.txt", io.BytesIO(b"bbb"), 3)
    ad.rename("new/sub/b.txt", "new/sub/c.txt")
    assert ad.stat("new/sub/c.txt").size == 3
    ad.remove("new")
    assert ad.stat("new") is None


def test_local_adapter_blocks_escape(tmp_path):
    ad = LocalAdapter(str(tmp_path))
    for bad in ("../x", "a/../../x", "C:\\Windows\\win.ini"):
        with pytest.raises(ValueError):
            ad.stat(bad)


def test_test_connection_local(tmp_path):
    (tmp_path / "d").mkdir()
    src = Source(id=1, name="本地", kind="local", root=str(tmp_path), writable=True)
    r = probe_connection(src, None)
    assert r["ok"] is True and r["count"] == 1 and r["detail"] == ""


class _Entry:
    def __init__(self, name, is_dir=False, size=5, mtime=1.0):
        self.name = name
        self._dir = is_dir
        self._size = size
        self._mtime = mtime

    def is_dir(self):
        return self._dir

    def stat(self):
        return type("S", (), {"st_size": self._size, "st_mtime": self._mtime})()


def test_smb_adapters_keep_per_source_credentials(monkeypatch):
    """同一台 NAS 上两个共享用不同账号：各自传自己的凭据，不能串。"""
    calls = []

    def fake_scandir(path, **kw):
        calls.append((path, kw.get("username"), kw.get("password")))
        return iter([_Entry("a.txt")])

    monkeypatch.setattr(storage.smbclient, "scandir", fake_scandir)
    a = Source(id=1, name="A", kind="smb", root=r"\\nas\A", username="u1")
    b = Source(id=2, name="B", kind="smb", root=r"\\nas\B", username="u2")
    ra = adapter_for(a, "p1").listdir("")
    rb = adapter_for(b, "p2").listdir("")
    assert ra[0].rel == "a.txt" and rb[0].rel == "a.txt"
    assert calls == [(r"\\nas\A", "u1", "p1"), (r"\\nas\B", "u2", "p2")]
