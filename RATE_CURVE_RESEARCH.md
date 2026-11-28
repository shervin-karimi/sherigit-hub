# Offline discount-rate research (not integrated)

Scope: Iranian rial government single-redemption instruments, as research proxies
for the TSE/IFB equity and fund options model. No market connections, gold,
trade/order logic, service, calendar or pricing-engine changes. This tool never
promotes a result to validated or execution-eligible, even for user-supplied data.

## Data needed

Obtain the official exchange finalized close and trade volume for each bond on
one dated session, and official instrument terms confirming ID, maturity,
IRR price per bond, issuer and actual single redemption cash flow per bond.
Do not assume face amount from a symbol, use a coupon instrument as a zero, or
substitute the issuance power-preservation rate for the secondary-market yield.
Retain dated source documents, timestamps, corrections and raw file hashes
privately for independent review. A source string is only a claim, not proof.
The IFB YTM table is a research lead: https://www.ifb.ir/ytm.aspx .

JSON format: run the following offline command to print a **synthetic** example
of the input schema (do not reuse it as market data):

```sh
python -B -c 'import json; from rate_curve_research import demo_snapshot; print(json.dumps(demo_snapshot(), indent=2))'
```

`quote_date` is the Tehran session date of the finalized close, and
`available_date` is the Tehran date on which that exact snapshot and terms were
known; both must precede `--as-of`. All dates are Gregorian ISO YYYY-MM-DD.
Use `classification=unverified_observation` for real data not yet approved;
`synthetic` is reserved for simulated inputs. `price_source` and per-row
`terms_source` identify the documents. `trade_volume` must be positive but is
not a complete liquidity check. Maturity and ID duplicates are rejected rather
than averaged. No explicit quote-time, settlement-lag or liquidity model yet.

## Method and limitations

For a single cash flow F and observed price P, the continuous annual zero rate
at the quote date is ln(F/P)/(days/365). The effective annual equivalent is
expm1(r). For later valuation dates this prototype carries each zero rate flat
and recomputes remaining maturity and log discount. This roll-forward is a
research approximation, not an observed current market curve. Linear
interpolation in log-discount factors is used only inside the observed maturity
range; no extrapolation, including below the shortest bond. A maximum age of
four calendar days is a conservative prototype policy, not a market rule;
missing/invalid/expired/stale observations fail closed. No assumption that
sovereign yields are literally risk-free or that the curve is arbitrage-free.

## Reproduce

```sh
python -B rate_curve_research.py --demo --as-of 2026-11-28 --days 7 30 60 90 120 180
python -B -m unittest -v test_rate_curve_research.py
```

For a privately retained, independently sourced input:

```sh
python -B rate_curve_research.py --input /absolute/private/path/bonds.json --as-of YYYY-MM-DD --days 7 30 60 90
```

Output is JSON to stdout, including input SHA-256, raw node inputs, assumptions,
provenance, node IDs, and coverage status. No files are written by this CLI.
Keep the production rate and existing Greeks gate unchanged. Before integration,
validate real bond terms and prices independently; review liquidity, settlement,
curve construction/short-end coverage, point-in-time availability and model
sensitivity. Run full protected tests and CI, then request explicit approval of
any model change. Calendar/Preflight gates are independent and remain intact.
