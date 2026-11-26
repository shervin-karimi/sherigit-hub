# VPS preflight (read-only)

`vps_preflight.py` checks a deployment host before any service is enabled or any
TSETMC request is made. It performs **no** network calls, writes no files, runs no
`systemctl` command and starts no collector. It only reads the supplied files.

## Run

```sh
python -B vps_preflight.py \
  --config /absolute/path/to/pilot.json \
  --unit /absolute/path/to/pilot.service \
  --min-free-mb 50
```

Exit status `0` means every check passed. Any failure or missing prerequisite
produces a stable JSON object with `"وضعیت": "issues"` and exit status `1`.

## Checks

| Check | Requirement | Failure example |
| --- | --- | --- |
| `python` | Running interpreter 3.12 or newer | Python 3.11 on the host |
| `tehran_timezone` | `Asia/Tehran` tzdata available with UTC+03:30 | Missing `tzdata`, wrong offset |
| `files` | Private root (`0700`, owner), private config/calendar/paper files (`0600`), valid windows and interval | Symlink, group-readable file, overlapping windows |
| `disk` | Free space at least `--min-free-mb` (default 50 MiB) | Full volume |
| `calendar` | Non-empty dated sessions with valid Tehran-time intervals | Malformed or empty calendar |
| `systemd` | Unit has non-root `User`, matching `WorkingDirectory`, `ExecStart` with `--config`, `Environment=TZ=Asia/Tehran`, and `[Install] WantedBy` | Wrong user, missing install target |

A missing `TZ` environment variable is only a warning: the application always
uses `ZoneInfo("Asia/Tehran")` explicitly. The unit file is parsed as text; it is
not installed, enabled or started.

## Example unit (not activated by the tool)

```ini
[Unit]
Description=Shadow pilot (manual review first)

[Service]
Type=oneshot
User=pilot
WorkingDirectory=/absolute/path/to/project
ExecStart=/usr/bin/python3 -B shadow_pilot.py tick --config /absolute/path/to/pilot.json
Environment=TZ=Asia/Tehran

[Install]
WantedBy=multi-user.target
```

Enabling or scheduling this unit requires a separate explicit owner approval.
The preflight tool never performs that step.

## Remediation

- Install `tzdata` and Python 3.12+ before retrying.
- Create the pilot root with mode `0700` and private files with mode `0600`.
- Use absolute paths in both the configuration and the unit file.
- Restore a valid trading calendar from the reviewed local copy; never fabricate sessions.
- Free disk space or point the root at a volume with enough room.

The output contains file basenames and digests, not full paths or market data.
Keep the raw configuration and unit files out of Git.
