"""Google Sheets writers: append-only tabs (SheetsSink) and keyed tabs (SheetTab).

Uses the Sheets REST API with a service account. Never edits or deletes rows:
it creates the tab and header if missing, refuses to write under a different
header, then appends. Three API requests per run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Sequence
from urllib.parse import quote

import requests

from .config import ConfigError
from .tabs import check_writable, is_formula, norm, row_key

log = logging.getLogger(__name__)

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SheetsError(RuntimeError):
    """A Sheets API call failed or the tab layout doesn't match."""


def get_sheet_id() -> str:
    """SHEET_ID may be the bare ID or the full sheet URL."""
    raw = os.environ.get("SHEET_ID", "").strip()
    if not raw:
        raise ConfigError("SHEET_ID is not set (add it as a GitHub Secret).")
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def authorized_session():
    """requests-style session signed with the service account in GOOGLE_SERVICE_ACCOUNT_JSON."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not set (add it as a GitHub Secret).")
    try:
        info = json.loads(raw)
    except ValueError:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON. Paste the whole key file, "
            "including the { } braces."
        ) from None
    from google.auth.transport.requests import AuthorizedSession  # imported only for live runs
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
    log.info("Sheets: using service account %s", info.get("client_email", "?"))
    return AuthorizedSession(creds)


def to_sheet_value(column: str, value: Any) -> Any:
    """Numbers as numbers, the pre-start flag as a checkbox-style boolean, the rest as text."""
    if column == "line":
        return float(value) if value != "" else ""
    if column == "pre_start_check":
        return str(value).upper() == "TRUE"
    return value


class SheetsSink:
    def __init__(self, session, spreadsheet_id: str, tab: str, columns: Sequence[str],
                 max_attempts: int = 3, backoff_seconds: float = 2.0):
        self.session = session
        self.sid = spreadsheet_id
        self.tab = tab
        self.columns = list(columns)
        self.max_attempts = max_attempts
        self.backoff = backoff_seconds

    # -- HTTP -----------------------------------------------------------------
    def _call(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt < self.max_attempts:
                    log.warning("Sheets %s, retrying (%d/%d)", type(exc).__name__, attempt,
                                self.max_attempts)
                    time.sleep(self.backoff * attempt)
                    continue
                raise SheetsError(f"Sheets API unreachable: {type(exc).__name__}") from None
            if resp.status_code == 200:
                return resp.json() if resp.text else {}
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                log.warning("Sheets HTTP %s, retrying (%d/%d)", resp.status_code, attempt,
                            self.max_attempts)
                time.sleep(self.backoff * attempt)
                continue
            raise SheetsError(self._explain(resp))
        raise SheetsError("Sheets API: retries exhausted")  # pragma: no cover

    @staticmethod
    def _explain(resp) -> str:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = resp.text[:200]
        hint = ""
        if resp.status_code == 403:
            hint = (" Share the sheet with the service account's email "
                    "(Share button, Editor access) and check the Sheets API is enabled.")
        elif resp.status_code == 404:
            hint = " Check SHEET_ID is the ID from the sheet's URL."
        return f"Sheets API HTTP {resp.status_code}: {msg}.{hint}"

    def _range(self, a1: str) -> str:
        return quote(f"'{self.tab}'!{a1}", safe="")

    # -- Steps ----------------------------------------------------------------
    def _ensure_tab(self) -> None:
        meta = self._call("GET", f"{API}/{self.sid}", params={"fields": "sheets.properties.title"})
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if self.tab in titles:
            return
        log.info("Sheets: creating tab %s", self.tab)
        self._call("POST", f"{API}/{self.sid}:batchUpdate",
                   json={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]})

    def _ensure_header(self) -> None:
        last_col = _col_letter(len(self.columns))
        got = self._call("GET", f"{API}/{self.sid}/values/{self._range(f'A1:{last_col}1')}")
        header = (got.get("values") or [[]])[0]
        if not header:
            log.info("Sheets: writing header row to %s", self.tab)
            self._call("PUT", f"{API}/{self.sid}/values/{self._range('A1')}",
                       params={"valueInputOption": "RAW"}, json={"values": [self.columns]})
            return
        if (getattr(self, "extend_header", False) and len(header) < len(self.columns)
                and header == self.columns[:len(header)]):
            # New columns go only at the end; existing columns keep their order.
            added = self.columns[len(header):]
            start = _col_letter(len(header) + 1)
            self._call("PUT", f"{API}/{self.sid}/values/{self._range(f'{start}1')}",
                       params={"valueInputOption": "RAW"}, json={"values": [added]})
            log.warning("Sheets: added column(s) %s at the end of %s", ", ".join(added), self.tab)
            return
        if header != self.columns:
            raise SheetsError(
                f"{self.tab} header is {header}, expected {self.columns}. "
                "Not appending under a different layout."
            )

    def append(self, rows: Sequence[dict]) -> int:
        self._ensure_tab()
        self._ensure_header()
        if not rows:
            return 0
        values = [[to_sheet_value(c, r[c]) for c in self.columns] for r in rows]
        snaps = {r["snapshot_time_utc"] for r in rows}
        if len(snaps) != 1:
            raise SheetsError("All rows in one append must share one snapshot_time_utc")
        res = self._append_once(values, snaps.pop())
        if res is None:  # an earlier attempt had already written these rows
            return len(values)
        written = res.get("updates", {}).get("updatedRows", len(values))
        log.info("Appended %d rows to sheet tab %s (%s)", written, self.tab,
                 res.get("updates", {}).get("updatedRange", "?"))
        if written != len(values):
            raise SheetsError(f"Sheets wrote {written} rows, expected {len(values)}")
        return written


    def _rows_with_snapshot(self, snap: str) -> int:
        got = self._call("GET", f"{API}/{self.sid}/values/{self._range('A:A')}")
        return sum(1 for row in got.get("values", []) if row and row[0] == snap)

    def _append_once(self, values: list[list], snap: str) -> dict | None:
        """Append with retries that never duplicate rows.

        Before each retry, count rows already carrying this run's snapshot time.
        If the previous attempt landed (Google saved it, then the reply was lost
        or errored), stop instead of writing again.
        """
        url = f"{API}/{self.sid}/values/{self._range('A1')}:append"
        params = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                already = self._rows_with_snapshot(snap)
                if already == len(values):
                    log.warning("Sheets: previous attempt had already written %d rows; "
                                "not writing again", already)
                    return None
                if already:
                    raise SheetsError(
                        f"Sheets has {already} of {len(values)} rows for snapshot {snap}; "
                        "stopping so rows aren't duplicated. Check the tab by hand."
                    )
            try:
                resp = self.session.request("POST", url, params=params,
                                            json={"values": values}, timeout=30)
            except requests.RequestException as exc:
                reason = type(exc).__name__
            else:
                if resp.status_code == 200:
                    return resp.json() if resp.text else {}
                if resp.status_code not in RETRY_STATUSES:
                    raise SheetsError(self._explain(resp))
                reason = f"HTTP {resp.status_code}"
            if attempt == self.max_attempts:
                raise SheetsError(f"Sheets append failed after {attempt} attempts ({reason})")
            log.warning("Sheets append %s, checking before retry (%d/%d)", reason, attempt,
                        self.max_attempts)
            time.sleep(self.backoff * attempt)
        return None  # pragma: no cover


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_tab(session, spreadsheet_id: str, tab: str) -> list[dict]:
    """Read a whole tab as dicts keyed by its header row (read-only, one request)."""
    url = f"{API}/{spreadsheet_id}/values/{quote(repr_tab(tab), safe='')}"
    resp = session.request("GET", url, params={"valueRenderOption": "UNFORMATTED_VALUE"},
                           timeout=60)
    if resp.status_code != 200:
        raise SheetsError(SheetsSink._explain(resp))
    data = resp.json()
    values = data.get("values", [])
    log.info("Sheets: read %s (%d rows incl. header)", data.get("range", "?"), len(values))
    if not values:
        return []
    header, body = values[0], values[1:]
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in body]


def repr_tab(tab: str) -> str:
    return f"'{tab}'"


def _cell_value(value: Any) -> Any:
    """Numbers and booleans stay typed; everything else is text (RAW input)."""
    if isinstance(value, (bool, int, float)) or value == "":
        return value
    return str(value)


class SheetTab(SheetsSink):
    """A keyed tab: read rows, append new rows, update owned cells, delete rows.

    Writes go only to the cells named; protected columns (Claude's, step 4's)
    are refused before any request is sent. Formula columns take only formulas,
    sent with USER_ENTERED so the sheet evaluates them; everything else is RAW
    (so text like 2026-10-17 stays text). ensure() creates the tab and header
    if missing, adds columns that the contract appends at the end, and refuses
    any other header difference.
    """

    extend_header = True

    def __init__(self, session, spreadsheet_id: str, tab: str, columns: Sequence[str],
                 protected: Sequence[str] = (), formulas: Sequence[str] = (), **kwargs):
        super().__init__(session, spreadsheet_id, tab, columns, **kwargs)
        self.name = tab
        self.protected = frozenset(protected)
        self.formulas = frozenset(formulas)
        self._gid: int | None = None

    def ensure(self) -> "SheetTab":
        self._ensure_tab()
        self._ensure_header()
        return self

    def _sheet_gid(self) -> int:
        if self._gid is None:
            meta = self._call("GET", f"{API}/{self.sid}",
                              params={"fields": "sheets.properties(sheetId,title)"})
            for s in meta.get("sheets", []):
                if s["properties"]["title"] == self.tab:
                    self._gid = int(s["properties"]["sheetId"])
                    break
            else:
                raise SheetsError(f"Tab {self.tab} not found")
        return self._gid

    def read_rows(self) -> list[tuple[int, dict]]:
        last_col = _col_letter(len(self.columns))
        got = self._call("GET", f"{API}/{self.sid}/values/{self._range(f'A2:{last_col}')}",
                         params={"valueRenderOption": "UNFORMATTED_VALUE"})
        out = []
        for i, row in enumerate(got.get("values", [])):
            if not any(norm(v) for v in row):
                continue
            padded = list(row) + [""] * (len(self.columns) - len(row))
            out.append((i + 2, dict(zip(self.columns, padded))))
        return out

    def append_rows(self, rows: Sequence[dict], key_cols: Sequence[str]) -> int:
        if not rows:
            return 0
        for r in rows:
            check_writable(self, r, new_row=True)
        values = [[_cell_value(r.get(c, "")) for c in self.columns] for r in rows]
        wanted = {row_key(r, key_cols) for r in rows}
        url = f"{API}/{self.sid}/values/{self._range('A1')}:append"
        params = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:  # did the last attempt land after all?
                have = {row_key(r, key_cols) for _, r in self.read_rows()} & wanted
                if have == wanted:
                    log.warning("Sheets %s: previous attempt had already written the rows", self.tab)
                    return len(values)
                if have:
                    raise SheetsError(f"{self.tab}: {len(have)} of {len(wanted)} rows were written; "
                                      "stopping so rows aren't duplicated. Check the tab by hand.")
            try:
                resp = self.session.request("POST", url, params=params,
                                            json={"values": values}, timeout=30)
            except requests.RequestException as exc:
                reason = type(exc).__name__
            else:
                if resp.status_code == 200:
                    log.info("Appended %d rows to sheet tab %s", len(values), self.tab)
                    return len(values)
                if resp.status_code not in RETRY_STATUSES:
                    raise SheetsError(self._explain(resp))
                reason = f"HTTP {resp.status_code}"
            if attempt == self.max_attempts:
                raise SheetsError(f"{self.tab} append failed after {attempt} attempts ({reason})")
            log.warning("Sheets %s append %s, checking before retry", self.tab, reason)
            time.sleep(self.backoff * attempt)
        return 0  # pragma: no cover

    def update_cells(self, updates: Sequence[tuple[int, dict]]) -> int:
        """One batch request; each cell is its own range so nothing else is touched."""
        if not updates:
            return 0
        raw, formulas = [], []
        for rownum, cells in updates:
            check_writable(self, cells, new_row=False)
            if rownum < 2:
                raise SheetsError(f"{self.tab}: refusing to write the header row")
            for col, value in cells.items():
                letter = _col_letter(self.columns.index(col) + 1)
                cell = {"range": f"'{self.tab}'!{letter}{rownum}"}
                if col in self.formulas:
                    formulas.append(dict(cell, values=[[value]]))
                elif is_formula(value):
                    raise SheetsError(f"{self.tab}: {col} is not a formula column; refusing {value!r}")
                else:
                    raw.append(dict(cell, values=[[_cell_value(value)]]))
        for mode, data in (("RAW", raw), ("USER_ENTERED", formulas)):
            if data:
                self._call("POST", f"{API}/{self.sid}/values:batchUpdate",
                           json={"valueInputOption": mode, "data": data})
        log.info("Updated %d cells (%d formulas) in %d rows of %s",
                 len(raw) + len(formulas), len(formulas), len(updates), self.tab)
        return len(updates)

    def delete_rows(self, row_numbers: Sequence[int]) -> int:
        nums = sorted(set(row_numbers), reverse=True)  # bottom first so numbers stay valid
        if not nums:
            return 0
        if min(nums) < 2:
            raise SheetsError(f"{self.tab}: refusing to delete the header row")
        gid = self._sheet_gid()
        reqs = [{"deleteDimension": {"range": {"sheetId": gid, "dimension": "ROWS",
                                               "startIndex": n - 1, "endIndex": n}}}
                for n in nums]
        # No retry: a lost reply after a successful delete must not delete again.
        saved, self.max_attempts = self.max_attempts, 1
        try:
            self._call("POST", f"{API}/{self.sid}:batchUpdate", json={"requests": reqs})
        finally:
            self.max_attempts = saved
        log.info("Deleted %d rows from %s", len(nums), self.tab)
        return len(nums)

    def sort_rows(self, by: Sequence[str]) -> None:
        """Sort every row below the header, whole rows together (one request)."""
        specs = [{"dimensionIndex": self.columns.index(c), "sortOrder": "ASCENDING"} for c in by]
        self._call("POST", f"{API}/{self.sid}:batchUpdate", json={"requests": [{"sortRange": {
            "range": {"sheetId": self._sheet_gid(), "startRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(self.columns)},
            "sortSpecs": specs}}]})
        log.info("Sorted %s by %s", self.tab, ", ".join(by))
