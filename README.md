# uptime-relay

Relays UptimeRobot monitor status to a Telegram bot every 5 minutes.

UptimeRobot's free plan sends alerts by email only; Telegram and other integrations need a paid plan. This repo is the workaround: UptimeRobot still does the checking (every 5 minutes, from many locations), and a scheduled GitHub Actions workflow reads the results with the **read-only** API key and posts changes to Telegram.

It lives in its own small public repo because GitHub Actions minutes are free for public repos; a 5-minute schedule on a private repo would use about 8,600 minutes a month.

Stdlib-only Python 3.12, no dependencies.

## What it sends

One message when a monitor goes down, one when it comes back. Nothing on the runs in between.

```
🔴 DOWN: Example site
- URL: https://example.com/health
- Since: Mon 19/10/2026 14:05
- Reason: Connection Timeout

UptimeRobot relay · every 5 min
```

```
🟢 Back up: Example site
- URL: https://example.com/health
- Back up after 12 min
- Since: Mon 19/10/2026 14:17
- Down from: Mon 19/10/2026 14:05
```

If a monitor went down and came back between two runs (GitHub sometimes runs the schedule 10 minutes or more late), UptimeRobot's log still shows it, and the relay sends one message instead of the pair:

```
🟠 Down and back up: Example site
- URL: https://example.com/health
- Down for 4 min, between two relay checks
- Down from: Mon 19/10/2026 14:05
- Back up: Mon 19/10/2026 14:09
```

Times are UK time (Europe/London). "Seems down" (8) and "down" (9) both count as DOWN; paused and not-yet-checked monitors are ignored. Names, URLs and reasons are cut to 200 characters.

## Secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|--------|-------|
| `UPTIMEROBOT_API_KEY` | UptimeRobot → My Settings → API Settings → **Read-Only API Key** (never the Main API key) |
| `TELEGRAM_BOT_TOKEN` | the Telegram bot's token |
| `TELEGRAM_CHAT_ID` | the chat the bot posts to |

Until `UPTIMEROBOT_API_KEY` is set, every run logs `UPTIMEROBOT_API_KEY is not set: relay not configured yet, skipping` and exits 0, so the repo is safe to publish first and configure later. Once the key is set, missing Telegram secrets fail the run.

## Testing

Dry run on GitHub (reads UptimeRobot, sends nothing, keeps the stored state):

```
gh workflow run uptime-relay.yml -f dry_run=true
gh run watch
```

Unit tests (no network): `python -I test_uptime_relay.py`

Locally, with the key in your environment (never in a file in the repo), you can also see the full message text:

```
python -I uptime_relay.py --state .uptime-relay/state.json --dry-run --verbose
```

## Public logs

Actions logs in a public repo are public. The relay prints counts and kinds only, for example `3 monitors, compared state, 1 alert(s)` and `sent: 1 down, 0 up, 0 down-and-back-up`: never monitor names, URLs or reasons. Only `--dry-run --verbose` prints the messages, and the workflow never passes `--verbose`.

The API key goes in the POST body only. UptimeRobot's error answer echoes the key, so the script prints only the error type. The Telegram token is part of the request URL and is never printed; GitHub also masks secrets in logs.

## How state works

The last-known status of each monitor (up or down, since when, and the time of the newest down log seen) is kept in the GitHub Actions cache (`uptime-relay-state-*`), not in the repo. Each run restores the newest entry, saves a new one and prunes all but the newest 5. If the cache is empty (first run, or GitHub evicted it after 7 days without use), the relay seeds itself silently, except for monitors that are down right now, which alert. If Telegram refuses a message, that change is not recorded, so the next run tries again.

GitHub may delay scheduled runs, sometimes by several minutes, or skip one at busy times. Expect an alert within about 5 to 15 minutes of UptimeRobot noticing.

## The GitHub schedule is a backup

In practice GitHub ran the `*/5` schedule far less often than every 5 minutes (twice in the first ten hours), so the relay's main runner is a local scheduler on the maintainer's machine, running the same script every 5 minutes with its own state file (`--state <path>`) and the same three environment variables. The scheduled workflow here stays on as a slow backup for when that machine is off. Because the two keep separate state, expect most changes to be announced twice: promptly by the local runner, then again by the next GitHub run, which can be hours later.

## Keep-alive

GitHub disables scheduled workflows in a public repo after 60 days without repository activity. `.github/workflows/keep-alive.yml` runs on the 1st of each month (06:17 UTC) and commits the date to `KEEPALIVE`, which counts as activity. If the relay ever stops, check Actions → uptime-relay for a "disabled" banner and re-enable it (`gh workflow enable uptime-relay.yml`).

## Known limitations

- **"Seems down" alerts straight away.** Status 8 is treated as DOWN, like status 9. A false alarm that clears quickly gives a DOWN followed by "Back up after 0–5 min".
- **Log times and account timezone.** UptimeRobot v2 log `datetime` values are treated as UTC. Some accounts are reported to return times shifted by the account timezone, which would put "Since" an hour out during BST. Compare the first real alert with the UptimeRobot dashboard.
- **One cache entry per run.** About 288 a day; the prune step keeps the newest 5. If pruning fails, the run still succeeds and GitHub evicts unused entries after 7 days.
- **Telegram outages.** If Telegram is unreachable while many monitors change at once, the 20-second timeout per message could hit the 5-minute job limit; state would then not be written and already-delivered messages would repeat.
- **Down, up, down between runs** is treated as one continuous outage, so the recovery minutes count from the first down.
- A failed UptimeRobot API call fails the run (and GitHub emails a failure notice), which is also how a broken relay gets noticed.

## Licence

MIT, see [LICENSE](LICENSE).
