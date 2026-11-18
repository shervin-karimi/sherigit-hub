import json
import urllib.request

URL = "https://cdn.tsetmc.com/api/Index/GetIndexB1LastAll/All/1"

request = urllib.request.Request(
    URL,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    rows = data.get("indexB1", [])

    targets = {
        "32097828799138957": "شاخص کل",
        "67130298613737946": "شاخص کل هم‌وزن",
    }

    print("نتیجهٔ دریافت اطلاعات شاخص‌ها")
    print("=" * 40)

    found = set()

    for row in rows:
        code = str(row.get("insCode"))

        if code not in targets:
            continue

        found.add(code)

        name = targets[code]
        value = row.get("xDrNivJIdx004")
        change = row.get("xVarIdxJRfV")

        print("نام:", name)
        print("مقدار شاخص:", value)
        print("درصد تغییر:", change)
        print("-" * 40)

    for code, name in targets.items():
        if code not in found:
            print("اطلاعات پیدا نشد:", name)

except Exception as error:
    print("خطا در دریافت اطلاعات:", str(error))
