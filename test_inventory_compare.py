"""Checks for the CI test-inventory comparison helper."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compare_inventories import compare, main


class InventoryComparisonTests(unittest.TestCase):
    def test_added_removed_unchanged_and_duplicates(self):
        self.assertEqual(compare(["a", "b", "c"], ["a", "c", "d"]), (["d"], ["b"]))
        self.assertEqual(compare(["a", "a", "b"], ["b", "a"]), ([], []))
        self.assertEqual(compare([], ["z"]), (["z"], []))

    def test_cli_reports_both_directions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, current = root / "baseline.txt", root / "current.txt"
            baseline.write_text("old.py::kept\nold.py::gone\n", encoding="utf-8")
            current.write_text("old.py::kept\nnew.py::added\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = main([str(baseline), str(current), "--label", "vs run 42"])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "Test inventory vs run 42: 1 added, 1 removed.",
            "Added: new.py::added",
            "Removed: old.py::gone",
        ])


if __name__ == "__main__":
    unittest.main()
