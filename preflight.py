#!/usr/bin/env python3
"""
preflight.py - check EVERYTHING on this machine before day 1 of the paper test.

    python preflight.py --root %CACHE_DAILY_ROOT%               (fast checks, ~2-5 min)
    python preflight.py --root %CACHE_DAILY_ROOT% --watchlist   (+ a dry-run watchlist, ~15 min)

Prints PASS / WARN / FAIL per check and ends with READY FOR DAY 1 only when
nothing failed. Changes nothing (the dry-run watchlist writes files to
paper\\watchlists\\ but records nothing).

  1 environment    Python and library versions, disk space
  2 files          every bundle file at its expected version; declaration fingerprints;
                   daily_run.bat v5 with CRLF; no leftover files
  3 freeze         every frozen fingerprint (code, declarations, walk-forward runs)
  4 production     models present, fingerprints, library versions, and WIRING: the
                   production models must rank the last 60 walk-forward sessions like
                   the frozen walk-forward models did (a misaligned feature would push
                   the agreement to ~0)
  5 panel_live     present; every model input present; identical to panel_oc on old
                   sessions; how fresh it is against the cache
  6 ledger         hash chain and universe fingerprints (or empty before day 1)
  7 scheduler      a task runs daily_run.bat; the monthly retrain task is disabled
  8 watchlist      (--watchlist) a dry run: 10 per engine, finite scores, no ETFs, a card each
"""

from __future__ import annotations

import argparse
import csv
import re
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import research_common as RC       # first: from a subfolder, this folder's copy must win
import stage_b_screen as SB        # noqa: E402
import freeze_engines as FZ        # noqa: E402
import production_models as PM     # noqa: E402
import paper_pipeline as PP        # noqa: E402
import stage_c_screen as SCS       # noqa: E402

CODE_VERSION = "preflight v1.8"   # v1.8: soul_meta v1, stage_e_execution v1.2, the amended re-ranker declaration (v1.7: paper v1.6)
EXPECTED = {"paper_pipeline.py": "paper_pipeline v1.6", "production_models.py": "production_models v1.1",
            "soul_v4.py": "soul_v4 v1.1", "freeze_engines.py": "freeze_engines v1", "preflight.py": "preflight v1.8", "soul_meta.py": "soul_meta v1",
            "stage_e_execution.py": "stage_e_execution v1.2", "stage_e_paths.py": "stage_e_paths v1",
            "stage_d_gate.py": "stage_d_gate v1", "stage_d_forensics.py": "stage_d_forensics v1",
            "stage_dx_ensemble.py": "stage_dx_ensemble v1", "stage_dc_model.py": "stage_dc_model v1.1",
            "stage_c_features.py": "stage_c_features v1", "stage_c_screen.py": "stage_c_screen v1",
            "stage_b_screen.py": "stage_b_screen v1.2", "stage_d2_nested.py": "stage_d2_nested v1.1",
            "compare_d_variants.py": "compare_d_variants v1.2", "panel_build.py": "panel_build v32",
            "label_audit.py": None, "research_common.py": None}
DECLARATIONS = {"EXPERIMENT_OC_FAMILY.json": "1f5c13384e36e759", "STAGE_D_DECLARATION.json": "dcfab6eccc05fcb9",
                "FORENSICS_DECLARATION.json": "410278b832f2aa90", "STAGE_D2_DECLARATION.json": "1de7aa2028f1c21a",
                "STAGE_C_DECLARATION.json": "e945f62dd321625c", "STAGE_DX_DECLARATION.json": "8a0cf3feaf3b5979",
                "EXECUTION_DECLARATION.json": "5ba73cc0687eb482", "PATHS_DECLARATION.json": "f14e6e968b12a73b",
                "PAPER_DECLARATION.json": "f75aa3de0dc4278f", "SOUL_META_DECLARATION.json": "2cb4d8a4b416fd7c"}
WIRING_PASS, WIRING_WARN, WIRING_SESSIONS = 0.5, 0.3, 60


class Checks:
    def __init__(self):
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, status: str, area: str, msg: str) -> None:
        self.rows.append((status, area, msg))
        print(f"  {status:<4}  {area:<11} {msg}", flush=True)

    def failed(self) -> List[str]:
        return [f"{a}: {m}" for s, a, m in self.rows if s == "FAIL"]


def sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def check_env(C: Checks, root: Path) -> None:
    libs = PM.lib_versions()
    C.add("INFO", "environment", f"Python {libs['python']}, numpy {libs['numpy']}, pandas {libs['pandas']}, "
                                 f"scikit-learn {libs['sklearn']} on {platform.system()} {platform.release()}")
    if not root.exists():
        C.add("FAIL", "environment", f"{root} does not exist")
        return
    free = shutil.disk_usage(root).free / 1e9
    C.add("PASS" if free >= 5 else "WARN", "environment", f"{free:,.0f} GB free on the data drive (want >= 5)")


def check_files(C: Checks) -> None:
    bad = []
    for f, ver in EXPECTED.items():
        p = HERE / f
        if not p.exists():
            bad.append(f"{f} missing")
        elif ver and ver not in p.read_text(encoding="utf-8", errors="replace"):
            bad.append(f"{f} is not {ver}")
    C.add("FAIL" if bad else "PASS", "files", "; ".join(bad) if bad else f"all {len(EXPECTED)} code files present at their versions")
    bad = [f for f, h in DECLARATIONS.items() if not (HERE / f).exists() or sha16(HERE / f) != h]
    C.add("FAIL" if bad else "PASS", "files", f"declarations changed or missing: {bad}" if bad else
          f"all {len(DECLARATIONS)} declarations match their fingerprints")
    bat = HERE.parent / "daily_run.bat"
    if not bat.exists():
        C.add("FAIL", "files", f"{bat} missing")
    else:
        raw = bat.read_bytes()
        ok = b"daily_run.bat v5.4" in raw and raw.count(b"\r\n") == raw.count(b"\n")
        C.add("PASS" if ok else "FAIL", "files", "daily_run.bat is v5.4 with Windows line endings" if ok else
              "daily_run.bat is not v5.4 or lost its CRLF line endings - copy it again from the bundle")
    rd = HERE.parent / "run_daily.py"
    if not rd.exists() or "--no-panel" not in rd.read_text(encoding="utf-8", errors="replace"):
        C.add("FAIL", "files", "run_daily.py (main folder) lacks --no-panel - copy the new run_daily.py from the bundle")
    else:
        C.add("PASS", "files", "run_daily.py has --no-panel (step 1 skips the old panel)")
    now_bat = HERE.parent / "run_now.bat"
    if not now_bat.exists():
        C.add("WARN", "files", "run_now.bat missing from the main folder (manual runs with progress and a window that stays open)")
    left = [f for f in ("paper_daily.bat",) if (HERE / f).exists()]
    if left:
        C.add("WARN", "files", f"leftover files to delete from open_close: {left}")


def env_roots(text: str) -> list:
    """Every value env_daily.bat assigns to CACHE_DAILY_ROOT (set VAR=x, set "VAR=x", setx VAR x)."""
    out = []
    for line in text.splitlines():
        m = re.search(r'set(?:x)?\s+"?CACHE_DAILY_ROOT(?:=|\s+)"?([^"\r\n]+?)"?\s*$', line.strip(), re.I)
        if m and not line.strip().lower().startswith(("rem", "::")):
            out.append(m.group(1).strip().rstrip("\\"))
    return out


def check_env_root(C: Checks, root: Path, env_file: Path = None) -> None:
    env_file = env_file or (HERE.parent / "env_daily.bat")
    if not env_file.exists():
        C.add("FAIL", "env_daily", f"{env_file} not found - daily_run.bat needs it to set CACHE_DAILY_ROOT")
        return
    vals = env_roots(env_file.read_text(encoding="utf-8", errors="replace"))
    want = str(root).rstrip("\\/").lower()
    if not vals:
        C.add("FAIL", "env_daily", "env_daily.bat never sets CACHE_DAILY_ROOT")
    elif any(v.lower() != want for v in vals):
        C.add("FAIL", "env_daily", f"env_daily.bat sets CACHE_DAILY_ROOT to {vals}, but this check is for {root} - the "
                                   f"nightly job would use the wrong data folder; make env_daily.bat set {root}")
    else:
        C.add("PASS", "env_daily", f"env_daily.bat sets CACHE_DAILY_ROOT to {root}, the root being checked")


def check_freeze(C: Checks, research_pp: Path):
    try:
        man = FZ.verify_frozen(research_pp)
        C.add("PASS", "freeze", f"{len(man['files'])} frozen fingerprints verified (D {man['engines']['D']['run']}, "
                                f"ENS {man['engines']['ENS']['run']})")
        return man
    except SystemExit as e:
        C.add("FAIL", "freeze", str(e))
        return None


def read_since(pp: Path, cols: List[str], since) -> pd.DataFrame:
    import pyarrow.parquet as pq
    out = []
    for batch in pq.ParquetFile(pp).iter_batches(batch_size=250_000, columns=list(dict.fromkeys(["timestamp", "symbol"] + cols))):
        df = batch.to_pandas()
        df["timestamp"] = SB._naive(df["timestamp"])
        df = df[df["timestamp"] >= since]
        if len(df):
            out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["timestamp", "symbol"] + cols)


def wiring(research_pp: Path, man: dict, meta: dict, models: dict) -> dict:
    """Mean daily rank correlation between production scores and frozen walk-forward scores."""
    out = {}
    root = research_pp.parent / "stage_d"
    for tag, run in (("D", man["engines"]["D"]["run"]), ("C", man["engines"]["ENS"]["c_run"])):
        pr = pd.read_parquet(root / run / "preds.parquet")
        pr = pr[pr["target"].astype(str) == man["target"]].copy()
        pr["timestamp"] = SB._naive(pr["timestamp"])
        days = np.sort(pr["timestamp"].unique())[-WIRING_SESSIONS:]
        pr = pr[pr["timestamp"].isin(days)]
        feats = meta["models"][tag]["features"]
        if tag == "D":
            X = read_since(research_pp, feats, pd.Timestamp(days[0]))
        else:
            X = read_since(research_pp.parent / "stage_c" / "features_c.parquet", feats, pd.Timestamp(days[0]))
        X["symbol"] = X["symbol"].astype(str)
        X["prod"] = models[tag].predict(X[feats].to_numpy(dtype=np.float32))
        pr["symbol"] = pr["symbol"].astype(str)
        J = pr.merge(X[["timestamp", "symbol", "prod"]], on=["timestamp", "symbol"], how="inner")
        r = pd.Series({t: (g["score"].rank().corr(g["prod"].rank()) if len(g) > 5 else np.nan) for t, g in J.groupby("timestamp")},
                      dtype=float)
        out[tag] = {"corr": float(np.nanmean(r.to_numpy(float))), "days": int(r.notna().sum()), "rows": int(len(J))}
    return out


def check_production(C: Checks, research_pp: Path, man) -> None:
    if not (PM.prod_dir(research_pp) / "PRODUCTION.json").exists():
        C.add("FAIL", "production", "not trained yet - run: python production_models.py train --panel %CACHE_DAILY_ROOT%\\panel_oc")
        return
    try:
        meta, models = PM.load_verified(research_pp)
    except SystemExit as e:
        C.add("FAIL", "production", str(e))
        return
    C.add("PASS", "production", "models verified: " + ", ".join(f"{k} trained to {v['train_end']} on {v['fit_rows']:,} rows"
                                                          for k, v in meta["models"].items()))
    if man is None:
        C.add("WARN", "production", "wiring not checked (the freeze failed)")
        return
    w = wiring(research_pp, man, meta, models)
    for tag, x in w.items():
        st = "PASS" if x["corr"] >= WIRING_PASS else "WARN" if x["corr"] >= WIRING_WARN else "FAIL"
        C.add(st, "production", f"wiring {tag}: production vs walk-forward ranks agree {x['corr']:+.2f} on average over "
                                f"{x['days']} sessions ({x['rows']:,} rows); pass >= {WIRING_PASS}")


def check_live(C: Checks, root: Path, research_pp: Path, meta_ok: bool) -> None:
    P = PP.paths(root)
    if not P["live"].exists():
        C.add("FAIL", "panel_live", "missing - run: python paper_pipeline.py init-live --root %CACHE_DAILY_ROOT%")
        return
    import pyarrow.parquet as pq
    have = set(pq.ParquetFile(P["live"]).schema_arrow.names)
    need = set()
    mp = PM.prod_dir(research_pp) / "PRODUCTION.json"
    if mp.exists():
        need |= set(json.loads(mp.read_text(encoding="utf-8"))["models"]["D"]["features"])
    import stage_c_features as SC
    need |= set(SC.PANEL_INPUTS) | {c for _, c, _ in RC.STOCK_STATE_SPEC}
    miss = sorted(need - have)
    C.add("FAIL" if miss else "PASS", "panel_live", f"missing model inputs: {miss[:6]}" if miss else
          f"every model, C-feature and Soul input column present ({len(need)})")
    lt = pd.to_datetime(pd.read_parquet(P["live"], columns=["timestamp"])["timestamp"]).dt.tz_localize(None)
    rt = pd.to_datetime(pd.read_parquet(research_pp, columns=["timestamp"])["timestamp"]).dt.tz_localize(None)
    rdays = np.sort(rt.unique())
    old = [pd.Timestamp(d) for d in rdays[-60:-30:10]]
    cols = sorted((need & have) - set(SC.PANEL_INPUTS))[:60]
    a = read_since(P["live"], cols, old[0])
    b = read_since(research_pp, cols, old[0])
    a, b = a[a["timestamp"].isin(old)], b[b["timestamp"].isin(old)]
    M = a.merge(b, on=["timestamp", "symbol"], suffixes=("_l", "_r"))
    worst = 0.0
    for c in cols:
        x, y = M[c + "_l"].to_numpy(float), M[c + "_r"].to_numpy(float)
        d = np.abs(x - y)
        d[np.isnan(x) & np.isnan(y)] = 0
        worst = max(worst, float(np.nanmax(d)) if len(d) else 0.0, float(np.isnan(d).sum()))
    same_rows = len(M) == len(a) == len(b)
    C.add("PASS" if (worst == 0 and same_rows) else "FAIL", "panel_live",
          f"identical to panel_oc on {len(old)} old sessions ({len(M):,} rows x {len(cols)} columns)" if (worst == 0 and same_rows)
          else f"differs from panel_oc on old sessions (max difference {worst:g}, rows {len(a)} vs {len(b)})")
    C.add("INFO", "panel_live", f"latest session {lt.max().date()} (panel_oc ends {rt.max().date()}); the nightly job "
                                f"adds each new session")


def check_ledger(C: Checks, root: Path) -> None:
    P = PP.paths(root)
    try:
        L = PP.read_ledger(P["paper"])
    except SystemExit as e:
        C.add("FAIL", "ledger", str(e))
        return
    if L.empty:
        C.add("INFO", "ledger", "empty - day 1 not started (the first nightly run records it)")
        return
    C.add("PASS", "ledger", f"hash chain intact: {len(L):,} lines, {L['date'].nunique()} days, last {L['date'].max()}")


def parse_tasks(text: str) -> dict:
    """schtasks /Query /FO CSV /V output -> which tasks run daily_run.bat / monthly_retrain.bat, and which are enabled."""
    rows = list(csv.reader(io.StringIO(text)))
    head = rows[0]
    ti = next(i for i, h in enumerate(head) if "Task To Run" in h)
    si = next((i for i, h in enumerate(head) if "Scheduled Task State" in h), None)
    ni = next(i for i, h in enumerate(head) if h.strip() == "TaskName")
    body = [r for r in rows[1:] if len(r) > max(ti, ni) and r[ni] != "TaskName"]
    on = lambda r: si is None or "enabled" in r[si].lower()
    return {"daily": sorted({r[ni] for r in body if "daily_run.bat" in r[ti].lower()}),
            "daily_enabled": sorted({r[ni] for r in body if "daily_run.bat" in r[ti].lower() and on(r)}),
            "monthly_enabled": sorted({r[ni] for r in body if "monthly_retrain.bat" in r[ti].lower() and on(r)})}


def check_scheduler(C: Checks, text: str = None) -> None:
    if text is None:
        if platform.system() != "Windows":
            C.add("SKIP", "scheduler", "not Windows - check Task Scheduler by hand")
            return
        try:
            text = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/V"], capture_output=True, text=True, timeout=60).stdout
        except Exception as e:
            C.add("WARN", "scheduler", f"could not run schtasks ({type(e).__name__}) - check by hand")
            return
    try:
        t = parse_tasks(text)
    except Exception as e:
        C.add("WARN", "scheduler", f"could not read Task Scheduler ({type(e).__name__}) - check by hand")
        return
    C.add("PASS" if t["daily_enabled"] else "FAIL", "scheduler",
          f"a task runs daily_run.bat ({t['daily_enabled'][0]})" if t["daily_enabled"] else
          (f"the daily_run.bat task is DISABLED ({t['daily'][0]}) - enable it" if t["daily"] else
           "no task runs daily_run.bat - create the 23:45 weekday task"))
    C.add("FAIL" if t["monthly_enabled"] else "PASS", "scheduler",
          f"monthly retrain task still ENABLED: {t['monthly_enabled']} - disable it" if t["monthly_enabled"]
          else "no enabled monthly retrain task")


def check_watchlist(C: Checks, root: Path, isin) -> None:
    try:
        r = PP.cmd_watchlist(root, isin, True, verbose=False)
    except SystemExit as e:
        C.add("FAIL", "watchlist", str(e))
        return
    t, S = r["table"], r["scores"]
    ok = all(len(t[t["engine"] == e]) == 10 for e in PP.CFG["engines"]) and np.isfinite(S[["score_D", "score_C", "score_ENS"]]).all().all()
    extra = RC.load_symbol_list(root / RC.UNIVERSE_EXCLUDE_FILE)
    etfs = [s for s in t["symbol"] if RC.is_etf_symbol(s, extra)]
    cards = set(r["cards"]) == set(t["symbol"]) and all(len(c.get("c_profile", {}).get("rows", [])) == 12 and c.get("tug")
                                                        for c in r["cards"].values())
    C.add("PASS" if ok and not etfs and cards else "FAIL", "watchlist",
          f"dry run for the open after {pd.Timestamp(r['date']).date()}: {len(S):,} stocks scored; "
          + " | ".join(f"{e} " + ", ".join(t[t['engine'] == e]['symbol'].head(3)) for e in PP.CFG["engines"])
          + ("" if not etfs else f"; ETFs in the list: {etfs}") + ("" if cards else "; cards missing"))


def run(root: Path, watchlist: bool = False, isin=None) -> Checks:
    C = Checks()
    research_pp = root / "panel_oc" / "panel.parquet"
    print("=" * 76)
    print(f"{CODE_VERSION}: checking this machine before day 1")
    print("=" * 76)
    check_env(C, root)
    check_env_root(C, root)
    check_files(C)
    man = check_freeze(C, research_pp)
    check_production(C, research_pp, man)
    check_live(C, root, research_pp, True)
    check_ledger(C, root)
    check_scheduler(C)
    if watchlist:
        check_watchlist(C, root, isin)
    print("=" * 76)
    f = C.failed()
    if f:
        print(f"NOT READY - {len(f)} check(s) failed:")
        for x in f:
            print("   - " + x)
    else:
        warns = sum(1 for s, _, _ in C.rows if s == "WARN")
        print("READY FOR DAY 1" + (f" ({warns} warning(s) above - read them)" if warns else "")
              + ("" if watchlist else "  - run once more with --watchlist before the first night"))
    print("=" * 76)
    return C


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-flight checks before day 1 of the paper test")
    ap.add_argument("--root", required=True)
    ap.add_argument("--watchlist", action="store_true", help="also run a dry-run watchlist (~15 minutes)")
    ap.add_argument("--isin", nargs="+", default=None)
    a = ap.parse_args(argv)
    isin = a.isin
    auto = Path(a.root) / "paper" / "isin_bhavcopy.csv"
    if isin is None and auto.exists():
        isin = [str(auto)]
    C = run(Path(a.root), a.watchlist, isin)
    return 1 if C.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
