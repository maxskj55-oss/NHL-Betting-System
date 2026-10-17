# NHL Betting System

Paper-first NHL game-line system. GitHub Actions pull NHL data and FanDuel and Pinnacle odds into a Google Sheet and compute everything (no-vig baseline, EV, stakes, CLV). A separate claude.ai Project reads the sheet nightly and writes picks. This repo is only the data and math layer. The full plan is in `docs/project-overview.md`; read it before starting a new build step.

## Current step

Step 3: Pinnacle no-vig baseline, EV, stake, auto leans. Work on this step only. When it's done, update this line to the next step from the build order in the overview.

**Done when:** Every game line shows baseline probability, fair price and EV.

Step 1 (odds puller → Odds_Snapshots) finished Oct 17, 2026: real run wrote 168 rows to the sheet for 3 credits. Pull odds runs are manual only (see Scheduling).

Step 2 (NHL API → Tonight, Results, Game_Map) finished Oct 17, 2026: manual real run of `morning.yml` wrote 14 Tonight rows, 15 Results rows (Oct 14–16 finals, OT/SO flags and settlements) and 14 Game_Map rows, 0 Odds API credits. Daily schedule starts Oct 18.

## Checkpoints (stop signals)

- **Hour 15:** if the job can't run unattended and write tonight's lines, stop coding and switch to the Odds API Google Sheets add-on.
- **Hour 50:** if one full week of the complete loop (data → Claude pick on the sheet → closing line attached) doesn't exist, stop the project.

If I ask for a feature outside the current step before its checkpoint, name the checkpoint and ask whether I still want it.

## Data sources and limits

- **The Odds API:** sport `icehockey_nhl`, markets `h2h,spreads,totals`, bookmakers `fanduel,pinnacle`. Cost = markets × regions; a bookmaker list of up to 10 counts as one region, so one pull of all games = 3 credits. Free tier is 500 credits a month; budget about 300. Log the `x-requests-used` and `x-requests-remaining` headers on every call, and fail before a pull that would exceed the monthly budget in config.
- **NHL web API (unofficial, no key):** schedule, rosters, results. Endpoints can change without notice, so tests should fail loudly on schema changes.
- **MoneyPuck:** download the data files only; site scraping is blocked. Credit MoneyPuck. Only needed for the optional model.
- **Google Sheets API:** service account; 300 requests a minute per project. Batch writes.

## Sheet contract

Column names are shared with the claude.ai Project's rulebook. Don't rename or reorder columns without telling me, because the nightly picker depends on them.

| Tab | Written by | Rules |
| --- | --- | --- |
| Tonight | Code; Claude fills `claude_prob`, `claude_reason` | Today's games only. Never overwrite `claude_prob` or `claude_reason`. The morning job writes one game row per game (`market` blank); Pull odds turns it into one row per game × market × side (key: `game_id`, `market`, `side`) from the latest pre-start snapshot. `market` is `moneyline`, `puck_line` or `total`; `side` is the NHL team code (as in `home`/`away`) or `over`/`under`; `line` is FanDuel's. `adjusted_ev` and `stake_units` are sheet formulas that code writes (never values): `adjusted_ev` = EV of `fanduel_price` at `claude_prob` if filled and `claude_line` is blank or equals `line`, else at `baseline_prob`; `stake_units` from `adjusted_ev` using the bands in config. `claude_line` (last column, added in step 3) is written by code once: the line the row showed when Pull odds first saw `claude_prob` filled. Must include `projected_goalies` (Daily Faceoff projection when the baseline was captured, written by Pull odds; later step). |
| Tonight_Archive | Code, append only | Past days' Tonight rows, copied whole (Claude's cells and the formulas' computed values included) before they leave Tonight. Same columns plus `archived_at_utc`; `claude_line` comes after `archived_at_utc` because it was added later. |
| Game_Map | Code, append only | `odds_event_id` → NHL `game_id`; join key from odds to results. |
| Odds_Snapshots | Code, append only | Never edit or delete rows. Drop any snapshot taken after puck drop before writing. |
| Picks, Parlays | Claude, then me | Code reads only, except Results fills closing prices and CLV. |
| Results | Code | One row per final game: score (shootout goal included), OT/SO flag, settlement per market, closing prices for both books, CLV per pick. Rows are never rewritten; step 4 fills only `closing_fanduel`, `closing_pinnacle`, `clv` (their exact shape is settled in step 4). |
| Review | Formulas | Don't write from code. |

## Math (must match the rulebook)

- No-vig probability: proportional (divide each side's implied probability by the sum). Pinnacle is the baseline; implied probabilities use the exact American price.
- Baseline (step 3): `baseline_prob` = Pinnacle no-vig for the same side and line; `fair_price` = American odds of `baseline_prob`; `baseline_ev` = `baseline_prob` × FanDuel decimal − 1. If the books hang different puck lines or totals, no conversion is guessed: baseline blank, `auto_lean` = "pass: line mismatch". Pinnacle missing: "pass: no baseline".
- `auto_lean` from `baseline_ev`: "bet candidate" at `pricing.bet_candidate_min_ev` (3%) or more, "lean" above 0, "pass" at 0 or below. `stake_units` from `adjusted_ev`: 3–5% = 0.5, 5–8% = 0.75, 8% or more = 1.0, maximum 1.5, 0 below 3% (`pricing.stake_bands`, `pricing.max_units`).
- CLV = decimal price taken × closing no-vig probability of that side − 1. Pinnacle's close first; FanDuel's no-vig close if Pinnacle is missing.
- Settlement: moneyline, puck line and totals include OT and shootout; the shootout winner gets +1 goal and the total gets exactly +1 goal. Whole-number totals push.

## Code conventions

- Python, modular: one module per source plus `sheets`, `pricing` and `config`. Settings in `config.yaml`, logging (not print), pytest tests, minimal dependencies.
- No secrets in code or config. Read `ODDS_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` and `SHEET_ID` from environment variables (GitHub Secrets in Actions).
- Every script supports `--dry-run`, which prints what it would write and makes no API calls that cost credits (use saved fixtures).

## Verify before saying a step is done

1. `pytest` passes, including tests built from saved API fixtures.
2. A `--dry-run` shows the expected rows.
3. One real run: the rows appear in the sheet and the logged credit count is what you expected.

Report what you ran and what you saw. Don't claim a step works from code reading alone.

## Scheduling

- **Scheduled (approved Oct 17, 2026):**
  - `morning.yml` runs daily at 12:17 UTC (8:17 am ET in daylight time, 7:17 am ET in winter; GitHub cron is UTC only and often starts 10–60 minutes late). It writes Game_Map, Results and Tonight from the NHL API and uses no Odds API credits (the key isn't passed to it).
  - `heartbeat.yml` commits `.github/heartbeat.txt` to main every Monday so GitHub doesn't disable the schedule after 60 days without repository activity.
- **Manual only:** `pull_odds.yml`, `mismatch_report.yml`, `save_nhl_fixtures.yml`. They run only when I ask Claude to start them (`gh workflow run pull_odds.yml --ref main`) or I press "Run workflow". Don't add or change a schedule without asking.
- **No automatic retries.** If a live run fails (scheduled or manual), don't start another. Report the error from the run page and wait for me. Re-run only when I say to, and at most once per request.
- Projected goalies (later step) belong in the Pull odds job, captured at odds-pull time, not in the morning job.
- For closing prices (step 4), one manually started evening job can sleep until each start-time group (about 15 minutes before) instead of many runs.
- Step 4 note: Results should settle each pick at the line it was taken, not only at the last snapshot's line.

## Plan first

For anything larger than a single-file change, explore the repo and propose a short plan before editing. Ask before deleting files, rewriting history, or changing workflow schedules.
