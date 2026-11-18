
import json
import csv
import time
from datetime import datetime
import urllib.request
from pathlib import Path

INPUT = Path("data/all_contracts.json")
PRICE_FILE = Path("data/underlying_prices.json")
OUTPUT_JSON = Path("data/enriched_contracts.json")
OUTPUT_CSV = Path("data/enriched_contracts.csv")


def fetch_price(ins_code):
    url = (
        "https://cdn.tsetmc.com/api/ClosingPrice/"
        "GetClosingPriceInfo/" + str(ins_code)
    )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        received_at = datetime.now().astimezone().isoformat(timespec="seconds")
        result = json.loads(response.read())

    info = result.get("closingPriceInfo") or {}
    closing = info.get("pClosing")
    last = info.get("pDrCotVal")

    if closing is None or closing <= 0:
        raise ValueError("قیمت پایانی نامعتبر است")

    return {
        "closing_price": closing,
        "last_price": last,
        "yesterday_price": info.get("priceYesterday"),
        "trade_count": info.get("zTotTran"),
        "trade_volume": info.get("qTotTran5J"),
        "trade_value": info.get("qTotCap"),
        "date": info.get("dEven"),
        "received_at": received_at
    }


with INPUT.open("r", encoding="utf-8") as f:
    contracts = json.load(f)

underlying_codes = sorted({
    str(c["underlying_code"])
    for c in contracts
    if c.get("underlying_code")
})

if not underlying_codes:
    raise SystemExit("خطا: هیچ دارایی پایه‌ای پیدا نشد")

prices = {}
errors = {}

print("تعداد دارایی‌های پایه:", len(underlying_codes))

for i, code in enumerate(underlying_codes, 1):
    try:
        prices[code] = fetch_price(code)
        print(
            f"[{i}/{len(underlying_codes)}] موفق:",
            code,
            prices[code]["closing_price"]
        )
    except Exception as e:
        errors[code] = str(e)
        print(f"[{i}/{len(underlying_codes)}] خطا:", code, e)

    time.sleep(0.25)

# اگر حتی یک دریافت ناموفق باشد، خروجی‌های قبلی حفظ می‌شوند.
if errors:
    print("\nدریافت قیمت همه دارایی‌ها موفق نبود.")
    print("هیچ فایل خروجی جدیدی ذخیره نشد.")
    print("تعداد خطاها:", len(errors))
    raise SystemExit(1)

if len(prices) != len(underlying_codes):
    raise SystemExit("خطا: تعداد قیمت‌های دریافت‌شده کامل نیست")

enriched = []

for c in contracts:
    item = dict(c)
    base = prices[str(c["underlying_code"])]

    spot = c.get("underlying_closing_price") or 0
    strike = c.get("strike_price") or 0

    item["underlying_closing_price"] = spot
    item["underlying_last_price"] = c.get("underlying_last_price")
    item["underlying_yesterday_price"] = c.get("underlying_yesterday_price")
    item["underlying_trade_count"] = base["trade_count"]
    item["underlying_trade_volume"] = base["trade_volume"]
    item["underlying_price_date"] = base["date"]
    item["underlying_price_received_at"] = base["received_at"]

    if c["option_type"] == "CALL":
        intrinsic = max(spot - strike, 0)
    else:
        intrinsic = max(strike - spot, 0)

    item["intrinsic_value"] = intrinsic

    bid = c.get("best_bid") or 0
    ask = c.get("best_ask") or 0

    if bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        item["option_mid_price"] = round(mid, 2)
        item["time_value"] = round(mid - intrinsic, 2)
    else:
        item["option_mid_price"] = None
        item["time_value"] = None

    if spot > 0 and strike > 0:
        item["moneyness_percent"] = round(
            (spot - strike) / spot * 100, 3
        )
    else:
        item["moneyness_percent"] = None

    enriched.append(item)

# ابتدا فایل‌های موقت ساخته می‌شوند.
PRICE_TMP = PRICE_FILE.with_name("underlying_prices.tmp")
JSON_TMP = OUTPUT_JSON.with_name("enriched_contracts_json.tmp")
CSV_TMP = OUTPUT_CSV.with_name("enriched_contracts_csv.tmp")

try:
    PRICE_TMP.write_text(
        json.dumps(prices, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    JSON_TMP.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    with CSV_TMP.open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=enriched[0].keys()
        )
        writer.writeheader()
        writer.writerows(enriched)

    # جایگزینی خروجی‌ها پس از تکمیل ساخت هر سه فایل
    PRICE_TMP.replace(PRICE_FILE)
    JSON_TMP.replace(OUTPUT_JSON)
    CSV_TMP.replace(OUTPUT_CSV)

except Exception:
    for temp in (PRICE_TMP, JSON_TMP, CSV_TMP):
        if temp.exists():
            temp.unlink()
    raise

print("\nگزارش تکمیل اطلاعات")
print("تعداد قراردادها:", len(enriched))
print("قیمت‌های دریافت‌شده:", len(prices))
print("قراردادهای دارای قیمت پایه:", len(enriched))
print("\nفایل‌های ذخیره‌شده:")
print(PRICE_FILE)
print(OUTPUT_JSON)
print(OUTPUT_CSV)
