import json
from datetime import datetime

FILE = "data/market_indices.json"
NAMES = ["شاخص کل", "شاخص کل هم‌وزن"]

def format_time(value):
    value = int(value)
    hour = value // 10000
    minute = (value // 100) % 100
    second = value % 100

    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("ساعت سرویس نامعتبر است.")

    return f"{hour:02d}:{minute:02d}:{second:02d}"

def main():
    try:
        with open(FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        received = datetime.fromisoformat(data["fetched_at"])
        indices = data["indices"]

        received_seconds = (
            received.hour * 3600
            + received.minute * 60
            + received.second
        )

        for name in NAMES:
            item = indices[name]
            service_time = format_time(item["event_time"])

            h, m, s = map(int, service_time.split(":"))
            service_seconds = h * 3600 + m * 60 + s

            difference = abs(received_seconds - service_seconds)
            difference = min(difference, 86400 - difference)

            print(name)
            print("ساعت سرویس:", service_time)
            print("اختلاف با زمان دریافت:", difference, "ثانیه")
            print("تاریخ سرویس:", item["event_date"])
            print("-" * 35)

        print("توجه: تاریخ سرویس در پاسخ صفر است.")
        print("تازگی قطعی اطلاعات هنوز تأیید نشده است.")
        print("وضعیت معاملاتی: نامشخص؛ فعلاً معامله نکن.")

    except Exception as error:
        print("بررسی ساعت ناموفق بود:", str(error))
        print("وضعیت معاملاتی: نامشخص؛ فعلاً معامله نکن.")

if __name__ == "__main__":
    main()
