"""Compare sorted pytest node IDs and enforce documented test removals."""

import argparse
import json
from pathlib import Path


APPROVALS = Path(__file__).resolve().with_name("test-inventory-removals.json")


def read_ids(path):
    return path.read_text(encoding="utf-8").splitlines()


def compare(baseline, current):
    """Return sorted IDs added now and sorted IDs removed from the baseline."""
    return sorted(set(current) - set(baseline)), sorted(set(baseline) - set(current))


def load_approvals(path):
    """Return approved removed node IDs and baseline runs exempted from comparison."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("approval file must be version 1")
    removals, exemptions = data.get("removals"), data.get("baseline_exemptions")
    if not isinstance(removals, list) or not isinstance(exemptions, list):
        raise ValueError("approvals require removals and baseline_exemptions lists")
    for collection, key, fields in (
        (removals, "nodeid", ("nodeid", "reason", "approved_by", "date")),
        (exemptions, "run", ("run", "reason", "approved_by", "date")),
    ):
        for item in collection:
            if not isinstance(item, dict) or any(
                    not isinstance(item.get(field), str) or not item[field].strip()
                    for field in fields):
                raise ValueError(f"each approval requires {key} plus reason, approved_by and date")
    node_ids = [item["nodeid"] for item in removals]
    runs = [item["run"] for item in exemptions]
    if len(node_ids) != len(set(node_ids)) or len(runs) != len(set(runs)):
        raise ValueError("duplicate approval entries")
    return set(node_ids), set(runs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--label", default="", help="suffix describing the comparison")
    parser.add_argument("--approvals", type=Path, default=APPROVALS)
    parser.add_argument("--baseline-run", default="",
                        help="run ID used when reporting a missing baseline")
    args = parser.parse_args(argv)
    try:
        approved, exempted_runs = load_approvals(args.approvals)
    except (OSError, ValueError) as error:
        print(f"ERROR: invalid test-removal approvals: {error}")
        return 2
    label = f" {args.label}" if args.label else ""
    run = args.baseline_run or "none"
    if not args.baseline.is_file():
        if run in exempted_runs:
            print(f"Test inventory comparison: baseline run {run} unavailable; documented exemption applied.")
            print("No removal check was possible. Restore the artifact or extend the approval before the next run.")
            return 0
        print(f"ERROR: baseline inventory for run {run} is missing without documented approval.")
        print("Add a baseline_exemptions entry to test-inventory-removals.json with a reason and approval.")
        return 2
    added, removed = compare(read_ids(args.baseline), read_ids(args.current))
    approved_here = [node_id for node_id in removed if node_id in approved]
    unapproved = [node_id for node_id in removed if node_id not in approved]
    print(f"Test inventory{label}: {len(added)} added, {len(removed)} removed "
          f"({len(approved_here)} approved, {len(unapproved)} unapproved).")
    for node_id in added:
        print(f"Added: {node_id}")
    for node_id in approved_here:
        print(f"Removed (approved): {node_id}")
    for node_id in unapproved:
        print(f"Removed (UNAPPROVED): {node_id}")
    if unapproved:
        print(f"ERROR: {len(unapproved)} test(s) removed without documented approval.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
