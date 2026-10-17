# NHL Betting System

Paper-first NHL game-line system. GitHub Actions pull NHL data and FanDuel and Pinnacle odds into a Google Sheet and compute everything (no-vig baseline, EV, stakes, CLV). A separate claude.ai Project reads the sheet nightly and writes picks. This repo is only the data and math layer. The full plan is in `docs/project-overview.md`; read it before starting a new build step.

## Current step

Step 4: Closing snapshots, CLV, Parlays ledger. Work on this step only. When it's done, update this line to the next step from the build order in the overview.

**Done when:** Yesterday's picks show closing prices and CLV automatically.

Step 1 (odds puller → Odds_Snapshots) finished Oct 17, 2026: real run wrote 168 rows to the sheet for 3 credits. Pull odds runs are manual only (see Scheduling).

Step 2 (NHL API → Tonight, Results, Game_Map) finished Oct 17, 2026: manual real run of `morning.yml` wrote 14 Tonight rows, 15 Results rows (Oct 14–16 finals, OT/SO flags and settlements) and 14 Game_Map rows, 0 Odds API credits. Daily schedule starts Oct 18.

Step 3 (Pinnacle no-vig baseline, EV, stake, auto leans → Tonight) finished Oct 17, 2026: manual real Pull odds run (3 credits, 491 left) priced 13 games into 78 Tonight rows (60 pass, 18 "pass: line mismatch"; WSH–NJD had started and was left as its game row) and added `claude_line`; read back through the Sheets connector, `adjusted_ev` and `stake_units` formulas compute. The `claude_prob` branch of the formula is covered by tests but not yet seen live.

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
| Picks | Claude, then me | Code creates the tab with its header if missing, then only reads it (every column protected). Columns: `pick_id`, `pick_time_utc`, `game_id`, `market`, `side`, `line`, `price`, `baseline_prob`, `claude_prob`, `claude_reason`, `goalie_status`, `label`, `ev`, `stake_units`, `placed`, `actual_price`, `outside_prob`, `outside_source` (the last two added Oct 17, 2026: an outside model's probability for the same side and line, and its name; code extends an existing header at the end). `pick_id` is `YYYYMMDD-NN` (`TEST-` prefix for test picks: graded with "test" in `note`). `label` is "bet candidate", "lean" or "best available" (`PICK_LABELS`; "best available" = the night's best row picked to bet at a fixed small stake without positive EV, graded like any pick); passes aren't logged here (Tonight_Archive keeps them). `market`/`side` as in Tonight; `line` and `price` are FanDuel's when picked; `placed` yes/no; `actual_price` blank = `price`. |
| Parlays | Claude, then me | Created and read only, like Picks. Columns: `ticket_id` (`TYYYYMMDD-NN`), `ticket_time_utc`, `leg_pick_ids` (comma-separated Picks `pick_id`s), `combined_price`, `stake_units`, `placed`, `actual_price`. |
| Pick_Grades | Code, append only (morning job) | One row per graded item, key (`group`, `item_id`), never rewritten. `group`: `auto` (Tonight_Archive bet candidates and leans), `claude` (every Picks row), `placed` (Picks rows placed = yes, at `actual_price`), `parlay_leg`, `parlay`. Columns: `group`, `item_id`, `label`, `game_id`, `game_date_et`, `market`, `side`, `line`, `price_taken`, `prob_at_pick` (`claude_prob` if filled, else `baseline_prob`), `stake_units`, `result` (win/loss/push/void), `return_per_unit`, `close_snapshot_utc`, `close_pinnacle_line`, `close_pinnacle_price`, `close_fanduel_line`, `close_fanduel_price`, `close_prob`, `clv_book`, `clv`, `note`, `graded_at_utc`. An item is written once its game is final (or postponed/cancelled: void). A pick marked placed later gets its `placed` row on a later run; already-written rows don't change. |
| Results | Code | One row per final game: score (shootout goal included), OT/SO flag, settlement per market. Rows are never rewritten. `closing_fanduel`, `closing_pinnacle`, `clv` are superseded by Pick_Grades (step 4): they stay in place, blank, and code never writes them. |
| Review | Formulas | Don't write from code. |
| Week N (e.g. Week 3), Season Summary | Formulas (built Oct 17, 2026) | My bet card in a friend's NFL card layout, and season totals, tax set-aside and the Claude-vs-Pinnacle check. Display only: code never reads or writes them. Weeks count from the Monday in `Season Summary`!B5 (Oct 5, 2026). Stakes and the weekly cap are inputs there ($1 per bet in Week 3, $5 from Week 4, $20 a week). Tier on the card: STEAL when Claude's edge (estimate minus FanDuel implied) is at least `Season Summary`!B9 (5%), VALUE above 0, NO EDGE otherwise. |
| Card_Data | Formulas | Helper for the card: one row per Picks row (rows 2–201) and per Parlays row (202–301). Its formulas address Picks columns A–P, Parlays A–G and Pick_Grades by **column letter**, so reordering or inserting columns in those tabs breaks the card; new columns go at the end only. Code never writes it. |

## Math (must match the rulebook)

- No-vig probability: proportional (divide each side's implied probability by the sum). Pinnacle is the baseline; implied probabilities use the exact American price.
- Baseline (step 3): `baseline_prob` = Pinnacle no-vig for the same side and line; `fair_price` = American odds of `baseline_prob`; `baseline_ev` = `baseline_prob` × FanDuel decimal − 1. If the books hang different puck lines or totals, no conversion is guessed: baseline blank, `auto_lean` = "pass: line mismatch". Pinnacle missing: "pass: no baseline".
- `auto_lean` from `baseline_ev`: "bet candidate" at `pricing.bet_candidate_min_ev` (3%) or more, "lean" above 0, "pass" at 0 or below. `stake_units` from `adjusted_ev`: 3–5% = 0.5, 5–8% = 0.75, 8% or more = 1.0, maximum 1.5, 0 below 3% (`pricing.stake_bands`, `pricing.max_units`).
- CLV = decimal price taken × closing no-vig probability of that side − 1. Close = the game's last pre-start snapshot. Pinnacle's no-vig close when Pinnacle's closing line for that side equals the pick's line; else FanDuel's no-vig close at that line; else CLV blank and `note` says why (e.g. "no close at line 6.5 (pinnacle 6, fanduel 6)").
- Settlement: moneyline, puck line and totals include OT and shootout; the shootout winner gets +1 goal and the total gets exactly +1 goal. Whole-number totals push. Each pick settles at the line it was taken (its own `line`), from Results' score.
- Parlays: any losing leg loses the ticket. Void legs drop out (FanDuel's rule) and the ticket pays the remaining legs' product; pushed legs are treated as void too (assumption: FanDuel's general rules state only the void rule). All legs void = ticket void. Each leg gets its own CLV; the ticket's `clv` is the product of (1 + leg CLV) − 1.

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
- **Manual only:** `pull_odds.yml`, `closing.yml`, `mismatch_report.yml`, `save_nhl_fixtures.yml`. They run only when I ask Claude to start them (`gh workflow run pull_odds.yml --ref main`) or I press "Run workflow". Don't add or change a schedule without asking.
- **No automatic retries.** If a live run fails (scheduled or manual), don't start another. Report the error from the run page and wait for me. Re-run only when I say to, and at most once per request.
- Projected goalies (later step) belong in the Pull odds job, captured at odds-pull time, not in the morning job.
- **Closing job (`closing.yml`, step 4):** I press Run workflow once per betting night. It sleeps until 15 minutes before each start-time group (Tonight's `start_time_utc`), re-reads Picks and Tonight, and pulls (3 credits, every game not yet started, appended to Odds_Snapshots) only if the group has a Picks row or a Tonight row whose `auto_lean` is "bet candidate" or "lean"; empty groups are skipped and logged. Monthly budget check before every pull. It never writes Tonight, Picks or Parlays. GitHub stops jobs at 6 hours: when the next wake-up is past 330 minutes of run time, the run waits until then and starts a fresh run of itself (workflow_dispatch, `hop`/`after` inputs) for later groups; at most 2 fresh runs a night. That hand-off is part of one press of Run workflow, not a retry: a failed run starts nothing.

## Plan first

For anything larger than a single-file change, explore the repo and propose a short plan before editing. Ask before deleting files, rewriting history, or changing workflow schedules.
