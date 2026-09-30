#!/usr/bin/env python3
"""
tests/test_preflight.py - preflight v1. Prints VERIFIED on success.

Run from the open_close folder with the main folder on PYTHONPATH:
    python tests\\test_preflight.py

 1. Before setup: production models and panel_live are reported missing, with the command to fix each.
 2. Wiring, ABSENT: walk-forward scores unrelated to the production models -> wiring FAILS.
 3. Wiring, PLANTED: walk-forward scores that agree with the production models -> wiring PASSES,
    everything else passes, verdict READY FOR DAY 1 (with the dry-run watchlist).
 4. Tampering: one changed value in panel_live on an old session -> FAIL; a changed declaration -> FAIL.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import freeze_engines as FZ           # noqa: E402
import production_models as PM        # noqa: E402
import paper_pipeline as PP           # noqa: E402
import preflight as PF                # noqa: E402
import stage_b_screen as SB           # noqa: E402
import test_paper as TPA              # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL") + f"  {msg}")
    if not cond:
        FAILS.append(msg)


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


def status(C, area, word):
    return [s for s, a, m in C.rows if a == area and word in m]


def main():
    real_scheduler = PF.check_scheduler
    PF.check_scheduler = lambda C, text=None: C.add("SKIP", "scheduler", "isolated in tests - never reads this machine")
    real_env_root = PF.check_env_root
    PF.check_env_root = lambda C, root, env_file=None: C.add("SKIP", "env_daily", "isolated in tests - never reads this machine")
    tmp = Path(tempfile.mkdtemp())
    bat = HERE.parent.parent / "daily_run.bat"
    had_bat = bat.exists()
    try:
        if not had_bat:
            src = Path(__file__).resolve().parents[3] / "bundle" / "bigmove_deploy" / "daily_run.bat"
            bat.write_bytes(src.read_bytes() if src.exists() else b"REM daily_run.bat v5\r\n")
        pdir, pp, feats, names = TPA.world(tmp)
        root = tmp
        print("1. before setup")
        C, _ = quiet(PF.run, root)
        check(status(C, "production", "not trained yet") == ["FAIL"], "production models reported missing, with the command")
        check(status(C, "panel_live", "missing") == ["FAIL"], "panel_live reported missing, with the command")
        check(status(C, "freeze", "verified") == ["PASS"] and status(C, "ledger", "empty") == ["INFO"],
              "freeze verified; empty ledger reported as not started")
        quiet(PM.main, ["train", "--panel", str(pdir)])
        quiet(PP.main, ["init-live", "--root", str(root)])

        print("2. wiring, absent (walk-forward scores unrelated to the production models)")
        C, _ = quiet(PF.run, root)
        w = [(s, m) for s, a, m in C.rows if a == "production" and m.startswith("wiring")]
        check(len(w) == 2 and all(s == "FAIL" for s, _ in w), "wiring FAILS for both models: " + " | ".join(m[:40] for _, m in w))

        print("3. wiring, planted (walk-forward scores that agree with the production models)")
        meta, models = PM.load_verified(pp)
        man = FZ.verify_frozen(pp)
        rng = np.random.default_rng(1)
        for tag, run in (("D", man["engines"]["D"]["run"]), ("C", man["engines"]["ENS"]["c_run"])):
            fp = pdir / "stage_d" / run / "preds.parquet"
            pr = pd.read_parquet(fp)
            pr["timestamp"] = SB._naive(pr["timestamp"])
            fl = meta["models"][tag]["features"]
            src = PF.read_since(pp if tag == "D" else pdir / "stage_c" / "features_c.parquet", fl, pr["timestamp"].min())
            src["symbol"] = src["symbol"].astype(str)
            src["p"] = models[tag].predict(src[fl].to_numpy(np.float32))
            pr["symbol"] = pr["symbol"].astype(str)
            pr = pr.drop(columns="score").merge(src[["timestamp", "symbol", "p"]], on=["timestamp", "symbol"], how="left")
            pr["score"] = pr["p"].fillna(0) + 0.3 * pr["p"].std() * rng.normal(size=len(pr))
            pr.drop(columns="p").to_parquet(fp, index=False)
        quiet(FZ.main, ["--panel", str(pdir), "--rerun-reason", "test: planted wiring"])
        C, log = quiet(PF.run, root, True)
        w = [(s, m) for s, a, m in C.rows if a == "production" and m.startswith("wiring")]
        check(len(w) == 2 and all(s == "PASS" for s, _ in w), "wiring PASSES for both models: " + " | ".join(m[:40] for _, m in w))
        check(status(C, "panel_live", "identical") == ["PASS"], "panel_live identical to panel_oc on old sessions")
        check(status(C, "watchlist", "dry run") == ["PASS"], "dry-run watchlist: 10 per engine, finite scores, no ETFs, a card each")
        check(not C.failed() and "READY FOR DAY 1" in log, "verdict READY FOR DAY 1 when nothing fails")
        check(not (PP.paths(root)["paper"] / "ledger.jsonl").exists(), "preflight recorded nothing")

        print("4. tampering")
        live = PP.paths(root)["live"]
        L = pd.read_parquet(live)
        days = np.sort(L["timestamp"].unique())
        old = days[-60]
        col = meta["models"]["D"]["features"][0]
        L.loc[L["timestamp"] == old, col] = L.loc[L["timestamp"] == old, col] + 1.0
        L.to_parquet(live, index=False)
        C, log = quiet(PF.run, root)
        check(status(C, "panel_live", "differs") == ["FAIL"] and "NOT READY" in log,
              "one changed value on an old panel_live session -> FAIL, NOT READY")
        dp = HERE.parent / "PAPER_DECLARATION.json"
        raw = dp.read_bytes()
        try:
            dp.write_bytes(raw.replace(b'"record_top": 10', b'"record_top": 11'))
            C, _ = quiet(PF.run, root)
            check(any(s == "FAIL" and "declarations changed" in m for s, a, m in C.rows), "a changed declaration -> FAIL")
        finally:
            dp.write_bytes(raw)
        print("6. env_daily.bat must point at the root being checked (planted files)")
        cases = (('@echo off\r\nset CACHE_DAILY_ROOT=C:\\QuantData\\cache_daily\r\n', "PASS", "plain set, same root -> PASS"),
                 ('set "CACHE_DAILY_ROOT=C:\\QuantData\\cache_daily\\"\r\n', "PASS", "quoted set with a trailing backslash -> PASS"),
                 ('set CACHE_DAILY_ROOT=C:\\Users\\k\\Desktop\\Cache\\cache_daily_new\r\n', "FAIL", "a different folder -> FAIL"),
                 ('if not defined CACHE_DAILY_ROOT set CACHE_DAILY_ROOT=C:\\Other\r\nset CACHE_DAILY_ROOT=C:\\QuantData\\cache_daily\r\n',
                  "FAIL", "any assignment to a different folder -> FAIL"),
                 ('REM set CACHE_DAILY_ROOT=C:\\Other\r\nset CACHE_DAILY_ROOT=C:\\QuantData\\cache_daily\r\n', "PASS",
                  "a commented-out line is ignored -> PASS"),
                 ('@echo off\r\nset KITE_KEY=x\r\n', "FAIL", "never set -> FAIL"))
        for text, want, label in cases:
            ef = tmp / "env_test.bat"
            ef.write_text(text, encoding="utf-8")
            Cx = PF.Checks()
            quiet(real_env_root, Cx, Path("C:\\QuantData\\cache_daily"), ef)
            check([s_ for s_, a, _ in Cx.rows if a == "env_daily"] == [want], label)
        print("5. the scheduler check, on planted task lists (never this machine's)")
        H = '"HostName","TaskName","Next Run Time","Status","Task To Run","Scheduled Task State"\n'
        row = lambda n, run, st: f'"PC","{n}","x","Ready","{run}","{st}"\n'
        daily = row("\\bigmove_daily", "C:\\bm\\daily_run.bat", "Enabled")
        for text, want, label in (
                (H + daily + row("\\bigmove_monthly", "C:\\bm\\monthly_retrain.bat", "Enabled"), ["PASS", "FAIL"],
                 "monthly retrain ENABLED -> FAIL"),
                (H + daily + row("\\bigmove_monthly", "C:\\bm\\monthly_retrain.bat", "Disabled"), ["PASS", "PASS"],
                 "monthly retrain disabled -> PASS"),
                (H + row("\\other", "C:\\x.bat", "Enabled"), ["FAIL", "PASS"], "no daily_run.bat task -> FAIL"),
                (H + row("\\bigmove_daily", "C:\\bm\\daily_run.bat", "Disabled"), ["FAIL", "PASS"], "daily task disabled -> FAIL"),
                (H + daily + H + daily, ["PASS", "PASS"], "repeated header rows (one per task folder) are ignored")):
            Cx = PF.Checks()
            quiet(real_scheduler, Cx, text)
            check([s_ for s_, a, _ in Cx.rows if a == "scheduler"] == want, label)
    finally:
        PF.check_scheduler = real_scheduler
        PF.check_env_root = real_env_root
        if not had_bat and bat.exists():
            bat.unlink()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED  {len(FAILS)} check(s):")
        for f in FAILS:
            print("   - " + f)
        return 1
    print(f"VERIFIED  {PF.CODE_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
