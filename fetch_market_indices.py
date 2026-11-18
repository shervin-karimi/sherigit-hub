import json
import urllib.request
from datetime import datetime

URL = "https://cdn.tsetmc.com/api/Index/GetIndexB1LastAll/All/1"

TARGETS = {
    "32097828799138957": "شاخص کل",
    "67130298613737946": "شاخص کل هم‌وزن",
}

def main():
    fetched_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

        rows = data.get("indexB1", [])
        results = {}

        for row in rows:
            code = str(row.get("insCode"))

            if code not in TARGETS:
                continue

            results[TARGETS[code]] = {
                "index_value": row.get("xDrNivJIdx004"),
                "change_percent": row.get("xVarIdxJRfV"),
                "change_value": row.get("indexChange"),
                "event_date": row.get("dEven"),
                "event_time": row.get("hEven"),
                "last_flag": row.get("last"),
            }

        if len(results) != 2:
            raise ValueError("اطلاعات هر دو شاخص دریافت نشد.")

        output = {
            "status": "موفق",
            "fetched_at": fetched_at,
            "indices": results,
        }

        with open(
            "data/market_indices.json",
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                output,
                file,
                ensure_ascii=False,
                indent=2
            )

        print("اطلاعات شاخص‌ها با موفقیت ذخیره شد.")
        print("زمان دریافت:", fetched_at)

        for name, item in results.items():
            print("\n" + name)
            print("مقدار:", item["index_value"])
            print("تغییر درصدی:", item["change_percent"])
            print("ساعت سرویس:", item["event_time"])

    except Exception as error:
        print("دریافت اطلاعات شاخص‌ها ناموفق بود.")
        print("علت:", str(error))
        print("هیچ دادهٔ جدیدی جایگزین فایل قبلی نشد.")

if __name__ == "__main__":
    main()
