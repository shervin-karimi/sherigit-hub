import unittest

import contract_diagnostics as audit


class ContractDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "lval30_UA": "خودرو", "uaInsCode": "65883838195688438",
            "endDate": "20270127", "contractSize": 1000, "strikePrice": 600,
            "lVal18AFC_C": "ضخود1164", "insCode_C": "70550167438703193",
            "lVal30_C": "اختيارخ خودرو-600-1405/11/07",
            "lVal18AFC_P": "طخود1164", "insCode_P": "56684276529805049",
            "lVal30_P": "اختيارف خودرو-600-1405/11/07",
        }

    def test_matches_both_sides_but_never_adjacent_expiry(self):
        for side in ("C", "P"):
            self.assertEqual(audit.notice_side(self.row, side), ("archive_family_match", []))
        old = dict(self.row, endDate="20261230", lVal30_C="اختیارخ خودرو-600-1405/10/09")
        self.assertEqual(audit.notice_side(old, "C"), ("different_series", []))
        unrelated = dict(self.row, lval30_UA="فملی", uaInsCode="123",
                         lVal30_C="اختیارخ فملی-600-1405/11/07")
        self.assertEqual(audit.notice_side(unrelated, "C"), ("unrelated", []))

    def test_conflicting_expiry_strike_size_and_identity_are_reported(self):
        row = dict(self.row, endDate="20261230", strikePrice=650, contractSize=500, uaInsCode="42")
        result, issues = audit.notice_side(row, "P")
        self.assertEqual(result, "incomplete_or_conflicting")
        self.assertCountEqual(issues, ["mismatch:endDate", "mismatch:long_name_strike",
                                      "mismatch:contractSize", "mismatch:underlying_id"])

    def test_missing_metadata_is_not_promoted(self):
        row = dict(self.row, endDate=None, lVal30_P=None, insCode_P="0", contractSize=None)
        result, issues = audit.notice_side(row, "P")
        self.assertEqual(result, "incomplete_or_conflicting")
        self.assertCountEqual(issues, ["missing:endDate", "missing:contractSize",
                                      "missing_or_invalid:option_id", "missing:long_name"])

    def test_observation_order_is_bounded_and_preserves_gate_flags_and_reasons(self):
        rejected = dict(candidate_eligible=True, rank_eligible=False, rank_rejection="reason",
                        trade_count=30, trade_volume=500, spread_percent=1.0)
        states = {f"{i:02}": dict(rejected) for i in reversed(range(13))}
        states["00"]["spread_percent"] = 2
        states["01"]["rank_eligible"] = True
        states["02"]["candidate_eligible"] = False
        states["12"]["trade_count"] += 1
        selected = audit.shortlist(states)
        self.assertEqual([key for key, _ in selected], ["12", *(f"{i:02}" for i in range(3, 12))])
        self.assertTrue(all(not row["rank_eligible"] and row["rank_rejection"] == "reason"
                            for _, row in selected))
