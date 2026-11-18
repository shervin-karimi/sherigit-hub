
import argparse
import sqlite3
import sys
from datetime import datetime, date

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

    p = commands.add_parser("خرید", help="ثبت خرید واقعی")
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

    p = commands.add_parser("فروش", help="ثبت فروش و بستن معامله")
    p.add_argument("--شماره", type=int, required=True)
    p.add_argument("--قیمت", type=float, required=True)
    p.add_argument("--دلیل", default="بستن دستی معامله")
    p.set_defaults(func=close_trade)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
