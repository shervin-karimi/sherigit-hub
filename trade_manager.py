
import argparse
import json
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

from paper_limits import BotPolicy
from paper_monitor import PaperMonitor
from paper_quotes import load_catalog
from paper_replay import replay
from paper_trading import DB_PATH as PAPER_DB_PATH, PaperTrading
from shadow_schedule import load_calendar

DB_PATH = "data/trading.db"


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    return sqlite3.connect(DB_PATH)


def option_to_db(value):
    mapping = {
        "اختیار خرید": "CALL",
        "اختیار فروش": "PUT",
    }
    return mapping.get(value)


def option_to_fa(value):
    return {
        "CALL": "اختیار خرید",
        "PUT": "اختیار فروش",
    }.get(value, "نامشخص")


class PersianHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, *args, **kwargs):
        result = super()._format_usage(*args, **kwargs)
        return result.replace("usage:", "راهنما:", 1)


class PersianArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        kwargs["formatter_class"] = PersianHelpFormatter
        super().__init__(*args, **kwargs)
        self._positionals.title = "فرمان‌ها"
        self._optionals.title = "گزینه‌ها"
        self.add_argument(
            "--راهنما",
            action="help",
            help="نمایش راهنما و خروج"
        )

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "خطا: ورودی فرمان معتبر نیست. لطفاً راهنما را ببینید.\n")


def add_signal(args):
    if args.ورود <= 0 or args.هدف <= 0 or args.حدضرر <= 0:
        print("خطا: قیمت‌ها باید بزرگ‌تر از صفر باشند.")
        return

    if args.نوع == "اختیار خرید":
        if not args.حدضرر < args.ورود < args.هدف:
            print("خطا: برای اختیار خرید، حد ضرر باید پایین‌تر از ورود و هدف بالاتر از ورود باشد.")
            return
    else:
        if not args.حدضرر < args.ورود < args.هدف:
            print("خطا: قیمت‌های ورود، هدف و حد ضرر را بررسی کن. در این نسخه قیمت هدف باید بالاتر از ورود باشد.")
            return

    if args.مدت < 1 or args.مدت > 7:
        print("خطا: حداکثر مدت نگهداری باید بین ۱ تا ۷ روز باشد.")
        return

    if args.امتیاز < 0 or args.امتیاز > 100:
        print("خطا: امتیاز باید بین صفر تا صد باشد.")
        return

    with connect() as db:
        cur = db.execute("""
            INSERT INTO signals (
                created_at, symbol, option_type, underlying_symbol,
                entry_price, target_price, stop_loss,
                expiry_date, max_holding_days, score, status, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?)
        """, (
            now(), args.نماد, option_to_db(args.نوع), args.پایه,
            args.ورود, args.هدف, args.حدضرر,
            args.سررسید, args.مدت, args.امتیاز, args.دلیل
        ))
        signal_id = cur.lastrowid

    print(f"پیشنهاد شمارهٔ {signal_id} ثبت شد.")
    print(f"نماد: {args.نماد}")
    print(f"نوع: {args.نوع}")
    print(f"قیمت ورود: {args.ورود}")
    print(f"هدف: {args.هدف}")
    print(f"حد ضرر: {args.حدضرر}")
    print(f"حداکثر نگهداری: {args.مدت} روز")


def buy_trade(args):
    if args.تعداد <= 0 or args.قیمت <= 0:
        print("خطا: تعداد و قیمت خرید باید بزرگ‌تر از صفر باشند.")
        return

    signal = None

    with connect() as db:
        if args.پیشنهاد is not None:
            signal = db.execute(
                "SELECT * FROM signals WHERE id = ?",
                (args.پیشنهاد,)
            ).fetchone()

            if signal is None:
                print("خطا: پیشنهاد موردنظر پیدا نشد.")
                return

            symbol = signal[2]
            option_type = signal[3]
            underlying = signal[4]
            target = signal[6]
            stop = signal[7]
            expiry = signal[8]
            holding_days = signal[9]

            if signal[11] != "NEW":
                print("خطا: این پیشنهاد قبلاً استفاده شده یا دیگر در وضعیت قابل اجرا نیست.")
                return
        else:
            required = [
                args.نماد, args.نوع, args.پایه,
                args.هدف, args.حدضرر, args.سررسید
            ]
            if any(x is None for x in required):
                print("خطا: یا شمارهٔ پیشنهاد را وارد کن، یا تمام مشخصات خرید را تکمیل کن.")
                return

            symbol = args.نماد
            option_type = option_to_db(args.نوع)
            underlying = args.پایه
            target = args.هدف
            stop = args.حدضرر
            expiry = args.سررسید
            holding_days = args.مدت or 7

        if holding_days < 1 or holding_days > 7:
            print("خطا: مدت نگهداری باید بین ۱ تا ۷ روز باشد.")
            return

        if target <= 0 or stop <= 0:
            print("خطا: هدف و حد ضرر باید بزرگ‌تر از صفر باشند.")
            return

        cur = db.execute("""
            INSERT INTO trades (
                signal_id, symbol, option_type, underlying_symbol,
                quantity, entry_price, entry_at, initial_stop,
                current_stop, target_price, highest_price,
                expiry_date, max_holding_days, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """, (
            args.پیشنهاد, symbol, option_type, underlying,
            args.تعداد, args.قیمت, now(), stop,
            stop, target, args.قیمت,
            expiry, holding_days, args.یادداشت or ""
        ))

        trade_id = cur.lastrowid

        if args.پیشنهاد is not None:
            db.execute(
                "UPDATE signals SET status = 'ENTERED' WHERE id = ?",
                (args.پیشنهاد,)
            )

        db.execute("""
            INSERT INTO trade_events (trade_id, event_at, event_type, price, message)
            VALUES (?, ?, 'BUY', ?, ?)
        """, (trade_id, now(), args.قیمت, "ثبت خرید واقعی"))

    print(f"خرید شمارهٔ {trade_id} ثبت شد.")
    print(f"نماد: {symbol}")
    print(f"نوع: {option_to_fa(option_type)}")
    print(f"تعداد قرارداد: {args.تعداد}")
    print(f"قیمت خرید: {args.قیمت}")
    print("وضعیت: باز")


def show_open_trades(args):
    with connect() as db:
        rows = db.execute("""
            SELECT id, symbol, option_type, underlying_symbol,
                   quantity, entry_price, current_stop, target_price,
                   expiry_date, max_holding_days, entry_at
            FROM trades
            WHERE status = 'OPEN'
            ORDER BY entry_at DESC
        """).fetchall()

    if not rows:
        print("در حال حاضر معاملهٔ بازی ثبت نشده است.")
        return

    print("معاملات باز:")
    print("-" * 65)

    for r in rows:
        print(f"شماره: {r[0]} | نماد: {r[1]} | نوع: {option_to_fa(r[2])}")
        print(f"دارایی پایه: {r[3]} | تعداد: {r[4]}")
        print(f"قیمت ورود: {r[5]} | حد ضرر فعلی: {r[6]} | هدف: {r[7]}")
        print(f"سررسید: {r[8]} | حداکثر نگهداری: {r[9]} روز")
        print(f"زمان ثبت: {r[10]}")
        print("-" * 65)


def close_trade(args):
    if args.قیمت <= 0:
        print("خطا: قیمت فروش باید بزرگ‌تر از صفر باشد.")
        return

    with connect() as db:
        trade = db.execute("""
            SELECT id, quantity, entry_price, symbol, option_type
            FROM trades
            WHERE id = ? AND status = 'OPEN'
        """, (args.شماره,)).fetchone()

        if trade is None:
            print("خطا: معاملهٔ باز با این شماره پیدا نشد.")
            return

        trade_id, quantity, entry_price, symbol, option_type = trade

        gross = (args.قیمت - entry_price) * quantity

        db.execute("""
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_at = ?, exit_reason = ?
            WHERE id = ?
        """, (args.قیمت, now(), args.دلیل, trade_id))

        db.execute("""
            INSERT INTO trade_events (trade_id, event_at, event_type, price, message)
            VALUES (?, ?, 'SELL', ?, ?)
        """, (trade_id, now(), args.قیمت, args.دلیل))

    print(f"معاملهٔ شمارهٔ {trade_id} بسته شد.")
    print(f"نماد: {symbol} | نوع: {option_to_fa(option_type)}")
    print(f"قیمت خرید: {entry_price}")
    print(f"قیمت فروش: {args.قیمت}")
    print(f"تفاوت قیمت × تعداد: {gross:.2f}")
    print("توجه: این رقم بدون ضریب قرارداد، کارمزد و مالیات است و سود یا زیان خالص محسوب نمی‌شود.")


def run_paper(args):
    try:
        quote_actions = ("buy", "mark", "sell", "monitor", "replay")
        catalog = None
        if args.action in quote_actions and args.catalog is None:
            raise ValueError("--catalog is required for quote processing")
        if args.catalog is not None:
            catalog = load_catalog(json.loads(args.catalog.read_text(encoding="utf-8")))
        sessions = load_calendar(args.calendar)[0] if args.action in ("buy", "replay") and args.calendar else None
        policy = BotPolicy.parse(json.loads(args.policy.read_text(encoding="utf-8"))) if args.policy else None
        ledger = PaperTrading(args.db, catalog=catalog, sessions=sessions, policy=policy)
        if args.action == "replay":
            result = replay(json.loads(args.events.read_text(encoding="utf-8")), equity=args.equity,
                            catalog=catalog, sessions=sessions, policy=policy, input_class=args.input_class)
        elif args.action == "init":
            ledger.initialize(args.equity, args.stop_bps)
            result = ledger.status()
        elif args.action in ("buy", "mark", "sell"):
            quote = json.loads(args.quote.read_text(encoding="utf-8"))
            if args.action == "buy":
                result = ledger.buy(quote, args.quantity, order_id=args.order_id)
            elif args.action == "mark":
                result = ledger.mark(args.position_id, quote)
            else:
                result = ledger.sell(args.position_id, quote, order_id=args.order_id, quantity=args.quantity)
        elif args.action in ("status", "history"):
            result = ledger.status() if args.action == "status" else ledger.history()
        elif args.action in ("pause", "resume"):
            ledger.set_pause(args.action == "pause")
            result = ledger.status()
        elif args.action == "monitor":
            monitor = PaperMonitor(ledger, args.interval)
            monitor.run(args.feed, once=args.once)
            result = monitor.status()
        elif args.action == "monitor-status":
            result = PaperMonitor(ledger).status()
        else:
            result = PaperMonitor(ledger).incidents()
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Paper trading: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def add_paper_commands(commands):
    paper = commands.add_parser("paper", help="معاملات آزمایشی آفلاین؛ بدون ارسال سفارش")
    paper.add_argument("--db", type=Path, default=PAPER_DB_PATH)
    paper.add_argument("--catalog", type=Path, help="فهرست محلی مشخصات تأییدشدهٔ قراردادها")
    paper.add_argument("--policy", type=Path, help="پروندهٔ محلی شناسهٔ ربات، راهبرد و حدود معامله")
    actions = paper.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="راه‌اندازی حساب آزمایشی")
    init.add_argument("--equity", type=int, required=True, help="سرمایهٔ اولیه به ریال")
    init.add_argument("--stop-bps", type=int, default=300, help="آستانهٔ هشدار؛ ۳۰۰ یعنی ۳٪")
    for name in ("buy", "mark", "sell"):
        action = actions.add_parser(name)
        action.add_argument("--quote", type=Path, required=True, help="فایل محلی مظنه (JSON)")
        if name == "buy":
            action.add_argument("--calendar", type=Path, help="تقویم محلی جلسات معاملاتی تهران")
            action.add_argument("--quantity", type=int, required=True)
            action.add_argument("--order-id", required=True, help="شناسهٔ یکتا برای جلوگیری از ثبت تکراری")
        else:
            action.add_argument("--position-id", type=int, required=True)
            if name == "sell":
                action.add_argument("--quantity", type=int, help="تعداد خروج جزئی")
                action.add_argument("--order-id", required=True, help="شناسهٔ یکتای سفارش خروج")
    actions.add_parser("status", help="وجه نقد، تخصیص، سود و زیان و موقعیت‌های باز")
    actions.add_parser("history", help="تاریخچهٔ ورود و خروج آزمایشی")
    actions.add_parser("pause", help="توقف اضطراری خرید آزمایشی")
    actions.add_parser("resume", help="رفع توقف دستی خرید؛ محدودیت‌های ریسک همچنان برقرارند")
    monitor = actions.add_parser("monitor", help="پایش آفلاین مظنه‌ها و خروج در مهلت")
    monitor.add_argument("--feed", type=Path, required=True)
    monitor.add_argument("--interval", type=int, default=15)
    monitor.add_argument("--once", action="store_true")
    actions.add_parser("monitor-status", help="آخرین وضعیت و زمان ضربان ناظر")
    actions.add_parser("incidents", help="رخدادهای ثبت‌شده")
    historical = actions.add_parser("replay", help="بازپخش آفلاین در دفتر موقت")
    historical.add_argument("--events", type=Path, required=True)
    historical.add_argument("--calendar", type=Path, required=True)
    historical.add_argument("--equity", type=int, required=True)
    historical.add_argument("--input-class", choices=("operator_supplied", "synthetic_fixture"),
                            default="operator_supplied")
    paper.set_defaults(func=run_paper)


def build_parser():
    parser = PersianArgumentParser(
        description="سامانهٔ فارسی ثبت و مدیریت معاملات اختیار معامله"
    )

    commands = parser.add_subparsers(
        dest="فرمان",
        required=True,
        parser_class=PersianArgumentParser
    )

    p = commands.add_parser("پیشنهاد", help="ثبت پیشنهاد جدید")
    p.add_argument("--نماد", required=True)
    p.add_argument("--نوع", required=True, choices=["اختیار خرید", "اختیار فروش"])
    p.add_argument("--پایه", required=True)
    p.add_argument("--ورود", type=float, required=True)
    p.add_argument("--هدف", type=float, required=True)
    p.add_argument("--حدضرر", type=float, required=True)
    p.add_argument("--سررسید", required=True, help="به شکل سال‌ماه‌روز، مانند 20261202")
    p.add_argument("--مدت", type=int, default=7)
    p.add_argument("--امتیاز", type=float, default=0)
    p.add_argument("--دلیل", default="")
    p.set_defaults(func=add_signal)

    p = commands.add_parser("خرید", help="دفتر قدیمی: ثبت دستی خرید؛ بدون ارسال سفارش")
    p.add_argument("--پیشنهاد", type=int)
    p.add_argument("--نماد")
    p.add_argument("--نوع", choices=["اختیار خرید", "اختیار فروش"])
    p.add_argument("--پایه")
    p.add_argument("--تعداد", type=int, required=True)
    p.add_argument("--قیمت", type=float, required=True)
    p.add_argument("--هدف", type=float)
    p.add_argument("--حدضرر", type=float)
    p.add_argument("--سررسید")
    p.add_argument("--مدت", type=int)
    p.add_argument("--یادداشت")
    p.set_defaults(func=buy_trade)

    p = commands.add_parser("باز", help="نمایش معاملات باز")
    p.set_defaults(func=show_open_trades)

    p = commands.add_parser("فروش", help="دفتر قدیمی: ثبت دستی فروش؛ بدون ارسال سفارش")
    p.add_argument("--شماره", type=int, required=True)
    p.add_argument("--قیمت", type=float, required=True)
    p.add_argument("--دلیل", default="بستن دستی معامله")
    p.set_defaults(func=close_trade)

    add_paper_commands(commands)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
