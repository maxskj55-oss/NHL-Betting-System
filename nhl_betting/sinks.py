"""Where Odds_Snapshots rows go. Append only: never edit or delete rows.

CsvSink is the stand-in until the Google Sheet exists; a SheetsSink with the
same `append` method will replace it.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Protocol, Sequence

log = logging.getLogger(__name__)


class Sink(Protocol):
    def append(self, rows: Sequence[dict]) -> int: ...


class CsvSink:
    def __init__(self, path: Path, columns: Sequence[str]):
        self.path = Path(path)
        self.columns = list(columns)

    def append(self, rows: Sequence[dict]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        if not new_file:
            with self.path.open(newline="") as f:
                header = next(csv.reader(f), [])
            if header != self.columns:
                raise ValueError(
                    f"{self.path} has columns {header}, expected {self.columns}. "
                    "Not appending to a file with a different layout."
                )
        with self.path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.columns, extrasaction="raise")
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        log.info("Appended %d rows to %s", len(rows), self.path)
        return len(rows)


class PrintSink:
    """Dry run: show what would be written, write nothing."""

    def __init__(self, columns: Sequence[str]):
        self.columns = list(columns)

    def append(self, rows: Sequence[dict]) -> int:
        log.info("DRY RUN: would append %d rows to Odds_Snapshots", len(rows))
        widths = {c: max([len(c)] + [len(str(r[c])) for r in rows]) for c in self.columns}
        line = "  ".join(c.ljust(widths[c]) for c in self.columns)
        out = [line, "-" * len(line)]
        out += ["  ".join(str(r[c]).ljust(widths[c]) for c in self.columns) for r in rows]
        log.info("\n%s", "\n".join(out))
        return len(rows)
