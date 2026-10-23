from kbplatform.config import load_config


def test_load_config_reads_monitor_roots_and_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[monitor]\n'
        'roots = ["\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库"]\n'
        'poll_interval_sec = 30\n'
        'scan_qps = 20\n\n'
        '[paths]\n'
        'data_dir = "D:/kb-platform/kb/data"\n\n'
        '[drives]\n'
        '"W:" = "\\\\\\\\192.0.2.19\\\\信息化管理"\n',
        encoding="utf-8")
    c = load_config(str(cfg))
    assert c.monitor_roots == ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert c.poll_interval_sec == 30
    assert c.scan_qps == 20
    assert c.data_dir == "D:/kb-platform/kb/data"
    assert c.drive_map == {"W:": "\\\\192.0.2.19\\信息化管理"}
    assert c.db_path.endswith("kb.db")
    assert c.fts_path.endswith("fts.db")
    assert c.vector_dir.endswith("vectors")


def test_load_config_applies_defaults(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[monitor]\nroots = []\n', encoding="utf-8")
    c = load_config(str(cfg))
    assert c.poll_interval_sec == 60
    assert c.max_workers == 1
    assert c.llm_model == "qwen3.5-9b-64k"
    assert c.embed_model == "bge-m3"


def test_real_project_config_loads():
    c = load_config("D:/kb-platform/config.toml")
    assert c.monitor_roots == ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert "W:" in c.drive_map
    assert c.drive_map["W:"].endswith("信息化管理")


def test_storage_config_defaults_and_toml(tmp_path):
    from kbplatform.config import Config
    c = Config()
    assert c.trash_days == 30
    assert c.upload_max_mb == 2048
    assert c.source_scan_interval_sec == 300
    assert c.preview_dir.endswith("preview")
    p = tmp_path / "storage.toml"
    p.write_text('[paths]\ndata_dir="X:/d"\n[storage]\ntrash_days=7\n'
                 'upload_max_mb=100\nsource_scan_interval_sec=0\n', encoding="utf-8")
    c2 = load_config(str(p))
    assert (c2.trash_days, c2.upload_max_mb, c2.source_scan_interval_sec) == (7, 100, 0)
    assert c2.preview_dir.replace("\\", "/") == "X:/d/preview"
