# Test fixtures

- `events_sample.json`, `odds_sample.json`: handmade in the documented Odds API v4 shape
  (3 games; Pinnacle has no puck line for the Vegas game). Prices are made up.
- `real/`: raw `/odds` responses from real runs. Every `.json` file here is checked by
  `tests/test_real_fixtures.py` (schema, team names, two sides per market, matching lines).
  To add one: download a run's artifact zip from its Actions page, take `raw/odds_*.json`,
  and copy it here unchanged.
- `nhl/score_<date>.json`: raw NHL web API `/v1/score/{date}` responses, saved unchanged by the
  "Save NHL fixtures" workflow (Oct 2026). They cover regulation, OT and shootout finals
  (Oct 10: a 1-0 shootout; Oct 14-15: several OT and SO games), yesterday's finals (Oct 16)
  and today's slate (Oct 17, which matches `real/odds_20261017T153438Z.json`).
  `tests/test_nhl_api.py` validates every file here and breaks copies of them to check that
  shape changes fail loudly. To add more: run "Save NHL fixtures" for a date range, then
  `git fetch origin fixtures/nhl-<run id>` and copy the files you need into `nhl/`.
