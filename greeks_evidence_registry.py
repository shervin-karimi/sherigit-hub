"""Read a pinned local evidence registry and render research-only gaps to stdout."""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys


REGISTRY = Path(__file__).resolve().parent / "evidence" / "iran-options-v1.json"
TOPICS = ("strike", "multiplier", "currency_unit", "expiry", "exercise_settlement",
          "quote_timestamps", "interest_rate", "dividends")
CLASSES = {
    "official_primary": ("اسناد رسمی اولیه", {"missing_document", "document_unverified"}),
    "secondary_republication": ("بازنشرهای ثانویه", {"unretrieved_lead", "local_summary_unverified"}),
    "historical_market_observation": ("مشاهدات تاریخی بازار و سرنخ‌های آن", {"unretrieved_lead", "observation_unverified"}),
    "unsupported_assumption": ("فرض‌های بدون پشتوانه", {"unverified"}),
}
UNAVAILABLE = {"missing_document", "unretrieved_lead"}
STATUS_FA = {
    "missing_document": "سند در دست نیست",
    "document_unverified": "سند ثبت شده؛ تأیید مستقل نشده",
    "unretrieved_lead": "سرنخ واکشی و بررسی نشده",
    "local_summary_unverified": "خلاصهٔ محلیِ تأییدنشده",
    "observation_unverified": "مشاهدهٔ تأییدنشده",
    "unverified": "تأییدنشده",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def load_registry(path=REGISTRY):
    """Return a fresh in-memory snapshot and its byte-level provenance; never write."""
    raw = path.read_bytes()
    data = json.loads(raw)
    require(isinstance(data, dict) and all(
        type(data.get(k)) is int and data[k] == 1 for k in ("schema_version", "registry_version")
    ), "unsupported evidence registry version")
    topics, records = data.get("topics"), data.get("records")
    require(text(data.get("scope_fa")) and isinstance(topics, dict) and set(topics) == set(TOPICS)
            and isinstance(records, list), "invalid registry scope, topics or records")
    for topic in topics.values():
        require(isinstance(topic, dict) and all(text(topic.get(k)) for k in ("label_fa", "gap_fa")),
                "missing topic label or verification gap")

    ids = set()
    for record in records:
        require(isinstance(record, dict) and all(text(record.get(k)) for k in
                ("id", "publisher", "classification", "verification_status", "claim_fa")),
                "invalid evidence record")
        key, classification, status = (record[k] for k in ("id", "classification", "verification_status"))
        require(key not in ids, f"duplicate evidence ID: {key}")
        ids.add(key)
        require(classification in CLASSES and status in CLASSES[classification][1],
                f"invalid evidence classification/status: {key}")
        url = record.get("source_url")
        require("source_url" in record and (url is None and classification == "unsupported_assumption"
                and text(record.get("local_reference")) or text(url) and url.startswith("https://")),
                f"missing source provenance: {key}")
        for date in ("publication_date", "observation_date"):
            require(date in record and (record[date] is None or text(record[date])), f"invalid {date}: {key}")
        covered = record.get("topics")
        require(isinstance(covered, list) and covered and all(t in TOPICS for t in covered),
                f"invalid record topics: {key}")
        assertions = record.get("assertions", [])
        require(isinstance(assertions, list) and (not assertions or
                status not in UNAVAILABLE and classification != "unsupported_assumption"),
                f"invalid assertions for evidence status: {key}")
        seen = set()
        for claim in assertions:
            require(isinstance(claim, dict) and claim.get("topic") in covered
                    and all(text(claim.get(k)) for k in ("scope", "field", "value")),
                    f"invalid comparable assertion: {key}")
            match = tuple(claim[k] for k in ("topic", "scope", "field"))
            require(match not in seen, f"duplicate comparable assertion: {key}")
            seen.add(match)
    return data, sha256(raw).hexdigest()


def assess(data, digest):
    """Detect explicit comparable disagreements; never resolve or promote evidence.

    Curators use identical scope only for the same market/instrument, applicable
    date/version and measurement basis (including units/convention). Values must
    already be normalized; prose, missing fields and unrelated dates aren't compared.
    """
    claims = defaultdict(lambda: defaultdict(list))
    for record in data["records"]:
        for claim in record.get("assertions", []):
            key = tuple(claim[k] for k in ("topic", "scope", "field"))
            claims[key][claim["value"]].append(record["id"])
    topics = {}
    for topic in TOPICS:
        records = [r for r in data["records"] if topic in r["topics"]]
        conflicts = [
            {"scope": scope, "field": field, "values": {v: sorted(ids) for v, ids in sorted(values.items())}}
            for (item, scope, field), values in sorted(claims.items()) if item == topic and len(values) > 1
        ]
        primary = any(r["classification"] == "official_primary" and
                      r["verification_status"] == "document_unverified" for r in records)
        topics[topic] = {
            "status": "unresolved_conflict" if conflicts else "unverified" if primary else "missing_primary",
            "primary_document_present": primary,
            "source_ids": [r["id"] for r in records],
            "conflicts": conflicts,
            "gap_fa": data["topics"][topic]["gap_fa"],
        }
    return {
        "registry_version": data["registry_version"], "sha256": digest, "topics": topics,
        "greeks_assumption_status": "experimental_unverified", "execution_eligible": False,
    }


def current_assessment():
    return assess(*load_registry())


def markdown(data, assessment):
    statuses = {"unresolved_conflict": "تعارض حل‌نشده", "unverified": "سند اولیه تأیید نشده",
                "missing_primary": "سند اولیه در دست نیست"}
    lines = [
        "# شکاف شواهد مشخصات اختیار و فرض‌های Greeks", "",
        f"دامنه: {data['scope_fa']}", "",
        f"رجیستری نسخهٔ `{assessment['registry_version']}`، SHA-256: `{assessment['sha256']}`",
        "",
        "بازتولید آفلاین از ریشه: `python -B greeks_evidence_registry.py`؛ فقط از فایل رجیستری، بدون خواندن آرشیو یا واکشی URL.",
        "",
        "**فقط پژوهشی — `experimental_unverified` و `execution_eligible=false`.** هیچ شاهدی خودکار "
        "به `validated` ارتقا نمی‌یابد. نرخ 43.94% همچنان تأییدنشده است و بازده تاریخی جایگزین آن نمی‌شود. "
        "نبود تعارضِ قابل مقایسه دلیل تطابق یا تأیید نیست.", "",
        "## شواهد لازم برای تأیید مستقل", "",
    ]
    for key, topic in assessment["topics"].items():
        lines.append(f"- **{data['topics'][key]['label_fa']} — {statuses[topic['status']]}:** {topic['gap_fa']}")
        lines.append("  - شناسهٔ مراجع: " + "، ".join(f"`{i}`" for i in topic["source_ids"]))
        for conflict in topic["conflicts"]:
            values = "؛ ".join(f"`{v}` از {', '.join(ids)}" for v, ids in conflict["values"].items())
            lines.append(f"  - تعارض `{conflict['scope']}`، `{conflict['field']}`: {values}")
    lines += [
        "", "اصل سند، بند، ناشر، تاریخ انتشار/اثر و دامنهٔ شمول، زمان دریافت و هش نسخهٔ محلی را ثبت کنید؛ "
        "در اختلاف از مرجع صادرکننده استعلام و تا رفع اختلاف استفاده را مسدود کنید. «نامشخص» یعنی تاریخ در "
        "رجیستری احراز نشده است، نه اینکه تاریخی وجود ندارد.", "",
    ]
    for classification, (label, _) in CLASSES.items():
        lines += [f"## {label}", ""]
        for record in data["records"]:
            if record["classification"] != classification:
                continue
            published, observed = (record[k] or "نامشخص" for k in ("publication_date", "observation_date"))
            source = record["source_url"]
            link = f" — <{source}>" if source else ""
            status = record["verification_status"]
            lines += [
                f"- `{record['id']}` — {record['publisher']}{link} — {STATUS_FA[status]} (`{status}`).",
                f"  - انتشار: {published}؛ مشاهده: {observed}. {record['claim_fa']}",
            ]
            if record.get("local_reference"):
                lines.append(f"  - مرجع محلی: {record['local_reference']}.")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        data, digest = load_registry()
        sys.stdout.write(markdown(data, assess(data, digest)))
    except (OSError, ValueError) as error:
        print(f"Offline evidence report stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
