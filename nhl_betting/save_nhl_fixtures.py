"""Save raw NHL /v1/score responses for a date range (new test fixtures).

    python -m nhl_betting.save_nhl_fixtures 2026-10-07 2026-10-17
    python -m nhl_betting.save_nhl_fixtures 2026-10-07 2026-10-17 --dry-run   # list dates only

Free (no key, no Odds API credits). Files go to output/nhl_fixtures/score_<date>.json
unchanged, and the log lists how each final game ended so OT/SO dates are easy to spot.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import load_config
from .nhl_api import NhlApiClient, NhlApiError

log = logging.getLogger("nhl_betting.save_nhl_fixtures")


def date_range(first: date, last: date) -> list[date]:
    if last < first:
        raise ValueError("end date is before start date")
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def summarize(payload: dict) -> str:
    parts = []
    for g in payload.get("games", []):
        home, away = g.get("homeTeam", {}), g.get("awayTeam", {})
        end = (g.get("gameOutcome") or {}).get("lastPeriodType", "-")
        parts.append(f"{g.get('id')} {away.get('abbrev')} {away.get('score', '')}"
                     f" @ {home.get('abbrev')} {home.get('score', '')}"
                     f" [{g.get('gameType')}/{g.get('gameState')}/{end}]")
    return "; ".join(parts) or "no games"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    parser.add_argument("--out", default="output/nhl_fixtures")
    parser.add_argument("--dry-run", action="store_true", help="list dates; no API calls")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    days = date_range(args.start, args.end)
    if args.dry_run:
        log.info("DRY RUN: would save %d dates: %s", len(days), ", ".join(map(str, days)))
        return 0
    cfg = load_config()
    client = NhlApiClient(cfg.nhl_api_base_url, cfg.nhl_api_timeout_seconds)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        for d in days:
            payload = client._get(f"/score/{d.isoformat()}")  # raw: save even if the shape changed
            (out / f"score_{d.isoformat()}.json").write_text(json.dumps(payload, indent=2))
            log.info("%s: %s", d, summarize(payload))
    except NhlApiError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
