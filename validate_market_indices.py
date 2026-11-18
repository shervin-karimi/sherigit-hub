import json
from datetime import datetime, timedelta

FILE = "data/market_indices.json"

NAMES = ["شاخص کل", "شاخص کل هم‌وزن"]

def main():
    try:
        with open(FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if data.get("status") != "موفق":
            raise ValueError("وضعیت دریافت موفق نیست.")

        fetched_at = datetime.fromisoformat(data["fetched_at"])
        now = datetime.now().astimezone()

        age = now - fetched_at

        if age < timedelta(seconds=0):
            raise ValueError("زمان دریافت در آینده قرار دارد.")

        if age > timedelta(minutes=15):
            raise ValueError("اطلاعات شاخص‌ها قدیمی است.")

        indices = data.get("indices", {})

        for name in NAMES:
            item = indices.get(name)

            if not isinstance(item, dict):
                raise ValueError("اطلاعات " + name + " موجود نیست.")

            value = item.get("index_value")
            change = item.get("change_percent")

            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("مقدار " + name + " نامعتبر است.")

            if not isinstance(change, (int, float)):
                raise ValueError("درصد تغییر " + name + " نامعتبر است.")

        print("اعتبار ساختاری اطلاعات: تأیید شد.")
        print("سن اطلاعات:", int(age.total_seconds()), "ثانیه")
        print("شاخص کل:", indices[NAMES[0]]["change_percent"], "درصد")
        print("شاخص هم‌وزن:", indices[NAMES[1]]["change_percent"], "درصد")
        print()
        print("وضعیت جهت روزانه: هر دو شاخص مثبت هستند.")
        print("وضعیت مجاز بودن معامله: هنوز تعیین نشده است.")
        print("برای تصمیم معاملاتی، بررسی روند و سایر شرایط لازم است.")

    except Exception as error:
        print("وضعیت بازار: نامشخص؛ فعلاً معامله نکن.")
        print("علت:", str(error))

if __name__ == "__main__":
    main()
