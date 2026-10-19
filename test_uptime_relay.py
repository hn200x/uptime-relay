#!/usr/bin/env python3
"""Unit tests for uptime_relay.py (stdlib unittest, no network).

Run: python -I test_uptime_relay.py
`-I` does not put this folder on sys.path, so uptime_relay.py is loaded by file path.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("uptime_relay", Path(__file__).resolve().parent / "uptime_relay.py")
ur = importlib.util.module_from_spec(_spec)
sys.modules["uptime_relay"] = ur
_spec.loader.exec_module(ur)

NOW = datetime(2026, 10, 19, 13, 5, tzinfo=timezone.utc)  # 14:05 BST
T_DOWN = int(datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc).timestamp())
T_UP = int(datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc).timestamp())
KEY = "ur-secret-key-123"
TOKEN = "123:tg-secret-token"


def mon(mid, status, logs=None, name=None, url=None):
    return {"id": mid, "friendly_name": name or f"Mon {mid}", "url": url or f"https://m{mid}.example/health",
            "status": status, "logs": logs or []}


def down_log(t=T_DOWN, detail="Connection Timeout", code="333333"):
    return {"type": 1, "datetime": t, "reason": {"code": code, "detail": detail}}


def up_log(t=T_UP):
    return {"type": 2, "datetime": t, "reason": {"code": "200", "detail": "OK"}}


class Classify(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(ur.classify(2), "up")
        self.assertEqual(ur.classify(8), "down")
        self.assertEqual(ur.classify(9), "down")
        self.assertIsNone(ur.classify(0))
        self.assertIsNone(ur.classify(1))
        self.assertIsNone(ur.classify("x"))


class Diff(unittest.TestCase):
    def test_first_run_seeds_up_silently_and_alerts_down(self):
        state, alerts = ur.diff(None, [mon(1, 2), mon(2, 9, [down_log()])], NOW)
        self.assertEqual(state["1"]["status"], "up")
        self.assertEqual(state["2"], {"status": "down", "since": "2026-10-19T12:30:00+00:00", "last_down": T_DOWN})
        self.assertEqual([(a["kind"], a["id"]) for a in alerts], [("down", "2")])
        self.assertEqual(alerts[0]["reason"], "Connection Timeout (333333)")

    def test_new_down_alerts_once(self):
        prev = {"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00"}}
        state, alerts = ur.diff(prev, [mon(1, 8, [down_log()])], NOW)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "down")
        # next tick, still down: silent, state unchanged
        state2, alerts2 = ur.diff(state, [mon(1, 9, [down_log()])], NOW)
        self.assertEqual(alerts2, [])
        self.assertEqual(state2, state)

    def test_recovery_reports_minutes_from_state_since(self):
        prev = {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}
        state, alerts = ur.diff(prev, [mon(1, 2, [up_log(), down_log()])], NOW)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual((a["kind"], a["minutes"]), ("up", 30))
        self.assertEqual(state["1"], {"status": "up", "since": "2026-10-19T13:00:00+00:00", "last_down": T_DOWN})
        # still up next tick: silent
        _, alerts2 = ur.diff(state, [mon(1, 2, [up_log()])], NOW)
        self.assertEqual(alerts2, [])

    def test_recovery_without_up_log_uses_now(self):
        prev = {"1": {"status": "down", "since": "2026-10-19T12:05:00+00:00"}}
        _, alerts = ur.diff(prev, [mon(1, 2)], NOW)
        self.assertEqual(alerts[0]["minutes"], 60)

    def test_down_without_log_uses_now(self):
        state, alerts = ur.diff({}, [mon(1, 9)], NOW)
        self.assertEqual(state["1"]["since"], "2026-10-19T13:05:00+00:00")
        self.assertEqual(alerts[0]["reason"], "")

    def test_paused_and_unchecked_keep_previous_and_stay_silent(self):
        prev = {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}
        state, alerts = ur.diff(prev, [mon(1, 0), mon(2, 1)], NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(state, prev)

    def test_new_monitor_up_seeds_silently_on_later_run(self):
        prev = {"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00"}}
        state, alerts = ur.diff(prev, [mon(1, 2), mon(3, 2)], NOW)
        self.assertEqual(alerts, [])
        self.assertIn("3", state)

    def test_removed_monitor_dropped(self):
        prev = {"9": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}
        state, alerts = ur.diff(prev, [], NOW)
        self.assertEqual((state, alerts), ({}, []))

    # ── review of PR #36 ──

    def test_numbers_sent_as_strings(self):
        """status, log type and datetime may come back as strings (heartbeat.py's _int)."""
        logs = [{"type": "1", "datetime": str(T_DOWN), "reason": {"code": "333333", "detail": "Timeout"}}]
        state, alerts = ur.diff({"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00"}},
                                [mon(1, "9", logs)], NOW)
        self.assertEqual(alerts[0]["since"], datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(alerts[0]["reason"], "Timeout (333333)")
        self.assertEqual(state["1"]["last_down"], T_DOWN)

    def test_junk_log_values_do_not_crash(self):
        logs = [{"type": 1, "datetime": "soon"}, {"type": None}, "x", {"type": 2, "datetime": None}]
        state, alerts = ur.diff(None, [mon(1, 9, logs), mon(2, 2, {"not": "a list"})], NOW)
        self.assertEqual(state["1"]["since"], "2026-10-19T13:05:00+00:00")
        self.assertEqual(state["2"]["last_down"], 0)
        self.assertEqual([a["kind"] for a in alerts], ["down"])

    def test_seems_down_ignores_old_finished_outage(self):
        """Status 8 has no down log yet: the newest one is last week's, already followed by an up log."""
        week_ago_down, week_ago_up = T_DOWN - 7 * 86400, T_DOWN - 7 * 86400 + 600
        prev = {"1": {"status": "up", "since": "2026-10-12T12:40:00+00:00", "last_down": week_ago_down}}
        state, (a,) = ur.diff(prev, [mon(1, 8, [up_log(week_ago_up), down_log(week_ago_down)])], NOW)
        self.assertEqual(a["since"], NOW)
        self.assertEqual(a["reason"], "")
        # then it recovers 5 minutes later: the outage is 5 min, not a week
        later = datetime(2026, 10, 19, 13, 10, tzinfo=timezone.utc)
        _, (b,) = ur.diff(state, [mon(1, 2, [up_log(week_ago_up), down_log(week_ago_down)])], later)
        self.assertEqual((b["kind"], b["minutes"]), ("up", 5))

    def test_recovery_ignores_up_log_older_than_the_outage(self):
        prev = {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}
        _, (a,) = ur.diff(prev, [mon(1, 2, [down_log(), up_log(T_DOWN - 3600)])], NOW)
        self.assertEqual((a["since"], a["minutes"]), (NOW, 35))

    def test_down_and_back_up_between_runs_alerts_once(self):
        """GitHub can delay a scheduled run past a short outage: the newer down log reveals it."""
        prev = {"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00", "last_down": 0}}
        logs = [up_log(), down_log()]  # down 12:30, up 13:00 UTC, both since the last run
        state, (a,) = ur.diff(prev, [mon(1, 2, logs)], NOW)
        self.assertEqual((a["kind"], a["minutes"]), ("blip", 30))
        self.assertEqual(state["1"], {"status": "up", "since": "2026-10-19T13:00:00+00:00", "last_down": T_DOWN})
        text = ur.format_alert(a)
        self.assertEqual(text.splitlines()[0], "🟠 <b>Down and back up: Mon 1</b>")
        self.assertIn("- Down for 30 min, between two relay checks", text)
        self.assertIn("- Down from: Mon 19/10/2026 13:30", text)
        self.assertIn("- Back up: Mon 19/10/2026 14:00", text)
        # next run, same logs: silent
        _, alerts2 = ur.diff(state, [mon(1, 2, logs)], NOW)
        self.assertEqual(alerts2, [])

    def test_state_without_last_down_learns_it_silently(self):
        """State written before last_down existed (or after a reseed) must not alert on old logs."""
        prev = {"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00"}}
        state, alerts = ur.diff(prev, [mon(1, 2, [up_log(), down_log()])], NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(state["1"]["last_down"], T_DOWN)
        self.assertEqual(state["1"]["since"], "2026-10-18T00:00:00+00:00")

    def test_first_run_with_old_down_log_is_silent(self):
        state, alerts = ur.diff(None, [mon(1, 2, [up_log(), down_log()])], NOW)
        self.assertEqual(alerts, [])
        _, alerts2 = ur.diff(state, [mon(1, 2, [up_log(), down_log()])], NOW)
        self.assertEqual(alerts2, [])

    def test_flapping_down_up_down_across_runs(self):
        s1, a1 = ur.diff({"1": {"status": "up", "since": "x", "last_down": 0}}, [mon(1, 9, [down_log()])], NOW)
        s2, a2 = ur.diff(s1, [mon(1, 2, [up_log(), down_log()])], NOW)
        t3 = T_UP + 120
        s3, a3 = ur.diff(s2, [mon(1, 9, [down_log(t3), up_log(), down_log()])], NOW)
        s4, a4 = ur.diff(s3, [mon(1, 9, [down_log(t3), up_log(), down_log()])], NOW)
        self.assertEqual([[a["kind"] for a in x] for x in (a1, a2, a3, a4)], [["down"], ["up"], ["down"], []])

    def test_paused_then_resumed_down_alerts(self):
        prev = {"1": {"status": "up", "since": "2026-10-18T00:00:00+00:00", "last_down": 0}}
        s1, a1 = ur.diff(prev, [mon(1, 0)], NOW)
        s2, a2 = ur.diff(s1, [mon(1, 1)], NOW)
        _, a3 = ur.diff(s2, [mon(1, 9, [down_log()])], NOW)
        self.assertEqual((a1, a2, [a["kind"] for a in a3]), ([], [], ["down"]))


class Format(unittest.TestCase):
    def test_down_message(self):
        _, (a,) = ur.diff(None, [mon(1, 9, [down_log()], name="LottoLab <API>", url="https://api.x/health?a=1&b=2")], NOW)
        text = ur.format_alert(a)
        self.assertEqual(text.splitlines()[0], "🔴 <b>DOWN: LottoLab &lt;API&gt;</b>")
        self.assertIn("- URL: https://api.x/health?a=1&amp;b=2", text)
        self.assertIn("- Since: Mon 19/10/2026 13:30", text)  # 12:30 UTC = 13:30 BST
        self.assertIn("- Reason: Connection Timeout (333333)", text)
        self.assertTrue(text.endswith("UptimeRobot relay · every 5 min"))

    def test_up_message(self):
        prev = {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}
        _, (a,) = ur.diff(prev, [mon(1, 2, [up_log(), down_log()], name="Web")], NOW)
        text = ur.format_alert(a)
        self.assertEqual(text.splitlines()[0], "🟢 <b>Back up: Web</b>")
        self.assertIn("- Back up after 30 min", text)
        self.assertIn("- Since: Mon 19/10/2026 14:00", text)

    def test_winter_time(self):
        self.assertEqual(ur.fmt_uk(datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)), "Tue 01/12/2026 09:00")

    def test_clock_change_edges_and_fallback_rule(self):
        cases = [(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc), "Sun 29/03/2026 00:59"),
                 (datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), "Sun 29/03/2026 02:00"),
                 (datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc), "Sun 25/10/2026 01:59"),
                 (datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc), "Sun 25/10/2026 01:00")]
        saved = ur._LONDON
        try:
            for tz in (saved, None):  # zoneinfo, then the built-in rule used without tzdata
                ur._LONDON = tz
                for dt, want in cases:
                    self.assertEqual(ur.fmt_uk(dt), want, (tz, dt))
        finally:
            ur._LONDON = saved

    def test_long_fields_are_clipped_and_message_stays_small(self):
        a = {"kind": "down", "name": "<b>" * 2000, "url": "https://x/" + "&" * 5000, "since": NOW,
             "reason": "r" * 9000}
        text = ur.format_alert(a)
        self.assertLess(len(text), 2000)
        self.assertNotIn("<b><b>", text)
        self.assertIn("…", text)


class FakePost:
    def __init__(self, monitors, telegram_ok=True, api_answer=None):
        self.monitors, self.telegram_ok, self.api_answer = monitors, telegram_ok, api_answer
        self.calls = []

    def __call__(self, url, fields):
        self.calls.append((url, fields))
        if "uptimerobot" in url:
            if self.api_answer is not None:
                return self.api_answer
            return 200, {"stat": "ok", "pagination": {"total": len(self.monitors)}, "monitors": self.monitors}
        if self.telegram_ok:
            return 200, {"ok": True}
        return 400, {"ok": False, "description": "Bad Request: chat not found"}

    def sent(self):
        return [f["text"] for u, f in self.calls if "telegram" in u]


ENV = {"UPTIMEROBOT_API_KEY": KEY, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sub" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, post, *extra, env=ENV):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ur.run(["--state", str(self.state), *extra], env=env, post=post, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def test_key_in_post_body_and_state_written(self):
        post = FakePost([mon(1, 2), mon(2, 9, [down_log()])])
        code, out, _ = self.go(post)
        self.assertEqual(code, 0)
        url, fields = post.calls[0]
        self.assertEqual(url, ur.API_URL)
        self.assertEqual((fields["api_key"], fields["logs"], fields["logs_limit"]), (KEY, "1", "3"))
        self.assertEqual(len(post.sent()), 1)
        saved = json.loads(self.state.read_text(encoding="utf-8"))["monitors"]
        self.assertEqual(saved["2"]["status"], "down")
        # second run: nothing new
        post2 = FakePost([mon(1, 2), mon(2, 9, [down_log()])])
        self.assertEqual(self.go(post2)[0], 0)
        self.assertEqual(post2.sent(), [])
        self.assertNotIn(KEY, out)

    def test_dry_run_sends_nothing_writes_nothing(self):
        post = FakePost([mon(2, 9, [down_log()])])
        code, out, _ = self.go(post, "--dry-run", "--verbose", env={"UPTIMEROBOT_API_KEY": KEY})
        self.assertEqual(code, 0)
        self.assertEqual(post.sent(), [])
        self.assertFalse(self.state.exists())
        self.assertIn("DOWN: Mon 2", out)

    def test_api_error_prints_type_only(self):
        answer = (200, {"stat": "fail", "error": {"type": "invalid_parameter", "parameter_name": "api_key",
                                                   "passed_value": KEY}})
        code, out, err = self.go(FakePost([], api_answer=answer))
        self.assertEqual(code, 1)
        self.assertIn("invalid_parameter", err)
        self.assertNotIn(KEY, out + err)
        self.assertFalse(self.state.exists())

    def test_telegram_failure_exits_1_and_retries_next_run(self):
        code, out, err = self.go(FakePost([mon(1, 2), mon(2, 9, [down_log()])], telegram_ok=False))
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out + err)
        saved = json.loads(self.state.read_text(encoding="utf-8"))["monitors"]
        self.assertNotIn("2", saved)  # not delivered, so not recorded
        post = FakePost([mon(1, 2), mon(2, 9, [down_log()])])
        self.assertEqual(self.go(post)[0], 0)
        self.assertEqual(len(post.sent()), 1)

    def test_missing_env(self):
        # no key at all: not configured yet, skip quietly with exit 0 and call nothing
        post = FakePost([])
        code, out, err = self.go(post, env={})
        self.assertEqual((code, err, post.calls), (0, "", []))
        self.assertIn("UPTIMEROBOT_API_KEY is not set", out)
        self.assertEqual(self.go(FakePost([]), "--dry-run", env={"UPTIMEROBOT_API_KEY": "  "})[0], 0)
        # key set but the Telegram secrets missing: a real misconfiguration
        self.assertEqual(self.go(FakePost([]), env={"UPTIMEROBOT_API_KEY": KEY})[0], 1)

    # ── public-log hygiene: no monitor names, URLs or reasons unless --dry-run --verbose ──

    SECRET_NAME, SECRET_URL, SECRET_REASON = "Private Billing Box", "https://hidden.example/health", "Kaboom42"

    def _hygiene_monitors(self):
        return [mon(1, 9, [down_log(detail=self.SECRET_REASON)], name=self.SECRET_NAME, url=self.SECRET_URL),
                mon(2, 2, name="Other Quiet Site")]

    def assert_no_monitor_detail(self, text):
        for needle in (self.SECRET_NAME, self.SECRET_URL, "hidden.example", self.SECRET_REASON, "Other Quiet Site"):
            self.assertNotIn(needle, text)

    def test_default_output_has_no_monitor_names(self):
        post = FakePost(self._hygiene_monitors())
        code, out, err = self.go(post)
        self.assertEqual(code, 0)
        self.assertEqual(len(post.sent()), 1)
        self.assertIn(self.SECRET_NAME, post.sent()[0])  # the Telegram message itself does name it
        self.assertIn("sent: 1 down, 0 up, 0 down-and-back-up", out)
        self.assert_no_monitor_detail(out + err)

    def test_send_failure_output_has_no_monitor_names(self):
        code, out, err = self.go(FakePost(self._hygiene_monitors(), telegram_ok=False))
        self.assertEqual(code, 1)
        self.assertIn("sent: 0 down, 0 up, 0 down-and-back-up (1 failed)", out)
        self.assert_no_monitor_detail(out + err)

    def test_dry_run_without_verbose_has_no_monitor_names(self):
        code, out, err = self.go(FakePost(self._hygiene_monitors()), "--dry-run", env={"UPTIMEROBOT_API_KEY": KEY})
        self.assertEqual(code, 0)
        self.assertIn("dry run: would send 1 down, 0 up, 0 down-and-back-up", out)
        self.assert_no_monitor_detail(out + err)

    def test_dry_run_verbose_prints_messages(self):
        code, out, _ = self.go(FakePost(self._hygiene_monitors()), "--dry-run", "--verbose",
                               env={"UPTIMEROBOT_API_KEY": KEY})
        self.assertEqual(code, 0)
        self.assertIn(f"DOWN: {self.SECRET_NAME}", out)

    def test_workflow_dry_run_does_not_pass_verbose(self):
        wf = (Path(__file__).resolve().parent / ".github" / "workflows" / "uptime-relay.yml").read_text(encoding="utf-8")
        self.assertIn("--dry-run", wf)
        self.assertNotIn("--verbose", wf)

    def test_dry_run_end_to_end_with_state_shows_recovery_and_keeps_state(self):
        prev = {"updated": "x", "monitors": {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}}
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps(prev), encoding="utf-8")
        before = self.state.read_text(encoding="utf-8")
        post = FakePost([mon(1, 2, [up_log(), down_log()], name="LottoLab API")])
        code, out, _ = self.go(post, "--dry-run", "--verbose", env={"UPTIMEROBOT_API_KEY": KEY})
        self.assertEqual(code, 0)
        self.assertIn("1 monitors, compared state, 1 alert(s)", out)
        self.assertIn("🟢 <b>Back up: LottoLab API</b>", out)
        self.assertIn("- Back up after 30 min", out)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)
        self.assertEqual(post.sent(), [])

    def test_one_telegram_failure_does_not_lose_the_other_alerts(self):
        calls = {"n": 0}

        def post(url, fields):
            if "uptimerobot" in url:
                return 200, {"stat": "ok", "pagination": {"total": 2},
                             "monitors": [mon(1, 9, [down_log()]), mon(2, 9, [down_log()])]}
            calls["n"] += 1
            return (200, {"ok": True}) if calls["n"] == 2 else (429, {"ok": False, "description": "Too Many Requests"})

        code, _, err = self.go(post)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        saved = json.loads(self.state.read_text(encoding="utf-8"))["monitors"]
        self.assertEqual(sorted(saved), ["2"])  # 1 not delivered: retried next run; 2 delivered: recorded

    def test_recovery_telegram_failure_keeps_down_entry(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"monitors": {"1": {"status": "down", "since": "2026-10-19T12:30:00+00:00"}}}),
                              encoding="utf-8")
        code, _, _ = self.go(FakePost([mon(1, 2, [up_log(), down_log()])], telegram_ok=False))
        self.assertEqual(code, 1)
        saved = json.loads(self.state.read_text(encoding="utf-8"))["monitors"]
        self.assertEqual(saved["1"]["status"], "down")
        post = FakePost([mon(1, 2, [up_log(), down_log()])])
        self.assertEqual(self.go(post)[0], 0)
        self.assertIn("Back up after 30 min", post.sent()[0])

    def test_api_http_error_and_non_json_answer(self):
        for answer in [(503, None), (200, None), (200, ["not", "a", "dict"]), (200, {"stat": "fail"})]:
            code, out, err = self.go(FakePost([], api_answer=answer))
            self.assertEqual(code, 1, answer)
            self.assertIn("UptimeRobot getMonitors failed", err)
            self.assertNotIn(KEY, out + err)

    def test_unreadable_state_reseeds(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json", encoding="utf-8")
        post = FakePost([mon(1, 2), mon(2, 9, [down_log()])])
        code, out, err = self.go(post)
        self.assertEqual(code, 0)
        self.assertIn("seeded", out)
        self.assertEqual(len(post.sent()), 1)

    def test_pagination_total_as_string_or_junk(self):
        def make(total):
            def post(url, fields):
                return 200, {"stat": "ok", "pagination": {"total": total}, "monitors": [mon(1, 2)]}
            return post
        self.assertEqual(len(ur.fetch_monitors(KEY, make("1"))), 1)
        self.assertEqual(len(ur.fetch_monitors(KEY, make("junk"))), 1)

    def test_pagination(self):
        pages = [[mon(i, 2) for i in range(50)], [mon(50, 2)]]
        calls = []

        def post(url, fields):
            calls.append(fields)
            page = pages[len(calls) - 1]
            return 200, {"stat": "ok", "pagination": {"total": 51}, "monitors": page}

        self.assertEqual(len(ur.fetch_monitors(KEY, post)), 51)
        self.assertEqual([c["offset"] for c in calls], ["0", "50"])


class _Handler(BaseHTTPRequestHandler):
    """Local fake server for _post: path decides the answer."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.server.bodies.append(self.rfile.read(length).decode("utf-8"))
        if self.path.startswith("/json500"):
            self._answer(500, b'{"stat": "fail", "error": {"type": "internal"}}')
        elif self.path.startswith("/html"):
            self._answer(502, b"<html>Bad gateway</html>")
        elif self.path.startswith("/slow"):
            threading.Event().wait(1.0)
            self._answer(200, b"{}")
        else:
            self._answer(200, b'{"stat": "ok"}')

    def _answer(self, code, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class PostOverHttp(unittest.TestCase):
    """_post against a real local HTTP server: no exception text (which could hold the URL) escapes."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.bodies = []
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ok_and_key_only_in_body(self):
        status, data = ur._post(self.base + "/ok", {"api_key": KEY, "format": "json"})
        self.assertEqual((status, data), (200, {"stat": "ok"}))
        self.assertIn(f"api_key={KEY}", self.server.bodies[-1])

    def test_http_error_body_is_parsed_not_raised(self):
        self.assertEqual(ur._post(self.base + "/json500", {}), (500, {"stat": "fail", "error": {"type": "internal"}}))
        self.assertEqual(ur._post(self.base + "/html", {}), (502, None))

    def test_timeout_and_refused_become_relay_errors_without_url(self):
        saved = ur.TIMEOUT
        ur.TIMEOUT = 0.2
        try:
            with self.assertRaises(ur.RelayError) as ctx:
                ur._post(self.base + f"/slow/bot{TOKEN}/x", {})
        finally:
            ur.TIMEOUT = saved
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        with self.assertRaises(ur.RelayError) as ctx:
            ur._post(f"http://127.0.0.1:1/bot{TOKEN}/sendMessage", {})
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_invalid_url_text_does_not_leak(self):
        with self.assertRaises(ur.RelayError) as ctx:
            ur._post(self.base + "/bot123:bad token/sendMessage", {})
        self.assertNotIn("bad token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
