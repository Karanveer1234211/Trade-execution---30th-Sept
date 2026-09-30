# bigmove_deploy - bundle manifest (30 Sep 2026)

How to install: RUNBOOK.md section 6. In short - `daily_run.bat`, `RUNBOOK.md` and
`MANIFEST.md` go into `bigmove_deploy\`; the `open_close\` folder (with `tests\`)
goes over yours. Nothing else in the main folder changes. Keep your own
`env_daily.bat` and `watchlist.txt`.

## A. Nightly automation (main folder)
| file | version | what it does |
|---|---|---|
| run_now.bat | v1 | run the nightly job by hand: progress on screen, window stays open, RESULT at the end (use after 18:00 or before 09:00; never while the scheduled job runs) |
| daily_run.bat | v5.3 | weekdays after 18:00 IST; step 1 gives the cache its 600-day window and sets aside stocks with old gaps (--allow-partial); the watchlist refuses a day whose entry session already opened; a progress line at every step: 1 Kite cache + quality gate + report (run_daily.py, as v4) -> 2 panel_live update -> 3 watchlist + Soul cards recorded in the paper ledger -> 4 settle + tracker + daily log + report. Replaces v4; v4's Soul v3 / signal_engine steps are retired. |

## B. Production: the paper test (`open_close\`)
| file | version | what it does |
|---|---|---|
| paper_pipeline.py | v1.5 | `init-live` (panel_live = copy of panel_oc, once) / `watchlist` (frozen production scores for D and ENS, ETFs out, top 10 each + Soul cards, appended to the hash-chained ledger; `--skip-if-recorded` for holidays; `--dry-run`) / `settle` (first 3 buyable per engine, honest exits, 35 bp, late flag, excess over every buyable non-ETF universe stock; rebuilds tracker.md/.csv and daily_log.md) / `report` (month-6 rule, sessions with no watchlist) / `verify-ledger`. The day's universe is hash-chained in the ledger |
| production_models.py | v1.1 | `train` once: the frozen recipe on all resolved history (D on its 205 frozen features, ENS's C member on its 123), SHA-256 fingerprinted in `panel_oc\frozen\production\`; records library versions and refuses to load under different ones; `verify` |
| preflight.py | v1.5 | run before day 1 (and whenever in doubt): environment, files and versions, declaration fingerprints, daily_run.bat v5, freeze, production models and their wiring against the walk-forward runs, panel_live vs panel_oc, ledger, Task Scheduler, dry-run watchlist; ends READY FOR DAY 1 or lists what to fix |
| soul_v4.py | v1.1 | Soul cards: per stock-day honest 5-session outcome (MFE, MAE, locked exit, days to +5%, overnight vs in-session); card = state percentiles, personality (1-year half-life, shrunk to the market, K = 30), memory (20 own + 50 cross nearest moments), the engines' past record with the stock, circuit flags; the stage C profile (12 research-backed features with their measured oc_5 IC, t and favoured side) and the stock's own tug of war; only finished trades |
| freeze_engines.py | v1 | freezes D (primary) and ENS (challenger): SHA-256 of their runs, code, declarations and panel metadata in `panel_oc\frozen\FROZEN_ENGINES.json`; every production and research tool re-verifies |
| PAPER_DECLARATION.json | oc_v1 paper (f75aa3de0dc4278f) | the paper test: roles, recording, settlement, late rule, month-6 rule for D, month-12 rule for ENS, Soul settings - fixed before day 1 |

## C. Research: how the engines were found (`open_close\`, frozen history)
| file | version | what it did |
|---|---|---|
| panel_build.py | v32 | model-ready panel with open-to-close targets, buyable flag, ETFs out (also builds panel_live nightly) |
| label_audit.py | - | independent re-computation of every label |
| research_common.py | - | shared research code (ledger, state, market state, guards) |
| stage_b_screen.py | v1.2 | stage B: every existing feature screened one at a time (606 of 684 cells) |
| stage_d_gate.py | v1 | stage D: the walk-forward model on all 205 clean features - engine D |
| stage_d_forensics.py | v1 | honest exits: locked lower-circuit closes sold at the first sellable open; tick sizes; ISIN rules |
| stage_d2_nested.py | v1.1 | D2: nested feature selection - kept baseline D |
| stage_c_features.py | v1 | stage C: 123 new features (overnight/intraday, market model, lottery, liquidity, circuits, ...) |
| stage_c_screen.py | v1 | stage C screen + leak checks (364 of 452 cells, no leak suspects) |
| stage_dc_model.py | v1.1 | D_C: D plus all C features (and the C-only arm) - kept baseline D |
| stage_dx_ensemble.py | v1 | the C-only model and the 50/50 rank ensemble - ENS |
| compare_d_variants.py | v1.2 | scores every variant with corrected exits; paired decisions; pick agreement |
| stage_e_execution.py | v1.1 | execution backtest (Zerodha costs, impact, capacity, grids, stops, benchmarks). v1.1 adds an optional per-trade exit planner for the future Soul re-ranker and a `held` column; with no planner it runs exactly as v1 |
| stage_e_paths.py | v1 | inside the trades: MFE/MAE, time to target, stops, 39 exit rules vs the plain exit |

## D. Declarations (fixed before their runs)
| file | fingerprint | covers |
|---|---|---|
| EXPERIMENT_OC_FAMILY.json | 1f5c13384e36e759 | the open-to-close target family |
| STAGE_D_DECLARATION.json | dcfab6eccc05fcb9 | engine D's model and gate |
| FORENSICS_DECLARATION.json | 410278b832f2aa90 | honest-exit rules |
| STAGE_D2_DECLARATION.json | 1de7aa2028f1c21a | nested selection |
| STAGE_C_DECLARATION.json | e945f62dd321625c | the 123 C features |
| STAGE_DX_DECLARATION.json | 8a0cf3feaf3b5979 | C-only arm, ensemble, closure rule |
| EXECUTION_DECLARATION.json | 5ba73cc0687eb482 | execution backtest |
| PATHS_DECLARATION.json | f14e6e968b12a73b | path study |
| PAPER_DECLARATION.json | f75aa3de0dc4278f | the paper test (amended before day 1: Soul C profile) |
| SOUL_META_DECLARATION.json | dad9b1c21101132b | the Soul re-ranker - DECLARED, NOT BUILT (amended before any code: C profile and tug-of-war inputs) |

## E. Tests (`open_close\tests\`) - each prints VERIFIED
test_panel_oc, test_stage_b, test_stage_d, test_stage_d_forensics, test_stage_d2,
test_compare_d, test_stage_c_features, test_stage_c_screen_dc, test_stage_dx,
test_stage_e, test_stage_e_paths, test_paper (production models and library pinning, Soul
point in time, watchlist, ledger and universe integrity, settlement and its benchmark,
tracker and log, missing days), test_preflight (wiring planted vs absent, tampering, missing setup).

## F. Main folder - unchanged, not in this bundle
Daily_cache_v27.py (history anchor 2015-01-01 - never change), run_daily.py,
data_quality.py, features_daily.py, daily_report.py, panel_build.py (v31, the old
panel), research_common.py (main copy), tradability_audit.py, requirements.txt,
setup_once.bat, env_daily.bat and watchlist.txt (yours).

## G. Retired - keep the files, never schedule them
signal_engine.py, meta_label_test.py, soul.py, soul_v3.py, soul_test.py,
challenger_test.py, monthly_retrain.bat (disable its task: nothing is retrained
during the paper test), open_close\paper_daily.bat (replaced by daily_run.bat v5 -
delete it if you copied it).

## H. Deliberately excluded
A `soul_meta.py` and its test left behind by an interrupted earlier attempt at the
re-ranker - unapproved and unverified. The re-ranker will be built fresh against
SOUL_META_DECLARATION.json, with its own tests.
