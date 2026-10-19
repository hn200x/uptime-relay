#!/usr/bin/env python3
"""UptimeRobot -> Telegram relay.

UptimeRobot's free plan alerts by email only. This script, run every 5 minutes
by .github/workflows/uptime-relay.yml, reads the monitors with the READ-ONLY
API key and posts to the Telegram bot when a monitor changes state:
one "DOWN" message per outage and one "Back up after N min" on recovery.

Usage:
  python -I uptime_relay.py --state .uptime-relay/state.json [--dry-run [--verbose]]

Env: UPTIMEROBOT_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

State file: {"monitors": {"<id>": {"status": "up|down", "since": "<iso UTC>", "last_down": <unix>}}}
- No state file: seed silently, except monitors that are down now (they alert).
- Paused (0) and not-yet-checked (1) monitors keep their previous entry.
- `last_down` is the datetime of the newest down log seen (0 for none). A monitor that is
  up now but has a newer down log went down and came back between two runs (GitHub can
  delay a scheduled run by 10+ minutes): that sends one "Down and back up" message.
- --dry-run sends nothing and writes no state. It prints the full messages only with --verbose.

Public-log hygiene: this runs in a public repo, so the normal output is counts and kinds only
(never monitor names, URLs or reasons). Only `--dry-run --verbose` prints message text.

No UPTIMEROBOT_API_KEY: one log line and exit 0 (the relay is not configured yet).
Exit 0 unless the UptimeRobot or Telegram call itself fails, or the Telegram secrets are
missing while the key is set (exit 1, one line).
Secrets are never printed: the API key goes in the POST body only and an error
answer (which echoes the key) is reduced to its `type`.

Stdlib only (Python 3.12).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://api.uptimerobot.com/v2/getMonitors"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
PAGE_SIZE = 50
TIMEOUT = 20
FOOTER = "UptimeRobot relay · every 5 min"
MAX_FIELD = 200  # characters per name / URL / reason, before escaping (Telegram's limit is 4096)

# UptimeRobot monitor status codes
ST_PAUSED, ST_NOT_CHECKED, ST_UP, ST_SEEMS_DOWN, ST_DOWN = 0, 1, 2, 8, 9
# UptimeRobot log types
LOG_DOWN, LOG_UP = 1, 2


class RelayError(Exception):
    """A failure that must end the run with exit 1 and a one-line, secret-free reason."""


# ── Time ────────────────────────────────────────────────────────────────────

def _london_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/London")
    except Exception:  # Windows without tzdata: fall back to the UK DST rule below
        return None


_LONDON = _london_tz()


def _last_sunday(year: int, month: int) -> date:
    d = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)


def to_london(utc: datetime) -> datetime:
    """Aware datetime -> naive London wall-clock time."""
    utc = utc.astimezone(timezone.utc)
    if _LONDON is not None:
        return utc.astimezone(_LONDON).replace(tzinfo=None)
    y = utc.year
    start = datetime.combine(_last_sunday(y, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(y, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return (utc + timedelta(hours=1 if start <= utc < end else 0)).replace(tzinfo=None)


def fmt_uk(utc: datetime) -> str:
    """e.g. 'Mon 19/10/2026 14:05' in London time."""
    return to_london(utc).strftime("%a %d/%m/%Y %H:%M")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_unix(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ── Pure diff logic (unit-tested) ───────────────────────────────────────────

def _int(value) -> int | None:
    """The API's numbers, whether sent as 2 or "2"; None for anything else (as heartbeat.py)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def classify(status) -> str | None:
    """UptimeRobot status code -> 'up' | 'down' | None (paused / not checked / unknown)."""
    code = _int(status)
    if code == ST_UP:
        return "up"
    if code in (ST_SEEMS_DOWN, ST_DOWN):
        return "down"
    return None


def _latest_log(monitor: dict, log_type: int) -> dict | None:
    raw = monitor.get("logs")
    logs = [lg for lg in (raw if isinstance(raw, list) else [])
            if isinstance(lg, dict) and _int(lg.get("type")) == log_type and _int(lg.get("datetime"))]
    logs.sort(key=lambda lg: _int(lg.get("datetime")), reverse=True)
    return logs[0] if logs else None


def _log_time(log: dict | None) -> int:
    return (_int(log.get("datetime")) or 0) if log else 0


def _reason(log: dict | None) -> str:
    if not log:
        return ""
    reason = log.get("reason") or {}
    if isinstance(reason, dict):
        detail = str(reason.get("detail") or "").strip()
        code = str(reason.get("code") or "").strip()
        if detail and code and code not in detail:
            return f"{detail} ({code})"
        return detail or code
    return str(reason).strip()


def diff(previous: dict | None, monitors: list[dict], now: datetime) -> tuple[dict, list[dict]]:
    """Compare stored state with the current monitors.

    previous: the stored {"<id>": {"status", "since", "last_down"}} map, or None when there is no state yet.
    Returns (new state map, alerts). Each alert is a dict with kind 'down', 'up' or 'blip' (down and
    back up between two runs), the monitor's name/url, `since` (datetime) and for 'up' / 'blip' the
    outage `minutes` and `down_since`.
    """
    # No state (first run / cache miss) is just an empty map: up monitors seed silently, down ones alert.
    previous = previous or {}
    new_state: dict = {}
    alerts: list[dict] = []

    for m in monitors:
        mid = str(m.get("id"))
        name = str(m.get("friendly_name") or mid)
        url = str(m.get("url") or "")
        current = classify(m.get("status"))
        old = previous.get(mid) if isinstance(previous.get(mid), dict) else None

        if current is None:  # paused or not checked yet: keep what we knew, say nothing
            if old:
                new_state[mid] = old
            continue

        down_log, up_log = _latest_log(m, LOG_DOWN), _latest_log(m, LOG_UP)
        down_t, up_t = _log_time(down_log), _log_time(up_log)

        if current == "down":
            # Only a down log newer than the newest up log belongs to this outage. "Seems down" (8)
            # has no down log yet, so the newest one may be an old, finished outage.
            current_log = down_log if down_t and down_t >= up_t else None
            since = (from_unix(down_t) if current_log else None) or now
            if old and old.get("status") == "down":
                new_state[mid] = old  # same outage, already alerted
                continue
            new_state[mid] = {"status": "down", "since": iso(since), "last_down": down_t}
            alerts.append({"kind": "down", "id": mid, "name": name, "url": url,
                           "since": since, "reason": _reason(current_log)})
            continue

        # current == "up": the newest up log is the recovery only if it follows the newest down log
        up_since = (from_unix(up_t) if up_t and up_t >= down_t else None) or now
        if old and old.get("status") == "down":
            down_since = parse_iso(old.get("since")) or up_since
            if up_since < down_since:  # e.g. a "seems down" that cleared: no fresh up log, so now
                up_since = now
            minutes = max(0, round((up_since - down_since).total_seconds() / 60))
            new_state[mid] = {"status": "up", "since": iso(up_since), "last_down": down_t}
            alerts.append({"kind": "up", "id": mid, "name": name, "url": url, "since": up_since,
                           "down_since": down_since, "minutes": minutes, "reason": _reason(down_log)})
        elif old and old.get("status") == "up":
            seen = _int(old.get("last_down"))
            if seen is not None and down_t > seen:  # went down and came back since the last run
                down_since = from_unix(down_t) or up_since
                minutes = max(0, round((up_since - down_since).total_seconds() / 60))
                new_state[mid] = {"status": "up", "since": iso(up_since), "last_down": down_t}
                alerts.append({"kind": "blip", "id": mid, "name": name, "url": url, "since": up_since,
                               "down_since": down_since, "minutes": minutes, "reason": _reason(down_log)})
            else:  # still up (a state without last_down just learns it, silently)
                new_state[mid] = {**old, "last_down": down_t}
        else:  # first sight of this monitor (or first run): seed silently
            new_state[mid] = {"status": "up", "since": iso(up_since), "last_down": down_t}

    return new_state, alerts


def _clip(value) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD - 1] + "…"


def format_alert(alert: dict) -> str:
    """Telegram HTML text for one alert. Every dynamic value is html-escaped."""
    def esc(value) -> str:
        return html.escape(_clip(value))

    if alert["kind"] == "down":
        lines = [f"🔴 <b>DOWN: {esc(alert['name'])}</b>"]
        if alert.get("url"):
            lines.append(f"- URL: {esc(alert['url'])}")
        lines.append(f"- Since: {esc(fmt_uk(alert['since']))}")
    elif alert["kind"] == "blip":
        lines = [f"🟠 <b>Down and back up: {esc(alert['name'])}</b>"]
        if alert.get("url"):
            lines.append(f"- URL: {esc(alert['url'])}")
        lines.append(f"- Down for {alert['minutes']} min, between two relay checks")
        lines.append(f"- Down from: {esc(fmt_uk(alert['down_since']))}")
        lines.append(f"- Back up: {esc(fmt_uk(alert['since']))}")
    else:
        lines = [f"🟢 <b>Back up: {esc(alert['name'])}</b>"]
        if alert.get("url"):
            lines.append(f"- URL: {esc(alert['url'])}")
        lines.append(f"- Back up after {alert['minutes']} min")
        lines.append(f"- Since: {esc(fmt_uk(alert['since']))}")
        lines.append(f"- Down from: {esc(fmt_uk(alert['down_since']))}")
    if alert.get("reason"):
        lines.append(f"- Reason: {esc(alert['reason'])}")
    lines.append("")
    lines.append(esc(FOOTER))
    return "\n".join(lines)


# ── I/O ─────────────────────────────────────────────────────────────────────

def _post(url: str, fields: dict) -> tuple[int, dict | None]:
    """POST form fields; return (HTTP status, parsed JSON or None). Never raises with the URL in text."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
        "User-Agent": "uptime-relay/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read() or b"")
    except Exception as e:  # noqa: BLE001 - URLError, timeouts, IncompleteRead, InvalidURL (its text holds the URL)
        raise RelayError(f"network error ({type(e).__name__})") from None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def fetch_monitors(api_key: str, post=_post) -> list[dict]:
    monitors: list[dict] = []
    offset = 0
    while True:
        status, data = post(API_URL, {"api_key": api_key, "format": "json", "logs": "1", "logs_limit": "3",
                                      "offset": str(offset), "limit": str(PAGE_SIZE)})
        if not isinstance(data, dict) or data.get("stat") != "ok":
            err = data.get("error") if isinstance(data, dict) else None
            etype = err.get("type") if isinstance(err, dict) else None
            raise RelayError(f"UptimeRobot getMonitors failed (HTTP {status}, error type: {etype or 'unknown'})")
        page = data.get("monitors") if isinstance(data.get("monitors"), list) else []
        monitors.extend(m for m in page if isinstance(m, dict))
        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        total = _int(pagination.get("total")) or 0
        offset += len(page)
        if not page or offset >= total:
            return monitors


def send_telegram(token: str, chat_id: str, text: str, post=_post) -> None:
    status, data = post(TELEGRAM_URL.format(token=token), {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    if not (isinstance(data, dict) and data.get("ok")):
        desc = data.get("description") if isinstance(data, dict) else None
        raise RelayError(f"Telegram sendMessage failed (HTTP {status}{', ' + str(desc) if desc else ''})")


def load_state(path: Path) -> dict | None:
    """Stored monitor map, or None when there is no usable state (first run / cache miss)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        print(f"state file unreadable, reseeding: {path}", file=sys.stderr)
        return None
    monitors = data.get("monitors") if isinstance(data, dict) else None
    return monitors if isinstance(monitors, dict) else None


def save_state(path: Path, monitors: dict, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"updated": iso(now), "monitors": monitors}, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def count_kinds(alerts: list[dict]) -> str:
    """'1 down, 0 up, 0 down-and-back-up': counts only, safe for a public log."""
    n = {k: sum(1 for a in alerts if a.get("kind") == k) for k in ("down", "up", "blip")}
    return f"{n['down']} down, {n['up']} up, {n['blip']} down-and-back-up"


def run(argv: list[str] | None = None, *, env=None, post=_post, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description="Relay UptimeRobot state changes to Telegram.")
    ap.add_argument("--state", required=True, help="path of the JSON state file")
    ap.add_argument("--dry-run", action="store_true", help="send nothing, write no state")
    ap.add_argument("--verbose", action="store_true",
                    help="with --dry-run: print the full messages (names and URLs; keep out of public logs)")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    state_path = Path(args.state)

    api_key = (env.get("UPTIMEROBOT_API_KEY") or "").strip()
    if not api_key:
        print("UPTIMEROBOT_API_KEY is not set: relay not configured yet, skipping (nothing sent)")
        return 0
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not args.dry_run and not (token and chat_id):
        print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 1

    try:
        monitors = fetch_monitors(api_key, post)
    except RelayError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    previous = load_state(state_path)
    new_state, alerts = diff(previous, monitors, now)
    seeded = "seeded" if previous is None else "compared"
    print(f"{len(monitors)} monitors, {seeded} state, {len(alerts)} alert(s)")

    if args.dry_run:
        if args.verbose:
            for a in alerts:
                print("--- would send ---")
                print(format_alert(a))
        print(f"dry run: would send {count_kinds(alerts)}; nothing sent, state not written")
        return 0

    failure = None
    sent: list[dict] = []
    for a in alerts:
        try:
            send_telegram(token, chat_id, format_alert(a), post)
            sent.append(a)
        except RelayError as e:
            failure = failure or str(e)
            # Not delivered: keep the old entry so the next run tries again.
            old = (previous or {}).get(a["id"])
            if old:
                new_state[a["id"]] = old
            else:
                new_state.pop(a["id"], None)
    if alerts:
        print(f"sent: {count_kinds(sent)}" + (f" ({len(alerts) - len(sent)} failed)" if len(sent) < len(alerts) else ""))
    save_state(state_path, new_state, now)
    if failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # emoji on a Windows console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(run())
