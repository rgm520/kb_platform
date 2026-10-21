from kbplatform.config import load_config


def test_load_config_reads_monitor_roots_and_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[monitor]\n'
        'roots = ["\\\\\\\\192.0.2.19\\\\信息化管理\\\\AI知识库"]\n'
        'poll_interval_sec = 30\n'
        'scan_qps = 20\n\n'
        '[paths]\n'
        'data_dir = "D:/opencode/kb/data"\n\n'
        '[drives]\n'
        '"W:" = "\\\\\\\\192.0.2.19\\\\信息化管理"\n',
        encoding="utf-8")
    c = load_config(str(cfg))
    assert c.monitor_roots == ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert c.poll_interval_sec == 30
    assert c.scan_qps == 20
    assert c.data_dir == "D:/opencode/kb/data"
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
    c = load_config("D:/opencode/config.toml")
    assert c.monitor_roots == ["\\\\192.0.2.19\\信息化管理\\AI知识库"]
    assert "W:" in c.drive_map
    assert c.drive_map["W:"].endswith("信息化管理")
