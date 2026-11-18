import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

BASE_URL = (
    "https://cdn.tsetmc.com/api/ClosingPrice/"
    "GetClosingPriceDailyList"
)

DATA_DIR = Path("data")
INPUT_FILE = DATA_DIR / "candidates.json"
OUTPUT_FILE = DATA_DIR / "underlying_history.json"
TEMP_FILE = DATA_DIR / "underlying_history.tmp"

REQUIRED_HISTORY = 21
REQUEST_TIMEOUT = 30


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def positive_number(value):
    try:
        number = float(value)
        return number > 0
    except (TypeError, ValueError):
        return False


def build_symbols(candidates):
    symbols = {}

    for item in candidates:
        name = str(item.get("underlying_symbol") or "").strip()
        code = str(item.get("underlying_code") or "").strip()

        if not name or not code:
            raise ValueError("نام یا کد دارایی پایه در candidates ناقص است.")

        if name in symbols and symbols[name] != code:
            raise ValueError(
                f"کدهای متناقض برای {name}: "
                f"{symbols[name]} و {code}"
            )

        symbols[name] = code

    if not symbols:
        raise ValueError("هیچ نماد پایه‌ای در candidates پیدا نشد.")

    return symbols


def fetch_history(code):
    url = f"{BASE_URL}/{code}/40"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT
    ) as response:
        if response.status != 200:
            raise ValueError(f"HTTP status: {response.status}")

        data = json.loads(response.read().decode("utf-8"))

    rows = data.get("closingPriceDaily")

    if not isinstance(rows, list) or not rows:
        raise ValueError("پاسخ سرویس فاقد تاریخچه معتبر است.")

    seen_dates = set()
    prepared = []

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("رکورد نامعتبر در پاسخ سرویس.")

        date_value = row.get("dEven")

        try:
            date_number = int(date_value)
            datetime.strptime(str(date_number), "%Y%m%d")
        except (TypeError, ValueError):
            raise ValueError(f"تاریخ نامعتبر: {date_value}")

        if date_number in seen_dates:
            raise ValueError(f"تاریخ تکراری: {date_number}")

        seen_dates.add(date_number)

        if str(row.get("insCode")) != str(code):
            raise ValueError(
                f"کد نماد در رکورد با درخواست تطبیق ندارد: {date_number}"
            )

        close_ok = positive_number(row.get("pClosing"))
        volume_ok = positive_number(row.get("qTotTran5J"))

        ohlc_ok = all(
            positive_number(row.get(key))
            for key in ("priceFirst", "priceMax", "priceMin")
        ) and close_ok

        if ohlc_ok:
            first = float(row["priceFirst"])
            high = float(row["priceMax"])
            low = float(row["priceMin"])
            close = float(row["pClosing"])

            ohlc_ok = (
                low <= high
                and low <= close <= high
                and low <= first <= high
            )

        item = dict(row)
        item["_quality"] = {
            "close_valid": close_ok,
            "volume_valid": volume_ok,
            "ohlc_valid": ohlc_ok,
        }

        prepared.append(item)

    prepared.sort(
        key=lambda item: int(item["dEven"]),
        reverse=True
    )

    active_closes = [
        item for item in prepared
        if item["_quality"]["close_valid"]
        and item["_quality"]["volume_valid"]
    ]

    if len(active_closes) < REQUIRED_HISTORY:
        raise ValueError(
            "جلسات دارای قیمت و حجم معتبر کمتر از حد لازم است: "
            f"{len(active_closes)}"
        )

    return prepared[:40]


def main():
    DATA_DIR.mkdir(exist_ok=True)

    with open(INPUT_FILE, encoding="utf-8") as file:
        candidates = json.load(file)

    symbols = build_symbols(candidates)

    results = {}
    failures = []

    print("شروع دریافت تاریخچه دارایی‌های پایه")
    print("تعداد نمادها:", len(symbols))

    for name, code in sorted(symbols.items()):
        print(f"\nدریافت {name} ...")

        try:
            rows = fetch_history(code)

            close_count = sum(
                row["_quality"]["close_valid"] for row in rows
            )
            active_count = sum(
                row["_quality"]["close_valid"]
                and row["_quality"]["volume_valid"]
                for row in rows
            )
            ohlc_count = sum(
                row["_quality"]["ohlc_valid"] for row in rows
            )

            results[name] = {
                "code": code,
                "fetched_at": now_iso(),
                "record_count": len(rows),
                "quality": {
                    "valid_close_count": close_count,
                    "active_session_count": active_count,
                    "valid_ohlc_count": ohlc_count,
                },
                "history": rows,
            }

            print("کل رکوردها:", len(rows))
            print("قیمت پایانی معتبر:", close_count)
            print("جلسات دارای حجم:", active_count)
            print("OHLC معتبر:", ohlc_count)
            print("آخرین تاریخ:", rows[0]["dEven"])

        except Exception as error:
            failures.append((name, str(error)))
            print("خطا:", str(error))

    if failures:
        if TEMP_FILE.exists():
            TEMP_FILE.unlink()

        print("\nدریافت کامل نشد؛ فایل قبلی حفظ شد.")
        for name, error in failures:
            print(f"{name}: {error}")
        return

    document = {
        "metadata": {
            "fetched_at": now_iso(),
            "symbol_count": len(results),
            "source": "TSETMC ClosingPrice API",
        },
        "symbols": results,
    }

    try:
        with open(TEMP_FILE, "w", encoding="utf-8") as file:
            json.dump(
                document,
                file,
                ensure_ascii=False,
                indent=2
            )
            file.flush()

        TEMP_FILE.replace(OUTPUT_FILE)

    except Exception:
        if TEMP_FILE.exists():
            TEMP_FILE.unlink()
        raise

    print("\nدریافت کامل شد.")
    print("تعداد نمادها:", len(results))
    print("فایل تاریخچه به‌روزرسانی شد.")


if __name__ == "__main__":
    main()
