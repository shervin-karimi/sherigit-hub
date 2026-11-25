"""Single-tick live SHADOW pilot and read-only operational status."""

import argparse
from collections import Counter
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfoNotFoundError

from paper_limits import BotPolicy
from paper_monitor import PaperMonitor
from paper_quotes import TEHRAN, load_catalog, positive_int, timestamp
from paper_replay import evidence_coverage, screen_candidate
from paper_trading import PaperTrading, incident
import shadow_collector as collector
from shadow_pilot_evidence import private_json, signed_batch
import shadow_schedule as schedule
import shadow_watch as watch


PAPER_FIELDS = {"db", "catalog", "policy", "feed", "key"}
QUOTE_GAPS = [
    "مظنهٔ دارای زمان واقعی بورس و زمان دریافت مستقل",
    "هویت، ضریب و آخرین مهلت معاملهٔ تأییدشدهٔ همان قرارداد",
    "مدرک رسمی اعمال و تسویه برای موقعیت منقضی",
]
REASONS = {
    "invalid_quote_or_closing_spot": "مظنه یا قیمت پایانی پایه نامعتبر",
    "insufficient_trades": "تعداد معاملات ناکافی",
    "no_trade_volume": "بدون حجم معامله",
    "no_bid_depth": "بدون عمق خرید",
    "no_ask_depth": "بدون عمق فروش",
    "near_expiry": "سررسید نزدیک",
    "wide_spread": "اسپرد زیاد",
    "ask_below_intrinsic": "قیمت فروش کمتر از ارزش ذاتی",
}


def absolute_path(value):
    watch.require(isinstance(value, str) and Path(value).is_absolute(), "absolute path required")
    return Path(value)


def configuration(path):
    data = private_json(path, watch.MAX_META)
    required = {"root", "calendar", "windows", "capture_interval_seconds"}
    watch.require(isinstance(data, dict) and required <= data.keys() <= required | {"paper"},
                  "invalid pilot configuration fields")
    root = absolute_path(data["root"])
    gap = positive_int(data["capture_interval_seconds"], "capture interval", 86400)
    watch.require(gap >= 60, "capture interval must be at least 60 seconds")
    if "paper" in data:
        paper = data["paper"]
        watch.require(isinstance(paper, dict) and set(paper) == PAPER_FIELDS,
                      "all independent paper evidence paths are required")
        paper = {key: absolute_path(value) for key, value in paper.items()}
        watch.require(not any(path.resolve().is_relative_to(root.resolve()) for path in paper.values()),
                      "paper trust files and database must be outside the observation root")
    else:
        paper = None
    return root, absolute_path(data["calendar"]), data["windows"], gap, paper


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    watch.require(path.is_dir() and not path.is_symlink()
                  and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                  "pilot directory must be private and owned by the pilot user")


def atomic_json(path, data):
    raw = collector.json_bytes(data)
    with NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def load_state(path):
    if not os.path.lexists(path):
        watch.require(not any(item.is_file() for name in ("reports", "trusted-batches")
                              for item in (path.parent / name).glob("*.json")),
                      "pilot state is missing while durable records exist")
        return {"schema": 1, "last_tick": None, "last_attempt": None,
                "last_sequence": 0, "last_claimed": 0, "last_finished": None,
                "last_capture": None, "last_capture_error": None, "result": None, "paper_scope": None}
    data = private_json(path, watch.MAX_META)
    watch.require(isinstance(data, dict) and set(data) == {
        "schema", "last_tick", "last_attempt", "last_sequence", "last_claimed",
        "last_finished", "last_capture", "last_capture_error", "result", "paper_scope"
    } and type(data["schema"]) is int and data["schema"] == 1
        and type(data["last_sequence"]) is int and type(data["last_claimed"]) is int
        and 0 <= data["last_sequence"] <= data["last_claimed"],
        "invalid or corrupt pilot state")
    for key in ("last_tick", "last_attempt", "last_finished", "last_capture"):
        if data[key] is not None:
            collector.utc(data[key])
    return data


def scan_day(stream, day):
    with collector.locked(stream, exclusive=False):
        records = collector.read_stream(stream)
        captures = [row for row in records if row["receipt_tehran"][:10] == day]
        latest = captures[-1] if captures else None
        rows = watch.contract_states(latest["path"], latest) if latest else {}
    counts = {}
    for kind in ("CALL", "PUT"):
        side = [row for row in rows.values() if row["option_type"] == kind]
        counts[kind] = {"قرارداد": len(side), "دوطرفه": sum(row["two_sided"] for row in side),
                        "نامزد": sum(row["candidate_eligible"] for row in side)}
    return {
        "تعداد ثبت روز": len(captures),
        "آخرین ثبت": {key: latest[key] for key in (
            "receipt_tehran", "body_sha256", "head_sha256", "exchange_quote_timestamp",
            "execution_eligible", "dataset_class")} if latest else None,
        "پوشش": counts,
        "نامزدهای پژوهشی": [{"شناسه": code, "نماد": row["symbol"], "نوع": row["option_type"]}
                            for code, row in rows.items() if row["candidate_eligible"]],
        "دلایل رد غربال": dict(Counter(
            REASONS.get(row["candidate_rejection"], row["candidate_rejection"])
            for row in rows.values() if row["candidate_rejection"])),
    }


def position_report(ledger, quotes):
    account = ledger.status()
    limits = ledger.require_policy().limits
    positions = []
    for row in account["positions"]:
        if not ledger.owns(row):
            continue
        quote = quotes.get(row["ins_code"])
        capacity = None
        gap = None
        if row["status"] == "EXPIRY_REVIEW":
            capacity = 0
            gap = "مدرک رسمی تسویه برای این قرارداد موجود نیست"
        elif quote is None:
            gap = "مظنهٔ مستقل و معتبر برای این موقعیت در بستهٔ فعلی نیست"
        else:
            bid = quote["best_bid"]
            capacity = (min(row["remaining"], limits.max_sell_quantity,
                            limits.max_sell_value // (bid * row["contract_size"]))
                        if bid >= limits.min_sell_price else 0)
        positions.append({"شناسه": row["id"], "نماد": row["symbol"], "نوع": row["option_type"],
                          "وضعیت": row["status"], "تعداد باقی‌مانده": row["remaining"],
                          "هشدار حد ضرر": row["stop_loss_alert"], "ظرفیت فروش هر سفارش": capacity,
                          "شاهد لازم": gap})
    owned_history = [row for row in ledger.history() if ledger.owns(row)]
    own_ids = {str(row["id"]) for row in owned_history}
    today = ledger.clock().astimezone(TEHRAN).date()
    exits = [{"موقعیت": row["id"], "نوع": row["option_type"], "سفارش": fill["order_id"],
              "تعداد": fill["quantity"], "قیمت ریال": fill["price"], "زمان UTC": fill["filled_at"]}
             for row in owned_history for fill in row["exit_orders"]
             if timestamp(fill["filled_at"]).astimezone(TEHRAN).date() == today]
    alerts = [row for row in PaperMonitor(ledger).incidents() if row["severity"] == "critical"
              and row["resolved_at"] is None and (
                  ":" not in row["incident_key"]
                  or row["incident_key"].rsplit(":", 1)[-1] in own_ids)]
    return {
        "موقعیت‌های ربات": positions, "خروج‌های آزمایشی امروز": exits,
        "توقف ورود": account["entries_paused"],
        "حدود و ظرفیت خرید": account["گزارش حدود و ظرفیت روزانه (ریال)"],
        "سقف فروش روزانه": None,
        "هشدارهای بحرانی دفتر": alerts,
    }


def claim_batch(root, payload, digest, quotes):
    folder = root / "trusted-batches"
    private_directory(folder)
    path = folder / f'{payload["sequence"]:019d}.json'
    record = {"sha256": digest, "evidence_ref": payload["evidence_ref"], "issued_at": payload["issued_at"],
              "instruments": sorted(quotes), "order_ids": [item["order_id"] for item in payload["entries"]]}
    if os.path.lexists(path):
        watch.require(private_json(path) == record, "signed batch sequence was reused with different evidence")
    else:
        atomic_json(path, record)


def paper_tick(root, paths, sessions, now, state, save, *, may_fill):
    if paths is None:
        return {"وضعیت": "فقط مشاهده", "مانع": "ورودی مستقل مظنه و دفتر آزمایشی پیکربندی نشده",
                "شواهد لازم": QUOTE_GAPS}
    policy = BotPolicy.parse(private_json(paths["policy"], watch.MAX_META))
    catalog = load_catalog(private_json(paths["catalog"]))
    private_directory(root / "trusted-batches")
    db_path = paths["db"]
    watch.require(db_path.is_file(), "initialized paper ledger required")
    # Check privacy without reading the database contents.
    info = db_path.lstat()
    watch.require(not db_path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                  "paper ledger must be private and owned by the pilot user")
    scope = [policy.bot_id, policy.strategy_id, str(db_path.resolve()), str(paths["key"].resolve())]
    watch.require(state["paper_scope"] in (None, scope), "paper scope changed; use a new pilot root")
    state["paper_scope"] = scope
    save()
    def session_at(at):
        local = at.astimezone(TEHRAN)
        day = local.date().isoformat()
        return day, schedule.containing(sessions.get(day, ()), local.time())

    current = session_at(now)
    fill_enabled = False
    last_clock = now

    def ledger_clock():
        nonlocal last_clock
        at = collector.checked_clock(collector.utc_now(), last_clock)
        last_clock = at
        if fill_enabled and (current[1] is None or session_at(at) != current):
            raise ValueError("Tehran session ended before paper execution")
        return at

    ledger = PaperTrading(db_path, clock=ledger_clock, policy=policy, catalog=catalog, sessions=sessions)
    monitor = PaperMonitor(ledger)
    quotes = {}
    payload = None
    reason = None
    try:
        if not may_fill:
            raise ValueError("paper fills require a current Tehran session and a valid observation archive")
        batch, signed_quotes, digest, issued = signed_batch(paths["feed"], paths["key"], now, catalog)
        instants = [issued]
        for quote in signed_quotes.values():
            instants.extend(timestamp(quote[key]) for key in ("quoted_at", "received_at"))
        for at in instants:
            if session_at(at) != current:
                raise ValueError("signed quote, receipt or issuance is outside the current Tehran session")
        if batch["sequence"] <= state["last_sequence"] or batch["sequence"] < state["last_claimed"]:
            raise ValueError("trusted batch sequence is already processed or older")
        claim_batch(root, batch, digest, signed_quotes)
        state["last_claimed"] = batch["sequence"]
        save()
        payload, quotes = batch, signed_quotes
    except (OSError, ValueError) as error:
        reason = str(error)
    with monitor._lock():
        fill_enabled = payload is not None
        try:
            monitor.cycle(list(quotes.values()) if payload else None)
        except ValueError as error:
            if not fill_enabled:
                raise
            fill_enabled = False
            reason = str(error)
            payload, quotes = None, {}
            monitor.cycle(None)
        rejected = []
        if payload:
            for entry in payload["entries"]:
                try:
                    data = quotes[entry["ins_code"]]
                    _, rejection = screen_candidate(entry["market"], data, ledger_clock(), catalog)
                    if rejection is not None:
                        raise ValueError(rejection)
                    ledger.buy(data, entry["quantity"], order_id=entry["order_id"])
                except ValueError as error:
                    rejected.append({"سفارش": entry["order_id"], "علت": str(error)})
            state["last_sequence"] = payload["sequence"]
            save()
    fill_enabled = False
    return position_report(ledger, quotes) | {
        "وضعیت": "دفتر آزمایشی",
        "مظنهٔ مستقل پذیرفته‌شده": len(quotes),
        "مانع": reason,
        "شواهد لازم": QUOTE_GAPS if reason else [],
        "ورودهای ردشده": rejected,
        "تسویه": "هیچ تسویه‌ای بدون مدرک رسمی ثبت نمی‌شود",
    }


def tick(config, *, dry_run=False):
    root, calendar, windows, gap, paper = configuration(config)
    sessions, calendar_hash = schedule.load_calendar(calendar)
    windows = schedule.intervals(windows)
    if dry_run:
        now = collector.checked_clock(collector.utc_now())
        slot, reason = schedule.schedule_slot(sessions, windows, now)
        return {"وضعیت": "پیش‌بررسی", "پنجرهٔ فعال": slot is not None, "مانع": reason,
                "مسیر مستقل دفتر تعریف شده": paper is not None}
    private_directory(root)
    with collector.locked(root, exclusive=True):
        return tick_locked(root, sessions, windows, gap, paper, calendar_hash)


def tick_locked(root, sessions, windows, gap, paper, calendar_hash):
    state_file = root / "state.json"
    state = load_state(state_file)
    previous = max((collector.utc(state[key]) for key in (
        "last_tick", "last_attempt", "last_finished", "last_capture"
    ) if state[key]), default=None)
    now = collector.checked_clock(collector.utc_now(), previous)
    slot, reason = schedule.schedule_slot(sessions, windows, now)

    def save():
        atomic_json(state_file, state)

    def gate(at):
        collector.checked_clock(at, now)
        if schedule.schedule_slot(sessions, windows, at)[0] != slot:
            raise collector.CaptureSkipped("collection_slot_changed")

    def reserve(at):
        prior = state["last_attempt"]
        if prior and at < collector.utc(prior) + timedelta(seconds=gap):
            raise collector.CaptureSkipped("persistent_capture_rate_limit")
        state["last_attempt"] = at.isoformat()
        save()

    stream = root / "observations"
    private_directory(stream)
    reports = root / "reports"
    private_directory(reports)
    state["last_tick"] = now.isoformat()
    save()
    capture = {"وضعیت": "گذشته از گردآوری", "علت": reason}
    alerts = []
    if slot and state["last_attempt"] and now < collector.utc(state["last_attempt"]) + timedelta(seconds=gap):
        capture["علت"] = "persistent_capture_rate_limit"
    elif slot:
        try:
            receipt = collector.collect(stream=stream, max_attempts=1, gate=gate, before_attempt=reserve)
        except collector.DuplicateCapture as error:
            capture["علت"] = str(error)
            state["last_capture_error"] = None
        except collector.CaptureSkipped as error:
            capture["علت"] = str(error)
        except collector.CAPTURE_ERRORS as error:
            capture = {"وضعیت": "خطای گردآوری", "علت": str(error)}
            state["last_capture_error"] = capture["علت"]
        else:
            capture = {"وضعیت": "ثبت شد", "علت": None}
            state.update(last_capture=receipt["receipt_utc"], last_capture_error=None)
        save()
    if state["last_capture_error"]:
        alerts.append({"وضعیت": "آخرین خطای گردآوری", "علت": state["last_capture_error"]})
    now = collector.checked_clock(collector.utc_now(), now)
    local = now.astimezone(TEHRAN)
    day = local.date().isoformat()
    try:
        observations = scan_day(stream, day)
    except collector.CAPTURE_ERRORS as error:
        observations = None
        alerts.append({"وضعیت": "خطای یکپارچگی یا غربال بایگانی", "علت": str(error)})
    try:
        paper_now = collector.checked_clock(collector.utc_now(), now)
        paper_local = paper_now.astimezone(TEHRAN)
        in_session = (paper_local.date().isoformat() == day
                      and schedule.containing(sessions.get(day, ()), paper_local.time()) is not None)
        account = paper_tick(root, paper, sessions, paper_now, state, save,
                             may_fill=observations is not None and in_session)
    except (OSError, ValueError, sqlite3.Error) as error:
        account = {"وضعیت": "مسدود", "مانع": str(error), "شواهد لازم": QUOTE_GAPS}
        alerts.append(account)
    report = {
        "تاریخ تهران": day, "زمان گزارش": local.isoformat(), "هش تقویم": calendar_hash,
        "گردآوری": capture, "مشاهدات و غربال پژوهشی": observations,
        "صلاحیت اجرای مشاهده": False, "پوشش شواهد واقعی": evidence_coverage(),
        "دفتر آزمایشی": account, "هشدارهای عملیاتی": alerts,
    }
    atomic_json(reports / f"{day}.json", report)
    finished = collector.checked_clock(collector.utc_now(), now)
    unhealthy = alerts or account.get("هشدارهای بحرانی دفتر")
    state.update(last_finished=finished.isoformat(), result="degraded" if unhealthy else "ok")
    save()
    return report


def status(config):
    root, _, _, _, _ = configuration(config)
    if not os.path.lexists(root):
        return {"وضعیت": "آغاز نشده"}
    info = root.lstat()
    watch.require(root.is_dir() and not root.is_symlink()
                  and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                  "pilot directory must be private and owned by the pilot user")
    state = load_state(root / "state.json")
    now = collector.utc_now()
    finished = collector.utc(state["last_finished"]) if state["last_finished"] else None
    stale = finished is None or not timedelta(0) <= now - finished <= timedelta(minutes=3)
    return {"وضعیت": "گزارش قدیمی یا ناقص" if stale else state["result"], "سلامت": state}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("tick", "check", "status"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = status(args.config) if args.action == "status" else tick(args.config, dry_run=args.action == "check")
    except (OSError, ValueError, ZoneInfoNotFoundError) as error:
        collector.log_event("pilot_failed", reason=str(error))
        return 1
    print(collector.json_bytes(result).decode(), end="")
    return int(bool(result.get("هشدارهای عملیاتی")))


if __name__ == "__main__":
    raise SystemExit(main())
