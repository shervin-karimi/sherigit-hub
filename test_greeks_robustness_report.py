"""Offline stress checks, holdings of scenarios, reproducibility and safety boundaries."""

from datetime import datetime, timedelta
from io import StringIO
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import greeks_robustness_report as audit
import rank_candidates
import shadow_collector as collector
import shadow_watch as watch


class GreeksRobustnessReportTests(unittest.TestCase):
    def setUp(self):
        for api in ("socket.socket", "socket.getaddrinfo", "sqlite3.connect", "shadow_collector.fetch"):
            guard = patch(api, side_effect=AssertionError(f"offline audit called {api}"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_fixed_volatility_and_fixed_premium_are_distinct_for_call_and_put(self):
        for kind, sign in (("CALL", 1), ("PUT", -1)):
            with self.subTest(kind=kind):
                p = audit.base_point(kind)
                premium = audit.reprice(p, .4)["premium_model"]
                scenarios = audit.matrix(p, premium * .95, premium * 1.05, 99)["scenarios"]
                base = scenarios["baseline"]["refit"]
                rate = scenarios["rate_high"]
                self.assertAlmostEqual(base["sigma"], .4, places=8)
                self.assertEqual(rate["refit"]["premium_target"], base["premium_target"])
                self.assertAlmostEqual(rate["refit"]["premium_model"], premium, places=8)
                self.assertGreater(sign * (base["sigma"] - rate["refit"]["sigma"]), 0)
                self.assertGreater(sign * (rate["fixed_volatility"]["premium_model"] - premium), 0)
                self.assertEqual(rate["fixed_volatility"]["sigma"], base["sigma"])
                self.assertEqual(set(rate["refit"]["greeks"]), set(audit.GREEKS))
                self.assertAlmostEqual(scenarios["rate_if_effective"]["inputs"]["rate"], math.log1p(.4394))
                self.assertAlmostEqual(scenarios["sigma_up"]["fixed_volatility"]["sigma"], .5, places=8)
                self.assertNotIn("refit", scenarios["sigma_up"])

    def test_quote_selection_moves_iv_not_assumptions(self):
        p = audit.base_point("PUT", rate=0)
        data = audit.matrix(p, 5, 7, 98)["scenarios"]
        ordered = [data[name]["refit"] for name in ("bid", "quote_25", "baseline", "quote_75", "ask")]
        self.assertEqual([r["premium_target"] for r in ordered], [5, 5.5, 6, 6.5, 7])
        ivs = [r["sigma"] for r in ordered]
        self.assertTrue(all(a < b for a, b in zip(ivs, ivs[1:])))
        self.assertEqual(data["bid"]["inputs"], data["ask"]["inputs"])
        self.assertNotIn("fixed_volatility", data["ask"])
        self.assertEqual(data["spot_down"]["inputs"]["spot"], 99)
        self.assertEqual(data["closing_spot"]["inputs"]["spot"], 98)
        self.assertEqual(data["dividend"]["inputs"]["dividend"], .1)
        self.assertAlmostEqual(data["day_less"]["inputs"]["years"], 89 / 365)

    def test_refit_failure_does_not_remove_fixed_sigma_values(self):
        # In this fixture the positive spot shock puts the fixed price below the bound.
        p = audit.base_point("CALL", spot=102)
        data = audit.matrix(p, 12.9, 13.1, 99)["scenarios"]
        r = data["spot_up"]
        self.assertIsNotNone(data["baseline"]["refit"]["sigma"])
        self.assertEqual(r["refit"]["status"], "below_theoretical_bound")
        self.assertTrue(all(x is None for x in r["refit"]["greeks"].values()))
        self.assertGreater(r["fixed_volatility"]["premium_model"], 13)

    def test_mathematical_bounds_are_separate_from_solver_range(self):
        for kind in ("CALL", "PUT"):
            rows = {r["case"]: r["refit"] for r in audit.stress_cases() if r["inputs"]["kind"] == kind}
            self.assertEqual(rows["above_upper_bound"]["bound_relation"], "above")
            self.assertEqual(rows["above_upper_bound"]["status"], "above_theoretical_bound")
            self.assertEqual(rows["at_upper_bound"]["status"], "near_upper_bound_unidentifiable")
            self.assertEqual(rows["above_sigma_cap"]["bound_relation"], "inside")
            self.assertEqual(rows["above_sigma_cap"]["status"], "outside_solver_range")
            self.assertEqual(rows["below_sigma_floor"]["status"], "outside_solver_range")
            self.assertEqual(rows["below_intrinsic"]["bound_relation"], "below")
            self.assertEqual(rows["expiry_zero"]["status"], "invalid_input")
            self.assertEqual(rows["sigma_floor"]["sigma"], audit.model.MIN_VOL)
            self.assertEqual(rows["sigma_cap"]["sigma"], audit.model.MAX_VOL)

    def test_cancellation_low_vega_and_intrinsic_resolution_are_diagnosed(self):
        for kind in ("CALL", "PUT"):
            rows = {r["case"]: r for r in audit.stress_cases() if r["inputs"]["kind"] == kind}
            tiny = rows["atm_tiny_width"]
            self.assertGreater(tiny["atm_analytic_reference_premium"], 0)
            self.assertTrue(math.isclose(tiny["fixed_volatility"]["premium_model"],
                                         tiny["atm_analytic_reference_premium"], rel_tol=1e-14))
            self.assertAlmostEqual(tiny["refit"]["sigma"], .2, places=9)
            self.assertEqual(rows["near_intrinsic_one_ulp"]["refit"]["status"], "near_lower_bound_unidentifiable")
            near = rows["near_intrinsic_reconstruction"]["refit"]
            self.assertEqual(near["status"], "ok")
            self.assertEqual(near["premium_model"], near["premium_target"])
            low_vega = rows["near_intrinsic_low_vega"]
            self.assertEqual(low_vega["conditioning"]["status"], "perturbation_not_solved")
            self.assertGreater(low_vega["conditioning"]["linear_iv_change_percentage_points"], 5)

    def test_invalid_quotes_and_dates_are_not_repaired(self):
        row = dict(option_type="CALL", strike_price=100, underlying_last_price=100, underlying_closing_price=99,
                   best_bid=5, best_ask=6, remaining_days=90, maturity_date="20261121")
        receipt = "2026-11-22T10:30:00+03:30"
        self.assertEqual(audit.contract_matrix(row, receipt)["input_failure"], "nonpositive_calendar_time")
        row.update(maturity_date="20270127", best_bid=7)
        self.assertEqual(audit.contract_matrix(row, receipt)["input_failure"], "unusable_bid_ask")

    def test_outputs_repeat_without_archive_writes_or_eligibility_promotion(self):
        with TemporaryDirectory() as temp:
            stream = Path(temp)
            for day in (21, 22):
                at = datetime.fromisoformat(f"2026-11-{day}T09:00:00+00:00")
                raw = collector.json_bytes({"instrumentOptMarketWatch": [{
                    "insCode_C": "60210342490439455", "insCode_P": "124", "uaInsCode": "65883838195688438",
                    "strikePrice": 100, "contractSize": 1000, "endDate": "20270127", "remainedDay": 67,
                    "pDrCotVal_UA": 101, "pClosing_UA": 102,
                    **{f"{k}_{s}": v for s in "CP" for k, v in (
                        ("pMeDem", 20), ("pMeOf", 21 + day - 21), ("qTitMeDem", 3),
                        ("qTitMeOf", 4), ("zTotTran", 50), ("qTotTran5J", 100))},
                }]})
                with collector.locked(stream, exclusive=True) as fd:
                    collector.append(stream, fd, collector.read_stream(stream), raw, at - timedelta(seconds=1), at, {})
            before = {p: watch.digest(p.read_bytes()) for p in stream.glob("*/*.json")}
            outputs = []
            for _ in range(2):
                with patch("sys.stdout", new_callable=StringIO) as stdout:
                    self.assertEqual(audit.main(["--stream", temp, "--format", "json"]), 0)
                    outputs.append(stdout.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(before, {p: watch.digest(p.read_bytes()) for p in stream.glob("*/*.json")})
        out = json.loads(outputs[0])
        self.assertEqual(out["observations"][0]["receipt_tehran"], "2026-11-21T12:30:00+03:30")
        self.assertEqual(out["observations"][0]["targets"]["60210342490439455"]["result"]["calendar_days"], 67)
        self.assertFalse(out["execution_eligible"])
        self.assertFalse(out["market_validation"])
        self.assertEqual(out["greeks_assumption_status"], "experimental_unverified")
        self.assertEqual([s["existing_rank_eligible"] for s in out["observations"]], [0, 0])
        self.assertIn("غیرقابل اجرا", audit.markdown(out))
        eligible, _ = rank_candidates.check_contract(dict(
            option_type="CALL", delta=.60, remaining_days=30, spread_percent=1, trade_count=30,
            trade_volume=1, best_bid=9, best_ask=10, bid_volume=1, ask_volume=1, ask_time_value=1,
            iv_status="ok", greeks_assumption_status=out["greeks_assumption_status"],
        ))
        self.assertFalse(eligible)

    def test_revision_comparison_reports_changes_without_promoting_or_accepting_other_captures(self):
        item = {"refit_premium": 13, "premium_at_common_sigma": 15, "greeks": None}
        old = {"rows": {"123": item | {"iv": None, "iv_failure_reason": "price_reconstruction_failed"}},
               "receipt_tehran": "fixture", "body_sha256": "body", "seal_sha256": "seal",
               "screening_status": "existing_replay", "screened_ids": ["123"], "ranked_ids": []}
        new = old | {"rows": {"123": item | {"iv": .2, "iv_failure_reason": None}}}
        baseline = json.dumps({"schema_version": 1, "model_sha256": audit.BEFORE_MODEL_SHA256,
                               "captures": {"manual:1": old}}).encode()
        current = {"model_sha256": "new", "captures": {"manual:1": new}}
        result = audit.compare_revision(current, baseline)["captures"]["manual:1"]
        self.assertEqual(result["iv_gained_ids"], ["123"])
        self.assertEqual(result["material_difference_ids"], ["123"])
        self.assertEqual(result["new_rank_promotions"], [])
        new["body_sha256"] = "different"
        with self.assertRaisesRegex(watch.ArchiveError, "inputs differ"):
            audit.compare_revision(current, baseline)
