"""Read-only VPS deployment preflight; never enables services or collects data."""

import argparse
from datetime import datetime, timedelta
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import shadow_collector as collector
import shadow_schedule as schedule
from shadow_pilot import configuration, verify_private_directory


MIN_PYTHON = (3, 12)
MAX_UNIT_BYTES = 64 * 1024
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.*+!_-]*)$")


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def check_python(version):
    ok = tuple(version[:2]) >= MIN_PYTHON
    return {"وضعیت": "ok" if ok else "خطا",
            "نسخه": ".".join(str(part) for part in version[:3]),
            "حداقل": ".".join(str(part) for part in MIN_PYTHON)}


def check_timezone(now=None):
    try:
        zone = ZoneInfo("Asia/Tehran")
    except ZoneInfoNotFoundError:
        return {"وضعیت": "خطا", "علت": "منطقه زمانی Asia/Tehran در سیستم موجود نیست"}, []
    offset = (now or datetime.now(zone)).astimezone(zone).utcoffset()
    minutes = int(offset.total_seconds() // 60)
    text = f"{minutes // 60:+03d}:{abs(minutes) % 60:02d}"
    check = {"وضعیت": "ok" if offset == TEHRAN_OFFSET else "خطا", "آفست": text}
    warnings = []
    if os.environ.get("TZ") not in ("Asia/Tehran", ":Asia/Tehran"):
        warnings.append("متغیر TZ تنظیم نشده است؛ برنامه صریحاً از ZoneInfo استفاده میکند.")
    return check, warnings


def check_dependencies(path):
    """Compare installed distributions with an offline pinned manifest."""
    if not path.is_file() or path.is_symlink():
        raise ValueError("dependency manifest is missing or unsafe")
    pins = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"invalid dependency pin on line {number}")
        pins.append((match.group(1), match.group(2)))
    if not pins:
        raise ValueError("dependency manifest has no pins")
    installed = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            installed[canonical_name(name)] = distribution.version
    mismatches = [{"بسته": name, "لازم": required,
                   "نصبشده": installed.get(canonical_name(name))}
                  for name, required in pins if installed.get(canonical_name(name)) != required]
    return {"وضعیت": "ok" if not mismatches else "خطا", "فایل": path.name,
            "تعداد": len(pins), "عدم_تطابق": mismatches}


def private_file(path, *, optional=False, mode=None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise ValueError(f"required private file is missing: {path.name}") from None
    permissions = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or permissions & 0o077:
        raise ValueError(f"file must be regular, private and owned by the pilot user: {path.name}")
    if mode is not None and permissions != mode:
        raise ValueError(f"file must have mode {mode:04o}: {path.name}")
    return True


def check_sqlite(path):
    """Validate the configured ledger and any existing SQLite sidecars without opening them."""
    private_file(path, mode=0o600)
    return [suffix for suffix in ("-wal", "-shm", "-journal")
            if private_file(path.with_name(path.name + suffix), optional=True, mode=0o600)]


def check_files(config):
    root, calendar, windows, gap, paper = configuration(config)
    verify_private_directory(root)
    private_file(calendar)
    check = {"وضعیت": "ok", "پنجرهها": len(schedule.intervals(windows)),
             "فاصله_ثانیه": gap, "دفتر_مستقل": paper is not None}
    if paper is not None:
        for name, path in paper.items():
            if name != "db":
                private_file(path)
        check["sqlite"] = {"فایل": paper["db"].name, "فایلهای_جانبی": check_sqlite(paper["db"])}
    return check, (root, calendar, windows)


def check_disk(root, minimum_mb):
    if minimum_mb < 1:
        raise ValueError("minimum free disk space must be positive")
    free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    return {"وضعیت": "ok" if free_mb >= minimum_mb else "خطا",
            "آزاد_مگابایت": free_mb, "حداقل_مگابایت": minimum_mb}


def check_calendar(calendar, windows):
    sessions, digest = schedule.load_calendar(calendar)
    schedule.intervals(windows)
    return {"وضعیت": "ok", "تعداد جلسات": len(sessions), "هش تقویم": digest}


def check_systemd(unit, root):
    if not unit.is_file() or unit.is_symlink():
        raise ValueError("systemd unit file is missing or unsafe")
    raw = unit.read_bytes()
    if len(raw) > MAX_UNIT_BYTES:
        raise ValueError("systemd unit file exceeds size limit")
    directives, section = {}, None
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            key, value = line.split("=", 1)
            directives[(section, key)] = value.strip()
    user = directives.get(("Service", "User"), "")
    working = directives.get(("Service", "WorkingDirectory"), "")
    executable = directives.get(("Service", "ExecStart"), "")
    environment = directives.get(("Service", "Environment"), "").strip('"')
    wanted = directives.get(("Install", "WantedBy"), "")
    if not user or user == "root":
        raise ValueError("systemd unit must use a dedicated non-root user")
    if not working or Path(working).resolve() != root.resolve():
        raise ValueError("systemd WorkingDirectory does not match the pilot root")
    if "--config" not in executable or ".py" not in executable:
        raise ValueError("systemd ExecStart must reference the pilot CLI with --config")
    if environment != "TZ=Asia/Tehran":
        raise ValueError("systemd unit must set TZ=Asia/Tehran")
    if not wanted:
        raise ValueError("systemd unit has no [Install] WantedBy target")
    return {"وضعیت": "ok", "واحد": unit.name, "کاربر": user}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--unit", type=Path, required=True)
    parser.add_argument("--requirements", type=Path,
                        default=Path(__file__).resolve().with_name("requirements-test.txt"))
    parser.add_argument("--min-free-mb", type=int, default=50)
    args = parser.parse_args(argv)
    checks, warnings = {}, []
    try:
        checks["python"] = check_python(sys.version_info)
        checks["tehran_timezone"], extra = check_timezone()
        warnings.extend(extra)
        checks["dependencies"] = check_dependencies(args.requirements)
        checks["files"], (root, calendar, windows) = check_files(args.config)
        checks["disk"] = check_disk(root, args.min_free_mb)
        checks["calendar"] = check_calendar(calendar, windows)
        checks["systemd"] = check_systemd(args.unit, root)
    except (OSError, ValueError, ZoneInfoNotFoundError, UnicodeError) as error:
        checks["خطا"] = {"وضعیت": "خطا", "علت": str(error)}
    ok = all(check.get("وضعیت") == "ok" for check in checks.values())
    result = {"وضعیت": "ok" if ok else "issues", "بررسیها": checks, "هشدارها": warnings}
    print(collector.json_bytes(result).decode(), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
