"""Offline sensitivity and numerical audit; no inputs or outputs for trading."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import sys

import calculate_greeks as model
import greeks_evidence_registry as evidence
import market_data_quality as quality
from options_model_sensitivity import GREEKS
import shadow_collector as collector
import shadow_watch as watch


LABELS = {
    "baseline": "مبنا", "rate_low": "نرخ پیوسته ۳۰٪", "rate_high": "نرخ پیوسته ۵۰٪",
    "rate_if_effective": "۴۳٫۹۴٪ مؤثر ← نرخ پیوسته", "dividend": "بازده سود پیوسته ۱۰٪",
    "day_less": "یک روز کمتر", "day_more": "یک روز بیشتر", "one_day": "یک روز تا سررسید",
    "spot_down": "پایه ۱٪ کمتر", "spot_up": "پایه ۱٪ بیشتر", "closing_spot": "پایانی به‌جای آخر پایه",
    "sigma_down": "نوسان ۱۰ واحد درصد کمتر", "sigma_up": "نوسان ۱۰ واحد درصد بیشتر",
    "bid": "خرید", "quote_25": "۲۵٪ فاصلهٔ خرید تا فروش", "quote_75": "۷۵٪ فاصلهٔ خرید تا فروش", "ask": "فروش",
}
MODES = ("refit", "fixed_volatility")
METRICS = ("premium_model", "sigma", *GREEKS)
PERTURBATION = 0.001
HIGH_IV_SHIFT = 0.05
BEFORE_MODEL_SHA256 = "a9f598a3cc8acffae855b221f381cf1e23d9072be9e1be4489a1fdcb666e592e"
COMPARISON_SIGMA = .40
MATERIAL_IV_PP = .01
MATERIAL_RAW_PREMIUM = .01


def pricing_args(point):
    return tuple(point[k] for k in ("kind", "spot", "strike", "years", "rate", "dividend"))


def theoretical_bounds(point):
    return model.price_bounds(*pricing_args(point))


def reprice(point, sigma):
    """Use supplied sigma for price and all Greeks; no calibration in this path."""
    args = pricing_args(point)
    price = model.bs_price(*args, sigma) if sigma is not None else None
    greeks = model.calculate_greeks(*args, sigma) if price is not None else None
    return {
        "sigma": sigma, "premium_model": price, "greeks": {k: greeks[k] if greeks else None for k in GREEKS},
        "status": "no_baseline_iv" if sigma is None else "invalid_pricing_inputs" if price is None
        else "greeks_unavailable" if greeks is None else "ok",
    }


def fit(point, target):
    """Calibrate to the supplied premium; report bounds separately from solver status."""
    bounds = theoretical_bounds(point)
    relation = None
    if bounds is not None and model.finite_numbers(target):
        lo, hi = bounds
        relation = ("below" if target < lo else "at_lower" if target == lo else
                    "above" if target > hi else "at_upper" if target == hi else "inside")
    kind, *inputs = pricing_args(point)
    iv, reason = model._solve_iv(kind, target, *inputs)
    result = reprice(point, iv)
    return result | {
        "status": reason or result["status"], "premium_target": target, "theoretical_bounds": bounds,
        "bound_relation": relation, "reconstruction_error": (
            result["premium_model"] - target if result["premium_model"] is not None else None),
    }


def conditioning(point, baseline):
    """Local inverse-price stress. Flags are diagnostics, not gates or statistical errors."""
    premium, iv = baseline["premium_target"], baseline["sigma"]
    if iv is None:
        return {"status": "baseline_not_solved"}
    probes = {name: fit(point, premium * (1 + direction * PERTURBATION))
              for name, direction in (("minus", -1), ("plus", 1))}
    shifts = {k: v["sigma"] - iv if v["sigma"] is not None else None for k, v in probes.items()}
    vega = baseline["greeks"]["vega_per_1pct"]
    linear = premium * PERTURBATION / vega if vega and vega > 0 else None
    status = ("perturbation_not_solved" if None in shifts.values() else
              "large_iv_shift" if max(map(abs, shifts.values())) >= HIGH_IV_SHIFT else "both_solved_small_iv_shift")
    return {
        "status": status, "iv_fraction_change": shifts,
        "probe_status": {k: v["status"] for k, v in probes.items()},
        "linear_iv_change_percentage_points": linear if model.finite_numbers(linear) else None,
    }


def matrix(point, bid, ask, closing_spot):
    mid = model.quote_midpoint(bid, ask)
    base = fit(point, mid)
    scenarios = {}
    for key, updates in (
        ("baseline", {}),
        ("rate_low", {"rate": 0.30}), ("rate_high", {"rate": 0.50}),
        ("rate_if_effective", {"rate": math.log1p(model.RISK_FREE_RATE)}),
        ("dividend", {"dividend": 0.10}),
        ("day_less", {"years": point["years"] - 1 / model.DAYS_IN_YEAR}),
        ("day_more", {"years": point["years"] + 1 / model.DAYS_IN_YEAR}),
        ("one_day", {"years": 1 / model.DAYS_IN_YEAR}),
        ("spot_down", {"spot": point["spot"] * .99}), ("spot_up", {"spot": point["spot"] * 1.01}),
        ("closing_spot", {"spot": closing_spot or 0}),
    ):
        varied = point | updates
        scenarios[key] = {
            "inputs": varied, "refit": base if key == "baseline" else fit(varied, mid),
            "fixed_volatility": reprice(varied, base["sigma"]),
        }
    for key, shift in (("sigma_down", -.10), ("sigma_up", .10)):
        iv = base["sigma"]
        scenarios[key] = {"inputs": point, "fixed_volatility": reprice(point, iv + shift if iv is not None else None)}
    for key, weight in (("bid", 0), ("quote_25", .25), ("quote_75", .75), ("ask", 1)):
        scenarios[key] = {"inputs": point, "refit": fit(point, bid + weight * (ask - bid))}
    return {"scenarios": scenarios, "conditioning": conditioning(point, base)}


def base_point(kind, spot=100, strike=100, years=90 / 365,
               rate=model.RISK_FREE_RATE, dividend=model.DIVIDEND_YIELD):
    return dict(kind=kind, spot=spot, strike=strike, years=years, rate=rate, dividend=dividend)


def contract_point(row, receipt):
    # Use the original Tehran receipt date only, never archive's remainedDay fallback.
    t, days = model.get_time_to_expiry({
        "snapshot_received_at": receipt, "maturity_date": row["maturity_date"],
    })
    if model.quote_midpoint(row["best_bid"], row["best_ask"]) is None:
        return None, None, "unusable_bid_ask"
    if not row["underlying_last_price"]:
        return None, None, "missing_positive_underlying_last"
    if t is None:
        return None, None, "nonpositive_calendar_time"
    args = base_point(row["option_type"], row["underlying_last_price"], row["strike_price"], t)
    return args, days, None


def contract_matrix(row, receipt):
    args, days, failure = contract_point(row, receipt)
    if failure:
        return {"input_failure": failure}
    return {
        "input_failure": None, "calendar_days": days, "bid": row["best_bid"], "ask": row["best_ask"],
        "closing_spot": row["underlying_closing_price"],
        **matrix(args, row["best_bid"], row["best_ask"], row["underlying_closing_price"]),
    }


def metrics(row):
    return {k: row[k] for k in ("premium_model", "sigma")} | row["greeks"]


def summarize(evaluations):
    failures = Counter(e["input_failure"] for e in evaluations if e["input_failure"])
    used = [e for e in evaluations if not e["input_failure"]]
    summaries = {}
    for name in LABELS:
        modes = {}
        for mode in MODES:
            pairs = [(e["scenarios"]["baseline"][mode], e["scenarios"][name][mode])
                     for e in used if mode in e["scenarios"][name]]
            if not pairs:
                continue
            changes = {key: [] for key in METRICS}
            for a, b in pairs:
                old, new = metrics(a), metrics(b)
                for key in METRICS:
                    if old[key] is not None and new[key] is not None and (
                        key != "sigma" or (a["status"] == b["status"] == "ok")
                    ):
                        changes[key].append((new[key] - old[key]) * (100 if key == "sigma" else 1))
            modes[mode] = {
                "status_counts": dict(Counter(b["status"] for _, b in pairs)),
                "bound_relation_counts": dict(Counter(b["bound_relation"] or "not_evaluated" for _, b in pairs))
                if mode == "refit" else None,
                "paired_change_vs_mode_baseline": {
                    key: {"n": len(values), "median": median(values) if values else None}
                    for key, values in changes.items()
                },
                "max_absolute_reconstruction_error": max(
                    (abs(b["reconstruction_error"]) for _, b in pairs
                     if b.get("reconstruction_error") is not None), default=None),
            }
        summaries[name] = modes
    return {
        "contracts": len(evaluations), "usable_inputs": len(used), "input_failures": failures,
        "conditioning_counts": dict(Counter(e["conditioning"]["status"] for e in used)),
        "scenarios": summaries,
    }


def stress_cases():
    """Stress production math against exact ATM zero-carry prices and explicit limits."""
    rows = []
    for kind in ("CALL", "PUT"):
        atm = base_point(kind, years=1, rate=0, dividend=0)
        itm = atm | ({"spot": 150} if kind == "CALL" else {"strike": 150}) | {"years": 7 / 365}
        otm = atm | ({"strike": 1e100} if kind == "CALL" else {"spot": 1e100})
        cases = (
            ("atm_reference", atm, .4, None), ("expiry_one_day", atm | {"years": 1 / 365}, .4, None),
            ("expiry_zero", atm | {"years": 0}, .4, 1), ("atm_tiny_width", atm | {"years": 1e-32}, .2, None),
            ("sigma_floor", atm, model.MIN_VOL, None), ("sigma_cap", atm, model.MAX_VOL, None),
            ("below_sigma_floor", atm, model.MIN_VOL / 2, None), ("above_sigma_cap", atm, model.MAX_VOL + .1, None),
            ("otm_underflow", otm, model.MAX_VOL, None),
            ("below_intrinsic", itm, None, 49.99), ("near_intrinsic_one_ulp", itm, None, math.nextafter(50, math.inf)),
            ("near_intrinsic_reconstruction", itm, None, 50 + 1e-12),
            ("near_intrinsic_low_vega", itm, None, 50 + 1e-8),
            ("at_upper_bound", atm, None, 100), ("above_upper_bound", atm, None, 100.01),
        )
        for case, point, sigma, supplied in cases:
            priced = reprice(point, sigma) if sigma is not None else None
            generated = priced["premium_model"] if priced is not None else None
            # For ATM, r=q=0: S [2Φ(w/2)-1] = S erf(w/(2√2)); avoids CDF subtraction.
            reference = (point["spot"] * math.erf(sigma * math.sqrt(point["years"]) / (2 * math.sqrt(2)))
                         if sigma is not None and point["spot"] == point["strike"] and point["years"] > 0 else None)
            target = reference if case == "atm_tiny_width" else generated if supplied is None else supplied
            fitted = fit(point, target)
            rows.append({
                "case": case, "inputs": point, "fixed_volatility": priced, "refit": fitted,
                "atm_analytic_reference_premium": reference,
                "fixed_minus_reference_premium": generated - reference if reference is not None else None,
                "fitted_minus_input_sigma": (fitted["sigma"] - sigma
                                             if sigma is not None and fitted["sigma"] is not None else None),
                "conditioning": conditioning(point, fitted),
            })
    return rows


def comparison_input(source):
    """Record per-ID outputs from the exact archived inputs, compatible with the prior solver."""
    result = {"schema_version": 1, "model_sha256": watch.digest(Path(model.__file__).read_bytes()), "captures": {}}
    for obs in source["observations"]:
        rows, screened, ranked = {}, [], []
        for code, row in obs["contracts_by_id"].items():
            screening = row["screening"] or {}
            if screening.get("candidate_eligible"):
                screened.append(code)
            if screening.get("rank_eligible"):
                ranked.append(code)
            point, _, failure = contract_point(row, obs["receipt_tehran"])
            if failure:
                continue
            kind, *args = pricing_args(point)
            mid = model.quote_midpoint(row["best_bid"], row["best_ask"])
            iv, reason = model._solve_iv(kind, mid, *args)
            g = model.calculate_greeks(kind, *args, iv) if iv is not None else None
            rows[code] = {
                "iv": iv, "iv_failure_reason": reason,
                "premium_at_common_sigma": model.bs_price(kind, *args, COMPARISON_SIGMA),
                "refit_premium": model.bs_price(kind, *args, iv) if iv is not None else None,
                "greeks": {k: g[k] for k in GREEKS} if g is not None else None,
            }
        result["captures"][obs["capture_id"]] = {
            **{k: obs[k] for k in ("receipt_tehran", "body_sha256", "seal_sha256")},
            "screening_status": obs["screening_status"],
            "screened_ids": sorted(screened), "ranked_ids": sorted(ranked), "rows": rows,
        }
    return result


def compare_revision(current, baseline_raw):
    baseline = json.loads(baseline_raw)
    watch.require(baseline.get("schema_version") == 1 and baseline.get("model_sha256") == BEFORE_MODEL_SHA256
                  and baseline["captures"].keys() == current["captures"].keys(), "unexpected numerical baseline")
    captures = {}
    for capture, new in current["captures"].items():
        old = baseline["captures"][capture]
        watch.require(all(old[k] == new[k] for k in ("receipt_tehran", "body_sha256", "seal_sha256", "screening_status"))
                      and old["rows"].keys() == new["rows"].keys(), f"numerical comparison inputs differ: {capture}")
        gained, lost, reasons, material = [], [], {}, []
        difference = dict.fromkeys(("iv_percentage_points", "premium_at_common_sigma", "refit_premium", *GREEKS))
        paired = 0
        for code, b in sorted(new["rows"].items()):
            a = old["rows"][code]
            if a["iv"] is None and b["iv"] is not None:
                gained.append(code)
            if a["iv"] is not None and b["iv"] is None:
                lost.append(code)
            if a["iv_failure_reason"] != b["iv_failure_reason"]:
                reasons[code] = [a["iv_failure_reason"], b["iv_failure_reason"]]
            metrics = {"iv_percentage_points": (a["iv"], b["iv"], 100)}
            metrics.update((k, (a[k], b[k], 1)) for k in ("premium_at_common_sigma", "refit_premium"))
            if a["greeks"] is not None and b["greeks"] is not None:
                metrics.update((k, (a["greeks"][k], b["greeks"][k], 1)) for k in GREEKS)
            paired += a["iv"] is not None and b["iv"] is not None
            if any((x is None) != (y is None) for x, y, _ in metrics.values()):
                material.append(code)
            for key, (x, y, scale) in metrics.items():
                if x is None or y is None:
                    continue
                change = abs(y - x) * scale
                difference[key] = max(difference[key] or 0, change)
                if (key == "iv_percentage_points" and change >= MATERIAL_IV_PP) or (
                    key in ("premium_at_common_sigma", "refit_premium") and change >= MATERIAL_RAW_PREMIUM
                ):
                    material.append(code)
        old_ranked, new_ranked = set(old["ranked_ids"]), set(new["ranked_ids"])
        captures[capture] = {
            "old_iv_solved": sum(r["iv"] is not None for r in old["rows"].values()),
            "new_iv_solved": sum(r["iv"] is not None for r in new["rows"].values()),
            "paired_iv_solved": paired, "iv_gained_ids": gained, "iv_lost_ids": lost,
            "iv_failure_reason_changes": reasons, "max_absolute_changes": difference,
            "material_difference_ids": sorted(set(material)),
            "screening_ids_unchanged": old["screened_ids"] == new["screened_ids"],
            "old_ranked_ids": sorted(old_ranked), "new_ranked_ids": sorted(new_ranked),
            "new_rank_promotions": sorted(new_ranked - old_ranked),
        }
    return {"baseline_input_sha256": watch.digest(baseline_raw), "old_model_sha256": baseline["model_sha256"],
            "new_model_sha256": current["model_sha256"], "common_sigma": COMPARISON_SIGMA,
            "material_iv_change_pp": MATERIAL_IV_PP, "material_premium_change_raw": MATERIAL_RAW_PREMIUM,
            "captures": captures}


def build_report(stream=collector.STREAM, baseline_raw=None):
    source = quality.build_report(stream)
    observations = []
    for obs in source["observations"]:
        rows = obs["contracts_by_id"]
        values = {code: contract_matrix(row, obs["receipt_tehran"]) for code, row in rows.items()}
        observations.append({
            **{k: obs[k] for k in ("capture_id", "receipt_tehran", "receipt_utc", "body_sha256", "seal_sha256",
                                    *quality.TIME_FIELDS)},
            "by_type": {kind: summarize([values[c] for c, row in rows.items() if row["option_type"] == kind])
                        for kind in ("CALL", "PUT")},
            "existing_rank_eligible": (obs["summary"]["screening"] or {}).get("rank_eligible"),
            "targets": {code: {"symbol": symbol, "result": values.get(code)} for code, symbol in quality.TARGETS.items()},
        })
    names = (*watch.MODEL_FILES, "shadow_watch.py", "shadow_collector.py", "greeks_evidence_registry.py",
             "market_data_quality.py", "options_model_sensitivity.py", Path(__file__).name)
    return {
        "schema_version": 2, "dataset_class": watch.DATA_CLASS, "source_url": watch.SOURCE,
        "execution_eligible": False, "greeks_assumption_status": "experimental_unverified",
        "time_basis": "Tehran_HTTP_receipt_calendar_days_not_exchange_quote_time", "market_validation": False,
        "units": {"sigma": "annual_fraction", "sigma_change_in_summary": "percentage_points",
                  "rate": "annual_continuous_fraction", "dividend": "annual_continuous_fraction",
                  "premium_model": "raw_unscaled_feed_price_units", "delta": "price_per_spot_unit",
                  "gamma": "delta_per_spot_unit", "theta_per_day": "price_per_calendar_day",
                  "vega_per_1pct": "price_per_volatility_percentage_point",
                  "rho_per_1pct": "price_per_continuous_rate_percentage_point"},
        "mode_definitions": {
            "refit": "fixed midpoint except quote selection; calibrate sigma then compute price and Greeks",
            "fixed_volatility": "reprice and compute Greeks with baseline fitted sigma, except explicit sigma shocks",
        },
        "conditioning_probe": {"relative_premium_shock": PERTURBATION, "large_iv_shift_fraction": HIGH_IV_SHIFT},
        "code_sha256": {name: watch.digest((watch.ROOT / name).read_bytes()) for name in names},
        "evidence_registry": evidence.current_assessment(), "scenario_labels_fa": LABELS,
        "observations": observations, "synthetic_stress": stress_cases(),
        "revision_comparison": compare_revision(comparison_input(source), baseline_raw) if baseline_raw is not None else None,
    }


def fmt(value, scale=1):
    return "—" if value is None else f"{value * scale:.6g}"


def markdown(report):
    mode_label = {"refit": "برازش", "fixed_volatility": "بازقیمت‌گذاری"}
    lines = [
        "# ممیزی آفلاین پایداری و حساسیت Greeks — اختیار TSE/IFB", "",
        "**پژوهشی؛ `experimental_unverified`؛ غیرقابل اجرا.** بازتولید: "
        "`python -B greeks_robustness_report.py --format markdown` یا `--format json`. "
        "دو ثبت دستی با زمان اصلی دریافت تهران؛ نه زمان مظنه یا پایان جلسه. هش بدنه و مهر زنجیره در JSON.", "",
        "- **برازش:** پریمیوم وسط ثابت و IV دوباره حل می‌شود؛ فقط در سناریوهای انتخاب قیمت، "
        "پایه/سررسید/نرخ/سود ثابت‌اند و هدف بین خرید و فروش جایگزین می‌شود. پریمیوم بازساخته باید نزدیک هدف باشد.",
        "- **بازقیمت‌گذاری:** IV مبنای همان ردیف ثابت است (به‌جز شوک صریح ±۱۰ واحد درصد نوسان)؛ "
        "پریمیوم و هر پنج Greek از همان نوسان محاسبه می‌شوند، نه از برازش مجدد. عدم برازش سناریوی متناظر مانع این مسیر نیست.",
        "- فقط متغیر نام‌برده عوض می‌شود؛ اعمال ثابت است. نرخ/سود سالانه پیوسته، زمان تقویمی ÷۳۶۵، "
        "و قیمت آخر پایه در مبنا. سناریوی مؤثر از `ln(1+0.4394)` استفاده می‌کند؛ این نرخ مشاهده‌شده نیست.",
        "- پریمیوم و Greeks پولی به واحد خام و بدون اعمال ضریب؛ دلتا تغییر قیمت بر واحد پایه، گاما تغییر دلتا بر واحد پایه، "
        "تتا بر روز تقویمی، وگا و رو بر یک واحد درصد نوسان و نرخ پیوسته. IV درصد سالانه است. "
        "ΔIV در خلاصه واحد درصد است؛ مخرج مجاز هر معیار جداگانه در JSON است.", "",
        "## پایداری عددی و نقاط شکست", "",
        "- کران تنزیل‌شدهٔ مدل از محدودهٔ حل‌گر (`0.01%–500%`) جداست؛ بالاتر از کران، فاصلهٔ غیرقابل‌تفکیک "
        "از کران و خارج از براکت، دلیل‌های جداگانه دارند.",
        "- نزدیک کران ذاتی، وگای کوچک یا دقت محدود قیمت می‌تواند IV را بدشرط کند. آزمون محلی با تغییر "
        "±۰٫۱٪ پریمیوم در ورودی‌های ثابت انجام شد؛ شکست یکی از دو برازش یا جابه‌جایی ≥۵ واحد درصد IV "
        "فقط هشدار پژوهشی است، نه شرط پذیرش. نبود هشدار تضمین پایداری نیست.",
        "- **اصلاح:** در ATM با نرخ/سود صفر و زمان `1e-32` سال، قیمت غیرصفر مرجع تحلیلی `S·erf(σ√T/(2√2))` "
        "حفظ شد. خارج از ATM، در صورت حذف دقت از انتگرال مثبت وگا استفاده می‌شود. برای پریمیوم `50+1e-12` "
        "برازش در مختصات ارزش زمانی موفق است؛ قیمت یک ULP بالاتر از کران هنوز قابل شناسایی تلقی نمی‌شود. "
        "نزدیک کران بالا برازش از فاصلهٔ مثبت و مستقیم تا سقف استفاده می‌کند.",
        "- برازش یا تطابق عددی نه تأیید سبک اعمال اروپایی/تسویه است، نه مدرک واحد پول/ضریب، نرخ/سود، "
        "منشأ مظنه و هم‌زمانی قیمت پایه. [شکاف شواهد](greeks-evidence-gaps-v1.fa.md).", "",
        "| آزمون مصنوعی | نوع | پریمیوم محاسبه‌شده / مرجع ATM | هدف برازش | IV٪ / نسبت به کران / نتیجه |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in report["synthetic_stress"]:
        f = row["refit"]
        p = (row["fixed_volatility"] or {}).get("premium_model")
        lines.append(f"| `{row['case']}` | {row['inputs']['kind']} | {fmt(p)} / {fmt(row['atm_analytic_reference_premium'])} | "
                     f"{fmt(f['premium_target'])} | {fmt(f['sigma'], 100)} / {f['bound_relation'] or '—'} / `{f['status']}` |")
    if (comparison := report.get("revision_comparison")) is not None:
        lines += ["", "## مقایسهٔ نسخهٔ قبل و بعد روی همان بدنه‌ها", "",
                  "برای بازتولید این بخش به دستور بالا `--baseline-input reports/greeks-robustness-v1-comparison-input.json` اضافه کنید.",
                  "مقایسه به شناسه و SHA-256 همان ذخیره‌ها مقید است. اختلاف بااهمیت پژوهشی: حداقل ۰٫۰۱ واحد "
                  "قیمت خام یا ۰٫۰۱ واحد درصد IV، یا تغییر امکان محاسبه؛ این‌ها نه tick بازارند نه گیت رتبه.",
                  "| ثبت | IV قدیم / جدید | افزوده / از دست رفته | بیشینهٔ ΔIV (واحد درصد) | بیشینهٔ Δپریمیوم (σ=۴۰٪) | ردیف بااهمیت | رتبه قدیم / جدید |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for capture, d in sorted(comparison["captures"].items()):
            delta = d["max_absolute_changes"]
            lines.append(f"| {capture} | {d['old_iv_solved']} / {d['new_iv_solved']} | "
                         f"{len(d['iv_gained_ids'])} / {len(d['iv_lost_ids'])} | {fmt(delta['iv_percentage_points'])} | "
                         f"{fmt(delta['premium_at_common_sigma'])} | {len(d['material_difference_ids'])} | "
                         f"{len(d['old_ranked_ids'])} / {len(d['new_ranked_ids'])} |")
    lines += ["", "## واچ‌لیست به تفکیک CALL/PUT", "",
              "همهٔ قراردادها جدا از غربال؛ قیمت‌های مثبت مرتب، پایهٔ آخر مثبت و زمان مثبت لازم‌اند. "
              "عمق برای محاسبهٔ پژوهشی شرط نیست. جفت‌های موفق هر روش با مبنای همان روش مقایسه می‌شوند.", ""]
    for obs in report["observations"]:
        lines += [f"### {obs['capture_id']} — `{obs['receipt_tehran']}`", "",
                  f"واجد رتبه طبق مسیر موجود: {fmt(obs['existing_rank_eligible'])}؛ غیرقابل اجرا در این گزارش.", ""]
        for kind, group in obs["by_type"].items():
            cond = "؛ ".join(f"`{k}`: {v}" for k, v in sorted(group["conditioning_counts"].items()))
            lines += [f"**{kind}** — {group['usable_inputs']} از {group['contracts']} ورودی قابل استفاده؛ {cond}", "",
                      "| سناریو / روش | جفت پریمیوم | Δپریمیوم | ΔIV | Δدلتا | Δگاما | Δتتا | Δوگا | Δرو |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
            for key in LABELS:
                modes = group["scenarios"][key]
                for mode in MODES:
                    if mode not in modes:
                        continue
                    summary = modes[mode]
                    changes = summary["paired_change_vs_mode_baseline"]
                    vals = " | ".join(fmt(changes[k]["median"]) for k in METRICS)
                    lines.append(f"| {LABELS[key]} / {mode_label[mode]} | {changes['premium_model']['n']} | {vals} |")
            lines.append("")
        for code, target in obs["targets"].items():
            lines += [f"#### {target['symbol']} — `{code}`", ""]
            out = target["result"]
            if out is None or out["input_failure"]:
                lines += ["در این دریافت غایب یا فاقد ورودی مدل.", ""]
                continue
            base = out["scenarios"]["baseline"]["inputs"]
            lines += [f"خرید / فروش: {out['bid']} / {out['ask']}؛ آخر / پایانی پایه: "
                      f"{base['spot']} / {out['closing_spot']}؛ اعمال {base['strike']}؛ {out['calendar_days']} روز.", "",
                      "| سناریو / روش | هدف | پریمیوم مدل | σ٪ | دلتا | گاما | تتا | وگا | رو | نتیجه |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
            for key in LABELS:
                scenario = out["scenarios"][key]
                for mode in MODES:
                    if mode not in scenario:
                        continue
                    row = scenario[mode]
                    values = metrics(row)
                    vals = " | ".join(fmt(values[k], 100 if k == "sigma" else 1) for k in METRICS)
                    lines.append(f"| {LABELS[key]} / {mode_label[mode]} | {fmt(row.get('premium_target'))} | {vals} | `{row['status']}` |")
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", type=Path, default=collector.STREAM)
    parser.add_argument("--format", choices=("markdown", "json", "comparison-input"), default="markdown")
    parser.add_argument("--baseline-input", type=Path, help="local comparison-input from the pinned old model")
    args = parser.parse_args(argv)
    try:
        if args.format == "comparison-input":
            report = comparison_input(quality.build_report(args.stream))
        else:
            report = build_report(args.stream, args.baseline_input.read_bytes() if args.baseline_input else None)
        if args.format != "markdown":
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        else:
            sys.stdout.write(markdown(report))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Offline Greeks robustness report stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
