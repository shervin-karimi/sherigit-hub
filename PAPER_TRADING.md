# Offline paper trading

Use the `paper` commands in `trade_manager.py`. They never fetch market data,
connect to a broker, or submit orders. The scanner, its APIs, and the older
manual trade log are separate. A new paper account uses `data/paper_trading.db`
by default. Put `--db PATH` after `paper` to choose a different paper ledger.

```sh
python trade_manager.py paper init --equity 10000000 --stop-bps 300
python trade_manager.py paper --policy policy.json --catalog catalog.json buy --calendar sessions.json --order-id order-001 --quantity 1 --quote quote.json
python trade_manager.py paper --policy policy.json --catalog catalog.json mark --position-id 1 --quote updated-quote.json
python trade_manager.py paper --policy policy.json --catalog catalog.json sell --position-id 1 --order-id exit-001 --quantity 1 --quote updated-quote.json
python trade_manager.py paper --policy policy.json --catalog catalog.json monitor --feed feed.json --interval 15
python trade_manager.py paper monitor-status
python trade_manager.py paper incidents
python trade_manager.py paper pause
python trade_manager.py paper resume
python trade_manager.py paper --policy policy.json status
python trade_manager.py paper history
```

## Bot ownership and hard limits

Use a trusted local policy file with explicit positive integer limits. Example
values below are illustrative. Prices are per option unit in whole rials.
Order values include the quantity and contract multiplier.

```json
{
  "bot_id": "research-bot",
  "strategy_id": "paper-strategy",
  "limits": {
    "max_buy_quantity": 3,
    "max_buy_value": 500000,
    "max_buy_price": 500,
    "max_daily_purchase_value": 1000000,
    "max_open_exposure": 1000000,
    "max_sell_quantity": 3,
    "max_sell_value": 1000000,
    "min_sell_price": 1,
    "max_daily_loss": 100000
  }
}
```

Pass the policy to buy, mark, sell and monitor. The policy file and SQLite
ledger are trusted operator inputs. An opening order permanently associates
the position with its bot and strategy. Legacy positions without recorded
ownership and positions owned by other strategies are left untouched. Their
allocation still counts toward the account cap. Each exit requires a unique
order ID and records its owner, position, quantity, bid and quote evidence in
the same transaction as cash and remaining quantity. Exact retries cannot
execute a second fill. Omit sell quantity to request the entire remainder.

Daily limits apply across the ledger using Tehran dates. Sales never restore
the daily purchase budget. The loss cap uses gross realized losses from
evidenced fills, including partial exits. Reaching it or being unable to
verify the day's losses blocks new entries. Emergency pause also blocks entry,
and both leave risk-reducing exits subject to the sell limits. The existing
10% account exposure cap still applies. The stop-loss threshold produces an
alert. A required full exit blocked by sell limits or quote evidence stays
pending with a critical alert. An operator may submit a partial exit within
the configured limits before the trading cutoff.

The Persian JSON report from `paper --policy policy.json status` shows the
configured buy/sell limits and the remaining daily purchase and loss capacity.

## Offline replay

```sh
python trade_manager.py paper --policy policy.json --catalog catalog.json replay \
  --calendar sessions.json --equity 10000000 --events replay-events.json
```

Replay processes a JSON array in nondecreasing `at` timestamp order and uses
a fresh disposable ledger. Each event has `at` and `action`. A `screen` event
uses the shared shortlist rules, then submits a paper buy only when the
candidate passes. Supply `quote`, `quantity`, `order_id`, and a `market` object
with `observed_at` matching the quote time, `strike`,
`underlying_closing_price`, `trade_count`, `trade_volume`, `bid_volume` and
`ask_volume`. Strike and underlying price must use the same unit as the
quote. The catalog's last trading date provides remaining days, and the
CALL/PUT side determines intrinsic value. Screening is a research shortlist,
and all ledger checks still apply to entry. An explicit `buy` supplies
`quote`, `quantity` and `order_id`; a `sell` also supplies `position_id`
(and may use null quantity for the full remainder). A `monitor` supplies a
`quotes` array; `pause` and `resume` take no additional fields. The report
contains the final state, simulated fills, screening decisions, alerts and
rejected events. It separately reports actual evidence coverage from the
versioned research registry. The registry currently lacks independently
verified evidence for executable historical fills and official settlement.
Provide only separately provenance-checked historical provider quotes.
Archive receipt time alone does not establish an executable quote or
settlement. Label disposable synthetic replays with
`--input-class synthetic_fixture`. This label describes the data and does not
relax any validation.

The deterministic CLI lifecycle is exercised by
`python -B -m unittest test_paper_trading.PaperTradingTests.test_deterministic_end_to_end_call_and_put_cli_lifecycle`.
It screens and enters CALL and PUT positions, processes scripted partial exits
and automatic deadline exits, and leaves an unresolved PUT awaiting official
settlement.

## Input and trust boundary

`catalog.json` is an operator-controlled array of verified contracts. Each
record specifies `ins_code` (string), `symbol`, `option_type` (`CALL` or `PUT`),
`contract_size` (positive whole number), `source` (one allowed provider per
instrument), and `expires_at` (verified time after which trading is not
permitted, with UTC offset). Do not infer the cutoff from a date alone. Use the
same catalog for CLI and monitoring; restart the monitor after authorized
changes. A held position's verified cutoff cannot be changed by editing the
catalog file.

For new purchases add verified `first_trading_date` and `last_trading_date`
(Tehran dates in `YYYY-MM-DD`) to each contract. Use `buy --calendar sessions.json`
with the same dated calendar format as `shadow_schedule.py`. The calendar has
a nonempty `source` and a `sessions` object mapping each actual session date to
one or more Tehran-time intervals, such as `["09:00-12:30"]`. Include the
contract's first session and the actual sessions after it, omitting holidays
and cancellations. The quote must be from at or after the close of the fifth
listed session, counting the first. A date with multiple intervals counts once
and completes at its final close. Purchases are blocked when this evidence is
missing or the exit cutoff has already arrived.

Supply one local JSON object for a manual CLI operation, or a JSON array of
the same objects for the monitor's feed:

```json
{
  "ins_code": "12345678",
  "symbol": "نماد نمونه",
  "option_type": "CALL",
  "contract_size": 1000,
  "best_bid": 490,
  "best_ask": 500,
  "source": "verified-provider-id",
  "quote_class": "provider",
  "quoted_at": "2026-11-21T10:20:30+03:30",
  "received_at": "2026-11-21T10:20:32+03:30"
}
```

The timestamps are illustrative. Require actual provider quote time ≤ local
receipt time ≤ engine processing time (all timezone-aware, normalized to UTC),
and provider quote age of at most five minutes. The engine records processing
time itself. Never substitute fetch time, file mtime, or last trade time for
provider quote time. Manual or unverifiable records are rejected for marks and
fills; the scanner is not a verified quote producer. Source, identity and
multiplier are checked against the catalog, **not authenticated**: only a
separately validated producer should write the feed and its receipt timestamps.
Keep these files and the SQLite database in private, trusted local storage.
Have the producer replace snapshots atomically. Do not treat the `source` or
`quote_class` JSON label as proof of provenance.

## Risk and monitoring behavior

- Prices and account values are whole rials. Buys fill at ask; full exits fill
  at bid. Purchase cost is ask × number of contracts × contract size. The total
  purchase cost of open positions cannot exceed 10% of initial equity or use
  unavailable cash. Each buy requires an order ID: exact retries return the
  recorded position, while reuse with different input is rejected.
- Bid marks are saved on buy and `mark`. A loss of at least 3% of purchase cost
  triggers an alert (300 basis points by default, configurable at `init`).
  It does not trigger a sale. Stale marks report unknown unrealized P&L and
  alert status (`null`), not zero. Old ledger marks without provenance are
  also unknown until refreshed; historical entries are preserved, not verified.
- The exit deadline is the earlier of seven calendar days after the fill
  (UTC) and midnight at the start of the Tehran date ten calendar days before
  the verified last trading date. Existing open positions keep their previous
  deadline unless a matching catalog record supplies an earlier deadline,
  which the ledger records when it observes the position.
  At or after it, new purchases are blocked even if the monitor is stopped.
  The monitor records the exit request and a critical incident, and uses the
  newest valid bid available for that instrument; contradictory prices at the
  newest timestamp are not used. Without a valid quote, `EXIT_PENDING` remains
  open and reserves capital. The next valid quote processed by a running
  monitor closes it at the bid; `exit_late_us` is the exact processing-time
  delay past the deadline, in microseconds. No fills are fabricated or
  backdated. `mark` alone never closes a position; explicit `sell` remains
  available with a valid quote. `pause` persists across restarts and blocks new
  entries only.
- Trading eligibility ends at the earlier of the verified `expires_at`
  cutoff and the start of the Tehran date after `last_trading_date`. An open
  position then becomes `EXPIRY_REVIEW` (awaiting official settlement evidence)
  and keeps its capital reserved. A replayed quote, including one observed before
  the deadline, cannot book or backdate an exit after trading has ended. The
  cutoff and the observed settlement state persist across restarts. If a legacy
  position has no verified final trading date, the engine blocks quote-based
  exits and retains it as `EXIT_PENDING` once its holding deadline is reached.
- Historical realized P&L requires a stored, executable provider quote with
  matching price and timestamps before the verified cutoff. The ledger reports
  unknown realized P&L for historical closes without that evidence and leaves
  recorded cash and history intact for reconciliation. This engine has no
  settlement-evidence ingestion path. Official settlement requires a separate
  reconciliation workflow. Execution does not model fees, tax, order-book
  depth, partial fills or intraday market hours beyond a verified cutoff.
- One foreground monitor per local database. `--once` processes one cycle
  and exits. A successful cycle commits position changes, deduplicated
  incidents, and heartbeat together. `monitor-status` reports `unresponsive`
  if a previously running/error heartbeat is older than twice its interval
  plus five seconds. A detected gap is written to history on the next
  successful cycle. Incidents require local inspection; there is no remote
  alerting. A database/storage failure can prevent writing new incidents.
- Use Ctrl+C or SIGTERM for a graceful stop. On restart, open positions and
  unresolved incidents are read from SQLite. Android/Termux can suspend or
  kill the process during sleep, battery restrictions, or app shutdown; a
  wake lock may help but is not a guarantee. The monitor cannot observe or
  exit while stopped, cannot guarantee a fill at the deadline even while
  running, and is **not** a 24/7 service. Continuous operation requires
  reliable hosting, a verified feed and a separate watchdog.

Offline tests: `python -B -m unittest -v test_paper_trading`

## Repeatable offline tests

CI uses a Python 3.12/3.13 matrix and the pinned packages in `requirements-test.txt`.
The test runner installs the network and data-write guards before pytest
discovery. Loopback HTTP is allowed for local dashboard tests. Replay
subprocesses run with the same guards.

```sh
python -m pip install -r requirements-test.txt
python -B run_offline_tests.py
```

Use `python -B run_offline_tests.py --inventory /path/to/pytest-nodeids.txt`
to save the sorted list of collected test IDs (not runtime subtests). CI
publishes this file as the `pytest-nodeids-<python-version>` artifact for comparison across runs.
CI logs added and removed IDs relative to the latest successful branch run
(the target branch for pull requests), when its inventory is available, using
`compare_inventories.py`. Actions pinned to major v7 run on Node.js 24.
Before discovery, an isolated fixture checks that the guard rejects an external
connect event and writes to a protected path while allowing loopback. Another
fixture checks that guarded replay children stop immediately on a blocked write,
external connect or DNS lookup event. CI runs the suite and both browser checks
under `umask 022` so tests cannot depend on a permissive local default. The
browser checks use Chrome or Chromium with stubbed scan responses and real pilot
503 pages for missing, legacy and invalid reports; CI runs them as separate
steps: `python -B -m browser_tests.legacy_dashboard --check legacy` and
`python -B -m browser_tests.legacy_dashboard --check pilot` (omit `--check` to
run all checks locally).

The three TSETMC diagnostic scripts run their requests only when executed
directly. Test discovery imports them without contacting TSETMC. The
sensitivity test uses in-memory data. Run `sensitivity_test.py PATH` only
when intentionally analyzing a separately generated research file.
