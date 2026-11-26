"""Checks for the CI test-inventory comparison helper."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compare_inventories import compare, main


def write_approvals(path, *, removals=(), exemptions=()):
    path.write_text(json.dumps({
        "version": 1,
        "removals": [{"nodeid": item, "reason": "documented cleanup",
                      "approved_by": "project owner", "date": "2026-11-26"} for item in removals],
        "baseline_exemptions": [{"run": item, "reason": "baseline artifact unavailable",
                                 "approved_by": "project owner", "date": "2026-11-26"}
                                for item in exemptions],
    }), encoding="utf-8")
    return path


class InventoryComparisonTests(unittest.TestCase):
    def test_added_removed_unchanged_and_duplicates(self):
        self.assertEqual(compare(["a", "b", "c"], ["a", "c", "d"]), (["d"], ["b"]))
        self.assertEqual(compare(["a", "a", "b"], ["b", "a"]), ([], []))
        self.assertEqual(compare([], ["z"]), (["z"], []))

    def test_cli_reports_both_directions_and_fails_on_unapproved_removal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, current = root / "baseline.txt", root / "current.txt"
            baseline.write_text("old.py::kept\nold.py::gone\n", encoding="utf-8")
            current.write_text("old.py::kept\nnew.py::added\n", encoding="utf-8")
            approvals = write_approvals(root / "approvals.json")
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(baseline), str(current), "--label", "vs run 42",
                             "--approvals", str(approvals)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue().splitlines(), [
            "Test inventory vs run 42: 1 added, 1 removed (0 approved, 1 unapproved).",
            "Added: new.py::added",
            "Removed (UNAPPROVED): old.py::gone",
            "ERROR: 1 test(s) removed without documented approval.",
        ])

    def test_approved_removal_passes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, current = root / "baseline.txt", root / "current.txt"
            baseline.write_text("old.py::kept\nold.py::gone\n", encoding="utf-8")
            current.write_text("old.py::kept\nnew.py::added\n", encoding="utf-8")
            approvals = write_approvals(root / "approvals.json", removals=["old.py::gone"])
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(baseline), str(current), "--approvals", str(approvals)])
        self.assertEqual(code, 0)
        self.assertIn("Removed (approved): old.py::gone", output.getvalue().splitlines())

    def test_missing_baseline_needs_documented_exemption(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current.txt"
            current.write_text("tests.py::ok\n", encoding="utf-8")
            missing = root / "missing.txt"
            plain = write_approvals(root / "plain.json")
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(missing), str(current), "--approvals", str(plain),
                             "--baseline-run", "42"])
            self.assertEqual(code, 2)
            self.assertIn("missing without documented approval", output.getvalue())
            exempted = write_approvals(root / "exempted.json", exemptions=["42"])
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(missing), str(current), "--approvals", str(exempted),
                             "--baseline-run", "42"])
        self.assertEqual(code, 0)
        self.assertIn("documented exemption applied", output.getvalue())

    def test_invalid_approval_file_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, current = root / "baseline.txt", root / "current.txt"
            baseline.write_text("old.py::kept\n", encoding="utf-8")
            current.write_text("old.py::kept\n", encoding="utf-8")
            approvals = root / "approvals.json"
            approvals.write_text('{"version": 2}', encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(baseline), str(current), "--approvals", str(approvals)])
        self.assertEqual(code, 2)
        self.assertIn("invalid test-removal approvals", output.getvalue())


if __name__ == "__main__":
    unittest.main()
