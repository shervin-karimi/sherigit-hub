import json
import urllib.request
from pathlib import Path
from datetime import datetime

URL = (
    "https://cdn.tsetmc.com/api/"
    "Instrument/GetInstrumentOptionMarketWatch/1"
)

DATA_DIR = Path("data")
DATA_FILE = DATA_DIR / "options_market_watch.json"
META_FILE = DATA_DIR / "options_market_watch_meta.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

def main():
    try:
        print("در حال دریافت اطلاعات اختیار معامله...")

        request = urllib.request.Request(
            URL,
            headers=HEADERS
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            status = response.status
            raw = response.read()

        if status != 200:
            raise ValueError("پاسخ سرویس معتبر نیست.")

        data = json.loads(raw.decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("ساختار اطلاعات دریافتی نامعتبر است.")

        rows = data.get("instrumentOptMarketWatch")

        if not isinstance(rows, list) or not rows:
            raise ValueError("فهرست قراردادهای اختیار خالی است.")

        received_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        metadata = {
            "status": "موفق",
            "fetched_at": received_at,
            "http_status": status,
            "record_count": len(rows)
        }

        DATA_DIR.mkdir(exist_ok=True)

        data_temp = DATA_DIR / "options_market_watch.tmp"
        meta_temp = DATA_DIR / "options_market_watch_meta.tmp"

        data_temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        meta_temp.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        data_temp.replace(DATA_FILE)
        meta_temp.replace(META_FILE)

        print()
        print("دریافت اطلاعات با موفقیت انجام شد.")
        print("تعداد رکوردها:", len(rows))
        print("زمان دریافت:", received_at)
        print("فایل اطلاعات:", DATA_FILE)
        print("فایل زمان دریافت:", META_FILE)

        print()
        print("توجه: زمان دریافت برنامه ثبت شده است.")
        print("زمان واقعی آخرین به‌روزرسانی سرویس هنوز مشخص نیست.")

    except Exception as error:
        print()
        print("دریافت اطلاعات ناموفق بود.")
        print("علت:", str(error))
        print("اطلاعات قبلی حفظ شدند.")

if __name__ == "__main__":
    main()
