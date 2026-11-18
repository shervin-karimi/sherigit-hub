import json
import urllib.request

url = (
    "https://cdn.tsetmc.com/api/ClosingPrice/"
    "GetClosingPriceDailyList/32097828820363860/5"
)

request = urllib.request.Request(
    url,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
)

try:
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read().decode("utf-8")

    print("پاسخ خام سرویس:")
    print(raw[:5000])

    data = json.loads(raw)

    print("\nکلیدهای اصلی پاسخ:")
    if isinstance(data, dict):
        print(list(data.keys()))
    else:
        print("نوع پاسخ:", type(data).__name__)

except Exception as error:
    print("خطا:", repr(error))
