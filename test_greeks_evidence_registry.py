"""Fixtures for the offline, non-promoting evidence registry."""

from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import greeks_contract_report as contract_report
import greeks_evidence_registry as registry


class EvidenceRegistryTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.socket", "socket.getaddrinfo", "sqlite3.connect"):
            guard = patch(name, side_effect=AssertionError(f"offline test called {name}"))
            guard.start()
            self.addCleanup(guard.stop)
        self.data, self.digest = registry.load_registry()
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)

    def fixture(self):
        path = Path(self.scratch.name) / "fixture.json"
        path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        return path

    def claim(self, key, classification, status, topic, scope, field, value):
        self.data["records"].append({
            "id": key, "classification": classification, "verification_status": status,
            "publisher": "fixture", "source_url": f"https://example.invalid/{key}",
            "publication_date": "2026-11-22", "observation_date": None, "topics": [topic],
            "claim_fa": "نمونهٔ مصنوعی",
            "assertions": [{"topic": topic, "scope": scope, "field": field, "value": value}],
        })

    def assess_fixture(self):
        return registry.assess(*registry.load_registry(self.fixture()))

    def test_required_leads_are_separated_and_unretrieved(self):
        records = {r["source_url"]: r for r in self.data["records"] if r["source_url"]}
        for url, classification in (
            ("https://www.shakhesban.com/stocks/messages/269501", "secondary_republication"),
            ("https://hoghogh.ai/circulars/c-63f0dae0", "secondary_republication"),
            ("https://www.tasnimnews.ir/fa/news/1405/04/28/3650667/", "historical_market_observation"),
        ):
            with self.subTest(url=url):
                lead = records[url]
                self.assertEqual(lead["classification"], classification)
                self.assertEqual(lead["verification_status"], "unretrieved_lead")
                self.assertIsNone(lead["observation_date"])
                self.assertIsNone(lead["publication_date"])
                self.assertNotIn("assertions", lead)
        self.assertEqual({r["classification"] for r in self.data["records"]}, set(registry.CLASSES))

    def test_all_eight_topics_have_gaps_and_missing_primary_evidence(self):
        topics = registry.current_assessment()["topics"]
        self.assertEqual(set(topics), {"strike", "multiplier", "currency_unit", "expiry",
                                       "exercise_settlement", "quote_timestamps", "interest_rate", "dividends"})
        for topic in topics.values():
            self.assertEqual(topic["status"], "missing_primary")
            self.assertFalse(topic["primary_document_present"])
            self.assertTrue(topic["gap_fa"])
            self.assertEqual(topic["conflicts"], [])

    def test_conflict_between_sources_is_preserved_without_winner(self):
        scope = "TSE/test-series/2026-11-22/shares-per-contract"
        self.claim("notice", "official_primary", "document_unverified", "multiplier", scope, "size", "1000")
        self.claim("reprint", "secondary_republication", "local_summary_unverified", "multiplier", scope, "size", "500")
        self.claim("other_series", "official_primary", "document_unverified", "multiplier",
                   "TSE/other-series/2026-11-22/shares-per-contract", "size", "250")
        result = self.assess_fixture()
        item = result["topics"]["multiplier"]
        self.assertTrue(item["primary_document_present"])
        self.assertEqual(item["status"], "unresolved_conflict")
        self.assertEqual(item["conflicts"], [{"scope": scope, "field": "size",
                                             "values": {"1000": ["notice"], "500": ["reprint"]}}])
        self.assertIn("تعارض حل‌نشده", registry.markdown(self.data, result))
        self.assertFalse(result["execution_eligible"])

    def test_even_agreement_with_primary_document_never_promotes(self):
        scope = "TSE/test-series/2026-11-22/shares-per-contract"
        self.claim("notice", "official_primary", "document_unverified", "multiplier", scope, "size", "1000")
        self.claim("reprint", "secondary_republication", "local_summary_unverified", "multiplier", scope, "size", "1000")
        result = self.assess_fixture()
        self.assertEqual(result["topics"]["multiplier"]["status"], "unverified")
        self.assertEqual(result["topics"]["multiplier"]["conflicts"], [])
        self.assertEqual(result["greeks_assumption_status"], "experimental_unverified")
        self.assertFalse(result["execution_eligible"])

    def test_historical_yield_never_replaces_experimental_rate(self):
        self.claim("observed_yield", "historical_market_observation", "observation_unverified", "interest_rate",
                   "treasury/test-bill/2026-07-19/effective-annual", "yield", "0.31")
        result = self.assess_fixture()
        rate = next(r for r in self.data["records"] if r["id"] == "model_rate")
        self.assertEqual(rate["verification_status"], "unverified")
        self.assertEqual(result["topics"]["interest_rate"]["status"], "missing_primary")
        self.assertEqual(result["topics"]["interest_rate"]["conflicts"], [])
        self.assertEqual(contract_report.model.RISK_FREE_RATE, 0.4394)
        self.assertEqual(contract_report.SCENARIOS[0][2], 0.4394)

    def test_invalid_status_and_absent_provenance_fail_closed(self):
        record = self.data["records"][0]
        record["verification_status"] = "validated"
        with self.assertRaisesRegex(ValueError, "classification/status"):
            self.assess_fixture()
        record["verification_status"] = "missing_document"
        record.pop("source_url")
        with self.assertRaisesRegex(ValueError, "source provenance"):
            self.assess_fixture()

    def test_cli_is_reproducible_and_registry_is_read_only(self):
        path = self.fixture()
        before = path.read_bytes()
        path.chmod(0o400)
        outputs = []
        load = registry.load_registry
        with patch.object(registry, "load_registry", side_effect=lambda: load(path)):
            for _ in range(2):
                with patch("sys.stdout", new_callable=StringIO) as output:
                    self.assertEqual(registry.main([]), 0)
                    outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertIn(sha256(before).hexdigest(), outputs[0])
        self.assertIn("43.94% همچنان تأییدنشده", outputs[0])
