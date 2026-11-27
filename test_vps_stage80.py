import json

import pytest

from vps_stage80 import backup_and_drill, digest, snapshot


@pytest.fixture
def paths(tmp_path):
    config, root, backup = (tmp_path / name for name in ("config", "root", "backup"))
    for path in (config, root, backup):
        path.mkdir(mode=0o700)
    payload = {"root": str(root), "calendar": str(config / "sessions.json"),
               "windows": ["09:00-12:30"], "capture_interval_seconds": 120}
    (config / "config.json").write_text(json.dumps(payload))
    (config / "shadow-pilot.service").write_text("fixture only; never installed\n")
    for path in config.iterdir():
        path.chmod(0o600)
    return config, root, backup


def test_backup_missing_calendar_is_explicit(paths):
    config, root, destination = paths
    before = snapshot(config, root)
    result = backup_and_drill(*paths)
    assert result["status"] == result["restore_drill"] == "ok"
    assert result["calendar_present"] is False
    stored = destination / result["backup"]
    assert list(destination.iterdir()) == [stored]
    manifest = json.loads((stored / "manifest.json").read_text())
    assert manifest["sha256"] == {name: digest(raw) for name, raw in before.items()}
    assert snapshot(config, root) == before
    assert not (stored / "sessions.json").exists()
    assert not any((stored / "observation-root").iterdir())
    assert stored.stat().st_mode & 0o777 == 0o700
    assert (stored / "config.json").stat().st_mode & 0o777 == 0o600


def test_backup_calendar_bytes_not_certified(paths):
    config, _, destination = paths
    calendar = config / "sessions.json"
    calendar.write_text('{"source":"synthetic test fixture","sessions":{}}')
    calendar.chmod(0o600)
    result = backup_and_drill(*paths)
    assert result["calendar_present"] is True
    assert (destination / result["backup"] / calendar.name).read_bytes() == calendar.read_bytes()


@pytest.mark.parametrize("kind", ["symlink", "permissions", "nonempty", "paper", "fifo", "bad_path", "nested_destination"])
def test_refuses_unsafe_inputs(paths, kind):
    config, root, destination = paths
    target = config / "shadow-pilot.service"
    if kind == "symlink":
        target.unlink()
        target.symlink_to(config / "config.json")
    elif kind == "permissions":
        target.chmod(0o644)
    elif kind == "nonempty":
        (root / "existing-data").write_text("keep")
    elif kind == "paper":
        path = config / "config.json"
        content = json.loads(path.read_text())
        content["paper"] = {}
        path.write_text(json.dumps(content))
    elif kind == "fifo":
        import os
        target.unlink()
        os.mkfifo(target, 0o600)
    elif kind == "bad_path":
        path = config / "config.json"
        content = json.loads(path.read_text())
        content["calendar"] = "/not/the/calendar"
        path.write_text(json.dumps(content))
    else:
        destination = root
    with pytest.raises(ValueError):
        backup_and_drill(config, root, destination)
    assert not list(paths[2].iterdir())


def test_failed_copy_does_not_leave_successful_backup(paths, monkeypatch):
    import vps_stage80
    original = vps_stage80.write_private

    def corrupt(path, raw):
        original(path, raw + b"corrupted" if path.name == "config.json" else raw)

    monkeypatch.setattr(vps_stage80, "write_private", corrupt)
    before = snapshot(*paths[:2])
    with pytest.raises(ValueError, match="digest mismatch"):
        backup_and_drill(*paths)
    assert not list(paths[2].iterdir())
    assert snapshot(*paths[:2]) == before
