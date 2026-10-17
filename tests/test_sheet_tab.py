"""SheetTab (keyed tab writer) against a fake Sheets API grid: exact cells only."""

import json
import re
from urllib.parse import unquote

import pytest
import requests

from nhl_betting.morning import ARCHIVE_COLUMNS, TONIGHT_COLUMNS, TONIGHT_FORMULAS, TONIGHT_PROTECTED
from nhl_betting.sheets import SheetsError, SheetTab
from nhl_betting.tabs import ProtectedColumnError

SID = "sid"
TAB = "Tonight"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeGrid:
    """One tab as a list of lists (row 0 = header). Records every request."""

    def __init__(self, header=None, rows=(), faults=None):
        self.grid = ([list(header)] if header else []) + [list(r) for r in rows]
        self.exists = header is not None
        self.calls = []
        self.faults = list(faults or [])   # per POST append: "saved_503", "timeout", 503

    def request(self, method, url, params=None, json=None, timeout=None):
        u = unquote(url)
        self.calls.append((method, u, json))
        if method == "GET" and "/values/" not in u:
            sheets = [{"properties": {"title": TAB, "sheetId": 7}}] if self.exists else []
            return Resp(body={"sheets": sheets})
        if u.endswith(f"{SID}:batchUpdate"):
            for r in json["requests"]:
                if "addSheet" in r:
                    self.exists = True
                elif "sortRange" in r:
                    idx = [sp["dimensionIndex"] for sp in r["sortRange"]["sortSpecs"]]
                    self.grid[1:] = sorted(self.grid[1:], key=lambda row: [
                        str(row[i]) if i < len(row) else "" for i in idx])
                else:
                    rng = r["deleteDimension"]["range"]
                    del self.grid[rng["startIndex"]:rng["endIndex"]]
            return Resp()
        if u.endswith("values:batchUpdate"):
            for d in json["data"]:
                m = re.match(r"'.+'!([A-Z]+)(\d+)$", d["range"])
                c, r = col_index(m.group(1)), int(m.group(2)) - 1
                while len(self.grid) <= r:
                    self.grid.append([])
                row = self.grid[r]
                row.extend([""] * (c + 1 - len(row)))
                row[c] = d["values"][0][0]
            return Resp()
        if u.endswith(":append"):
            fault = self.faults.pop(0) if self.faults else None
            if fault == "timeout":
                raise requests.Timeout()
            if fault in (503,):
                return Resp(503)
            self.grid.extend([list(v) for v in json["values"]])
            if fault == "saved_503":
                return Resp(503)
            return Resp(body={"updates": {"updatedRows": len(json["values"])}})
        if method == "PUT":
            col = col_index(re.search(r"!([A-Z]+)1$", u).group(1))
            if col == 0:
                self.grid = [json["values"][0]] + self.grid
            else:                                  # extend the header row
                self.grid[0] = self.grid[0][:col] + list(json["values"][0])
            return Resp()
        if method == "GET":
            if "!A2:" in u:
                return Resp(body={"values": self.grid[1:]})
            return Resp(body={"values": self.grid[:1]} if self.grid else {})
        raise AssertionError(f"unexpected {method} {u}")


def tab(grid):
    return SheetTab(grid, SID, TAB, TONIGHT_COLUMNS, TONIGHT_PROTECTED, formulas=TONIGHT_FORMULAS,
                    backoff_seconds=0)


def blank_row(**kw):
    r = [""] * len(TONIGHT_COLUMNS)
    for k, v in kw.items():
        r[TONIGHT_COLUMNS.index(k)] = v
    return r


def test_creates_tab_and_header_then_refuses_other_layouts():
    g = FakeGrid()
    tab(g).ensure()
    assert g.exists and g.grid[0] == list(TONIGHT_COLUMNS)
    bad = FakeGrid(header=["game_id", "home"])
    with pytest.raises(SheetsError, match="expected"):
        tab(bad).ensure()


def test_update_writes_only_the_named_cells_and_never_claude_columns():
    g = FakeGrid(TONIGHT_COLUMNS, [blank_row(game_id=1, home="X", claude_prob=0.6,
                                             claude_reason="why", adjusted_ev="=f()")])
    t = tab(g).ensure()
    t.update_cells([(2, {"home": "WSH", "home_b2b": True})])
    row = dict(zip(TONIGHT_COLUMNS, g.grid[1]))
    assert (row["home"], row["home_b2b"]) == ("WSH", True)
    assert (row["claude_prob"], row["claude_reason"], row["adjusted_ev"]) == (0.6, "why", "=f()")
    ranges = [d["range"] for _, u, body in g.calls if u.endswith("values:batchUpdate") for d in body["data"]]
    assert ranges == ["'Tonight'!E2", "'Tonight'!G2"]
    with pytest.raises(ProtectedColumnError):
        t.update_cells([(2, {"claude_prob": 0.1})])
    with pytest.raises(SheetsError):
        t.update_cells([(1, {"home": "x"})])   # header row


def test_read_rows_keeps_sheet_row_numbers_and_skips_blank_rows():
    g = FakeGrid(TONIGHT_COLUMNS, [blank_row(game_id=1), [], blank_row(game_id=3)])
    assert [(n, r["game_id"]) for n, r in tab(g).read_rows()] == [(2, 1), (4, 3)]


def test_append_retry_never_duplicates():
    g = FakeGrid(TONIGHT_COLUMNS, faults=["saved_503"])
    t = tab(g).ensure()
    assert t.append_rows([{"game_id": 5}, {"game_id": 6}], ("game_id", "market", "side")) == 2
    assert [r[0] for r in g.grid[1:]] == [5, 6]
    g2 = FakeGrid(TONIGHT_COLUMNS, faults=["timeout"])
    tab(g2).ensure().append_rows([{"game_id": 5}], ("game_id", "market", "side"))
    assert [r[0] for r in g2.grid[1:]] == [5]


def test_delete_is_bottom_up_and_never_retried():
    g = FakeGrid(TONIGHT_COLUMNS, [blank_row(game_id=i) for i in (1, 2, 3, 4)])
    t = tab(g).ensure()
    t.delete_rows([2, 4])
    assert [r[0] for r in g.grid[1:]] == [2, 4]
    deletes = [b for _, u, b in g.calls if u.endswith(f"{SID}:batchUpdate")]
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in deletes[-1]["requests"]]
    assert starts == [3, 1]

    class Flaky(FakeGrid):
        def request(self, method, url, **kw):
            if unquote(url).endswith(f"{SID}:batchUpdate"):
                self.calls.append((method, url, kw.get("json")))
                return Resp(503)
            return super().request(method, url, **kw)
    f = Flaky(TONIGHT_COLUMNS, [blank_row(game_id=1)])
    with pytest.raises(SheetsError):
        tab(f).ensure().delete_rows([2])
    assert sum(1 for _, u, _ in f.calls if unquote(u).endswith(f"{SID}:batchUpdate")) == 1
    with pytest.raises(SheetsError):
        tab(FakeGrid(TONIGHT_COLUMNS)).delete_rows([1])


def test_step3_header_gains_claude_line_at_the_end_only():
    old_header = list(TONIGHT_COLUMNS[:-1])                 # the step-2 sheet
    g = FakeGrid(old_header, [blank_row(game_id=1)[:-1]])
    tab(g).ensure()
    assert g.grid[0] == list(TONIGHT_COLUMNS)
    puts = [u for m, u, _ in g.calls if m == "PUT"]
    assert len(puts) == 1 and puts[0].endswith("'Tonight'!V1")
    assert g.grid[1][0] == 1                                # data untouched
    # Archive: claude_line goes after archived_at_utc, existing order kept.
    a = FakeGrid(list(ARCHIVE_COLUMNS[:-1]))
    SheetTab(a, SID, TAB, ARCHIVE_COLUMNS, backoff_seconds=0).ensure()
    assert a.grid[0][-2:] == ["archived_at_utc", "claude_line"]
    # Reordered or renamed columns are still refused.
    swapped = list(TONIGHT_COLUMNS[:-1])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    with pytest.raises(SheetsError, match="expected"):
        tab(FakeGrid(swapped)).ensure()


def test_formulas_are_user_entered_and_values_stay_raw():
    g = FakeGrid(TONIGHT_COLUMNS, [blank_row(game_id=1, game_date_et="2026-10-17")])
    t = tab(g).ensure()
    t.update_cells([(2, {"line": 6.5, "auto_lean": "pass", "adjusted_ev": "=LET(r,ROW(),1)"})])
    writes = [b for _, u, b in g.calls if u.endswith("values:batchUpdate")]
    assert [w["valueInputOption"] for w in writes] == ["RAW", "USER_ENTERED"]
    assert [d["range"] for d in writes[1]["data"]] == ["'Tonight'!T2"]
    with pytest.raises(ProtectedColumnError):
        t.update_cells([(2, {"stake_units": 0.5})])        # a value in a formula column
    with pytest.raises(SheetsError):
        t.update_cells([(2, {"side": "=HACK()"})])          # a formula in a value column


def test_sort_moves_whole_rows():
    g = FakeGrid(TONIGHT_COLUMNS, [blank_row(game_id=2, market="total", claude_prob=0.6),
                                   blank_row(game_id=1, market="moneyline"),
                                   blank_row(game_id=2, market="moneyline")])
    tab(g).ensure().sort_rows(("game_id", "market"))
    rows = [dict(zip(TONIGHT_COLUMNS, r)) for r in g.grid[1:]]
    assert [(r["game_id"], r["market"]) for r in rows] == [(1, "moneyline"), (2, "moneyline"), (2, "total")]
    assert rows[2]["claude_prob"] == 0.6
