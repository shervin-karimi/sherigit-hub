import urllib.request
import json

url = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/%D8%AE%D9%88%D8%AF%D8%B1%D9%88"

headers = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

try:
    req = urllib.request.Request(
        url,
        headers=headers
    )

    with urllib.request.urlopen(req, timeout=20) as response:

        print("HTTP Status:", response.status)

        raw_data = response.read().decode("utf-8")

        data = json.loads(raw_data)

        print("\nConnection successful!")

        print("\nFirst response:")

        print(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            )[:3000]
        )

except Exception as e:
    print("ERROR:", repr(e))
