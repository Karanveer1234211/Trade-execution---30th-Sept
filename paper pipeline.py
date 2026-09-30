#!/usr/bin/env python3
"""
paper_pipeline.py - the forward paper test of the frozen engines, with Soul cards.

    python paper_pipeline.py init-live      --root %CACHE_DAILY_ROOT%     (once)
    python paper_pipeline.py watchlist      --root %CACHE_DAILY_ROOT%     (every evening / before 09:00)
    python paper_pipeline.py settle         --root %CACHE_DAILY_ROOT%     (every day)
    python paper_pipeline.py report         --root %CACHE_DAILY_ROOT%
    python paper_pipeline.py verify-ledger  --root %CACHE_DAILY_ROOT%

Every settle also writes the TRACKER (paper\\tracker.csv, tracker.md): each recorded
pick of the last days - waiting for its open, open (session k of 5, unrealised
return, MFE / MAE so far), closed (net result), or skipped (locked open / no bar);
ranks beyond the traded three are followed as SHADOWS (what they would have done).
And the LOG (paper\\daily_log.md): session by session, what was bought at the open,
what was skipped, what was sold and for how much, what is held at the close, and
the running totals. Both are rebuilt from the ledger and the cache every night.

Rules: PAPER_DECLARATION.json (fixed before day 1). In short:
  * panel_live (a copy of panel_oc, then updated nightly by the frozen
    panel_build) gives each stock's features on the latest session T;
  * the frozen production models score every universe stock (D, and the
    ensemble = 0.5 rank(D) + 0.5 rank(C)); ETFs are removed; the top 10 of each
    engine are appended to the ledger, with the recording time, BEFORE the open;
  * every recorded stock gets a Soul card (soul_v4) - descriptive only;
  * settlement: the first 3 recorded stocks buyable at the next open, held 5
    sessions, honest exits, 35 bp; late recordings (after 09:00 of the entry
    session) are shown but excluded from the evidence;
  * the month-6 rule for D is printed in every report.
The ledger (<root>\\paper\\ledger.jsonl) is append-only and hash-chained: each
line carries the SHA-256 of the previous one, so any edit is detected.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import research_common as RC       # first: from a subfolder, this folder's copy must win
import panel_build as PB           # noqa: E402,F401
import stage_b_screen as SB        # noqa: E402
import stage_d_gate as SD          # noqa: E402
import stage_d_forensics as SF     # noqa: E402
import stage_c_features as SC      # noqa: E402
import stage_e_execution as EX     # noqa: E402
import freeze_engines as FZ        # noqa: E402
import production_models as PM     # noqa: E402
import soul_v4 as SV               # noqa: E402

CODE_VERSION = "paper_pipeline v1.5"   # v1.5: never records a day whose entry session already opened unless --allow-late (v1.4: progress)
DECL_FILE = "PAPER_DECLARATION.json"
CFG = {
 "engines": [
  "D",
  "ENS"
 ],
 "target": "label_oc_5",
 "record_top": 10,
 "trade_top": 3,
 "hold": 5,
 "cost_bps": 35.0,
 "late_after": "09:00",
 "boot_block": 10,
 "boot_B": 10000,
 "seed": 7,
 "production_seed": [
  7,
  99,
  5
 ],
 "soul": {
  "halflife_days": 365,
  "prior_k": 30.0,
  "self_k": 20,
  "cross_k": 50,
  "self_shrink_k": 10.0,
  "cross_pool": 200000,
  "kernel_eps": 0.1,
  "extra_dims": [
   "D_drawdown_252",
   "D_atr_pct_z252"
  ],
  "target_pct": 0.05,
  "c_profile": [
   {
    "feature": "I_cgw_1",
    "idea": "past return x volume shock (Campbell-Grossman-Wang)",
    "ic_oc5": -0.030179,
    "t_oc5": -15.96,
    "favoured": "low"
   },
   {
    "feature": "I_mdi_vol",
    "idea": "trend strength x volatility",
    "ic_oc5": 0.046519,
    "t_oc5": 15.53,
    "favoured": "high"
   },
   {
    "feature": "C_resid_1d",
    "idea": "market-adjusted return, last session (reversal)",
    "ic_oc5": -0.036351,
    "t_oc5": -15.23,
    "favoured": "low"
   },
   {
    "feature": "C_ret_to_band",
    "idea": "distance to the circuit band",
    "ic_oc5": -0.036838,
    "t_oc5": -14.75,
    "favoured": "low"
   },
   {
    "feature": "I_rev_spread",
    "idea": "reversal x spread (the one C feature whose favoured fifth cleared costs)",
    "ic_oc5": -0.04219,
    "t_oc5": -11.39,
    "favoured": "low"
   },
   {
    "feature": "C_streak",
    "idea": "up/down streak",
    "ic_oc5": -0.030408,
    "t_oc5": -11.43,
    "favoured": "low"
   },
   {
    "feature": "C_resid_5d_z",
    "idea": "market-adjusted 5-session return, z-scored (reversal)",
    "ic_oc5": -0.042799,
    "t_oc5": -10.65,
    "favoured": "low"
   },
   {
    "feature": "C_max1_resid_21",
    "idea": "lottery: biggest market-adjusted day in 21 sessions (MAX)",
    "ic_oc5": -0.042398,
    "t_oc5": -9.14,
    "favoured": "low"
   },
   {
    "feature": "C_on_sum_20",
    "idea": "overnight gaps, 20-session sum (tug of war)",
    "ic_oc5": -0.034208,
    "t_oc5": -8.21,
    "favoured": "low"
   },
   {
    "feature": "I_gap_turn",
    "idea": "gap x turnover shock",
    "ic_oc5": -0.017362,
    "t_oc5": -8.19,
    "favoured": "low"
   },
   {
    "feature": "C_idio_vol_60",
    "idea": "idiosyncratic volatility, 60 sessions",
    "ic_oc5": -0.049071,
    "t_oc5": -7.12,
    "favoured": "low"
   },
   {
    "feature": "C_cs_spread_20",
    "idea": "Corwin-Schultz spread estimate, 20 sessions (cost)",
    "ic_oc5": -0.023123,
    "t_oc5": -3.87,
    "favoured": "low"
   }
  ],
  "tug_window": 250
 }
}


class PreconditionError(SystemExit):
    pass


def _log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S}  {msg}", flush=True)


def _stage(job: str, k: int, n: int, label: str, verbose: bool = True) -> None:
    """One progress line: which sub-stage is starting and how much of the job is done."""
    if verbose:
        pct = int(round(100 * (k - 1) / n))
        bar = "#" * (pct // 5) + "." * (20 - pct // 5)
        print(f"{dt.datetime.now():%H:%M:%S}  [{bar}] {pct:3d}%  {job} {k}/{n}: {label}", flush=True)


def paths(root: Path) -> dict:
    return {"research": root / "panel_oc" / "panel.parquet", "live": root / "panel_live" / "panel.parquet",
            "paper": root / "paper"}


def load_declaration() -> Tuple[dict, str]:
    p = HERE / DECL_FILE
    if not p.exists():
        raise PreconditionError(f"{DECL_FILE} not found next to {Path(__file__).name}")
    raw = p.read_bytes()
    decl = json.loads(raw.decode("utf-8"))
    if decl.get("stage") != "paper" or decl.get("config") != json.loads(json.dumps(CFG)):
        raise PreconditionError(f"settings differ from {DECL_FILE}; the declaration is fixed")
    if CFG["production_seed"] != PM.PROD_SEED:
        raise PreconditionError("production seed differs from production_models.PROD_SEED")
    return decl, hashlib.sha256(raw).hexdigest()[:16]


# ----------------------------------------------------------------------
# the ledger (append-only, hash-chained)
# ----------------------------------------------------------------------
def universe_sha(symbols) -> str:
    return hashlib.sha256(json.dumps(sorted(symbols)).encode("utf-8")).hexdigest()


def _h(prev: str, payload: dict) -> str:
    return hashlib.sha256((prev + json.dumps(payload, sort_keys=True, default=str)).encode("utf-8")).hexdigest()


def read_ledger(paper: Path) -> pd.DataFrame:
    fp = paper / "ledger.jsonl"
    if not fp.exists():
        return pd.DataFrame()
    rows, prev = [], "genesis"
    for i, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec.get("prev") != prev or _h(prev, rec) != h:
            raise PreconditionError(f"paper ledger line {i} was edited or reordered - the forward evidence is broken")
        prev = h
        rows.append(rec)
    return pd.DataFrame(rows)


def append_ledger(paper: Path, recs: List[dict]) -> int:
    paper.mkdir(parents=True, exist_ok=True)
    fp = paper / "ledger.jsonl"
    L = read_ledger(paper)
    prev = "genesis"
    if fp.exists():
        last = fp.read_text(encoding="utf-8").splitlines()
        prev = json.loads(last[-1])["hash"] if last else "genesis"
    with open(fp, "a", encoding="utf-8") as f:
        for r in recs:
            r = dict(r, prev=prev)
            h = _h(prev, r)
            f.write(json.dumps(dict(r, hash=h), sort_keys=True, default=str) + "\n")
            prev = h
    return len(L) + len(recs)


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------
def read_day(pp: Path, cols: List[str], day) -> pd.DataFrame:
    """Rows of one session, reading the file batch by batch (never all columns at once)."""
    import pyarrow.parquet as pq
    T = pd.Timestamp(day)
    out = []
    for batch in pq.ParquetFile(pp).iter_batches(batch_size=250_000, columns=list(dict.fromkeys(["timestamp"] + cols))):
        df = batch.to_pandas()
        df["timestamp"] = SB._naive(df["timestamp"])
        df = df[df["timestamp"] == T]
        if len(df):
            out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["timestamp"] + cols)


def latest_session(pp: Path) -> pd.Timestamp:
    return pd.Timestamp(SB._naive(pd.read_parquet(pp, columns=["timestamp"])["timestamp"]).max())


def etf_set(root: Path, symbols, isin: Optional[List[str]]) -> set:
    extra = RC.load_symbol_list(root / RC.UNIVERSE_EXCLUDE_FILE)
    if isin:
        cls = SF.classify_symbols(symbols, SF.load_isin_files(isin), extra)
        return set(cls.loc[cls["etf"], "symbol"])
    return {s for s in symbols if RC.is_etf_symbol(s, extra)}


def score_day(root: Path, T, meta: dict, models: dict, c_frame: Optional[pd.DataFrame] = None, verbose: bool = False):
    P = paths(root)
    _stage("watchlist", 1, 6, f"reading {pd.Timestamp(T).date()}'s features and scoring engine D", verbose)
    fd = meta["models"]["D"]["features"]
    fc = meta["models"]["C"]["features"]
    D = read_day(P["live"], ["symbol"] + fd, T)
    if D.empty:
        raise PreconditionError(f"no panel_live rows on {pd.Timestamp(T).date()}")
    D["symbol"] = D["symbol"].astype(str)
    D["score_D"] = models["D"].predict(D[fd].to_numpy(dtype=np.float32))
    if c_frame is None:
        _stage("watchlist", 2, 6, "rebuilding the 123 stage C features (the longest part - several minutes)", verbose)
        _, _, names = SC.load_declaration()
        c_frame = SC.build(P["live"], root, names, verbose=verbose)
    _stage("watchlist", 3, 6, "scoring model C and the ensemble", verbose)
    C = c_frame[pd.to_datetime(c_frame["timestamp"]) == pd.Timestamp(T)].copy()
    C["symbol"] = C["symbol"].astype(str)
    C["score_C"] = models["C"].predict(C[fc].to_numpy(dtype=np.float32))
    S = D[["symbol", "score_D"]].merge(C[["symbol", "score_C"]], on="symbol", how="inner")
    S["score_ENS"] = 0.5 * S["score_D"].rank(pct=True) + 0.5 * S["score_C"].rank(pct=True)
    return S, C


def rank_engine(S: pd.DataFrame, engine: str, etf: set, top: int) -> pd.DataFrame:
    col = f"score_{engine}"
    x = S[~S["symbol"].isin(etf)].sort_values([col, "symbol"], ascending=[False, True]).head(top).copy()
    x["rank"] = np.arange(1, len(x) + 1)
    x["engine"] = engine
    x["score"] = x[col]
    return x[["engine", "rank", "symbol", "score"]]


# ----------------------------------------------------------------------
# Soul
# ----------------------------------------------------------------------
def walkforward_picks(research_pp: Path, man: dict, etf: set) -> pd.DataFrame:
    parts = []
    for eng in ("D", "ENS"):
        lists, _ = EX.load_lists(research_pp.parent / "stage_d" / man["engines"][eng]["run"], man["target"], etf, 3)
        parts.append(pd.DataFrame([(d, s, eng) for d, v in lists.items() for s in v[:3]],
                                  columns=["signal_day", "symbol", "engine"]))
    P = pd.concat(parts, ignore_index=True)
    P["signal_day"] = pd.to_datetime(P["signal_day"])
    return P


def build_soul(root: Path, man: dict, etf: set, bars: Dict[str, EX.Bars], c_today=None) -> SV.Soul:
    P = paths(root)
    O = SV.build_outcomes(bars, CFG["hold"], CFG["soul"]["target_pct"])
    S = SV.state_table(P["live"], CFG["soul"]["extra_dims"])
    return SV.Soul(O, S, CFG["soul"], walkforward_picks(P["research"], man, etf), bars,
                   CFG["cost_bps"] / 1e4, CFG["hold"], c_today)


# ----------------------------------------------------------------------
# settlement
# ----------------------------------------------------------------------
def upper_locked_open(b: EX.Bars, e: int) -> bool:
    if e < 1:
        return False
    tk = SF.tick_size(np.array([b.ts[e]]), np.array([b.c[e - 1]]))[0]
    return bool(SF.band_up(b.o[e], b.c[e - 1], tk) and b.o[e] >= b.h[e] * (1 - 1e-9))


def settle_rows(L: pd.DataFrame, bars: Dict[str, EX.Bars], calendar: np.ndarray, soul_O: Optional[pd.DataFrame],
                universe: Dict, hold: int, top: int, cost: float, late_after: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    trades, days = [], []
    hh, mm = (int(x) for x in late_after.split(":"))
    for (d, eng), g in L.groupby(["date", "engine"]):
        T = np.datetime64(pd.Timestamp(d), "ns")
        i = int(np.searchsorted(calendar, T, side="right"))
        if i >= len(calendar):
            days.append({"date": d, "engine": eng, "status": "pending (entry session not yet traded)"})
            continue
        entry = calendar[i]
        rec_at = pd.Timestamp(g["recorded_at"].iloc[0])
        late = rec_at > pd.Timestamp(entry) + pd.Timedelta(hours=hh, minutes=mm)
        taken, pend = [], False
        for _, r in g.sort_values("rank").iterrows():
            if len(taken) >= top:
                break
            b = bars.get(r["symbol"])
            e = b.index.get(entry) if b is not None else None
            if e is None or upper_locked_open(b, e):
                continue
            x, kind, raw, fl = EX.plan_exit(b, e, hold, None)
            if fl["data_end"] or (fl["locked_exit"] and kind == "close" and x == len(b.c) - 1):
                pend = True
                taken.append(None)
                continue
            ret = raw / b.o[e] - 1
            trades.append({"date": d, "engine": eng, "rank": int(r["rank"]), "symbol": r["symbol"], "entry_day": entry,
                           "exit_day": b.ts[x], "entry": float(b.o[e]), "exit": float(raw), "exit_kind": kind,
                           "locked_exit": bool(fl["locked_exit"]), "ret": float(ret), "net": float(ret - cost), "late": bool(late)})
            taken.append(ret)
        got = [t for t in taken if t is not None]
        if pend or len(taken) == 0:
            days.append({"date": d, "engine": eng, "status": "pending (exit not yet reached)" if pend else "no buyable pick"})
            continue
        bench = np.nan
        if soul_O is not None and universe.get(d) is not None:
            u = soul_O[(soul_O["signal_day"] == pd.Timestamp(d)) & soul_O["symbol"].isin(universe[d])]
            u = u[np.isfinite(u["r5"]) & u["buyable"].astype(bool)]      # buy everything the same way: buyable opens only
            bench = float(u["r5"].mean()) if len(u) else np.nan
        net = float(np.mean(got) - cost)
        days.append({"date": d, "engine": eng, "status": "settled", "late": bool(late), "trades": len(got),
                     "net": net, "bench_net": bench - cost if np.isfinite(bench) else np.nan,
                     "excess": net - (bench - cost) if np.isfinite(bench) else np.nan})
    return pd.DataFrame(trades), pd.DataFrame(days)


def month6_status(dd: pd.DataFrame, first_day: pd.Timestamp, today: pd.Timestamp) -> dict:
    ok = dd[(dd["status"] == "settled") & (~dd["late"].astype(bool))] if len(dd) else dd
    due = first_day + pd.DateOffset(months=6)
    out = {"first_day": str(first_day.date()), "month6_on": str(due.date()), "days": int(len(ok))}
    if len(ok) < 20:
        out["status"] = "too early"
        return out
    b = SD.block_bootstrap(ok.sort_values("date")["net"].to_numpy(float), 0.95, CFG["boot_B"], CFG["boot_block"], CFG["seed"])
    out.update({"mean_net_bp": b["mean"] * 1e4, "lo_bp": b["lo"] * 1e4, "hi_bp": b["hi"] * 1e4,
                "mean_excess_bp": float(ok["excess"].mean() * 1e4)})
    if today < due:
        out["status"] = f"running - the month-6 check is due on {due.date()}"
    elif b["mean"] > 0 and ok["excess"].mean() > 0:
        out["status"] = "CONFIRMED"
    elif np.isfinite(b["hi"]) and b["hi"] < 0:
        out["status"] = "FAILED"
    else:
        out["status"] = "INCONCLUSIVE - continue to month 12"
    return out


# ----------------------------------------------------------------------
# the tracker and the log
# ----------------------------------------------------------------------
def truncate_bars(bars: Dict[str, EX.Bars], asof) -> Dict[str, EX.Bars]:
    """The cache as it stood at the close of `asof` (tests; production uses all data)."""
    if asof is None:
        return bars
    A = np.datetime64(pd.Timestamp(asof), "ns")
    out = {}
    for s, b in bars.items():
        k = int(np.searchsorted(b.ts, A, side="right"))
        if k:
            ts = b.ts[:k]
            out[s] = EX.Bars(ts, b.o[:k], b.h[:k], b.l[:k], b.c[:k], b.vol20[:k], {t: i for i, t in enumerate(ts)})
    return out


def track(L: pd.DataFrame, bars: Dict[str, EX.Bars], calendar: np.ndarray, hold: int, top: int, cost: float) -> pd.DataFrame:
    rows = []
    for (d, eng), g in L.groupby(["date", "engine"]):
        T = np.datetime64(pd.Timestamp(d), "ns")
        i = int(np.searchsorted(calendar, T, side="right"))
        entry = calendar[i] if i < len(calendar) else None
        taken = 0
        for _, r in g.sort_values("rank").iterrows():
            base = {"date": d, "engine": eng, "rank": int(r["rank"]), "symbol": r["symbol"]}
            if entry is None:
                rows.append({**base, "role": "traded" if taken < top else "shadow", "status": "waiting for the next open"})
                taken += 1
                continue
            b = bars.get(r["symbol"])
            e = b.index.get(entry) if b is not None else None
            if e is None or upper_locked_open(b, e):
                why = "no bar on the entry session" if e is None else "opened locked at the upper circuit - not buyable"
                rows.append({**base, "role": "skipped" if taken < top else "shadow", "status": why,
                             "entry_day": entry})
                continue
            role = "traded" if taken < top else "shadow"
            taken += 1 if role == "traded" else 0
            j_last = len(b.c) - 1
            x, kind, raw, fl = EX.plan_exit(b, e, hold, None)
            closed = not fl["data_end"] and not (fl["locked_exit"] and kind == "close" and x == j_last)
            upto = min(x, j_last) if closed else j_last
            rec = {**base, "role": role, "entry_day": entry, "entry": float(b.o[e]),
                   "mfe": float(b.h[e:upto + 1].max() / b.o[e] - 1), "mae": float(b.l[e:upto + 1].min() / b.o[e] - 1),
                   "locked_exit": bool(fl["locked_exit"])}
            if closed:
                ret = raw / b.o[e] - 1
                rec.update({"status": "closed", "exit_day": b.ts[x], "exit": float(raw), "ret": float(ret),
                            "net": float(ret - cost), "sessions": int(min(x, j_last) - e + 1)})
            else:
                held = j_last - e + 1
                st = f"open - session {min(held, hold)} of {hold}" if held <= hold else "locked at the lower circuit - waiting to sell"
                rec.update({"status": st, "last_close": float(b.c[j_last]), "ret": float(b.c[j_last] / b.o[e] - 1),
                            "sessions": int(held)})
            rows.append(rec)
    cols = ["date", "engine", "rank", "symbol", "role", "status", "entry_day", "entry", "sessions", "last_close",
            "exit_day", "exit", "ret", "net", "mfe", "mae", "locked_exit"]
    return pd.DataFrame(rows).reindex(columns=cols)


def _pct(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}%"


def write_tracker(paper: Path, TR: pd.DataFrame, bars: Dict[str, EX.Bars], calendar: np.ndarray, L: pd.DataFrame,
                  dd: pd.DataFrame, today: pd.Timestamp) -> None:
    TR.to_csv(paper / "tracker.csv", index=False)
    tr = TR[TR["role"] == "traded"] if len(TR) else TR
    # ---- tracker.md: what is open now, and the last recorded days
    M = [f"# Tracker - {today.date()}", "", f"{CODE_VERSION} | rebuilt from the ledger and the cache every night", ""]
    op = tr[tr["status"].astype(str).str.startswith(("open", "locked"))] if len(tr) else tr
    M += ["## Open positions", "", "| engine | symbol | bought | entry | status | now | MFE so far | MAE so far |",
          "|---|---|---|---|---|---|---|---|"]
    for _, r in op.sort_values(["engine", "entry_day"]).iterrows():
        M.append(f"| {r['engine']} | {r['symbol']} | {pd.Timestamp(r['entry_day']).date()} | {r['entry']:,.2f} | {r['status']} | "
                 f"{_pct(r['ret'])} | {_pct(r['mfe'])} | {_pct(r['mae'])} |")
    if not len(op):
        M.append("| - | none | | | | | | |")
    M += ["", "## Last recorded days (traded picks; shadows = ranks beyond the traded three)", ""]
    for d in sorted(TR["date"].unique(), reverse=True)[:10] if len(TR) else []:
        M.append(f"### watchlist of {d}")
        for eng in CFG["engines"]:
            g = TR[(TR["date"] == d) & (TR["engine"] == eng)]
            t = g[g["role"] == "traded"]
            sh = g[(g["role"] == "shadow") & (g["status"] == "closed")]
            parts = []
            for _, r in t.iterrows():
                v = f"{r['net'] * 1e4:+.0f} bp net" if r.get("status") == "closed" else f"{r['status']}, {_pct(r.get('ret'))}" \
                    if pd.notna(r.get("ret")) else r["status"]
                parts.append(f"{r['symbol']} ({v})")
            line = f"- {eng}: " + ("; ".join(parts) if parts else "no trade")
            if len(sh):
                line += f" | shadows closed: {len(sh)}, average {sh['net'].mean() * 1e4:+.0f} bp"
            M.append(line)
        M.append("")
    (paper / "tracker.md").write_text("\n".join(M), encoding="utf-8")
    # ---- daily_log.md: every session since the first watchlist, newest first (one pass over the picks)
    first = pd.Timestamp(L["date"].min()) if len(L) else today
    sess = [pd.Timestamp(x) for x in calendar if first <= pd.Timestamp(x) <= today] if len(calendar) else []
    ev = {d_: {"buy": [], "skip": [], "sell": [], "hold": []} for d_ in sess}
    for _, r in (TR.iterrows() if len(TR) else []):
        if pd.isna(r.get("entry_day")):
            continue
        E0 = pd.Timestamp(r["entry_day"])
        if r["role"] == "skipped" and E0 in ev:
            ev[E0]["skip"].append(f"{r['engine']} skipped {r['symbol']} (rank {r['rank']}): {r['status']}")
            continue
        if r["role"] != "traded" or E0 not in ev:
            continue
        ev[E0]["buy"].append((r["engine"], f"{r['symbol']} (rank {r['rank']}) Rs {r['entry']:,.2f}"))
        X0 = pd.Timestamp(r["exit_day"]) if r["status"] == "closed" else None
        if X0 is not None and X0 in ev:
            ev[X0]["sell"].append(f"{r['engine']} sold {r['symbol']} (bought {E0.date()}): {r['net'] * 1e4:+.0f} bp net"
                                  f"{' - LOCKED EXIT, sold at a later open' if r['locked_exit'] else ''} "
                                  f"(MFE {_pct(r['mfe'])}, MAE {_pct(r['mae'])})")
        bb = bars.get(r["symbol"])
        if bb is None:
            continue
        e = bb.index.get(np.datetime64(E0, "ns"))
        stop = bb.index.get(np.datetime64(X0, "ns")) if X0 is not None else len(bb.c)
        for k in range(e, min(stop, len(bb.c))):
            dk = pd.Timestamp(bb.ts[k])
            if dk in ev:
                ev[dk]["hold"].append(f"{r['engine']} {r['symbol']} {_pct(bb.c[k] / r['entry'] - 1)}")
    G = ["# Daily log - newest first", "", f"{CODE_VERSION} | every session since the first watchlist, rebuilt nightly", ""]
    st = {}
    for eng in CFG["engines"]:
        de = dd[(dd["engine"] == eng) & (dd["status"] == "settled")] if len(dd) else dd
        if len(de):
            ok = de[~de["late"].astype(bool)]
            st[eng] = f"{len(ok)} on-time settled days, mean net {ok['net'].mean() * 1e4:+.1f} bp per trade" if len(ok) else "no on-time day yet"
    if st:
        G += ["Running: " + " | ".join(f"{k}: {v}" for k, v in st.items()), ""]
    for D0 in reversed(sess):
        x = ev[D0]
        G.append(f"## {D0.date()} ({D0:%a})")
        for eng in CFG["engines"]:
            bl = [t for e_, t in x["buy"] if e_ == eng]
            if bl:
                G.append(f"- {eng} bought at the open: " + ", ".join(bl))
        G += [f"- {t}" for t in x["skip"]] + [f"- {t}" for t in x["sell"]]
        if x["hold"]:
            G.append("- holding at the close: " + ", ".join(x["hold"]))
        wl = L[L["date"] == str(D0.date())]
        if len(wl):
            G.append("- watchlist recorded for the next open: " + " | ".join(
                f"{eng} " + ", ".join(wl[wl["engine"] == eng].sort_values("rank")["symbol"].head(3)) for eng in CFG["engines"]))
        G.append("")
    (paper / "daily_log.md").write_text("\n".join(G), encoding="utf-8")


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_init_live(root: Path) -> int:
    P = paths(root)
    if P["live"].exists():
        raise PreconditionError(f"{P['live']} already exists")
    FZ.verify_frozen(P["research"])
    P["live"].parent.mkdir(parents=True)
    shutil.copy2(P["research"], P["live"])
    shutil.copy2(P["research"].parent / "panel_meta.json", P["live"].parent / "panel_meta.json")
    print(f"panel_live created from panel_oc ({P['live']}). From now on the nightly update runs on panel_live only;")
    print("panel_oc stays frozen for research.")
    return 0


def entry_deadline(T) -> pd.Timestamp:
    """09:00 of the first weekday after session T - when a watchlist for T stops being on time."""
    d = pd.Timestamp(T).normalize() + pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return d + pd.Timedelta(hours=9)


def cmd_watchlist(root: Path, isin=None, dry_run=False, recorded_at=None, c_frame=None, verbose=True,
                  allow_late=False) -> dict:
    P = paths(root)
    decl, dsha = load_declaration()
    man = FZ.verify_frozen(P["research"])
    meta, models = PM.load_verified(P["research"])
    if not P["live"].exists():
        raise PreconditionError("panel_live does not exist - run init-live once")
    T = latest_session(P["live"])
    L = read_ledger(P["paper"])
    if len(L) and (L["date"] == str(T.date())).any() and not dry_run:
        raise PreconditionError(f"the watchlist for {T.date()} is already recorded - the ledger is append-only")
    now_chk = pd.Timestamp(recorded_at) if recorded_at is not None else pd.Timestamp(dt.datetime.now())
    if not dry_run and not allow_late and now_chk > entry_deadline(T):
        raise PreconditionError(
            f"panel_live's latest session is {T.date()}, and its entry session opened at {entry_deadline(T):%Y-%m-%d %H:%M} - "
            f"a watchlist for it now would be LATE. The data steps did not bring in a newer session: check the Kite "
            f"token and the cache step's output. Nothing was recorded. (--allow-late records a late day on purpose.)")
    t0 = time.perf_counter()
    S, C_T = score_day(root, T, meta, models, c_frame, verbose)
    etf = etf_set(root, S["symbol"].unique(), isin)
    table = pd.concat([rank_engine(S, eng, etf, CFG["record_top"]) for eng in CFG["engines"]], ignore_index=True)
    now = pd.Timestamp(recorded_at) if recorded_at is not None else pd.Timestamp(dt.datetime.now())
    syms = sorted(set(table["symbol"]))
    uni_syms = sorted(set(S["symbol"]) - etf)          # the benchmark universe: every scored non-ETF stock
    _stage("watchlist", 4, 6, f"loading prices for {S['symbol'].nunique():,} stocks", verbose)
    bars = EX.load_bars(root, sorted(set(S["symbol"])))
    _stage("watchlist", 5, 6, "building Soul: every past trade's outcome and every stock's state (a few minutes)", verbose)
    soul = build_soul(root, man, etf, bars, C_T)
    _stage("watchlist", 6, 6, f"writing {len(syms)} Soul cards and the watchlist files", verbose)
    cards = {s: soul.card(s, T) for s in syms}
    out = P["paper"] / "watchlists"
    out.mkdir(parents=True, exist_ok=True)
    tag = T.strftime("%Y%m%d")
    table.to_csv(out / f"WL_{tag}.csv", index=False)
    pd.DataFrame([SV.card_row(cards[s]) for s in syms]).to_csv(out / f"soul_cards_{tag}.csv", index=False)
    (out / f"WL_{tag}.md").write_text(watchlist_md(T, table, cards, now, dsha), encoding="utf-8")
    n = 0
    if not dry_run:
        recs = [{"date": str(T.date()), "engine": r["engine"], "rank": int(r["rank"]), "symbol": r["symbol"],
                 "score": float(r["score"]), "recorded_at": now.isoformat(), "tool": CODE_VERSION,
                 "declaration_sha": dsha, "models": {k: v["sha256"][:16] for k, v in meta["models"].items()},
                 "universe_sha": universe_sha(uni_syms)}
                for _, r in table.iterrows()]
        n = append_ledger(P["paper"], recs)
        uni_fp = P["paper"] / "universe.jsonl"
        with open(uni_fp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": str(T.date()), "symbols": uni_syms}) + "\n")
    if verbose:
        print(f"{dt.datetime.now():%H:%M:%S}  [{'#' * 20}] 100%  watchlist done", flush=True)
        _log(f"watchlist {T.date()}: {len(S):,} stocks scored, top {CFG['record_top']} per engine"
             f"{' (dry run - not recorded)' if dry_run else f', ledger now {n} lines'} ({time.perf_counter() - t0:.0f}s)")
    return {"date": T, "table": table, "cards": cards, "scores": S, "soul": soul}


def watchlist_md(T, table, cards, now, dsha) -> str:
    L = [f"# Watchlist for the open after {pd.Timestamp(T).date()}", "",
         f"{CODE_VERSION} | paper declaration {dsha} | recorded {pd.Timestamp(now):%Y-%m-%d %H:%M}", "",
         "**How to use:** in the pre-open (09:00-09:08) buy the first 3 of D's list whose open is NOT locked at the "
         "upper band; hold 5 sessions; sell at the 5th close. No stops, no targets (the path study: every stop and target "
         "lost to the plain exit). D is the primary arm; the ensemble is recorded as the challenger.", ""]
    for eng in CFG["engines"]:
        g = table[table["engine"] == eng]
        L += [f"## {eng} {'(primary)' if eng == 'D' else '(challenger)'}", "",
              "| rank | symbol | score | Soul: own record | moments like today | flags |", "|---|---|---|---|---|---|"]
        for _, r in g.iterrows():
            cd = cards[r["symbol"]]
            ps = cd["personality"]["shrunk"]
            bl = cd["memory"].get("blend", {})
            fl = cd["flags"]
            fs = []
            if fl.get("lower_lock_closes_20"):
                fs.append(f"{fl['lower_lock_closes_20']} lower-circuit closes")
            if fl.get("under_rs20"):
                fs.append("under Rs 20")
            L.append(f"| {int(r['rank'])} | {r['symbol']} | {r['score']:+.4f} | {ps.get('net_bp', np.nan):+.0f} bp, "
                     f"win {ps.get('win_rate', np.nan):.0%} | {bl.get('net_bp', np.nan):+.0f} bp | {', '.join(fs) or '-'} |")
        L.append("")
    L += ["## Soul cards", ""] + [SV.card_markdown(cards[s]) for s in sorted(cards)]
    return "\n".join(L)


def cmd_settle(root: Path, verbose=True, asof=None) -> dict:
    P = paths(root)
    L = read_ledger(P["paper"])
    if L.empty:
        raise PreconditionError("the paper ledger is empty - record a watchlist first")
    _stage("track", 1, 3, "loading prices for every recorded pick and the benchmark universe", verbose)
    syms = sorted(set(L["symbol"]))
    uni = {}
    uf = P["paper"] / "universe.jsonl"
    if uf.exists():
        for line in uf.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            uni[r["date"]] = r["symbols"]
    want = L.groupby("date")["universe_sha"].first().to_dict() if "universe_sha" in L else {}
    for d_, h_ in want.items():
        if isinstance(h_, str) and (d_ not in uni or universe_sha(uni[d_]) != h_):
            raise PreconditionError(f"universe.jsonl for {d_} is missing or was edited - it no longer matches the ledger")
    allsyms = sorted(set(syms) | {s for v in uni.values() for s in v})
    bars = truncate_bars(EX.load_bars(root, allsyms), asof)
    cal = np.unique(np.concatenate([b.ts for b in bars.values()])) if bars else np.array([], dtype="datetime64[ns]")
    _stage("track", 2, 3, "settling finished trades and the buy-everything benchmark", verbose)
    O = SV.build_outcomes(bars, CFG["hold"], CFG["soul"]["target_pct"])
    tr, dd = settle_rows(L, bars, cal, O, uni, CFG["hold"], CFG["trade_top"], CFG["cost_bps"] / 1e4, CFG["late_after"])
    tr.to_csv(P["paper"] / "trades.csv", index=False)
    dd.to_csv(P["paper"] / "days.csv", index=False)
    _stage("track", 3, 3, "rebuilding the tracker, the daily log and the report", verbose)
    TR = track(L, bars, cal, CFG["hold"], CFG["trade_top"], CFG["cost_bps"] / 1e4)
    today = pd.Timestamp(cal[-1]) if len(cal) else pd.Timestamp.today()
    write_tracker(P["paper"], TR, bars, cal, L, dd, today)
    if verbose:
        print(f"{dt.datetime.now():%H:%M:%S}  [{'#' * 20}] 100%  track done", flush=True)
        s = dd[dd["status"] == "settled"] if len(dd) else dd
        _log(f"settled {len(s):,} engine-days, {len(tr):,} trades; pending {int((dd['status'] != 'settled').sum()) if len(dd) else 0}")
    return {"trades": tr, "days": dd, "tracker": TR}


def cmd_report(root: Path, today=None) -> str:
    P = paths(root)
    L = read_ledger(P["paper"])
    dd = pd.read_csv(P["paper"] / "days.csv") if (P["paper"] / "days.csv").exists() else pd.DataFrame()
    first = pd.Timestamp(L["date"].min()) if len(L) else pd.Timestamp.today()
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.today()
    lines = ["# Paper test - running report", "", f"{CODE_VERSION} | first watchlist {first.date()} | report {today.date()}", ""]
    for eng in CFG["engines"]:
        de = dd[dd["engine"] == eng] if len(dd) else dd
        st = month6_status(de, first, today) if len(de) else {"status": "no settled days yet"}
        lines += [f"## {eng} {'(primary - judged at month 6)' if eng == 'D' else '(challenger - descriptive until month 12)'}", ""]
        for k, v in st.items():
            lines.append(f"- {k}: {v:.1f}" if isinstance(v, float) else f"- {k}: {v}")
        lines.append("")
    if len(L) and P["live"].exists():
        ts = pd.to_datetime(pd.read_parquet(P["live"], columns=["timestamp"])["timestamp"]).dt.tz_localize(None).unique()
        sess = sorted(pd.Timestamp(x) for x in ts if pd.Timestamp(x) >= first)
        have = set(pd.to_datetime(L["date"]))
        miss = [x for x in sess if x not in have]
        lines += [f"Sessions since day 1 with NO recorded watchlist: {len(miss)}"
                  + (" - " + ", ".join(str(x.date()) for x in miss[-10:]) if miss else ""), ""]
    lines += ["Month-6 rule for D (declared before day 1): CONFIRMED if mean net per trade > 0 and mean excess > 0; "
              "FAILED if the 95% upper bound of the mean net < 0; otherwise INCONCLUSIVE and the test runs to month 12.", ""]
    txt = "\n".join(lines)
    (P["paper"] / "paper_report.md").write_text(txt, encoding="utf-8")
    return txt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Forward paper test of the frozen engines")
    ap.add_argument("cmd", choices=["init-live", "watchlist", "settle", "report", "verify-ledger"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--isin", nargs="+", default=None)
    ap.add_argument("--dry-run", action="store_true", help="watchlist only: write files, record nothing")
    ap.add_argument("--allow-late", action="store_true",
                    help="watchlist only: record even if the entry session already opened (the day is marked late)")
    ap.add_argument("--skip-if-recorded", action="store_true",
                    help="watchlist only: if the latest session is already recorded (holiday / re-run), exit cleanly")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.cmd == "init-live":
        return cmd_init_live(root)
    if a.cmd == "watchlist":
        if a.skip_if_recorded and not a.dry_run:
            P = paths(root)
            Lx = read_ledger(P["paper"])
            T = latest_session(P["live"]) if P["live"].exists() else None
            if T is not None and len(Lx) and (Lx["date"] == str(T.date())).any():
                print(f"no new session since {T.date()} (already recorded) - nothing to record")
                return 0
        r = cmd_watchlist(root, a.isin, a.dry_run, allow_late=a.allow_late)
        print(f"  watchlist: {paths(root)['paper'] / 'watchlists'}")
        top = r["table"]
        for eng in CFG["engines"]:
            print(f"  {eng}: " + ", ".join(top[top["engine"] == eng]["symbol"].head(5)) + " ...")
        return 0
    if a.cmd == "settle":
        cmd_settle(root)
        print(cmd_report(root))
        print(f"  tracker: {paths(root)['paper'] / 'tracker.md'}\n  log:     {paths(root)['paper'] / 'daily_log.md'}")
        return 0
    if a.cmd == "report":
        print(cmd_report(root))
        return 0
    L = read_ledger(paths(root)["paper"])
    print(f"paper ledger verified: {len(L):,} lines, hash chain intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
