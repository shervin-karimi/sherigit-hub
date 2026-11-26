"""Offline checks for the read-only VPS deployment preflight tool."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from importlib import metadata
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import shadow_pilot as pilot
import vps_preflight as preflight


PINNED_PACKAGES = ("pytest", "iniconfig", "packaging", "pluggy", "Pygments", "tzdata")


class VPSPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self):
        root = self.root / "pilot"
        pilot.private_directory(root)
        calendar = self.root / "calendar.json"
        pilot.atomic_json(calendar, {"source": "fixture",
                                     "sessions": {"2026-11-22": ["09:00-12:30"]}})
        config = self.root / "pilot.json"
        pilot.atomic_json(config, {"root": str(root), "calendar": str(calendar),
                                   "windows": ["09:00-12:30"], "capture_interval_seconds": 120})
        unit = self.root / "pilot.service"
        unit.write_text(
            "[Unit]\nDescription=Synthetic\n\n"
            "[Service]\nType=oneshot\nUser=pilot\n"
            f"WorkingDirectory={root}\n"
            f"ExecStart=/usr/bin/python3 -B shadow_pilot.py tick --config {config}\n"
            "Environment=TZ=Asia/Tehran\n\n"
            "[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
        requirements = self.root / "requirements-test.txt"
        lines = [f"{preflight.canonical_name(name)}=={metadata.version(name)}\n"
                 for name in PINNED_PACKAGES]
        requirements.write_text("".join(lines), encoding="utf-8")
        return config, unit, root, requirements

    def run_preflight(self, config, unit, requirements, *extra):
        out = StringIO()
        with redirect_stdout(out):
            code = preflight.main(["--config", str(config), "--unit", str(unit),
                                   "--requirements", str(requirements), *extra])
        return code, json.loads(out.getvalue())

    def test_successful_preflight_is_read_only(self):
        config, unit, root, requirements = self.fixture()
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with patch("os.system", side_effect=AssertionError("external command")), \
                patch("subprocess.run", side_effect=AssertionError("external command")):
            code, result = self.run_preflight(config, unit, requirements)
        self.assertEqual(code, 0)
        self.assertEqual(result["وضعیت"], "ok")
        self.assertEqual(set(result["بررسیها"]), {
            "python", "tehran_timezone", "dependencies", "files", "disk", "calendar", "systemd"})
        self.assertEqual(result["بررسیها"]["dependencies"]["تعداد"], len(PINNED_PACKAGES))
        self.assertEqual(result["بررسیها"]["dependencies"]["عدم_تطابق"], [])
        self.assertEqual(result["بررسیها"]["calendar"]["تعداد جلسات"], 1)
        self.assertEqual(result["بررسیها"]["systemd"]["واحد"], "pilot.service")
        after = {path.relative_to(self.root): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_helpers_report_timezone_and_python_floor(self):
        check, warnings = preflight.check_timezone(datetime(2026, 11, 26, 6, tzinfo=timezone.utc))
        self.assertEqual(check, {"وضعیت": "ok", "آفست": "+03:30"})
        self.assertIsInstance(warnings, list)
        self.assertEqual(preflight.check_python((3, 11))["وضعیت"], "خطا")
        self.assertEqual(preflight.check_python((3, 13))["وضعیت"], "ok")

    def test_dependency_mismatch_fails(self):
        config, unit, _, requirements = self.fixture()
        requirements.write_text("pytest==0.0.0\n", encoding="utf-8")
        code, result = self.run_preflight(config, unit, requirements)
        self.assertEqual(code, 1)
        self.assertEqual(result["بررسیها"]["dependencies"]["وضعیت"], "خطا")
        self.assertEqual(result["بررسیها"]["dependencies"]["عدم_تطابق"],
                         [{"بسته": "pytest", "لازم": "0.0.0",
                           "نصبشده": metadata.version("pytest")}])

    def test_invalid_dependency_manifest_fails_closed(self):
        config, unit, _, requirements = self.fixture()
        requirements.write_text("pytest>=9\n", encoding="utf-8")
        code, result = self.run_preflight(config, unit, requirements)
        self.assertEqual(code, 1)
        self.assertIn("invalid dependency pin", result["بررسیها"]["خطا"]["علت"])

    def test_bad_systemd_unit_fails(self):
        config, unit, _, requirements = self.fixture()
        unit.write_text(unit.read_text(encoding="utf-8").replace("[Install]\n", ""), encoding="utf-8")
        code, result = self.run_preflight(config, unit, requirements)
        self.assertEqual(code, 1)
        self.assertEqual(result["وضعیت"], "issues")
        self.assertIn("WantedBy", result["بررسیها"]["خطا"]["علت"])

    def test_low_disk_space_fails(self):
        config, unit, _, requirements = self.fixture()
        code, result = self.run_preflight(config, unit, requirements,
                                          "--min-free-mb", str(10 ** 12))
        self.assertEqual(code, 1)
        self.assertEqual(result["بررسیها"]["disk"]["وضعیت"], "خطا")

    def test_invalid_calendar_fails(self):
        config, unit, _, requirements = self.fixture()
        data = json.loads(config.read_text(encoding="utf-8"))
        pilot.atomic_json(Path(data["calendar"]), {"source": "fixture", "sessions": {}})
        code, result = self.run_preflight(config, unit, requirements)
        self.assertEqual(code, 1)
        self.assertEqual(result["وضعیت"], "issues")


if __name__ == "__main__":
    unittest.main()
