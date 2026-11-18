import json
import csv
from pathlib import Path

SOURCE = Path("data/options_market_watch.json")
META_SOURCE = Path("data/options_market_watch_meta.json")
OUTPUT_JSON = Path("data/all_contracts.json")
OUTPUT_CSV = Path("data/all_contracts.csv")

with SOURCE.open("r", encoding="utf-8") as f:
    raw = json.load(f)

with META_SOURCE.open("r", encoding="utf-8") as f:
    metadata = json.load(f)

if metadata.get("status") != "موفق":
    raise SystemExit("خطا: وضعیت دریافت داده موفق نیست.")

snapshot_time = metadata.get("fetched_at")
if not snapshot_time:
    raise SystemExit("خطا: زمان دریافت snapshot در متادیتا وجود ندارد.")

records = raw.get("instrumentOptMarketWatch", [])

if not isinstance(records, list) or not records:
    raise SystemExit("خطا: فهرست قراردادها خالی یا نامعتبر است.")

contracts = []

for record in records:
    for suffix, option_type in [
        ("C", "CALL"),
        ("P", "PUT")
    ]:
        ins_code = record.get(f"insCode_{suffix}")

        if not ins_code:
            continue

        contract = {
            "ins_code": str(ins_code),
            "symbol": record.get(f"lVal18AFC_{suffix}"),
            "contract_name": record.get(f"lVal30_{suffix}"),
            "option_type": option_type,

            "underlying_code": record.get("uaInsCode"),
            "underlying_symbol": record.get("lval30_UA"),

            "underlying_last_price": record.get("pDrCotVal_UA"),
            "underlying_closing_price": record.get("pClosing_UA"),
            "underlying_yesterday_price": record.get("priceYesterday_UA"),

            "strike_price": record.get("strikePrice"),
            "maturity_date": record.get("endDate"),
            "begin_date": record.get("beginDate"),
            "remaining_days": record.get("remainedDay"),
            "contract_size": record.get("contractSize"),

            "last_price": record.get(f"pDrCotVal_{suffix}"),
            "closing_price": record.get(f"pClosing_{suffix}"),
            "yesterday_price": record.get(f"priceYesterday_{suffix}"),

            "trade_count": record.get(f"zTotTran_{suffix}"),
            "trade_volume": record.get(f"qTotTran5J_{suffix}"),
            "trade_value": record.get(f"qTotCap_{suffix}"),
            "notional_value": record.get(f"notionalValue_{suffix}"),

            "op_value": record.get(f"oP_{suffix}"),
            "market_same_risk_op": record.get(
                f"marketSameRiskOP_{suffix}"
            ),

            "best_bid": record.get(f"pMeDem_{suffix}"),
            "bid_volume": record.get(f"qTitMeDem_{suffix}"),
            "best_ask": record.get(f"pMeOf_{suffix}"),
            "ask_volume": record.get(f"qTitMeOf_{suffix}"),

            "snapshot_received_at": snapshot_time,
            "snapshot_http_status": metadata.get("http_status"),
            "snapshot_record_count": metadata.get("record_count")
        }

        contracts.append(contract)

calls = [x for x in contracts if x["option_type"] == "CALL"]
puts = [x for x in contracts if x["option_type"] == "PUT"]

underlyings = {
    x["underlying_code"]
    for x in contracts
    if x["underlying_code"]
}

OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

json_tmp = OUTPUT_JSON.with_name("all_contracts_json.tmp")
csv_tmp = OUTPUT_CSV.with_name("all_contracts_csv.tmp")

try:
    json_tmp.write_text(
        json.dumps(contracts, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    if contracts:
        with csv_tmp.open(
            "w", newline="", encoding="utf-8-sig"
        ) as f:
            writer = csv.DictWriter(
                f, fieldnames=contracts[0].keys()
            )
            writer.writeheader()
            writer.writerows(contracts)

    json_tmp.replace(OUTPUT_JSON)

    if contracts:
        csv_tmp.replace(OUTPUT_CSV)

except Exception:
    for temp in (json_tmp, csv_tmp):
        if temp.exists():
            temp.unlink()
    raise

print("=" * 45)
print("TSE OPTIONS SCANNER - DATA SUMMARY")
print("=" * 45)
print("Snapshot received at:", snapshot_time)
print("Source records:", len(records))
print("Total contracts:", len(contracts))
print("CALL contracts:", len(calls))
print("PUT contracts:", len(puts))
print("Underlying assets:", len(underlyings))
print("\nSaved files:")
print(OUTPUT_JSON)
print(OUTPUT_CSV)
print("\nتوجه: این زمان فقط زمان دریافت snapshot است، نه زمان آخرین تغییر مظنه.")
