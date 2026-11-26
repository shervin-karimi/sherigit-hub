# Live shadow pilot

The Linux pilot reads the public TSETMC watchlist only in the supplied dated
Tehran sessions and configured collection windows. Each tick reserves its
attempt on disk before starting HTTP. It makes at most one request and enforces
a configured interval of at least 60 seconds across restarts, including failed
attempts. The observation archive stores raw bodies, HTTP receipts and an
append-only chain of seals. Duplicate bodies do not create another observation.
Inspect or restore a corrupt archive before continuing.

The day’s Persian JSON report is atomically replaced on each completed tick.
It includes the last valid snapshot’s full CALL/PUT shortlist and first
rejection reasons, coverage, bot-owned positions and exits, configured limits,
remaining daily buy capacity, per-order sell capacity when a valid bid is
available, and critical alerts. There is no daily sell limit in the existing
policy. The report shows this field as null and applies the existing per-order
and inventory limits. Status flags an incomplete or stale heartbeat after
three minutes. The archive and reports stay local, under the configured private
root. Full-chain validation increases tick cost as the archive grows. Monitor
runtime and disk space, and start a new private observation-only root when
retiring a large pilot archive after preserving it.

## Linux VPS setup

Use Python 3.10 or newer with system IANA timezone data and a dedicated
unprivileged Linux account. Place the checkout at `/opt/sherigit-hub` or adjust
both paths in the service unit. The following commands assume a user systemd
session. An administrator may enable lingering for that account if the timer
must run after logout.

Create `~/.config/sherigit-pilot/config.json` with mode 0600. Use absolute
paths and supply an operator-verified Tehran trading calendar in the dated
format expected by `shadow_schedule.py`. The windows below describe desired
collection hours and must be set for the deployment.

```json
{
  "root": "/home/pilot/.local/share/sherigit-pilot",
  "calendar": "/home/pilot/.config/sherigit-pilot/sessions.json",
  "windows": ["09:00-12:30"],
  "capture_interval_seconds": 120
}
```

```sh
python3 -B shadow_pilot.py check --config ~/.config/sherigit-pilot/config.json
install -d ~/.config/systemd/user
install -m 644 deploy/shadow-pilot.service deploy/shadow-pilot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shadow-pilot.timer
systemctl --user list-timers shadow-pilot.timer
python3 -B shadow_pilot.py status --config ~/.config/sherigit-pilot/config.json
journalctl --user -u shadow-pilot.service
systemctl --user disable --now shadow-pilot.timer
systemctl --user stop shadow-pilot.service
```

`check` makes no HTTP request or paper-ledger change. Read reports under
`<root>/reports/YYYY-MM-DD.json`. The service suppresses report bodies in the
journal. Back up the archive, report files, durable state and any opted-in
SQLite ledger securely. Respect the venue’s collection policy and provide
accurate clock synchronization. Omitted and out-of-session days do not fetch.

### Observation-only backup and restore drill

1. Stop the timer and service using the commands above. Keep them stopped
   throughout the drill. In a private backup directory, use `cp -a -- "$root"
   "$backup"` (with an unused destination) to copy the entire configured root,
   including `observations/`, `reports/` and `state.json`. Keep the verified
   calendar and configuration separately in private storage.
2. Record the source record count and final seal hash separately from the copy.
   Run the command below against the source, the backup and the staged restore.
   Require identical results. Checking the chain alone cannot detect a copy
   missing only its last records.
3. Restore to an unused staging directory as the pilot user, preserving modes.
   Once the archive matches, retain the previous root under a different name
   and move the staged copy to the configured path on the same filesystem.
   Run `shadow_pilot.py status --config PATH` and check that the durable capture
   timestamps and latest report match the backup. A historical heartbeat may
   legitimately be stale. Do not restore only the archive and discard state:
   state includes the last reserved HTTP attempt.

```sh
python3 -B shadow_pilot.py verify-archive --root "$root"
python3 -B shadow_pilot.py verify-archive --root "$backup" \
  --expected-count "$saved_count" --expected-head "$saved_head"
```

Use the first command to record `count` and `head_sha256`, and the second to
compare both values. A mismatch exits nonzero. For zero records the head is
`empty`. Repeat the comparison against the staged restore. A damaged, incomplete
or out-of-order archive, or malformed expected values, produce the stable output
`{"error": "archive verification failed"}` and exit 1; the path and raw error are
kept in the private journal, not in the command output.

The offline drill restores two synthetic observations and their report, rejects
a damaged copy and verifies the restored rate limit.

### Coordinated drill with an opted-in paper ledger

For an opted-in pilot, stop the timer and service before copying anything. The
ledger, the pilot root (including `trusted-batches/` and `state.json`) and the
external trust files must come from the same stopped point. Copy the SQLite
database together with any `-wal`, `-shm` or `-journal` sidecars, or use
`sqlite3.Connection.backup` into a new file while no writer is active. Use
`cp -a --` to preserve ownership and mode 0600; never keep only the main
database file while a writer was active.

Restore to an unused staging directory, verify the archive count and head, then
move the pilot root and the ledger back to their configured absolute paths.
Changing the ledger or key path changes the recorded paper scope, and the pilot
will refuse to continue with that root. After restore:

1. Run `shadow_pilot.py status --config PATH` and `verify-archive` as above.
2. Re-submit the last batch. The report must block it as already processed; the
   ledger history must not grow. A fresh batch must then be accepted once.
3. Compare the restored trusted-batch record with the backup. Do not delete
   `trusted-batches/` or reset `state.json` to force a replay.

The offline drill performs these checks with a synthetic position and a WAL
sidecar. Official settlement reconciliation remains blocked in `EXPIRY_REVIEW`.

## Independent paper evidence

The default is observation-only. TSETMC HTTP receipt time, HTTP headers and
research candidates cannot become executable paper quotes. Pilot ticks may
advance time-driven pending/expiry state for configured bot-owned positions
without pricing a fill. Official settlement reconciliation is outside the
pilot and remains blocked in `EXPIRY_REVIEW`.

To opt in only after reviewing a separately controlled producer, add a
`paper` object with absolute paths for `db`, `catalog`, `policy`, `feed`
and `key`. These must sit outside the observation root. Initialize a
dedicated paper ledger with `trade_manager.py paper init`, use files owned by
the pilot user with mode 0600, and keep the database and SQLite sidecars in a
private directory. The trusted producer must attest exchange quote and local
receipt timestamps, source, identity, multiplier and contract cutoff. For
entry signals it must independently check the screening facts. A signature
authenticates that producer’s assertions. Provenance still depends on the
operator's verification of the producer, the contract catalog and the
underlying official evidence.

The key contains 32–64 raw bytes provisioned outside Git. The producer
atomically replaces the feed with a JSON object containing `payload` and
`hmac_sha256`, the lowercase HMAC-SHA256 of the canonical UTF-8 payload
(`shadow_collector.json_bytes`, including its final newline). Payload schema 1
has `schema`, strictly increasing positive integer `sequence`, `issued_at`,
an independently auditable `evidence_ref`, `quotes`, and `entries`. Quotes
use the `PAPER_TRADING.md` schema. Each entry has `ins_code`, `market` (the
replay screening object), `quantity` and a stable unique `order_id`. Quotes
must match the catalog and the present Tehran session. The pilot applies
normal freshness, entry, quantity, capital, stop-loss, exit and expiry rules.
The public scanner submits no entry instructions. The pilot cannot settle or
send orders to a broker.

Keep a claimed batch available until `last_sequence` acknowledges it in
status. The batch digest and evidence reference are recorded locally before
paper writes. If the process stops after a paper write but before acknowledgment,
the same fresh batch can be retried without duplicating orders. Stale batches
require new evidence. A blocked required exit stays pending and raises a
critical alert. A sequence with conflicting evidence or an out-of-order
batch is rejected. Keep the same pilot root for the configured bot/strategy
and ledger; review its audit history before moving it or rotating trust.

The deterministic tests use temporary ledgers and synthetic HTTP responses.
They do not validate production market coverage, source authenticity, an
official trading calendar or settlement documents.

## Persian operational dashboard

Run the dashboard as the same dedicated account that writes the pilot reports.
The dashboard reads atomically published JSON files and scheduler state,
binds to loopback, and serves only GET and HEAD. It has no collector,
ledger-write or approval endpoint. Refresh the browser to read the latest
published report. The date, symbol, contract side and screening filters
work without JavaScript. Receipt time is shown separately from unverified
exchange time. The report may show the remaining purchase budget for a
historical date, while an expired sell-capacity estimate is shown as unknown.

Reports produced before this milestone lack the contract table. Let the
updated pilot complete a tick to publish today's compatible report. Keep
historical reports in private storage. The dashboard does not rewrite them
or read raw archive bodies to reconstruct them.

### Operator troubleshooting

When a report cannot be shown, the 503 page names the cause and one action.
The same table also covers the stable `verify-archive` failure output described
in the restore drill above:

| Message shown | Operator action |
| --- | --- |
| `گزارش این تاریخ هنوز منتشر نشده است.` | Wait for the next scheduled tick; check the timer, service and journal. |
| `نسخهٔ قدیمی گزارش جدول قراردادها را ندارد.` | Let the updated pilot publish one complete tick; never edit an old report. |
| `گزارش این تاریخ نامعتبر است.` | Do not modify the file; check private directory ownership and modes, then restore from backup if needed. |
| `{"error": "archive verification failed"}` | Keep the archive untouched. Compare the recorded count and head with the private backup, restore to an unused staging path, then re-run `verify-archive` before resuming. |

The `اقدام` guidance shown on the 503 page comes from `REPORT_GUIDANCE` in
`shadow_pilot_dashboard.py`. Treat a 503 as unavailable data, never as a signal.
The `verify-archive` error is intentionally stable: paths and raw exceptions stay
in the private journal and are never printed to the operator console.

```sh
install -m 644 deploy/shadow-pilot-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shadow-pilot-dashboard.service
systemctl --user status shadow-pilot-dashboard.service
```

From the operator's workstation, run `ssh -N -L 8766:127.0.0.1:8766 pilot@VPS`
and open `http://127.0.0.1:8766/`. Keep the same local and remote port so
the strict Host check matches. If the checkout lives elsewhere, adjust
`WorkingDirectory` and `ExecStart` in the unit. To stop the dashboard, run
`systemctl --user disable --now shadow-pilot-dashboard.service`. The pilot
timer continues independently.
