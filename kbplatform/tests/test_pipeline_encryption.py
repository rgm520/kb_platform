"""流水线（独立进程）必须自己初始化主密钥，否则解不开存储源的共享密码。

回归（2026-10-26）：tmp_ppt 接入后解析一直失败。网页进程初始化的主密钥
（make_app 里 set_encryption_key）不会传给 `python -m kbplatform.pipeline`
这个子进程，于是 `decrypt_secret(secret_enc)` 返回 None → SMB 适配器没有密码
→ 取不到文件 → 报 PackageNotFoundError。

表现上很像"文件坏了"，实际是"流水线拿不到凭据"。
"""
from __future__ import annotations

from kbplatform import auth
from kbplatform.config import load_config
from kbplatform.pipeline import Pipeline


def _cfg(tmp_path, secret="unit-secret"):
    data = str(tmp_path / "data").replace("\\", "/")
    p = tmp_path / "config.toml"
    p.write_text(
        f'[paths]\ndata_dir="{data}"\n'
        f'[security]\nsession_secret = "{secret}"\n'
        '[storage]\nsource_scan_interval_sec=0\n', encoding="utf-8")
    return load_config(str(p))


def test_pipeline_initializes_encryption_key(tmp_path):
    old_secret, old_enabled = auth.get_encryption_key()
    try:
        # 模拟"刚启动、还没设过密钥的流水线进程"
        auth.set_encryption_key(None)
        assert auth.encrypt_secret("pw520") is None

        Pipeline(_cfg(tmp_path))          # 构造时应当自己注入主密钥

        blob = auth.encrypt_secret("pw520")
        assert blob, "流水线没初始化主密钥，共享密码解不开"
        assert auth.decrypt_secret(blob) == "pw520"
    finally:
        auth.set_encryption_key(old_secret, old_enabled)


def test_pipeline_does_not_clear_existing_key_when_config_has_none(tmp_path):
    """配置里没写 session_secret 时，不要把已注入的密钥清掉。"""
    old_secret, old_enabled = auth.get_encryption_key()
    try:
        auth.set_encryption_key("keep-me")
        cfg = _cfg(tmp_path, secret="")
        assert cfg.session_secret == ""
        Pipeline(cfg)
        blob = auth.encrypt_secret("x")
        assert blob, "已有密钥被清掉了"
        assert auth.decrypt_secret(blob) == "x"
    finally:
        auth.set_encryption_key(old_secret, old_enabled)
