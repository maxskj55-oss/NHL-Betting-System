# NHL Betting System — Project Overview

Oct 16, 2026 · @Max Skjeveland

## Summary

Build a free, paper-first NHL game-line system: GitHub pulls data and odds into Google Sheets, Claude adds reasoned picks, and code grades every pick against Pinnacle's closing line. The realistic payoff is skills and an honest test of whether picks have an edge, not betting profit.

| Item | Decision |
| --- | --- |
| Verdict | Do a smaller version (confirmed after a second-opinion review) |
| Scope | Moneyline, puck line, game totals, and multi-game parlays built from those legs |
| Time | 40–65 hours core; optional own model adds 10–20 hours |
| Calendar | About 2–3.5 weeks at 20 hours/week |
| Cost | Free |
| Viability | 3 of 5. Biggest risk: the system works but the picks have no edge. |
| Usefulness | 1–2 of 5 for money, 4 of 5 for skills |
| Nights used | 1–3 per week, never Monday, Thursday or Sunday |
| Real money | Paper only until CLV beats your after-tax hurdle (Taxes section) |

The money outlook is weak: NHL game lines are efficient, and if you take the standard deduction you need roughly a 7–12% edge just to break even after tax.

## How it works

Code does all the math and data pulls; Claude adds judgment (goalie news, injuries) with a written reason; you decide what to bet.

&#91;embedded content: system flow · sources to sheet to Claude and you\]

Claude reads and writes only the sheet; GitHub does every pull and calculation, and you place any bet yourself.

| When (ET) | Who | What happens |
| --- | --- | --- |
| Every morning | GitHub (automatic) | Pulls schedule, results and stats from the NHL API and MoneyPuck. Writes the Tonight tab. |
| Afternoon, betting nights | GitHub | Pulls FanDuel and Pinnacle odds. Code computes Pinnacle no-vig probability, fair price, EV and stake. Logs an automatic lean for every game line. |
| 60–90 min before puck drop | You and Claude (chat) | Claude reads Tonight, searches for confirmed goalies and scratches, adjusts probabilities with a reason, and writes picks and parlays to the Picks tab. You choose what to place on FanDuel. |
| \~15 min before each start-time group | GitHub | Saves closing prices and discards any snapshot taken after puck drop. The sheet computes CLV. |
| Weekly | You and Claude | Review tab: CLV by market, automatic leans vs Claude picks, calibration. Adjust together. |

## Scope

Game lines only, because they fit the free odds tier; everything else costs paid API credits or can't be priced from the API.

| Market | In? | Why |
| --- | --- | --- |
| Moneyline (includes OT and shootout) | Yes | All games in one 3-credit pull |
| Puck line ±1.5 | Yes | Same pull |
| Game total (includes OT; shootout adds 1 goal) | Yes | Same pull |
| Multi-game parlays of qualifying legs | Yes | Priced from leg odds, no extra credits |
| Same-game parlays | No | The Odds API has no SGP prices |
| Player props, period lines, team totals, 3-way regulation | No | Priced per game; would exceed 500 free credits |
| Futures, live betting | No | Out of scope for the guide's pregame process |

**Parlay rules**

- Every leg must qualify as a straight bet on its own (positive EV above the threshold).
- No two legs from the same game.
- Stake 0.1–0.25 units per ticket, tracked in a separate Parlays ledger.
- CLV recorded for every leg; the ticket's win or loss is treated as mostly luck.
- Profit boosts: paste the token terms and Claude checks break-even (about 17.5% for a 3-leg parlay of -110 coin flips).

## Tools and data

Every tool is free at this scope; the binding limits are Odds API credits and your Claude plan's usage.

| Tool | Role | Price and limits |
| --- | --- | --- |
| [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) | FanDuel prices plus Pinnacle as sharp reference | Free: 500 credits/month. Cost = markets × regions; listing up to 10 bookmakers counts as 1 region, so 3 markets for all games = 3 credits. Budget \~300/month. Historical odds are paid only. |
| [Unofficial NHL web API](https://github.com/Zmalski/NHL-API-Reference) | Schedule, rosters, box scores, results | Free, no key, no official docs; endpoints can change |
| [MoneyPuck data](https://moneypuck.com/data.htm) | Team, goalie and shot stats (for the optional model) | Free for non-commercial use with credit; only download files, site scraping is blocked |
| GitHub Actions (public repo) | Runs the scripts on a schedule | Free and uncapped on public repos (private: 2,000 min/month). Keys stay in GitHub Secrets. |
| [Google Sheets API](https://developers.google.com/sheets/api/limits) + service account | Scripts write to your sheet | Free; 300 requests/minute per project |
| Google Sheets connector | Claude reads Tonight and writes Picks in chat | Free; connected in claude.ai |
| Odds API Google Sheets add-on | Fallback if the hour-15 checkpoint is missed | No code needed; scheduling not verified |
| Claude Project "NHL System" | Holds a \~1,500-word rulebook distilled from your guide | Your plan; estimated 5–10k tokens per night (not measured) |

Pinnacle odds come from its public website and may be delayed, so FanDuel's close is the backup CLV benchmark.

## Spreadsheet design

Six tabs; only Tonight and Picks are touched in the nightly chat, which keeps Claude's token use small.

| Tab | Written by | Key columns |
| --- | --- | --- |
| Tonight | GitHub | game\_id, start time, home, away, B2B flags, market, line, FanDuel price, Pinnacle no-vig probability, fair price, EV, auto lean |
| Odds\_Snapshots | GitHub (append only) | snapshot time, game\_id, book, market, side, line, price, start time, pre-start check |
| Picks | Claude, then you | pick time, game\_id, market, side, price, baseline probability, Claude probability, reason, EV, stake (units), placed? (yes/no), actual price |
| Parlays | Claude, then you | ticket id, leg ids, combined price, stake, placed?, result |
| Results | GitHub | game\_id, final score, OT/SO flag, settlement per market, closing prices both books, CLV per pick |
| Review | Formulas | CLV by market, auto leans vs Claude picks, calibration buckets, pick count, CLV vs tax hurdle |

Every snapshot is kept, never overwritten, so the sheet becomes your own odds history.

## Build order

Seven core steps total 40–65 hours; Claude writes most of the code, in Python with modules, a config file, logging, tests and no hard-coded keys.

| # | Piece | Hours | Done when |
| --- | --- | --- | --- |
| 1 | Odds puller → Odds\_Snapshots | 8–12 | Hour-15 checkpoint: runs unattended and writes tonight's FanDuel and Pinnacle game lines |
| 2 | NHL API puller → Tonight and Results | 4–6 | Tonight lists every game; yesterday's finals and OT/SO flags appear |
| 3 | Pinnacle no-vig baseline, EV, stake, auto leans | 3–5 | Every game line shows baseline probability, fair price and EV |
| 4 | Closing snapshots, CLV, Parlays ledger, weekly heartbeat commit | 8–12 | Yesterday's picks show closing prices and CLV automatically |
| 5 | Claude Project: rulebook, nightly prompt, Sheets connector | 5–10 | Hour-50 checkpoint: one full week of the complete loop |
| 6 | Review tab | 4–7 | CLV by market, auto vs Claude, calibration buckets |
| 7 | Fixes and testing | 8–13 | Two clean weeks without manual repair |
| Optional | Own model (regressed 5v5 xG share, goalie GSAx, home ice, Poisson for totals) | 10–20 | Only after hour 50; graded against the Pinnacle baseline |

## Measurement

CLV against Pinnacle's no-vig close is the main test, because it shows skill within a few hundred picks while profit takes thousands of bets.

CLV per pick, where D is the decimal price you took and p is Pinnacle's no-vig closing probability for your side (FanDuel's no-vig close if Pinnacle is missing):

```latex
\mathrm{CLV} = D_{\text{taken}} \cdot p_{\text{close}} - 1
```

Three groups are graded separately so you can see where any edge comes from:

| Group | What it tests |
| --- | --- |
| Automatic afternoon leans (no Claude) | Whether timing alone beats the close; an early benchmark |
| Claude-adjusted picks | Whether Claude's goalie and news adjustments add value over the baseline |
| Bets you actually placed | Whether your own choices beat the picks you skipped |

Also tracked: calibration (picks called 55–60% should win about that often), Brier score per group, parlay legs' CLV, and ROI shown with ±2 standard errors. Every logged pick counts toward the 200-pick test, including ones you don't bet.

## Taxes and the real-money rule

You're unsure whether you itemize, so the plan assumes the standard deduction: paper only.

Why tax matters: from 2026 only 90% of gambling losses are deductible federally, and only if you itemize. Indiana allows no loss deduction and taxes all winnings at 2.95% plus county tax. On the standard deduction, every winning bet's profit is taxed and losses offset nothing.

Approximate tax per $1 wagered at -110 with about a 50% hit rate (county tax not included):

| Your filing | Tax per $1 bet | Edge needed to break even |
| --- | --- | --- |
| Itemize | about 1.5–3¢ | about 2–3% |
| Standard deduction, 12% federal bracket | about 7¢ | about 7% |
| Standard deduction, 22% federal bracket | about 11–12¢ | about 11–12% |

**Real-money rule:** after 200 logged picks or by Feb 28, 2027 (whichever comes first), bet real money only if average CLV against the Pinnacle close beats your hurdle from the table. Public-data edges of 7–12% are not realistic, so unless you itemize, expect to stay on paper.

- [ ] Check last year's return: did you itemize (Schedule A) or take the standard deduction?

This is not tax advice; a tax preparer or the Indiana Department of Revenue (email or online) can confirm your situation.

## Stop signals and risks

Three checkpoints are set in advance so the project ends on evidence, not drift.

1. **Hour 15:** if the GitHub job can't run unattended and write tonight's FanDuel and Pinnacle game lines into the sheet, switch to the Odds API Google Sheets add-on.
2. **Hour 50:** if there isn't one full week of the complete loop (data → Claude pick on the sheet → closing line attached), stop.
3. **200 logged picks or Feb 28, 2027, whichever first:** if average CLV against the Pinnacle close doesn't beat your tax hurdle, no real money. If it's zero or negative, drop Claude's adjustment layer.

| Risk | What happens | Mitigation |
| --- | --- | --- |
| Picks have no edge | The likeliest outcome for public-data NHL game lines | Paper first; CLV decides |
| Project stalls before code works | Five betting ideas in four days; earlier versions stalled | Hour-15 checkpoint; build step 1 first, nothing else |
| Public-repo schedules auto-disable after 60 days without repo activity | Odds pulls stop silently mid-season | Weekly heartbeat commit plus a calendar reminder to check Actions |
| GitHub scheduled runs delayed or skipped | Missing closing prices | One evening job that sleeps until each start-time group; discard snapshots after puck drop |
| Pinnacle odds delayed | Noisier CLV benchmark | FanDuel no-vig close as backup |
| Unofficial NHL API changes | Puller breaks | Tests that fail loudly; MoneyPuck files as backup |
| Claude usage limits | Fewer nights | Compact Tonight tab; rulebook instead of the full guide |
| Wanting "as many bets as possible" | Pressure to bet thin edges | Only picks above threshold are marked as bets; leans stay logged but unbet |

## First step this week

Get one script writing tonight's odds into the sheet; nothing else until that works.

- [ ] Sign up for a free Odds API key
- [ ] Create a public GitHub repo and add the key as a GitHub Secret
- [ ] Create the Google Sheet and a Google Cloud service account; share the sheet with the service account's email
- [ ] Run `pull_odds.py` by hand: sport `icehockey_nhl`, markets `h2h,spreads,totals`, bookmakers `fanduel,pinnacle`, appending to Odds\_Snapshots with snapshot time and start time
- [ ] Check the `x-requests-used` header shows 3 credits for the call

## Sources

Checked October 16, 2026.

- [The Odds API v4 documentation](https://the-odds-api.com/liveapi/guides/v4/)
- [The Odds API bookmaker list](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html)
- [The Odds API: historical odds on paid plans](https://the-odds-api.com/sports/nbl-odds.html)
- [MoneyPuck data downloads and terms](https://moneypuck.com/data.htm)
- [NHL API Reference (community)](https://github.com/Zmalski/NHL-API-Reference)
- [GitHub Actions pricing 2026](https://cicdpipelinecost.com/github-actions-pricing)
- [GitHub community: scheduled workflow not triggering](https://github.com/orgs/community/discussions/210193)
- [GitHub Docs: disabling and enabling a workflow](https://docs.github.com/actions/managing-workflow-runs/disabling-and-enabling-a-workflow) (60-day rule, cited by the second-opinion reviewer; not opened by me)
- [Google Sheets API usage limits](https://developers.google.com/sheets/api/limits)
- [wild-tracker (CLV tracking reference)](https://github.com/osmond/wild-tracker)
- [edge-scanner (+EV vs Pinnacle reference)](https://github.com/DeliciousPipe1326/arbitrage-scanner)
- Tax rules: Section 8 of your NHL Betting Guide and its sources (CPA Practice Advisor, Indiana DOR, Bloomberg Tax)
