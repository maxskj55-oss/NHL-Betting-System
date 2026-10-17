# Test fixtures

- `events_sample.json`, `odds_sample.json`: handmade in the documented Odds API v4 shape
  (3 games; Pinnacle has no puck line for the Vegas game). Prices are made up.
- After the first real run, copy the raw response from the run's `output/raw/` artifact
  here as a real-data fixture so tests also cover FanDuel's and Pinnacle's actual output.
