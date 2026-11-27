# VPS stage 80: offline acceptance and handoff

This is a checklist, not permission to activate a service or collect market data.
The existing `vps_preflight.py` and its checks remain unchanged.

## Calendar gate (pending authoritative evidence)

- Obtain a dated exchange notice covering the option contracts/venues actually
  collected, plus the official holiday calendar and later closures or schedule
  changes. Record issuer, URL, publication date, affected dates and hours.
- For 2026-11-28 through 2026-12-02, the ordinary Saturday–Wednesday pattern is
  only a research candidate. Do not treat equity market hours, general calendars,
  or expiry calendars as verified option sessions. Resolve holiday discrepancies
  and verify any exceptional closures first.
- The existing `shadow_schedule.load_calendar` accepts `source` and nonempty
  `sessions` (ISO dates mapped to Tehran-time intervals). It validates syntax,
  not official provenance, freshness or exchange closures. Never invent a session
  or use a fixture as the actual `sessions.json`.
- Keep the reviewed calendar outside Git, mode 0600. Do not publish or install a
  speculative calendar. A missing calendar is a real acceptance blocker.

## Initial configuration backup

Only for an observation-only configuration whose data root is **empty**, with
no service running. Execute as the intended user, not root. Create a separate
private backup destination (0700). Example on the prepared VPS:

```sh
umask 077
mkdir -m 700 -p "$HOME/.local/share/sherigit-pilot-backups"
python -B vps_stage80.py \
  --config-dir "$HOME/.config/sherigit-pilot" \
  --root "$HOME/.local/share/sherigit-pilot" \
  --destination "$HOME/.local/share/sherigit-pilot-backups"
```

The tool copies the two private configuration files and calendar if present,
records SHA-256 digests, recreates a separate empty root, and verifies a bytewise
restore in a temporary directory. It refuses symlinks, unsafe permissions,
nonempty roots and paper configurations. It never overwrites the live files,
starts a unit or accesses the network. The restore is a copy drill, not a
functional restoration of a deployed service. `calendar_present: false` means
that the backup is incomplete for real operation. A local backup on the same
VPS does not protect against disk/host loss; arrange a separate approved private
backup destination before live use. For a populated archive or SQLite ledger,
follow the coordinated procedure in `SHADOW_PILOT.md`, not this tool.

## Acceptance checklist (not yet passed)

- [ ] Official option session evidence reviewed and private calendar installed.
- [ ] Existing read-only preflight returns exit 0 and status `ok` for the actual
      config, unit and pinned dependencies; check unit syntax separately before
      any activation. The current unit draft is not installed.
- [ ] Full offline test suite passes; no removed tests, unexpected network
      attempts or production data changes. Both existing CI matrix jobs pass.
- [ ] Private backup and isolated restore drill pass on the VPS; independent
      backup and operational recovery scope reviewed before live data use.
- [ ] Owner reviews evidence and accepts the 80% milestone. No service or market
      request may be enabled by this checklist; that requires separate approval.
