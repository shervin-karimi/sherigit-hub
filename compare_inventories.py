"""Compare sorted pytest node IDs collected now with a saved baseline."""

import argparse
from pathlib import Path


def read_ids(path):
    return path.read_text(encoding="utf-8").splitlines()


def compare(baseline, current):
    """Return sorted IDs added now and sorted IDs removed from the baseline."""
    return sorted(set(current) - set(baseline)), sorted(set(baseline) - set(current))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--label", default="", help="suffix describing the comparison")
    args = parser.parse_args(argv)
    added, removed = compare(read_ids(args.baseline), read_ids(args.current))
    label = f" {args.label}" if args.label else ""
    print(f"Test inventory{label}: {len(added)} added, {len(removed)} removed.")
    for node_id in added:
        print(f"Added: {node_id}")
    for node_id in removed:
        print(f"Removed: {node_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
