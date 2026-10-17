"""SheetsSink against a fake Sheets API (no network)."""

import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import unquote

from nhl_betting.config import ConfigError
from nhl_betting.sheets import SheetsError, SheetsSink, get_sheet_id, to_sheet_value
from nhl_betting.snapshots import COLUMNS, build_rows
from tests.helpers import load_fixture

SID = "1AbC_dEf-123"
TAB = "Odds_Snapshots"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSheets:
    """Minimal in-memory Sheets API: tabs, header row, appended rows."""

    def __init__(self, tabs=(TAB,), header=None, fail=None):
        self.tabs = list(tabs)
        self.header = header
        self.rows = []
        self.fail = list(fail or [])  # status codes to return before succeeding
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        u = unquote(url)
        self.calls.append((method, u))
        if self.fail:
            return Resp(self.fail.pop(0), {"error": {"message": "boom"}})
        if method == "GET" and "/values/" not in u:
            return Resp(body={"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        if u.endswith(":batchUpdate"):
            self.tabs.append(json["requests"][0]["addSheet"]["properties"]["title"])
            return Resp()
        if method == "GET":
            return Resp(body={"values": [self.header]} if self.header else {})
        if method == "PUT":
            self.header = json["values"][0]
            return Resp()
        if u.endswith(":append"):
            self.rows.extend(json["values"])
            return Resp(body={"updates": {"updatedRows": len(json["values"]),
                                          "updatedRange": f"{TAB}!A2:M99"}})
        raise AssertionError(f"unexpected {method} {u}")


def sample_rows():
    rows, _ = build_rows(load_fixture("odds_sample.json"),
                         datetime(2026, 10, 17, 20, tzinfo=timezone.utc),
                         ("fanduel", "pinnacle"), ("h2h", "spreads", "totals"))
    return rows


class SheetsSinkTest(unittest.TestCase):
    def test_new_sheet_gets_tab_header_and_rows(self):
        api = FakeSheets(tabs=("Sheet1",))
        n = SheetsSink(api, SID, TAB, COLUMNS).append(sample_rows())
        self.assertEqual(n, 34)
        self.assertIn(TAB, api.tabs)
        self.assertEqual(api.header, list(COLUMNS))
        self.assertEqual(len(api.rows), 34)

    def test_existing_header_is_kept_and_rows_appended(self):
        api = FakeSheets(header=list(COLUMNS))
        sink = SheetsSink(api, SID, TAB, COLUMNS)
        sink.append(sample_rows())
        sink.append(sample_rows())
        self.assertEqual(len(api.rows), 68)
        self.assertFalse(any(m == "PUT" for m, _ in api.calls))  # header never rewritten

    def test_refuses_different_header(self):
        api = FakeSheets(header=["game_id", "book"])
        with self.assertRaises(SheetsError):
            SheetsSink(api, SID, TAB, COLUMNS).append(sample_rows())
        self.assertEqual(api.rows, [])

    def test_value_types(self):
        api = FakeSheets(header=list(COLUMNS))
        SheetsSink(api, SID, TAB, COLUMNS).append(sample_rows())
        row = dict(zip(COLUMNS, api.rows[2]))  # FanDuel puck line, Toronto -1.5
        self.assertEqual(row["line"], -1.5)
        self.assertEqual(row["price_american"], 172)
        self.assertEqual(row["price_decimal"], 2.72)
        self.assertIs(row["pre_start_check"], True)
        self.assertEqual(row["snapshot_time_utc"], "2026-10-17T20:00:00Z")
        self.assertEqual(to_sheet_value("line", ""), "")

    def test_retries_then_succeeds(self):
        api = FakeSheets(header=list(COLUMNS), fail=[429, 503])
        n = SheetsSink(api, SID, TAB, COLUMNS, backoff_seconds=0).append(sample_rows())
        self.assertEqual(n, 34)

    def test_permission_error_explains_sharing(self):
        api = FakeSheets(fail=[403])
        with self.assertRaises(SheetsError) as ctx:
            SheetsSink(api, SID, TAB, COLUMNS).append(sample_rows())
        self.assertIn("Share the sheet", str(ctx.exception))

    def test_tab_name_with_underscore_is_quoted(self):
        api = FakeSheets(header=list(COLUMNS))
        SheetsSink(api, SID, TAB, COLUMNS).append(sample_rows())
        self.assertTrue(any("'Odds_Snapshots'!A1:append" in u for _, u in api.calls))


class SheetIdTest(unittest.TestCase):
    def test_bare_id_and_url(self):
        url = f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0"
        for value in (SID, url, f"  {url}  "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SHEET_ID": value}):
                self.assertEqual(get_sheet_id(), SID)

    def test_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                get_sheet_id()


if __name__ == "__main__":
    unittest.main()
