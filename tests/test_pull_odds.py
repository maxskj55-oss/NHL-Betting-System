"""End-to-end runs with a fake HTTP session (no network, no credits)."""

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nhl_betting.odds_api import BudgetError, OddsApiClient, OddsApiError
from nhl_betting.pull_odds import main, run_live, slate_window
from nhl_betting.sinks import CsvSink
from nhl_betting.snapshots import COLUMNS
from tests.helpers import FakeResponse, FakeSession, load_fixture, temp_config, usage_headers

KEY = "test-key-123"
NOW = datetime(2026, 10, 17, 19, 52, tzinfo=timezone.utc)  # 3:52 pm ET Saturday


def clock():
    return NOW


class LiveRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = temp_config(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, events, odds, used=40, after_used=43):
        session = FakeSession({
            "/events": FakeResponse(200, events, usage_headers(used, 500 - used, 0)),
            "/odds": FakeResponse(200, odds, usage_headers(after_used, 500 - after_used, 3)),
        })
        return OddsApiClient(self.cfg.odds_api, KEY, session=session), session

    def test_happy_path_writes_rows_and_raw(self):
        client, session = self._client(load_fixture("events_sample.json"),
                                       load_fixture("odds_sample.json"))
        with self.assertLogs("nhl_betting", level="INFO") as logs:
            self.assertEqual(run_live(self.cfg, client=client, clock=clock), 0)

        self.assertEqual(session.paths(), ["events", "odds"])
        _, odds_params = session.calls[1]
        self.assertEqual(odds_params["markets"], "h2h,spreads,totals")
        self.assertEqual(odds_params["bookmakers"], "fanduel,pinnacle")
        self.assertEqual(odds_params["commenceTimeFrom"], "2026-10-17T19:52:00Z")
        self.assertEqual(odds_params["commenceTimeTo"], "2026-10-18T10:00:00Z")  # 6 am EDT
        self.assertTrue(any("Credits for this run: 3" in m for m in logs.output))

        with self.cfg.csv_path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 34)
        self.assertEqual(list(rows[0].keys()), list(COLUMNS))
        self.assertEqual(rows[0]["snapshot_time_utc"], "2026-10-17T19:52:00Z")
        self.assertEqual(len(list(self.cfg.raw_dir.glob("odds_*.json"))), 1)

    def test_no_games_spends_nothing(self):
        client, session = self._client([], [])
        self.assertEqual(run_live(self.cfg, client=client, clock=clock), 0)
        self.assertEqual(session.paths(), ["events"])
        self.assertFalse(self.cfg.csv_path.exists())

    def test_over_budget_stops_before_paid_call(self):
        client, session = self._client(load_fixture("events_sample.json"),
                                       load_fixture("odds_sample.json"), used=299)
        with self.assertRaises(BudgetError):
            run_live(self.cfg, client=client, clock=clock)
        self.assertEqual(session.paths(), ["events"])

    def test_http_error_does_not_leak_key(self):
        session = FakeSession({"/events": FakeResponse(
            401, {"message": f"Invalid api key {KEY}"}, {})})
        client = OddsApiClient(self.cfg.odds_api, KEY, session=session)
        with self.assertRaises(OddsApiError) as ctx:
            run_live(self.cfg, client=client, clock=clock)
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_append_keeps_earlier_rows(self):
        for _ in range(2):
            client, _ = self._client(load_fixture("events_sample.json"),
                                     load_fixture("odds_sample.json"))
            run_live(self.cfg, client=client, clock=clock)
        with self.cfg.csv_path.open(newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 68)


class SlateWindowTest(unittest.TestCase):
    def test_afternoon_runs_until_next_morning(self):
        start, end = slate_window(NOW, "America/New_York", 6)
        self.assertEqual(start, NOW)
        self.assertEqual(end, datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc))

    def test_after_midnight_runs_until_same_morning(self):
        now = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)  # 1 am EDT
        _, end = slate_window(now, "America/New_York", 6)
        self.assertEqual(end, datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc))

    def test_winter_uses_standard_time(self):
        now = datetime(2026, 12, 1, 20, 52, tzinfo=timezone.utc)  # 3:52 pm EST
        _, end = slate_window(now, "America/New_York", 6)
        self.assertEqual(end, datetime(2026, 12, 2, 11, 0, tzinfo=timezone.utc))


class CsvSinkTest(unittest.TestCase):
    def test_refuses_file_with_other_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.csv"
            path.write_text("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                CsvSink(path, COLUMNS).append([])
            self.assertEqual(path.read_text(), "a,b\n1,2\n")


class DryRunTest(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        import nhl_betting.pull_odds as po
        original_client, original_sink = po.OddsApiClient, po.CsvSink
        po.OddsApiClient = None  # any attempt to build a client would crash
        po.CsvSink = None        # any attempt to write a file would crash
        try:
            with self.assertLogs("nhl_betting", level="INFO") as logs:
                self.assertEqual(main(["--dry-run"]), 0)
        finally:
            po.OddsApiClient, po.CsvSink = original_client, original_sink
        self.assertTrue(any("would append 34 rows" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()


class SinkSetupTest(unittest.TestCase):
    def test_missing_google_secret_stops_before_any_odds_call(self):
        import os
        from dataclasses import replace
        from unittest import mock
        from nhl_betting.config import ConfigError
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(temp_config(Path(d)), sink="sheets")
            session = FakeSession({})  # any odds call would raise AssertionError
            client = OddsApiClient(cfg.odds_api, KEY, session=session)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ConfigError):
                    run_live(cfg, client=client, clock=clock)
            self.assertEqual(session.calls, [])
