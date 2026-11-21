# Multi-market research architecture — interfaces only

The current scanner remains read-only; the current paper engine consumes
verified local data only. The interfaces below describe future boundaries,
not integrations, order permissions, or an implementation roadmap commitment.

| Boundary | Interface and invariants |
| --- | --- |
| Market data provider | `quotes(instrument_ids)`, `bars(query)`, `contract_metadata(id)`. Events include provider time, receipt/availability time, source, market and data-quality flags. Retain immutable raw input and revision history. Keep feed ingestion separate from execution. |
| Exchange and calendar | `session_at(utc_time)`, `next_session()`, `trading_cutoff(instrument)`, `settlement_rules(instrument)`. Version the exchange calendar, timezone rules, holidays, auctions, suspensions, and contract adjustments. A risk holding limit can be calendar-day based independently of trading sessions. |
| Instruments | Stable `(venue, instrument_id)` identifies a stock, ETF, CALL, or PUT. Version underlying, strike, expiration, exercise/settlement terms, lot size, multiplier and currency; symbols alone are not identity. |
| Money and currency | Use explicit currency and fixed-point minor units for money. Premium, contract multiplier and quantity are separate; never mix rials and tomans. Consolidate in a reporting currency using point-in-time FX with an age limit and provenance. |
| Portfolio and risk | Transactional ledger for balances, reservations, positions and fills; risk receives one consistent snapshot and returns allow/deny with reason. Apply instrument, currency, aggregate capital, liquidity and data freshness/health limits before simulated fills. |
| Backtesting | Replay only events whose **availability** time is no later than the simulated decision clock. Version calendars, market universes, corporate actions and revisions as known then. Fit only on past data; simulate spread, latency, depth and costs. Report assumptions, out-of-sample results and uncertainty. |
| Strategy and analytics | Versioned plugins consume read-only as-of inputs and emit signals or risk metrics, never execution commands. Isolate missing data, nondeterminism and plugin failure from the ledger. |
| Execution adapters | Default and sole implementation for this project phase is the local paper adapter. Any future broker adapter is disabled, absent from this deployment, and would require separate review, explicit authorization, credentials isolation and reconciliation. No fallback from paper to a broker. |

**Analytics candidates:** technical indicators (e.g. momentum, moving averages,
ATR) require point-in-time OHLCV and corporate-action handling. Greeks and
implied volatility require reliable option/underlying quotes, a validated pricing
model, consistent exercise terms, rates and dividend inputs; surface validation
needs historical chains and liquidity filters. Statistical risk (realized
volatility, stress testing, historical VaR/expected shortfall) requires
sufficient synchronized returns/FX history, regime and missing-data checks, and
out-of-sample calibration. Validate timestamps, units, coverage and stability
before using any metric as a risk gate.
