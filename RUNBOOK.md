# bigmove_deploy - RUNBOOK (30 Sep 2026)

Everything you need to run the paper test, end to end. Read sections 1-4 once;
after that you only need section 5 (reading the outputs) and section 8 (if
something fails).

---

## 1. What this system is, in one page

Four weeks of research (see `open_close\` and the Project knowledge) ended with
two **frozen engines**:

| engine | what it is | role |
|---|---|---|
| **D** | one model (HistGradientBoosting) on 205 clean daily features, ranking stocks by their expected return from the next open to the 5th close | **primary** - the arm judged for real money |
| **ENS** | 0.5 x D's daily rank + 0.5 x the rank of a second model built on 123 different features (stage C) | **challenger** - recorded alongside, descriptive until month 12 |

What the research established (same history, so not proof):
- Buying D's top 3 each day at the open and selling at the 5th close earned
  **+107.4 bp per trade** after 35 bp costs and honest exits (walk-forward, 2020-2026).
- Traded with full Zerodha costs, impact and liquidity limits on Rs 10 lakh:
  **+43.3% a year, max drawdown -47.6%**; random picks with the same mechanics: -12.3% a year.
- **No stops, no targets**: every stop, target and trailing stop tested did worse
  than the plain 5th-close exit.
- The edge is made **overnight**; it lives in a fat right tail (the best 5% of
  trades carry about 2x the total profit), so single months are noisy.

What happens now: a **forward paper test**. Every night the frozen engines pick
stocks for the next open; the picks are recorded before the market opens, in a
ledger nobody can edit; they are settled automatically from real prices. At
**month 6** a rule written in advance says whether D is CONFIRMED, FAILED or
INCONCLUSIVE. Real money only after CONFIRMED.

**Soul** gives every picked stock a card: what state it is in and what it has done
before. Cards are descriptive - they never change a pick. The **Soul re-ranker**
(using Soul to choose trades and exits) is declared (`SOUL_META_DECLARATION.json`)
but not built yet.

---

## 2. Where everything lives

```
C:\Users\karanvsi\PyCharmMiscProject\bigmove_deploy\        <- code (main folder)
    daily_run.bat          v5: the nightly job (this bundle)
    env_daily.bat          yours - sets CACHE_DAILY_ROOT (not in the bundle)
    watchlist.txt          yours - the symbols the cache downloads (not in the bundle)
    run_daily.py, Daily_cache_v27.py, data_quality.py, ...   the cache pipeline (unchanged)
    open_close\            the research + production code (this bundle)
        tests\

C:\QuantData\cache_daily\                                    <- data (CACHE_DAILY_ROOT)
    <SYMBOL>_daily.parquet  the Kite cache (updated nightly)
    panel\                  the OLD panel (still built by run_daily.py; harmless, unused)
    panel_oc\               the RESEARCH panel - FROZEN. Research runs, freeze, production models
        frozen\FROZEN_ENGINES.json         fingerprints of everything the engines depend on
        frozen\production\D.pkl, C.pkl     the production models, trained once
        research_ledger.jsonl              every research run, in order
    panel_live\             the LIVE panel - a copy of panel_oc, updated every night
    paper\                  the paper test
        ledger.jsonl        every recorded pick, hash-chained (NEVER edit)
        universe.jsonl      the stocks scored each day (for the buy-everything benchmark)
        watchlists\WL_YYYYMMDD.md / .csv, soul_cards_YYYYMMDD.csv
        trades.csv, days.csv                  settled paper trades and days
        tracker.md / tracker.csv              where every recent pick stands now
        daily_log.md                          session by session since day 1
        paper_report.md                       running totals + the month-6 status
    logs\daily_YYYY-MM-DD.log                 the nightly log
```

Why two panels: research must stay reproducible, so `panel_oc` never changes.
Paper trading needs today's features, so `panel_live` is updated nightly - by
the same frozen `panel_build` code.

---

## 3. What runs every night (automatic)

`daily_run.bat` v5, Task Scheduler, **23:45 IST on weekdays** (your existing task
already points at this file):

| step | what it does | typical time |
|---|---|---|
| 1 DATA | Kite cache update -> quality gate -> leak gate -> old panel -> daily report (exactly as v4) | as today |
| 2 PANEL LIVE | adds today's session to `panel_live` (frozen panel_build v32, incremental) | a few minutes |
| 3 WATCHLIST | scores every stock with the frozen production models; top 10 for D and for ENS; a Soul card for each; appends them to the paper ledger with the time | ~10-15 min (estimate) |
| 4 TRACK | settles every finished paper trade; rebuilds the tracker, the daily log and the report | a few minutes |

The watchlist is recorded around midnight - long before the 09:00 open - so
every day counts as on time. On a holiday the watchlist step finds nothing new,
says so and exits cleanly.

Everything is also written to `logs\daily_YYYY-MM-DD.log`. A failed step stops the
steps after it and the log says which one.

---

## 4. How the watchlist is created (step 3 in plain terms)

1. **Today's features.** `panel_live` holds each stock's 205 features for the
   latest session T, computed only from data up to T's close.
2. **D's score.** The frozen production model D scores every stock in the
   universe (the liquidity floor keeps ~800 stocks a day).
3. **The ensemble's score.** The C features for T are computed by the frozen
   stage C code; model C scores every stock; ENS = half D's percentile rank +
   half C's percentile rank.
4. **ETFs out.** By name rule, or by ISIN if `paper\isin_bhavcopy.csv` exists
   (decide before day 1 - see section 6).
5. **Top 10 per engine** by score, ties by symbol.
6. **Soul cards** for every one of those stocks (section 5).
7. **Recorded.** The 20 lines go into `paper\ledger.jsonl` with the recording
   time; each line carries the fingerprint of the one before, so any later edit
   is detected. The same day can never be recorded twice.

**Which stocks become trades** (settlement, automatic): the first 3 of each list
whose next open was buyable - a bar exists and it did not open locked at the
upper circuit. They are bought at that open, sold at the close of the 5th
session; if that close is locked at the lower circuit, the sale moves to the
first later open that is not locked. 35 bp per round trip.

---

## 5. Reading the outputs

**Watchlist - `paper\watchlists\WL_YYYYMMDD.md`** (the file for the NEXT open):
- A table per engine: rank, symbol, score, the stock's own record (Soul),
  what followed moments like today, and flags.
- **If you trade it by hand:** in the pre-open (09:00-09:08) buy the first 3 of
  D's list whose open is NOT locked at the upper circuit; hold 5 sessions; sell
  at the 5th close. No stops, no targets. (Paper settlement does this
  automatically; manual trading is optional and not part of the test.)

**Soul card** (one per stock, in the same file; all numbers also in
`soul_cards_YYYYMMDD.csv` for the month-6 review):
- **State** - the 8 canonical dimensions (trend, trend strength, volatility level
  and change, momentum, participation, 52-week location, shock) plus drawdown and
  ATR-z, as percentiles of that day's universe.
- **Personality** - the stock's own record on this exact trade (buy next open,
  sell 5th close, honest exits): net per trade, win rate, MFE, MAE, locked-exit
  rate, how often +5% came within 5 sessions and how fast, overnight vs
  in-session. Shrunk toward the market when the stock has little history (30
  effective trades = half weight).
- **Memory** - what followed the 20 moments of this stock most like today, and
  the 50 most like today across all stocks; the 5 closest listed by date.
- **Engine record** - how often D and ENS picked it before and how those trades ended.
- **Flags** - lower/upper-circuit closes in the last 20 sessions, days since the
  last lower lock, frozen sessions, price under Rs 20.
- **C profile** - the stock on 12 stage C features, one per mechanism the research
  backed (return x volume shock, trend strength x volatility, 1- and 5-session
  market-adjusted reversal, distance to the circuit band, reversal x spread, streak,
  lottery/MAX, overnight sum, gap x turnover, idiosyncratic volatility, spread), each
  as a percentile of that day's universe beside what the stage C screen measured on
  the traded target (IC, t, which side was favoured), and how many of the 12 sit on
  the favoured side. The spread estimate is also shown in bp, flagged when above the
  35 bp cost convention. This is what the ensemble's C half is built on.
- **Tug of war** - the stock's own last 250 sessions: does an overnight gap carry
  into the next overnight, and how does the session after a gap move.
- A card only uses trades that had fully finished by its date.

**Tracker - `paper\tracker.md`** (rebuilt nightly):
- Open positions: bought when, entry price, session k of 5, return now, MFE and
  MAE so far; "locked - waiting to sell" when an exit is stuck.
- Last recorded days: each traded pick's result or current state; shadows (ranks
  beyond the traded three) are followed too - what they would have done.

**Daily log - `paper\daily_log.md`** (every session since day 1, newest first):
- bought at the open (with prices), skipped (and why), sold (net, locked exits
  marked, MFE and MAE), holding at the close (each position's return), the
  watchlist recorded that night, and the running totals.

**Report - `paper\paper_report.md`:** settled on-time days, mean net per trade,
its 95% interval, mean excess over buying everything (every buyable non-ETF stock in the
universe, bought and sold the same way), the month-6 status, and any trading session since
day 1 with NO recorded watchlist (a failed night shows up here).

---

## 6. One-time setup (in this order)

**6.1 Install the bundle**
1. Back up `bigmove_deploy`.
2. Copy `daily_run.bat` and `RUNBOOK.md` / `MANIFEST.md` into `bigmove_deploy\`
   (daily_run.bat replaces v4).
3. Copy the `open_close\` folder over yours (all files, overwrite; `tests\` included).
4. If you copied `open_close\paper_daily.bat` last time, delete it - v5 replaces it.
5. Do NOT copy anything else into the main folder; keep your `env_daily.bat`
   and `watchlist.txt`.

**6.2 Disable the old monthly task** (it retrains the abandoned engine; nothing
may be retrained during the paper test):
```
schtasks /Change /TN "bigmove_monthly_retrain" /DISABLE
```
(If the task has another name, open Task Scheduler and disable the one that runs
`monthly_retrain.bat`.)

**6.3 Run the tests, train the production models, create panel_live, dry run**
```
call C:\Users\karanvsi\PyCharmMiscProject\bigmove_deploy\env_daily.bat
cd C:\Users\karanvsi\PyCharmMiscProject\bigmove_deploy\open_close
set PYTHONPATH=C:\Users\karanvsi\PyCharmMiscProject\bigmove_deploy
python tests\test_paper.py
python production_models.py train --panel %CACHE_DAILY_ROOT%\panel_oc
python paper_pipeline.py init-live --root %CACHE_DAILY_ROOT%
python paper_pipeline.py watchlist --root %CACHE_DAILY_ROOT% --dry-run
python preflight.py --root %CACHE_DAILY_ROOT% --watchlist
```
- The test must end `VERIFIED paper_pipeline v1.3 + production_models v1.1 + soul_v4 v1.1`.
- **`preflight.py` must end `READY FOR DAY 1`.** It checks, on your machine and your data: library
  versions and disk; every file at its version and every declaration's fingerprint; daily_run.bat v5
  with Windows line endings; the freeze; the production models, and their WIRING (they must rank the
  last 60 walk-forward sessions like the frozen walk-forward models did - a misaligned feature would
  push that agreement to about zero); panel_live identical to panel_oc on old sessions; the ledger;
  Task Scheduler (daily task present, monthly retrain disabled); and a dry-run watchlist. Any FAIL
  line says what to do. Do not start day 1 until it says READY.
- `production_models.py train` runs once (it refuses a second time).
- The dry run writes a watchlist to `paper\watchlists\` but records nothing - open
  it and check it looks right.

**6.4 Decide ETFs before day 1.** The name rule misses some ETFs. For ISIN-based
removal, download one NSE bhavcopy with ISINs and save it as
`%CACHE_DAILY_ROOT%\paper\isin_bhavcopy.csv`; daily_run.bat v5 uses it
automatically. Adding or removing it later would change the picks mid-test.

**6.5 Day 1.** The next scheduled 23:45 run records the first real watchlist.
That date is day 1; the month-6 check falls 6 calendar months later. To start
tonight by hand instead: run `daily_run.bat` from the main folder.

---

## 7. The rules that protect the test

- **Nothing is retrained or re-tuned.** The production models are fingerprinted;
  the paper pipeline refuses a changed byte.
- **Never upgrade scikit-learn or numpy during the test.** The models are saved as
  pickles under the versions recorded at training; the pipeline refuses to load
  them under different versions (a silent upgrade could change their output). The research freeze covers the code,
  the declarations and the walk-forward runs.
- **The ledger is append-only.** Never edit or delete `paper\ledger.jsonl`; every
  command re-checks its hash chain and stops if it was touched.
- **Late days don't count.** A watchlist recorded after 09:00 of its entry
  session is settled and shown but excluded from the month-6 evidence.
- **Month 6 for D (declared before day 1):** CONFIRMED if mean net per trade > 0
  and mean excess > 0; FAILED if the upper end of its 95% interval < 0;
  otherwise INCONCLUSIVE and the test continues to month 12. Six months can
  confirm the sign, not the size.
- **Month 12 for ENS:** it replaces D only if its paired daily difference over D
  has a 95% lower bound above zero AND it is itself CONFIRMED.
- **Never edit a file in the freeze list** (`FROZEN_ENGINES.json` shows them).
  Changing one means new engines and a new paper test.

---

## 8. If something fails

Open the night's log: `%CACHE_DAILY_ROOT%\logs\daily_YYYY-MM-DD.log`. The line
`STEP FAILED: ...` names the step.

| message | meaning | what to do |
|---|---|---|
| step 1 fails | cache / quality gate problem (as with v4) | fix as before; re-run daily_run.bat before 09:00 |
| `panel_live does not exist yet` | one-time setup not done | section 6.3 |
| `frozen files changed since the freeze` | a frozen file was edited or replaced | restore it from the bundle / backup; never edit frozen files |
| `production model ... changed` | D.pkl or C.pkl altered | restore from backup; retraining = new engines |
| `paper ledger line N was edited` | someone touched ledger.jsonl | restore from backup; the chain shows exactly where |
| `already recorded` | the day is in the ledger | normal on a re-run or a holiday (the nightly job exits cleanly) |
| step 3 slow | the C features rebuild takes minutes | normal; it must finish before 09:00 |
| `scikit-learn is X but the production models were trained under Y` | a library was upgraded | reinstall the recorded version (`pip install scikit-learn==Y`) |
| `universe.jsonl ... was edited` | the benchmark universe file was touched | restore it from backup |
| anything unclear | - | run `python preflight.py --root %CACHE_DAILY_ROOT%` - it checks every part and says what to fix |

**Re-running a failed night:** run `daily_run.bat` from the main folder any time
before 09:00. After 09:00 the watchlist still records, but that day is marked
late and does not count.

**Manual commands** (from `open_close\`, after `env_daily.bat` and `set PYTHONPATH=...`):
```
python paper_pipeline.py report --root %CACHE_DAILY_ROOT%          running totals
python paper_pipeline.py settle --root %CACHE_DAILY_ROOT%          settle + tracker + log now
python paper_pipeline.py verify-ledger --root %CACHE_DAILY_ROOT%   check the hash chain
python production_models.py verify --panel %CACHE_DAILY_ROOT%\panel_oc
```

---

## 9. What is next (not in this bundle)

1. **Soul re-ranker** - declared in `open_close\SOUL_META_DECLARATION.json`
   (re-rank D's top 10, 30% trap veto, exit menu H3/H5/H7/H10 + one bracket, 30 bp
   hurdle, walk-forward 2022-2026, paired test vs D). Build on request; a pass
   becomes a new engine for its own forward test.
2. **Honest-exit labels** - train on the price you could really sell at.
3. **Close-to-open family** - the overnight signal sits before the current entry.

Research history and every number: the Project knowledge files and
`panel_oc\research_ledger.jsonl`.
