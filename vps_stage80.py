"""Private, offline configuration backup and isolated restore drill for a new pilot.

Refuses nonempty observation roots and paper configurations. This is not a
backup tool for a running pilot or a SQLite ledger.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

LIMIT = 1024 * 1024
REQUIRED = {"root", "calendar", "windows", "capture_interval_seconds"}


def private_dir(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or path.resolve() != path):
        raise ValueError("directory must be an absolute, private, owned, non-symlink directory")


def read_private(path):
    # Reject FIFOs and symlinks before opening; verify the opened descriptor too.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path.name}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid()
                or stat.S_IMODE(actual.st_mode) != 0o600
                or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError(f"unsafe private file: {path.name}")
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError(f"file too large: {path.name}")
    return raw


def write_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(config_dir, root):
    private_dir(config_dir)
    private_dir(root)
    if root == config_dir or root.is_relative_to(config_dir) or config_dir.is_relative_to(root):
        raise ValueError("configuration and observation root must be separate")
    if any(root.iterdir()):
        raise ValueError("observation root is not empty; use the coordinated live-data backup procedure")
    files = {name: read_private(config_dir / name) for name in ("config.json", "shadow-pilot.service")}
    config = json.loads(files["config.json"])
    if not isinstance(config, dict) or set(config) != REQUIRED:
        raise ValueError("only the observation-only configuration is supported")
    calendar = config_dir / "sessions.json"
    if config["root"] != str(root) or config["calendar"] != str(calendar):
        raise ValueError("configuration paths do not match the supplied private directories")
    try:
        files[calendar.name] = read_private(calendar)
    except FileNotFoundError:
        if os.path.lexists(calendar):
            raise
    if any(root.iterdir()):
        raise ValueError("observation root changed during backup")
    return files


def backup_and_drill(config_dir, root, destination):
    for path in (config_dir, root, destination):
        private_dir(path)
    if any(destination == path or destination.is_relative_to(path) or path.is_relative_to(destination)
           for path in (config_dir, root)):
        raise ValueError("backup destination must be separate from live directories")
    files = snapshot(config_dir, root)
    backup = Path(tempfile.mkdtemp(prefix="stage80-", dir=destination))
    try:
        manifest = {"format": 1, "scope": "initial-observation-only-empty-root",
                    "calendar_present": "sessions.json" in files,
                    "sha256": {name: digest(raw) for name, raw in sorted(files.items())}}
        for name, raw in files.items():
            write_private(backup / name, raw)
        (backup / "observation-root").mkdir(mode=0o700)
        manifest_raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        write_private(backup / "manifest.json", manifest_raw)
        with tempfile.TemporaryDirectory(prefix="restore-drill-", dir=destination) as directory:
            restored = Path(directory)
            private_dir(restored)
            (restored / "observation-root").mkdir(mode=0o700)
            for name, expected in manifest["sha256"].items():
                raw = read_private(backup / name)
                if digest(raw) != expected:
                    raise ValueError("backup digest mismatch")
                write_private(restored / name, raw)
                if digest(read_private(restored / name)) != expected:
                    raise ValueError("restore digest mismatch")
            write_private(restored / "manifest.json", read_private(backup / "manifest.json"))
            if read_private(restored / "manifest.json") != manifest_raw:
                raise ValueError("manifest mismatch")
            private_dir(restored / "observation-root")
            if any(restored.joinpath("observation-root").iterdir()):
                raise ValueError("restored observation root is not empty")
        if snapshot(config_dir, root) != files:
            raise ValueError("live configuration changed during backup")
        return {"status": "ok", "backup": backup.name, "files": len(files),
                "calendar_present": manifest["calendar_present"], "restore_drill": "ok",
                "scope": manifest["scope"]}
    except BaseException:
        shutil.rmtree(backup)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = backup_and_drill(args.config_dir, args.root, args.destination)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "issues", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
