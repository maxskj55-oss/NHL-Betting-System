"""Keyed sheet tabs used by the morning job (Tonight, Tonight_Archive, Results, Game_Map).

`Tab` is the interface; `sheets.SheetTab` is the Google Sheets version and
`MemoryTab` the in-memory one used by --dry-run and tests. Each tab declares
`protected` columns that code must never write (Claude's cells, step-4 columns):
both implementations refuse an update that touches them.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, Sequence

log = logging.getLogger(__name__)


class ProtectedColumnError(RuntimeError):
    """Code tried to write a column it doesn't own."""


def norm(value: Any) -> str:
    """Comparable key text: 2026020119.0 -> '2026020119', None -> ''."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def row_key(row: dict, key_cols: Sequence[str]) -> tuple[str, ...]:
    return tuple(norm(row.get(c)) for c in key_cols)


class Tab(Protocol):
    name: str
    columns: list[str]
    protected: frozenset[str]

    def read_rows(self) -> list[tuple[int, dict]]: ...
    def append_rows(self, rows: Sequence[dict], key_cols: Sequence[str]) -> int: ...
    def update_cells(self, updates: Sequence[tuple[int, dict]]) -> int: ...
    def delete_rows(self, row_numbers: Sequence[int]) -> int: ...


def check_writable(tab: Tab, row: dict, new_row: bool) -> None:
    """Updates may not name protected columns; new rows must leave them blank."""
    for col, value in row.items():
        if col not in tab.columns:
            raise ProtectedColumnError(f"{tab.name}: unknown column {col!r}")
        if col in tab.protected and (not new_row or norm(value) != ""):
            raise ProtectedColumnError(f"{tab.name}: code may not write {col!r}")


class MemoryTab:
    """A tab held in memory. Row numbers match a sheet: header is row 1."""

    def __init__(self, name: str, columns: Sequence[str], protected: Sequence[str] = (),
                 rows: Sequence[dict] = ()):
        self.name = name
        self.columns = list(columns)
        self.protected = frozenset(protected)
        self.rows: list[dict] = [{c: r.get(c, "") for c in self.columns} for r in rows]
        self.appended: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self.deleted: list[int] = []

    def read_rows(self) -> list[tuple[int, dict]]:
        return [(i + 2, dict(r)) for i, r in enumerate(self.rows)]

    def append_rows(self, rows: Sequence[dict], key_cols: Sequence[str]) -> int:
        for r in rows:
            check_writable(self, r, new_row=True)
        full = [{c: r.get(c, "") for c in self.columns} for r in rows]
        self.rows.extend(full)
        self.appended.extend(full)
        return len(full)

    def update_cells(self, updates: Sequence[tuple[int, dict]]) -> int:
        for rownum, cells in updates:
            check_writable(self, cells, new_row=False)
            self.rows[rownum - 2].update(cells)
            self.updated.append((rownum, dict(cells)))
        return len(updates)

    def delete_rows(self, row_numbers: Sequence[int]) -> int:
        for n in sorted(set(row_numbers), reverse=True):
            del self.rows[n - 2]
            self.deleted.append(n)
        return len(set(row_numbers))
