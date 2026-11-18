"""飞书机器人配置装载。"""
import pytest

from kbplatform.feishu.config import (DEFAULT_WORKSPACE, load_feishu_config,
                                      parse_model_ref)


def test_parse_model_ref_splits_provider_and_model():
    assert parse_model_ref("opencode/minimax-m3.1-flash-free") == {
        "id": "minimax-m3.1-flash-free",
        "providerID": "opencode",
        "modelID": "minimax-m3.1-flash-free",
    }


def test_parse_model_ref_handles_ollama():
    assert parse_model_ref("ollama/qwen3.5-9b-64k:latest") == {
        "id": "qwen3.5-9b-64k:latest",
        "providerID": "ollama",
        "modelID": "qwen3.5-9b-64k:latest",
    }


def test_load_returns_none_when_disabled(tmp_path):
    (tmp_path / "config.toml").write_text("[feishu]\nenabled = false\n", encoding="utf-8")
    assert load_feishu_config(str(tmp_path / "config.toml")) is None


def test_load_returns_none_without_credentials(tmp_path):
    (tmp_path / "config.toml").write_text("[feishu]\nenabled = true\n", encoding="utf-8")
    assert load_feishu_config(str(tmp_path / "config.toml")) is None


def test_load_reads_secret_from_secrets_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    (tmp_path / "config.toml").write_text(
        '[feishu]\nenabled = true\nworkspace = "D:/kb-platform"\n', encoding="utf-8")
    (tmp_path / "secrets.toml").write_text(
        '[feishu]\napp_id = "cli_x"\napp_secret = "s3cr3t"\n', encoding="utf-8")
    cfg = load_feishu_config(str(tmp_path / "config.toml"),
                             str(tmp_path / "secrets.toml"))
    assert cfg is not None
    assert cfg.app_id == "cli_x" and cfg.app_secret == "s3cr3t"
    assert cfg.workspace == "D:/kb-platform"


def test_env_vars_take_precedence_over_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
    (tmp_path / "config.toml").write_text("[feishu]\nenabled = true\n", encoding="utf-8")
    (tmp_path / "secrets.toml").write_text(
        '[feishu]\napp_id = "cli_file"\napp_secret = "file_secret"\n', encoding="utf-8")
    cfg = load_feishu_config(str(tmp_path / "config.toml"),
                             str(tmp_path / "secrets.toml"))
    assert cfg.app_id == "cli_env" and cfg.app_secret == "env_secret"


def test_defaults_match_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    (tmp_path / "config.toml").write_text("[feishu]\nenabled = true\n", encoding="utf-8")
    cfg = load_feishu_config(str(tmp_path / "config.toml"))
    assert cfg.workspace == DEFAULT_WORKSPACE
    assert cfg.opencode_port == 4599
    assert cfg.cloud_model == "opencode/minimax-m3.1-flash-free"
    assert cfg.local_model == "ollama/qwen3.5-9b-64k:latest"
    assert cfg.kb_api_base == "http://127.0.0.1:8000"
    assert cfg.max_concurrent_cloud == 3
    assert cfg.max_concurrent_local == 1
    assert cfg.reply_chunk == 3000
