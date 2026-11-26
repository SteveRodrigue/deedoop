"""The rename from deedup to deedoop keeps existing installs working."""

from deedoop import config


def test_old_variable_names_still_work(monkeypatch, tmp_path, caplog):
    for k in ("DEEDOOP_CONFIG", "DEEDOOP_HTTPS", "DEEDOOP_ADMIN_PASSWORD", "DEEDUP_CONFIG", "DEEDUP_HTTPS",
              "DEEDUP_ADMIN_PASSWORD", "DEEDOOP_PASSWORD", "DEEDUP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEEDUP_CONFIG", str(tmp_path))
    monkeypatch.setenv("DEEDUP_HTTPS", "on")
    monkeypatch.setenv("DEEDUP_ADMIN_PASSWORD", "a-long-secret-value")
    cfg = config.load()
    assert cfg.config_dir == tmp_path and cfg.https and cfg.admin_password == "a-long-secret-value"
    assert "DEEDUP_HTTPS is the old name of DEEDOOP_HTTPS" in caplog.text
    assert "a-long-secret-value" not in caplog.text  # SC7: names only, never values


def test_new_variable_names_win(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEDUP_HTTPS", "on")
    monkeypatch.setenv("DEEDOOP_HTTPS", "off")
    monkeypatch.setenv("DEEDOOP_CONFIG", str(tmp_path))
    assert config.load().https is False


def test_existing_index_keeps_its_file_name(tmp_path):
    cfg = config.AppConfig(config_dir=tmp_path, mnt=tmp_path)
    assert cfg.db_path.name == "deedoop.db"
    (tmp_path / "deedup.db").write_bytes(b"")
    assert cfg.db_path.name == "deedup.db"
    (tmp_path / "deedoop.db").write_bytes(b"")
    assert cfg.db_path.name == "deedoop.db"
