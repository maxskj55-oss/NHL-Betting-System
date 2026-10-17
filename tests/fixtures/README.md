# Test fixtures

- `events_sample.json`, `odds_sample.json`: handmade in the documented Odds API v4 shape
  (3 games; Pinnacle has no puck line for the Vegas game). Prices are made up.
- `real/`: raw `/odds` responses from real runs. Every `.json` file here is checked by
  `tests/test_real_fixtures.py` (schema, team names, two sides per market, matching lines).
  To add one: download a run's artifact zip from its Actions page, take `raw/odds_*.json`,
  and copy it here unchanged.
