"""Run ordered historical events against a disposable paper ledger."""

from pathlib import Path
from tempfile import TemporaryDirectory

from candidate_rules import evaluate_candidate
from greeks_evidence_registry import current_assessment
from paper_monitor import PaperMonitor
from paper_quotes import MAX_SQLITE_INT, TEHRAN, Quote, positive_int, timestamp
from paper_trading import PaperTrading


EVENT_FIELDS = {
    "screen": {"at", "action", "quote", "market", "quantity", "order_id"},
    "buy": {"at", "action", "quote", "quantity", "order_id"},
    "sell": {"at", "action", "position_id", "quote", "quantity", "order_id"},
    "monitor": {"at", "action", "quotes"},
    "pause": {"at", "action"},
    "resume": {"at", "action"},
}


MARKET_FIELDS = {"observed_at", "strike", "underlying_closing_price",
                 "trade_count", "trade_volume", "bid_volume", "ask_volume"}


def screen_candidate(market, data, now, catalog):
    quote = Quote.parse(data, now, catalog)
    instrument = quote.instrument
    if not isinstance(market, dict) or set(market) != MARKET_FIELDS:
        raise ValueError("complete screening observation is required")
    if timestamp(market["observed_at"]) != quote.quoted_at:
        raise ValueError("screening observation must match the quote timestamp")
    strike = positive_int(market["strike"], "strike")
    spot = positive_int(market["underlying_closing_price"], "underlying closing price")
    for field in ("trade_count", "trade_volume", "bid_volume", "ask_volume"):
        value = market[field]
        if type(value) is not int or not 0 <= value <= MAX_SQLITE_INT:
            raise ValueError(f"invalid screening field: {field}")
    if instrument.last_trading_date is None:
        raise ValueError("verified final trading date is required for screening")
    remaining = (instrument.last_trading_date - quote.quoted_at.astimezone(TEHRAN).date()).days
    intrinsic = max(0, spot - strike if instrument.option_type == "CALL" else strike - spot)
    return evaluate_candidate(dict(market, best_bid=quote.bid, best_ask=quote.ask,
                                   remaining_days=remaining, intrinsic_value=intrinsic))


def evidence_coverage():
    assessment = current_assessment()
    topics = assessment["topics"]
    return {
        "دامنه": "رجیستری پژوهشی نسخه‌بندی‌شدهٔ پروژه",
        "هش رجیستری": assessment["sha256"],
        "سند اولیهٔ زمان مظنه": topics["quote_timestamps"]["status"],
        "سند اولیهٔ مهلت معامله": topics["expiry"]["status"],
        "سند اولیهٔ اعمال و تسویه": topics["exercise_settlement"]["status"],
        "شاهد اجرایی یا تسویهٔ مستقل تأییدشده در رجیستری": 0,
        "واجد شرایط اجرا": assessment["execution_eligible"],
    }


def replay(events, *, equity, catalog, sessions, policy, input_class="operator_supplied"):
    if input_class not in ("operator_supplied", "synthetic_fixture"):
        raise ValueError("invalid replay input classification")
    if not isinstance(events, list) or not events:
        raise ValueError("replay requires a nonempty event list")
    now = None
    rejected = []
    screening = []
    with TemporaryDirectory(prefix="paper-replay-") as directory:
        ledger = PaperTrading(Path(directory) / "ledger.db", clock=lambda: now,
                              catalog=catalog, sessions=sessions, policy=policy)
        ledger.require_policy()
        ledger.initialize(equity)
        monitor = PaperMonitor(ledger)
        for index, event in enumerate(events, start=1):
            action = event.get("action") if isinstance(event, dict) else None
            if not isinstance(action, str) or set(event) != EVENT_FIELDS.get(action):
                raise ValueError(f"invalid replay event {index}")
            at = timestamp(event["at"])
            if now is not None and at < now:
                raise ValueError(f"replay clock goes backwards at event {index}")
            now = at
            try:
                if action == "screen":
                    _, reason = screen_candidate(event["market"], event["quote"], now, catalog)
                    decision = {"event": index, "ins_code": event["quote"]["ins_code"],
                                "screen_passed": reason is None, "rejection": reason, "position_id": None}
                    screening.append(decision)
                    if reason is not None:
                        raise ValueError(reason)
                    position = ledger.buy(event["quote"], event["quantity"], order_id=event["order_id"])
                    decision["position_id"] = position["id"]
                elif action == "buy":
                    ledger.buy(event["quote"], event["quantity"], order_id=event["order_id"])
                elif action == "sell":
                    ledger.sell(event["position_id"], event["quote"],
                                quantity=event["quantity"], order_id=event["order_id"])
                elif action == "monitor":
                    if not isinstance(event["quotes"], list):
                        raise ValueError("monitor quotes must be an array")
                    monitor.cycle(event["quotes"])
                else:
                    ledger.set_pause(action == "pause")
            except ValueError as error:
                rejected.append({"event": index, "reason": str(error)})
        return {
            "نوع گزارش": "بازپخش آزمایشی آفلاین؛ همهٔ اجراها شبیه‌سازی‌شده‌اند",
            "ردهٔ ورودی": input_class,
            "پوشش شواهد واقعی": evidence_coverage(),
            "نتیجهٔ غربال": screening,
            "حساب": ledger.status(),
            "تاریخچه": ledger.history(),
            "هشدارها": monitor.incidents(),
            "رویدادهای ردشده": rejected,
        }
