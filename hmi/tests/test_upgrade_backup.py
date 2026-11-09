"""All fixtures live in pytest temporary directories; no device/system installs."""
import sqlite3
import pytest
from factory_hmi import upgrade_backup as backup
from test_software_installer import transaction
from test_software_updater import releases


def test_offline_snapshot_preserves_config_records_and_wal(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "run").mkdir()
    (state / "run/launcher.lock").write_text("never restore locks")
    (state / "config.yaml").write_text("station: one")
    database = sqlite3.connect(state / "queue.db")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE queue (id INTEGER)")
    database.execute("INSERT INTO queue VALUES (1)")
    database.commit()
    snapshot = tmp_path / "backup"
    result = backup.create_snapshot(state, snapshot)
    assert result["verified"] and not (snapshot / "state/run").exists()
    restored = backup.export_snapshot(snapshot, tmp_path / "export")
    with sqlite3.connect(restored / "queue.db") as db:
        assert db.execute("SELECT id FROM queue").fetchall() == [(1,)]
    database.execute("INSERT INTO queue VALUES (2)")
    database.commit()
    (state / "config.yaml").write_text("station: changed")
    with pytest.raises(FileExistsError):
        backup.export_snapshot(snapshot, state)
    assert database.execute("SELECT COUNT(*) FROM queue").fetchone() == (2,)
    assert (state / "config.yaml").read_text() == "station: changed"
    database.close()


def test_corruption_missing_and_extra_files_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "a").write_text("original")
    snapshot = tmp_path / "backup"
    backup.create_snapshot(state, snapshot)
    (snapshot / "state/a").write_text("changed")
    with pytest.raises(ValueError):
        backup.export_snapshot(snapshot, tmp_path / "export")
    assert not (tmp_path / "export").exists()
    (snapshot / "state/a").unlink()
    with pytest.raises(ValueError):
        backup.verify_snapshot(snapshot)


def test_symlinks_nested_backup_and_timeout_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "a").write_text("data")
    with pytest.raises(ValueError):
        backup.create_snapshot(state, state / "backup")
    with pytest.raises(TimeoutError):
        backup.create_snapshot(state, tmp_path / "timeout", timeout=-1)
    (state / "outside").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        backup.create_snapshot(state, tmp_path / "links")


def test_backup_failure_prevents_install(transaction, monkeypatch):
    from factory_hmi import installer
    monkeypatch.setattr(backup, "create_snapshot", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    arguments, _ = transaction
    result = installer.execute_install_transaction(**arguments)
    assert result["status"] == "failed"
    assert result["failed_phase"] == "backing_up_state"
    assert "install" not in arguments["executor"].calls
