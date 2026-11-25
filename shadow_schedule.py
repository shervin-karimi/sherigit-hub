"""Optional one-shot Tehran-time gate for the public Shadow Watch collector."""

import argparse
from datetime import date, time
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import shadow_collector as collector
import shadow_watch as watch


def interval(value):
    pattern = r"([01][0-9]|2[0-3]):[0-5][0-9]"
    watch.require(isinstance(value, str) and re.fullmatch(f"{pattern}-{pattern}", value),
                  "interval must be HH:MM-HH:MM in Tehran time")
    start, end = map(time.fromisoformat, value.split("-"))
    watch.require(start < end, "interval must be increasing within one Tehran date")
    return start, end


def intervals(values):
    watch.require(isinstance(values, list) and values, "at least one interval is required")
    result = sorted(map(interval, values))
    watch.require(all(left[1] <= right[0] for left, right in zip(result, result[1:])),
                  "intervals must not overlap")
    return result


def load_calendar(path):
    data, raw = watch.read_object(path, watch.MAX_META)
    source, sessions = data.get("source"), data.get("sessions")
    watch.require(isinstance(source, str) and source.strip()
                  and isinstance(sessions, dict) and sessions,
                  "schedule calendar requires source and non-empty dated sessions")
    hours = {}
    for day, values in sessions.items():
        watch.require(date.fromisoformat(day).isoformat() == day,
                      "session date must be canonical YYYY-MM-DD")
        hours[day] = intervals(values)
    return hours, watch.digest(raw)


def containing(spans, at):
    return next((span for span in spans if span[0] <= at < span[1]), None)


def schedule_slot(sessions, windows, at):
    local = at.astimezone(ZoneInfo("Asia/Tehran"))
    day = local.date().isoformat()
    if day not in sessions:
        return None, "no_listed_session"
    session = containing(sessions[day], local.time())
    if session is None:
        return None, "outside_session_hours"
    window = containing(windows, local.time())
    if window is None:
        return None, "outside_collection_window"
    return (day, session, window), None


def scheduled_capture(calendar, windows, *, dry_run=False, **network):
    sessions, digest = load_calendar(calendar)
    windows = intervals(windows)
    try:
        ZoneInfo("Asia/Tehran")
    except ZoneInfoNotFoundError as error:
        raise watch.ArchiveError("Asia/Tehran timezone data is required") from error

    def active(at):
        slot, reason = schedule_slot(sessions, windows, at)
        fields = {"at_tehran": at.astimezone(ZoneInfo("Asia/Tehran")).isoformat(),
                  "calendar_sha256": digest}
        if reason is not None:
            raise collector.CaptureSkipped(reason, **fields)
        return slot, fields

    initial = collector.checked_clock(collector.utc_now())
    slot, fields = active(initial)
    if dry_run:
        raise collector.CaptureSkipped("dry_run", **fields)

    def gate(at):
        collector.checked_clock(at, initial)
        current, fields = active(at)
        if current != slot:
            raise collector.CaptureSkipped("collection_slot_changed", **fields)

    return collector.collect(gate=gate, **network)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", required=True, type=Path,
                        help="operator-supplied JSON calendar with dated session hours")
    parser.add_argument("--window", required=True, action="append", metavar="HH:MM-HH:MM",
                        help="Tehran collection window, start inclusive and end exclusive; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="check the gate without any HTTP request")
    collector.add_network_options(parser)
    args = parser.parse_args(argv)
    return collector.run_capture(lambda: scheduled_capture(
        args.calendar, args.window, dry_run=args.dry_run, timeout=args.timeout, backoff=args.backoff))


if __name__ == "__main__":
    raise SystemExit(main())
