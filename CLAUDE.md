# NHL Betting System

Paper-first NHL game-line system. GitHub Actions pull NHL data and FanDuel and Pinnacle odds into a Google Sheet and compute everything (no-vig baseline, EV, stakes, CLV). A separate claude.ai Project reads the sheet nightly and writes picks. This repo is only the data and math layer. The full plan is in `docs/project-overview.md`; read it before starting a new build step.

## Current step

Step 2: NHL API puller → Tonight and Results. Work on this step only. When it's done, update this line to the next step from the build order in the overview.

**Done when:** Tonight lists every game; yesterday's finals and OT/SO flags appear.

Step 1 (odds puller → Odds_Snapshots) finished Oct 17, 2026: real run wrote 168 rows to the sheet for 3 credits. Runs are manual only (see Scheduling).

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
| Tonight | Code; Claude fills `claude_prob`, `claude_reason` | Never overwrite `claude_prob` or `claude_reason`. `adjusted_ev` is a sheet formula: EV from `claude_prob` if filled, else `baseline_prob`. Must include `projected_goalies` (Daily Faceoff projection when the baseline was captured). |
| Odds_Snapshots | Code, append only | Never edit or delete rows. Drop any snapshot taken after puck drop before writing. |
| Picks, Parlays | Claude, then me | Code reads only, except Results fills closing prices and CLV. |
| Results | Code | Final score, OT/SO flag, settlement per market, closing prices for both books, CLV per pick. |
| Review | Formulas | Don't write from code. |

## Math (must match the rulebook)

- No-vig probability: proportional (divide each side's implied probability by the sum).
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

- **Manual runs only.** No `schedule:` triggers. Workflows run only when I ask Claude to start them (`gh workflow run pull_odds.yml --ref main`) or I press "Run workflow". Don't add a schedule without asking.
- With no schedules, the 60-day auto-disable doesn't apply, so no heartbeat commit job is needed.
- For closing prices (step 4), one manually started evening job can sleep until each start-time group (about 15 minutes before) instead of many runs.

## Plan first

For anything larger than a single-file change, explore the repo and propose a short plan before editing. Ask before deleting files, rewriting history, or changing workflow schedules.
