"""Read-only VPS deployment preflight; never enables services or collects data."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import shadow_collector as collector
import shadow_schedule as schedule
from shadow_pilot import configuration, verify_private_directory


MIN_PYTHON = (3, 12)
MAX_UNIT_BYTES = 64 * 1024
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)


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


def private_file(path):
    info = path.lstat()
    if (not path.is_file() or path.is_symlink() or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f"file must be private and owned by the pilot user: {path.name}")


def check_files(config):
    root, calendar, windows, gap, paper = configuration(config)
    verify_private_directory(root)
    private_file(calendar)
    for path in (paper or {}).values():
        private_file(path)
    check = {"وضعیت": "ok", "پنجرهها": len(schedule.intervals(windows)),
             "فاصله_ثانیه": gap, "دفتر_مستقل": paper is not None}
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
    parser.add_argument("--min-free-mb", type=int, default=50)
    args = parser.parse_args(argv)
    checks, warnings = {}, []
    try:
        checks["python"] = check_python(sys.version_info)
        checks["tehran_timezone"], extra = check_timezone()
        warnings.extend(extra)
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
