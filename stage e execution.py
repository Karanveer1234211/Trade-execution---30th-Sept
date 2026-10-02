#!/usr/bin/env python3
"""
stage_e_execution.py - trade the frozen engines' watchlists with real-money
mechanics and answer the trading questions in one report.

    python stage_e_execution.py --panel %CACHE_DAILY_ROOT%\\panel_oc --root %CACHE_DAILY_ROOT%
    (optional) --isin <NSE bhavcopy file(s)>  to drop ETFs by ISIN instead of by name

THE RULES ARE IN EXECUTION_DECLARATION.json
The PRIMARY setup (engine D, 3 new positions a day, 5-session hold, 1/15 of
equity each, Rs 10 lakh, Zerodha delivery costs + slippage + impact, 2% of
turnover liquidity cap, honest locked exits) is fixed in advance. Every other
row in the report is DESCRIPTIVE: picking the best-looking row of a grid is
fitting the backtest. This runs on the same history the engines were judged
on - it is not new evidence of the edge; the forward paper ledger is.

HOW A DAY RUNS (the simulator)
  open : sells scheduled at this open (exits postponed by a lower-band lock,
         or stop gaps) -> buys for yesterday's watchlist, in rank order, each
         1/(N x H) of yesterday's closing equity; a pick with no bar today or
         no liquidity is replaced by the next rank (20 deep)
  day  : stop-loss fills at the stop price if the low reaches it (scenario)
  close: time exits at the close of the H-th session held; a close locked at
         the lower band cannot sell -> the first later open that is not locked
  mark : equity = cash + every open position at today's close
Costs: STT, stamp, NSE transaction, SEBI, GST, DP per stock per sell day, one
tick at the exit, and square-root market impact 0.5 x daily vol x
sqrt(order / median daily turnover). 'flat35' reproduces the research numbers.

OUTPUT: <panel>\\stage_e\\EXEC_YYYYMMDD_NNN\\ execution_report.md, summary.csv (every
simulation), equity_primary.csv, trades_primary.csv, monthly_primary.csv,
grid.csv, capacity.csv, rank.csv, regimes.csv, costs_primary.csv, stats.json;
ledger stage_e_declared (before) and stage_e_run (after).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import research_common as RC       # first: from a subfolder, this folder's copy must win
import panel_build as PB           # noqa: E402,F401
import stage_b_screen as SB        # noqa: E402
import stage_d_forensics as SF     # noqa: E402  (tick sizes, band test, ISIN)
import freeze_engines as FZ        # noqa: E402

CODE_VERSION = "stage_e_execution v1.2"   # v1.2: optional minimum order (min_order_frac; off by default - every earlier result unchanged)   # v1.1: optional per-trade exit_planner (Soul meta); default runs unchanged
DECL_FILE = "EXECUTION_DECLARATION.json"
CFG = {"engine": "D", "target": "label_oc_5", "n_per_day": 3, "hold": 5, "capital": 1000000,
       "sizing": "equal_tranche", "integer_shares": True, "reserve_depth": 20, "duplicates": True,
       "liquidity_cap_frac": 0.02, "costs": "zerodha_base", "stop_pct": None,
       "impact_k": {"zerodha_base": 0.5, "zerodha_stress": 1.0},
       "grid_n": [1, 2, 3, 5, 10], "grid_hold": [1, 2, 3, 5, 7, 10],
       "capital_sweep": [200000, 500000, 1000000, 2500000, 5000000, 10000000, 25000000, 50000000],
       "stops": [0.05, 0.08, 0.12], "random_seeds": 10, "boot_B": 2000, "boot_block": 10, "seed": 7}
STAT = {"stt": 0.001, "stamp_buy": 0.00015, "txn": 0.0000322, "sebi": 0.000001, "gst": 0.18, "dp": 15.93}
DAYS_PER_YEAR = 250


class PreconditionError(SystemExit):
    pass


def _log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S}  {msg}", flush=True)


@dataclass(frozen=True)
class Scenario:
    name: str
    engine: str = "D"
    n: int = 3
    hold: int = 5
    capital: float = 1_000_000.0
    costs: str = "zerodha_base"          # flat35 | zerodha_base | zerodha_stress
    integer_shares: bool = True
    liquidity_cap: Optional[float] = 0.02
    duplicates: bool = True
    stop: Optional[float] = None
    depth: int = 20
    random_seed: Optional[int] = None
    min_order_frac: Optional[float] = None   # skip an order cash can fund at less than this share of its target


def primary_scenario() -> Scenario:
    return Scenario("PRIMARY", engine=CFG["engine"], n=CFG["n_per_day"], hold=CFG["hold"], capital=CFG["capital"],
                    costs=CFG["costs"], integer_shares=CFG["integer_shares"], liquidity_cap=CFG["liquidity_cap_frac"],
                    duplicates=CFG["duplicates"], stop=CFG["stop_pct"], depth=CFG["reserve_depth"])


# ----------------------------------------------------------------------
# costs
# ----------------------------------------------------------------------
def buy_costs(value: float, sc: Scenario) -> Dict[str, float]:
    if sc.costs == "flat35":
        return {"stt": 0.0, "stamp": 0.0, "txn": 0.0, "sebi": 0.0, "gst": 0.0}
    txn, sebi = value * STAT["txn"], value * STAT["sebi"]
    return {"stt": value * STAT["stt"], "stamp": value * STAT["stamp_buy"], "txn": txn, "sebi": sebi,
            "gst": STAT["gst"] * (txn + sebi)}


def sell_costs(value: float, sc: Scenario, dp_due: bool, buy_value: float) -> Dict[str, float]:
    if sc.costs == "flat35":
        return {"stt": 0.0, "txn": 0.0, "sebi": 0.0, "gst": 0.0, "dp": 0.0, "flat": 0.0035 * buy_value}
    txn, sebi = value * STAT["txn"], value * STAT["sebi"]
    return {"stt": value * STAT["stt"], "txn": txn, "sebi": sebi, "gst": STAT["gst"] * (txn + sebi),
            "dp": STAT["dp"] if dp_due else 0.0, "flat": 0.0}


def impact_frac(sc: Scenario, value: float, adv: float, vol: float) -> float:
    k = CFG["impact_k"].get(sc.costs, 0.0)
    if k <= 0 or not (np.isfinite(adv) and adv > 0 and np.isfinite(vol)):
        return 0.0
    return float(k * vol * math.sqrt(max(value, 0.0) / adv))


def tick_frac(sc: Scenario, date, price: float, side: str) -> float:
    ticks = {"zerodha_base": {"entry": 0, "exit": 1}, "zerodha_stress": {"entry": 1, "exit": 1}}.get(sc.costs, {})
    k = ticks.get(side, 0)
    if k == 0 or not (price > 0):
        return 0.0
    return float(k * SF.tick_size(np.array([date], dtype="datetime64[ns]"), np.array([price]))[0] / price)


# ----------------------------------------------------------------------
# market data
# ----------------------------------------------------------------------
@dataclass
class Bars:
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    vol20: np.ndarray
    index: Dict = field(default_factory=dict)

    @classmethod
    def from_frame(cls, b: pd.DataFrame) -> "Bars":
        ts = b["timestamp"].to_numpy(dtype="datetime64[ns]")
        c = b["close"].to_numpy(float)
        r = pd.Series(c).pct_change()
        v20 = r.rolling(20, min_periods=20).std().to_numpy()
        return cls(ts, b["open"].to_numpy(float), b["high"].to_numpy(float), b["low"].to_numpy(float), c, v20,
                   {t: i for i, t in enumerate(ts)})


def load_bars(root: Path, symbols) -> Dict[str, Bars]:
    from data_quality import _paths
    out = {}
    for s in symbols:
        fp, _ = _paths(root, s)
        if not Path(fp).exists():
            continue
        b = pd.read_parquet(fp, columns=["timestamp", "open", "high", "low", "close"])
        b["timestamp"] = SB._naive(b["timestamp"])
        b = b.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if len(b):
            out[s] = Bars.from_frame(b)
    return out


def lower_locked(b: Bars, j: int, what: str) -> bool:
    """Close (what='close') or open ('open') of bar j sits locked at a lower band vs the previous close."""
    if j <= 0:
        return False
    prev = b.c[j - 1]
    tk = SF.tick_size(np.array([b.ts[j]]), np.array([prev]))[0]
    if what == "close":
        return bool(SF.band_down(b.c[j], prev, tk) and b.c[j] <= b.l[j] * (1 + 1e-9))
    return bool(SF.band_down(b.o[j], prev, tk) and b.o[j] <= b.l[j] * (1 + 1e-9))


def plan_exit(b: Bars, e: int, hold: int, stop: Optional[float], search: int = 20) -> Tuple[int, str, float, dict]:
    """Where and how a position bought at bar e's open is sold: (bar, 'open'|'close'|'stop', raw price, flags)."""
    n = len(b.c)
    flags = {"locked_exit": False, "stopped": False, "data_end": False}
    x = e + hold - 1
    if x >= n:
        x = n - 1
        flags["data_end"] = True
    if stop is not None:
        sp = b.o[e] * (1 - stop)
        for j in range(e, x + 1):
            frozen = b.o[j] == b.h[j] == b.l[j] == b.c[j] and lower_locked(b, j, "close")
            if frozen:
                continue
            if j > e and b.o[j] <= sp:
                flags["stopped"] = True
                return j, "open", float(b.o[j]), flags
            if b.l[j] <= sp:
                flags["stopped"] = True
                return j, "stop", float(sp), flags
    if not flags["data_end"] and lower_locked(b, x, "close"):
        flags["locked_exit"] = True
        last = min(x + search, n - 1)
        for k in range(x + 1, last + 1):
            if not lower_locked(b, k, "open"):
                return k, "open", float(b.o[k]), flags
        return last, "close", float(b.c[last]), flags
    return x, "close", float(b.c[x]), flags


# ----------------------------------------------------------------------
# the simulator
# ----------------------------------------------------------------------
def simulate(sc: Scenario, lists: Dict, bars: Dict[str, Bars], adv: Dict, cal: np.ndarray,
             keep_trades: bool = True, exit_planner=None) -> dict:
    """exit_planner(symbol, bars, entry_bar, signal_day) -> (sell_bar, kind, raw_price, flags) gives each trade
    its own exit; None = the scenario's hold and stop for every trade (the declared primary)."""
    cash = float(sc.capital)
    opened: List[dict] = []
    trades: List[dict] = []
    eq_rows = []
    last_close: Dict[str, float] = {}
    cost_tot = {k: 0.0 for k in ("stt", "stamp", "txn", "sebi", "gst", "dp", "flat", "slip_entry", "slip_exit")}
    prev_equity = cash
    stats = {"orders": 0, "filled": 0, "replaced": 0, "capped": 0, "cash_limited": 0, "skipped_small": 0,
             "n_trades": 0, "sum_ret": 0.0, "wins": 0}

    def close_position(p, day, raw, kind, dp_due):
        nonlocal cash
        b = bars[p["sym"]]
        slip = 0.0
        if kind in ("close", "open", "stop"):
            slip = impact_frac(sc, p["buy_value"], p["adv"], p["vol"]) + tick_frac(sc, day, raw, "exit")
        fill = raw * (1 - slip)
        sell_value = p["shares"] * fill
        c = sell_costs(sell_value, sc, dp_due, p["buy_value"])
        cash += sell_value - sum(c.values())
        for k, v in c.items():
            cost_tot[k] += v
        cost_tot["slip_exit"] += p["shares"] * (raw - fill)
        pnl = sell_value - sum(c.values()) - p["buy_value"] - p["buy_cost"]
        ret = pnl / (p["buy_value"] + p["buy_cost"])
        stats["n_trades"] += 1
        stats["sum_ret"] += ret
        stats["wins"] += int(ret > 0)
        if keep_trades:
            trades.append({"signal_day": p["signal_day"], "entry_day": p["entry_day"], "exit_day": day,
                           "symbol": p["sym"], "rank": p["rank"], "shares": p["shares"], "entry_fill": p["fill"],
                           "exit_raw": raw, "exit_fill": fill, "exit_kind": kind, "buy_value": p["buy_value"],
                           "buy_cost": p["buy_cost"], "sell_value": sell_value, "sell_cost": sum(c.values()),
                           "pnl": pnl, "ret": ret,
                           "raw_ret": raw / p["open_px"] - 1, "locked_exit": p["flags"]["locked_exit"],
                           "stopped": p["flags"]["stopped"], "data_end": p["flags"]["data_end"],
                           "capped": p["capped"], "held": p["held"]})

    for i, day in enumerate(cal):
        # ---- the open: scheduled open-sells, then buys
        dp_today = set()
        still = []
        for p in opened:
            if p["sell_day"] == day and p["sell_kind"] == "open":
                close_position(p, day, p["sell_raw"], "open", p["sym"] not in dp_today)
                dp_today.add(p["sym"])
            else:
                still.append(p)
        opened = still
        if i > 0:
            wl = lists.get(cal[i - 1])
            if wl:
                per = prev_equity / (sc.n * sc.hold)
                held = {p["sym"] for p in opened}
                n_f = 0
                for rank, sym in enumerate(wl[:sc.depth], 1):
                    if n_f >= sc.n:
                        break
                    if not sc.duplicates and sym in held:
                        continue
                    stats["orders"] += 1
                    b = bars.get(sym)
                    e = b.index.get(day) if b is not None else None
                    if e is None or not (b.o[e] > 0):
                        stats["replaced"] += 1
                        continue
                    a = adv.get((cal[i - 1], sym), float("nan"))
                    value = per
                    capped = False
                    if sc.liquidity_cap is not None:
                        cap = sc.liquidity_cap * a if np.isfinite(a) else 0.0
                        if cap <= 0:
                            stats["replaced"] += 1
                            continue
                        if cap < value:
                            value, capped = cap, True
                    vol = b.vol20[e - 1] if e >= 1 else float("nan")
                    slip = impact_frac(sc, value, a, vol) + tick_frac(sc, day, b.o[e], "entry")
                    fill = b.o[e] * (1 + slip)
                    budget = min(value, cash / (1 + 0.0025))
                    if budget < value:
                        stats["cash_limited"] += 1
                        if sc.min_order_frac is not None and budget < sc.min_order_frac * value:
                            stats["skipped_small"] += 1          # a sliver: the slot stays in cash
                            break
                    shares = budget / fill if not sc.integer_shares else math.floor(budget / fill)
                    if shares <= 0:
                        stats["replaced"] += 1
                        continue
                    bv = shares * fill
                    c = buy_costs(bv, sc)
                    cash -= bv + sum(c.values())
                    for k, v in c.items():
                        cost_tot[k] += v
                    cost_tot["slip_entry"] += shares * (fill - b.o[e])
                    if exit_planner is None:
                        sell_bar, kind, raw, flags = plan_exit(b, e, sc.hold, sc.stop)
                    else:
                        sell_bar, kind, raw, flags = exit_planner(sym, b, e, cal[i - 1])
                    p = {"sym": sym, "shares": shares, "fill": fill, "open_px": b.o[e], "buy_value": bv,
                         "buy_cost": sum(c.values()), "rank": rank, "signal_day": cal[i - 1], "entry_day": day,
                         "sell_day": b.ts[sell_bar], "sell_kind": kind, "sell_raw": raw, "flags": flags,
                         "adv": a, "vol": vol, "capped": capped, "held": int(sell_bar - e + 1)}
                    opened.append(p)
                    held.add(sym)
                    stats["filled"] += 1
                    stats["capped"] += int(capped)
                    n_f += 1
        # ---- the day and the close: stop fills, time exits
        still = []
        for p in opened:
            if p["sell_day"] == day and p["sell_kind"] in ("stop", "close"):
                close_position(p, day, p["sell_raw"], p["sell_kind"], p["sym"] not in dp_today)
                dp_today.add(p["sym"])
            else:
                still.append(p)
        opened = still
        # ---- mark to market
        value = 0.0
        for p in opened:
            b = bars[p["sym"]]
            j = b.index.get(day)
            if j is not None:
                last_close[p["sym"]] = b.c[j]
            value += p["shares"] * last_close.get(p["sym"], p["fill"])
        equity = cash + value
        eq_rows.append((day, equity, cash, value, len(opened)))
        prev_equity = equity
    eq = pd.DataFrame(eq_rows, columns=["date", "equity", "cash", "invested", "positions"]).set_index("date")
    return {"scenario": sc, "equity": eq, "trades": pd.DataFrame(trades), "costs": cost_tot, "stats": stats,
            "open_at_end": len(opened)}


# ----------------------------------------------------------------------
# performance
# ----------------------------------------------------------------------
def perf(eq: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1.0
    sd = r.std()
    dn = r[r < 0].std()
    dd = eq / eq.cummax() - 1
    under = (dd < 0).astype(int)
    runs = under.groupby((under != under.shift()).cumsum()).cumsum()
    neg = (r < 0).astype(int)
    streak = neg.groupby((neg != neg.shift()).cumsum()).cumsum().max() if len(neg) else 0
    m = eq.resample("ME").last()
    mret = m.pct_change()
    mret.iloc[0] = m.iloc[0] / eq.iloc[0] - 1
    w = eq.resample("W").last().pct_change().dropna()
    return {"cagr": float(cagr), "total_return": float(eq.iloc[-1] / eq.iloc[0] - 1), "vol": float(sd * math.sqrt(DAYS_PER_YEAR)),
            "sharpe": float(r.mean() / sd * math.sqrt(DAYS_PER_YEAR)) if sd > 0 else float("nan"),
            "sortino": float(r.mean() / dn * math.sqrt(DAYS_PER_YEAR)) if dn > 0 else float("nan"),
            "max_dd": float(dd.min()), "max_dd_sessions": int(runs.max()) if len(runs) else 0,
            "calmar": float(cagr / abs(dd.min())) if dd.min() < 0 else float("nan"),
            "worst_day": float(r.min()), "worst_week": float(w.min()) if len(w) else float("nan"),
            "worst_month": float(mret.min()), "best_month": float(mret.max()),
            "pos_months": float((mret > 0).mean()), "losing_day_streak": int(streak),
            "final_equity": float(eq.iloc[-1]), "days": int(len(eq))}


def boot_perf(eq: pd.Series, B: int, block: int, seed: int) -> dict:
    r = eq.pct_change().dropna().to_numpy()
    n = len(r)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    cagr, mdd = np.empty(B), np.empty(B)
    yrs = n / DAYS_PER_YEAR
    for b_ in range(B):
        st = rng.integers(0, n, size=nb)
        idx = ((st[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
        path = np.cumprod(1 + r[idx])
        cagr[b_] = path[-1] ** (1 / yrs) - 1
        mdd[b_] = (path / np.maximum.accumulate(path) - 1).min()
    q = lambda v: {"p5": float(np.percentile(v, 5)), "p50": float(np.percentile(v, 50)), "p95": float(np.percentile(v, 95))}
    return {"cagr": q(cagr), "max_dd": q(mdd), "B": B, "block": block}


def trade_stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    r = t["ret"].to_numpy()
    wins, losses = r[r > 0], r[r <= 0]
    s = np.sort(t["pnl"].to_numpy())[::-1]
    k = max(int(len(s) * 0.05), 1)
    return {"trades": int(len(t)), "win_rate": float((r > 0).mean()), "avg_ret_bp": float(r.mean() * 1e4),
            "median_ret_bp": float(np.median(r) * 1e4), "avg_win_bp": float(wins.mean() * 1e4) if len(wins) else float("nan"),
            "avg_loss_bp": float(losses.mean() * 1e4) if len(losses) else float("nan"),
            "payoff": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) and losses.mean() != 0 else float("nan"),
            "p5_bp": float(np.percentile(r, 5) * 1e4), "p95_bp": float(np.percentile(r, 95) * 1e4),
            "top5pct_share_of_pnl": float(s[:k].sum() / s.sum()) if s.sum() > 0 else float("nan"),
            "locked_exits": int(t["locked_exit"].sum()), "stopped": int(t["stopped"].sum()),
            "capped": int(t["capped"].sum()), "data_end": int(t["data_end"].sum()),
            "avg_locked_exit_bp": float(t.loc[t["locked_exit"], "ret"].mean() * 1e4) if t["locked_exit"].any() else float("nan")}


# ----------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------
def load_lists(folder: Path, target: str, etf: set, depth: int) -> Tuple[Dict, Dict]:
    pr = pd.read_parquet(folder / "preds.parquet")
    pr = pr[pr["target"].astype(str) == target].copy()
    pr["timestamp"] = SB._naive(pr["timestamp"])
    pr["symbol"] = pr["symbol"].astype(str)
    pr = pr[~pr["symbol"].isin(etf)]
    pr = pr.sort_values(["timestamp", "score", "symbol"], ascending=[True, False, True])
    lists = {np.datetime64(t, "ns"): g["symbol"].head(depth).tolist() for t, g in pr.groupby("timestamp")}
    universe = {np.datetime64(t, "ns"): g["symbol"].to_numpy() for t, g in pr.groupby("timestamp")}
    return lists, universe


def random_lists(universe: Dict, depth: int, seed: int) -> Dict:
    rng = np.random.default_rng(seed)
    return {d: list(rng.permutation(u)[:depth]) for d, u in sorted(universe.items())}


def load_declaration() -> Tuple[dict, str]:
    p = HERE / DECL_FILE
    if not p.exists():
        raise PreconditionError(f"{DECL_FILE} not found next to {Path(__file__).name}")
    raw = p.read_bytes()
    decl = json.loads(raw.decode("utf-8"))
    if decl.get("stage") != "E" or decl.get("config") != json.loads(json.dumps(CFG)):
        raise PreconditionError(f"settings differ from {DECL_FILE}; the declaration is fixed")
    return decl, hashlib.sha256(raw).hexdigest()[:16]


def prepare(pp: Path, root: Path, man: dict, isin: Optional[List[str]], verbose=True) -> dict:
    t0 = time.perf_counter()
    extra = RC.load_symbol_list(root / RC.UNIVERSE_EXCLUDE_FILE)
    P = pd.read_parquet(pp, columns=["timestamp", "symbol", "X_turnover_med"])
    P["timestamp"] = SB._naive(P["timestamp"])
    P["symbol"] = P["symbol"].astype(str)
    syms = P["symbol"].unique()
    if isin:
        cls = SF.classify_symbols(syms, SF.load_isin_files(isin), extra)
        etf = set(cls.loc[cls["etf"], "symbol"])
        etf_note = "by ISIN"
    else:
        etf = {s for s in syms if RC.is_etf_symbol(s, extra)}
        etf_note = "by the name rule (no bhavcopy given)"
    folder = pp.parent / "stage_d"
    lists = {}
    lists["D"], universe = load_lists(folder / man["engines"]["D"]["run"], man["target"], etf, CFG["reserve_depth"])
    lists["ENS"], _ = load_lists(folder / man["engines"]["ENS"]["run"], man["target"], etf, CFG["reserve_depth"])
    for sd in range(CFG["random_seeds"]):
        lists[f"RANDOM_{sd}"] = random_lists(universe, CFG["reserve_depth"], 1000 + sd)
    need, pairs = set(), set()
    for d in lists.values():
        for day_, v in d.items():
            need.update(v)
            pairs.update((day_, s_) for s_ in v)
    for u in universe.values():
        need.update(u)
    bars = load_bars(root, sorted(need))
    key = pd.MultiIndex.from_arrays([P["timestamp"].to_numpy(dtype="datetime64[ns]"), P["symbol"].to_numpy()])
    tv = pd.Series(pd.to_numeric(P["X_turnover_med"], errors="coerce").to_numpy(float), index=key)
    tv = tv[~tv.index.duplicated()]
    pl = list(pairs)
    got = tv.reindex(pd.MultiIndex.from_tuples(pl)) if pl else pd.Series(dtype=float)
    adv = {k_: float(v_) for k_, v_ in zip(pl, got.to_numpy())}
    days = sorted(lists["D"])
    first, last = days[0], days[-1]
    allts = np.unique(np.concatenate([b.ts for b in bars.values()]))
    cal = allts[(allts >= first)]
    cal = cal[: np.searchsorted(cal, last) + 1 + max(CFG["grid_hold"]) + 25]
    if verbose:
        _log(f"{len(days):,} signal days {pd.Timestamp(first).date()}..{pd.Timestamp(last).date()} | {len(bars):,} symbols' "
             f"bars | calendar {len(cal):,} sessions | ETFs {etf_note} ({time.perf_counter() - t0:.0f}s)")
    return {"lists": lists, "universe": universe, "bars": bars, "adv": adv, "cal": cal, "etf_note": etf_note}


def universe_benchmark(data: dict) -> pd.Series:
    """Equal-weight buy-and-hold of the eligible universe, rebalanced daily, no costs."""
    cal, bars, uni = data["cal"], data["bars"], data["universe"]
    rets = []
    for i in range(1, len(cal)):
        u = uni.get(cal[i - 1])
        if u is None:
            rets.append(np.nan)
            continue
        acc = []
        for s in u:
            b = bars.get(s)
            if b is None:
                continue
            j, k = b.index.get(cal[i]), b.index.get(cal[i - 1])
            if j is not None and k is not None and b.c[k] > 0:
                acc.append(b.c[j] / b.c[k] - 1)
        rets.append(float(np.mean(acc)) if acc else np.nan)
    r = pd.Series(rets, index=pd.DatetimeIndex(cal[1:])).fillna(0.0)
    return pd.concat([pd.Series([1.0], index=pd.DatetimeIndex(cal[:1])), (1 + r).cumprod()])


def rank_table(data: dict, engine: str, hold: int, depth: int = 10) -> pd.DataFrame:
    """Per-trade corrected net (35 bp) by watchlist rank 1..depth - independent of capital."""
    rows = []
    cal = data["cal"]
    pos = {d: i for i, d in enumerate(cal)}
    for d, wl in data["lists"][engine].items():
        i = pos.get(d)
        if i is None or i + 1 >= len(cal):
            continue
        day = cal[i + 1]
        for r_, s in enumerate(wl[:depth], 1):
            b = data["bars"].get(s)
            e = b.index.get(day) if b is not None else None
            if e is None:
                continue
            x, kind, raw, flags = plan_exit(b, e, hold, None)
            rows.append({"rank": r_, "net": raw / b.o[e] - 1 - 0.0035})
    t = pd.DataFrame(rows)
    return t.groupby("rank")["net"].agg(["count", "mean", "median"]).assign(
        mean_bp=lambda x: x["mean"] * 1e4, median_bp=lambda x: x["median"] * 1e4)[["count", "mean_bp", "median_bp"]]


def regimes(eq: pd.Series, bench: pd.Series) -> pd.DataFrame:
    r = eq.pct_change()
    br = bench.pct_change()
    trend = (bench / bench.shift(50) - 1).shift(1)
    vol = br.rolling(20).std().shift(1)
    q = vol.quantile([1 / 3, 2 / 3]).to_numpy()
    lab = pd.DataFrame({"r": r, "trend": np.where(trend > 0, "universe up (50d)", "universe down (50d)"),
                        "vol": np.where(vol <= q[0], "low vol", np.where(vol <= q[1], "mid vol", "high vol"))}).dropna()
    out = []
    for col in ("trend", "vol"):
        for k, g in lab.groupby(col):
            out.append({"regime": k, "days": int(len(g)), "share": float(len(g) / len(lab)),
                        "mean_daily_bp": float(g["r"].mean() * 1e4),
                        "annualised": float((1 + g["r"].mean()) ** DAYS_PER_YEAR - 1)})
    return pd.DataFrame(out)


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------
def summarize(res: dict) -> dict:
    sc = res["scenario"]
    p = perf(res["equity"]["equity"])
    st_ = res["stats"]
    n_t = st_.get("n_trades", 0)
    eq = res["equity"]
    return {"scenario": sc.name, "engine": sc.engine, "n": sc.n, "hold": sc.hold, "capital": sc.capital,
            "costs": sc.costs, "stop": sc.stop, "duplicates": sc.duplicates, **p,
            "trades": int(n_t), "win_rate": st_["wins"] / n_t if n_t else float("nan"),
            "avg_trade_bp": st_["sum_ret"] / n_t * 1e4 if n_t else float("nan"),
            "exposure": float((eq["invested"] / eq["equity"]).mean()),
            "avg_positions": float(eq["positions"].mean()), "capped_orders": res["stats"]["capped"],
            "cash_limited": res["stats"]["cash_limited"], "replaced": res["stats"]["replaced"],
            "costs_total": float(sum(res["costs"].values()))}


def run_all(data: dict, verbose=True) -> dict:
    t0 = time.perf_counter()
    P0 = primary_scenario()
    sims = {}

    def go(sc, lists=None, keep=False):
        r = simulate(sc, lists if lists is not None else data["lists"][sc.engine], data["bars"], data["adv"],
                     data["cal"], keep_trades=keep)
        sims[sc.name] = r
        return r
    prim = go(P0, keep=True)
    go(replace(P0, name="ENS", engine="ENS"), keep=True)
    half = replace(P0, capital=P0.capital / 2)
    a = simulate(replace(half, name="split_D"), data["lists"]["D"], data["bars"], data["adv"], data["cal"], False)
    b = simulate(replace(half, name="split_ENS", engine="ENS"), data["lists"]["ENS"], data["bars"], data["adv"], data["cal"], False)
    eqs = a["equity"].add(b["equity"], fill_value=0)
    sims["SPLIT_50_50"] = {"scenario": replace(P0, name="SPLIT_50_50", engine="D+ENS"), "equity": eqs,
                           "trades": pd.concat([a["trades"], b["trades"]]), "costs": {k: a["costs"][k] + b["costs"][k] for k in a["costs"]},
                           "stats": {k: a["stats"][k] + b["stats"][k] for k in a["stats"]}}
    go(replace(P0, name="FLAT35_research", costs="flat35", integer_shares=False, liquidity_cap=None), keep=True)
    go(replace(P0, name="STRESS_costs", costs="zerodha_stress"))
    go(replace(P0, name="NO_DUPLICATES", duplicates=False))
    for s in CFG["stops"]:
        go(replace(P0, name=f"STOP_{int(s * 100)}pct", stop=s), keep=True)
    for cap in CFG["capital_sweep"]:
        go(replace(P0, name=f"CAPITAL_{int(cap)}", capital=float(cap)))
    for n in CFG["grid_n"]:
        for h in CFG["grid_hold"]:
            go(replace(P0, name=f"GRID_N{n}_H{h}", n=n, hold=h))
    for sd in range(CFG["random_seeds"]):
        go(replace(P0, name=f"RANDOM_{sd}", engine="RANDOM", random_seed=sd), lists=data["lists"][f"RANDOM_{sd}"])
    if verbose:
        _log(f"{len(sims)} simulations ({time.perf_counter() - t0:.0f}s)")
    return {"sims": sims, "primary": prim}


def write_outputs(data: dict, R: dict, out: Path, decl_sha: str, man: dict) -> dict:
    sims, prim = R["sims"], R["primary"]
    S = pd.DataFrame([summarize(r) for r in sims.values()])
    S.to_csv(out / "summary.csv", index=False)
    eq = prim["equity"]
    eq.to_csv(out / "equity_primary.csv")
    prim["trades"].to_csv(out / "trades_primary.csv", index=False)
    bench = universe_benchmark(data)
    pP = perf(eq["equity"])
    pB = perf(bench * CFG["capital"])
    tP = trade_stats(prim["trades"])
    boot = boot_perf(eq["equity"], CFG["boot_B"], CFG["boot_block"], CFG["seed"])
    mret = eq["equity"].resample("ME").last().pct_change()
    mret.iloc[0] = eq["equity"].resample("ME").last().iloc[0] / eq["equity"].iloc[0] - 1
    mtab = mret.to_frame("ret").assign(year=lambda x: x.index.year, month=lambda x: x.index.month) \
        .pivot(index="year", columns="month", values="ret")
    mtab.to_csv(out / "monthly_primary.csv")
    yearly = eq["equity"].resample("YE").last().pct_change()
    yearly.iloc[0] = eq["equity"].resample("YE").last().iloc[0] / eq["equity"].iloc[0] - 1
    rank = rank_table(data, "D", CFG["hold"])
    rank.to_csv(out / "rank.csv")
    reg = regimes(eq["equity"], bench)
    reg.to_csv(out / "regimes.csv", index=False)
    grid = S[S["scenario"].str.startswith("GRID_")]
    grid.to_csv(out / "grid.csv", index=False)
    cap = S[S["scenario"].str.startswith("CAPITAL_")].sort_values("capital")
    cap.to_csv(out / "capacity.csv", index=False)
    costs = pd.Series(prim["costs"])
    costs.to_csv(out / "costs_primary.csv", header=["rupees"])
    t = prim["trades"]
    gross_profit = float((t["sell_value"] - t["buy_value"]).sum())
    rnd = S[S["scenario"].str.startswith("RANDOM_")]
    recent = {}
    for lab, start in (("since 2024", "2024-01-01"), ("since 2025-03-05", "2025-03-05")):
        seg = eq["equity"][eq.index >= pd.Timestamp(start)]
        recent[lab] = perf(seg) if len(seg) > 20 else {}
    stats = {"primary": pP, "primary_trades": tP, "universe_buy_and_hold": pB, "bootstrap": boot,
             "recent": recent, "yearly": {int(k.year): float(v) for k, v in yearly.items()},
             "costs_rupees": prim["costs"], "gross_trading_profit": gross_profit,
             "random_benchmark": {"cagr_mean": float(rnd["cagr"].mean()), "cagr_min": float(rnd["cagr"].min()),
                                  "cagr_max": float(rnd["cagr"].max()), "sharpe_mean": float(rnd["sharpe"].mean())},
             "sim_stats": prim["stats"], "etf_rule": data["etf_note"], "declaration_sha": decl_sha,
             "frozen": {k: v["run"] for k, v in man["engines"].items()}}
    (out / "stats.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    write_report(out / "execution_report.md", S, stats, grid, cap, rank, reg, mtab, costs, gross_profit)
    return stats


def _p(x, d=1):
    return "nan" if x is None or not np.isfinite(x) else f"{x * 100:+.{d}f}%"


def _r(x):
    return "nan" if x is None or not np.isfinite(x) else f"Rs {x:,.0f}"


def write_report(path, S, st, grid, cap, rank, reg, mtab, costs, gross):
    P, T, B = st["primary"], st["primary_trades"], st["bootstrap"]
    row = lambda name: S[S["scenario"] == name].iloc[0]
    L = ["# Execution backtest - frozen engines", "",
         f"{CODE_VERSION} | declaration {st['declaration_sha']} | D = {st['frozen']['D']}, ENS = {st['frozen']['ENS']} | "
         f"ETFs {st['etf_rule']}", "",
         "**Read this first.** The PRIMARY setup was fixed before this ran. Every grid below is descriptive: "
         "choosing its best row would be fitting the backtest. This is the same history the engines were judged "
         "on - the forward paper ledger is the only new evidence.", "",
         "## 1. The primary setup: D, 3 a day, hold 5 sessions, 1/15 of equity each, Rs 10 lakh, Zerodha costs", "",
         f"| CAGR | total | vol | Sharpe | Sortino | max drawdown | longest underwater | Calmar | final equity |",
         "|---|---|---|---|---|---|---|---|---|",
         f"| {_p(P['cagr'])} | {_p(P['total_return'], 0)} | {_p(P['vol'])} | {P['sharpe']:.2f} | {P['sortino']:.2f} | "
         f"{_p(P['max_dd'])} | {P['max_dd_sessions']} sessions | {P['calmar']:.2f} | {_r(P['final_equity'])} |", "",
         f"Bootstrap range (block {B['block']}, {B['B']:,} resamples): CAGR {_p(B['cagr']['p5'])} to {_p(B['cagr']['p95'])} "
         f"(median {_p(B['cagr']['p50'])}); max drawdown {_p(B['max_dd']['p5'])} to {_p(B['max_dd']['p95'])}.", "",
         f"Benchmarks: buying the whole eligible universe equal-weight (no costs) {_p(st['universe_buy_and_hold']['cagr'])} "
         f"CAGR, max drawdown {_p(st['universe_buy_and_hold']['max_dd'])}; random picks with identical mechanics "
         f"{_p(st['random_benchmark']['cagr_mean'])} CAGR on average (range {_p(st['random_benchmark']['cagr_min'])} to "
         f"{_p(st['random_benchmark']['cagr_max'])}).", "",
         "## 2. Calendar", "", "By year: " + "; ".join(f"{y} {_p(v)}" for y, v in st["yearly"].items()), ""]
    for lab, p in st["recent"].items():
        if p:
            L.append(f"- {lab}: CAGR {_p(p['cagr'])}, max drawdown {_p(p['max_dd'])}, Sharpe {p['sharpe']:.2f}")
    L += ["", f"Worst day {_p(P['worst_day'])}, worst week {_p(P['worst_week'])}, worst month {_p(P['worst_month'])}, "
          f"best month {_p(P['best_month'])}; {P['pos_months']:.0%} of months positive; longest losing streak "
          f"{P['losing_day_streak']} sessions.", "", "Monthly returns (%):", "",
          "| year | " + " | ".join(str(m) for m in range(1, 13)) + " |", "|---|" + "---|" * 12]
    for y, r_ in mtab.iterrows():
        L.append(f"| {y} | " + " | ".join("" if not np.isfinite(r_.get(m, np.nan)) else f"{r_[m] * 100:+.1f}" for m in range(1, 13)) + " |")
    L += ["", "## 3. Trades", "",
          f"{T['trades']:,} trades; win rate {T['win_rate']:.1%}; average {T['avg_ret_bp']:+.1f} bp, median "
          f"{T['median_ret_bp']:+.1f} bp; average win {T['avg_win_bp']:+.1f} bp, average loss {T['avg_loss_bp']:+.1f} bp, "
          f"payoff {T['payoff']:.2f}; 5th / 95th percentile {T['p5_bp']:+.0f} / {T['p95_bp']:+.0f} bp. The best 5% of "
          f"trades carry {(format(T['top5pct_share_of_pnl'], '.0%') if np.isfinite(T['top5pct_share_of_pnl']) else 'n/a (no net profit)')} "
          f"of the profit. Locked exits {T['locked_exits']} (average "
          f"{T['avg_locked_exit_bp']:+.0f} bp); orders capped by liquidity {T['capped']}.", "",
          "Per-trade net by watchlist rank (35 bp, honest exits, no capital effects):", "",
          "| rank | trades | mean bp | median bp |", "|---|---|---|---|"]
    for r_, x in rank.iterrows():
        L.append(f"| {r_} | {int(x['count']):,} | {x['mean_bp']:+.1f} | {x['median_bp']:+.1f} |")
    L += ["", "## 4. Costs (primary, whole period)", "", "| item | rupees |", "|---|---|"]
    for k, v in costs.items():
        L.append(f"| {k} | {_r(v)} |")
    L += ["", f"Total costs {_r(costs.sum())} against gross trading profit {_r(gross)} "
          f"({costs.sum() / gross:.0%} of it)." if gross > 0 else "", "",
          "## 5. Capacity (primary setup at other capital; impact and the 2% liquidity cap bite as size grows)", "",
          "| capital | CAGR | Sharpe | max DD | avg trade bp | orders capped |", "|---|---|---|---|---|---|"]
    for _, x in cap.iterrows():
        L.append(f"| {_r(x['capital'])} | {_p(x['cagr'])} | {x['sharpe']:.2f} | {_p(x['max_dd'])} | "
                 f"{x['avg_trade_bp']:+.1f} | {int(x['capped_orders']):,} |")
    L += ["", "## 6. Construction grid - DESCRIPTIVE (positions per day x hold; the model was trained for 5 sessions)", "",
          "CAGR / Sharpe / max drawdown. Do not pick the best cell: with 30 cells one will look best by chance.", "",
          "| n \\ hold | " + " | ".join(str(h) for h in CFG["grid_hold"]) + " |", "|---|" + "---|" * len(CFG["grid_hold"])]
    for n in CFG["grid_n"]:
        cells = []
        for h in CFG["grid_hold"]:
            x = grid[(grid["n"] == n) & (grid["hold"] == h)].iloc[0]
            mark = "**" if (n, h) == (CFG["n_per_day"], CFG["hold"]) else ""
            cells.append(f"{mark}{_p(x['cagr'], 0)} / {x['sharpe']:.1f} / {_p(x['max_dd'], 0)}{mark}")
        L.append(f"| {n} | " + " | ".join(cells) + " |")
    L += ["", "## 7. Rules, engines and costs - DESCRIPTIVE", "",
          "| variant | CAGR | Sharpe | max DD | trades | avg trade bp |", "|---|---|---|---|---|---|"]
    for name, lab in (("PRIMARY", "PRIMARY (D, zerodha_base)"), ("ENS", "ensemble instead of D"),
                      ("SPLIT_50_50", "50/50 capital D + ensemble"), ("FLAT35_research", "flat 35 bp (research convention)"),
                      ("STRESS_costs", "stress costs (impact x2, tick both sides)"), ("NO_DUPLICATES", "no duplicate holdings"),
                      *[(f"STOP_{int(s * 100)}pct", f"stop-loss {int(s * 100)}%") for s in CFG["stops"]]):
        x = row(name)
        L.append(f"| {lab} | {_p(x['cagr'])} | {x['sharpe']:.2f} | {_p(x['max_dd'])} | {int(x['trades']):,} | {x['avg_trade_bp']:+.1f} |")
    L += ["", "## 8. Regimes (primary; mean daily return by the universe's state the day before)", "",
          "| regime | share of days | mean daily bp | annualised |", "|---|---|---|---|"]
    for _, x in reg.iterrows():
        L.append(f"| {x['regime']} | {x['share']:.0%} | {x['mean_daily_bp']:+.1f} | {_p(x['annualised'])} |")
    L += ["", "## 9. What this does not say", "",
          "- It is not new evidence: same history as the research. The forward paper ledger decides.",
          "- Survivorship, trade-for-trade (BE) and surveillance restrictions are not modelled; taxes are not modelled.",
          "- Fills at the opening auction and the closing price are assumed; the paper test measures real fills.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Execution backtest of the frozen engines")
    ap.add_argument("--panel", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--isin", nargs="+", default=None)
    ap.add_argument("--rerun-reason", default=None)
    a = ap.parse_args(argv)
    pp = Path(a.panel) / "panel.parquet"
    man_, man_sha = SB.load_manifest()
    SB.check_panel(pp, man_sha)
    man = FZ.verify_frozen(pp)
    decl, sha = load_declaration()
    prior = [e for e in RC.ledger_entries(pp) if e.get("kind") == "stage_e_run"]
    if prior and not a.rerun_reason:
        raise PreconditionError(f"the execution backtest already ran ({prior[-1].get('run_id')}); a re-run needs "
                                f"--rerun-reason (a verified bug)")
    out_root = pp.parent / "stage_e"
    day = dt.date.today().strftime("%Y%m%d")
    k = 1
    while (out_root / f"EXEC_{day}_{k:03d}").exists():
        k += 1
    run_id = f"EXEC_{day}_{k:03d}"
    out = out_root / run_id
    out.mkdir(parents=True)
    RC.ledger_append(pp, {"kind": "stage_e_declared", "tool": CODE_VERSION, "run_id": run_id, "declaration_sha": sha,
                          "frozen": {k_: v["run"] for k_, v in man["engines"].items()}, "rerun_reason": a.rerun_reason})
    print("=" * 76)
    print(f"{CODE_VERSION} | declaration {sha} | frozen D {man['engines']['D']['run']}, ENS {man['engines']['ENS']['run']}")
    print("PRIMARY (fixed in advance): D, 3 a day, hold 5, 1/15 of equity each, Rs 10 lakh, Zerodha costs + impact.")
    print("Everything else is descriptive. Same history as the research - not new evidence.")
    print("=" * 76)
    data = prepare(pp, Path(a.root), man, a.isin)
    R = run_all(data)
    st = write_outputs(data, R, out, sha, man)
    P = st["primary"]
    n = RC.ledger_append(pp, {"kind": "stage_e_run", "tool": CODE_VERSION, "run_id": run_id, "declaration_sha": sha,
                              "primary_cagr": P["cagr"], "primary_max_dd": P["max_dd"], "primary_sharpe": P["sharpe"],
                              "trades": st["primary_trades"]["trades"], "rerun_reason": a.rerun_reason})
    print()
    print(f"PRIMARY: CAGR {_p(P['cagr'])} | Sharpe {P['sharpe']:.2f} | max drawdown {_p(P['max_dd'])} | "
          f"{st['primary_trades']['trades']:,} trades, win rate {st['primary_trades']['win_rate']:.1%}, "
          f"average {st['primary_trades']['avg_ret_bp']:+.1f} bp")
    print(f"  bootstrap CAGR {_p(st['bootstrap']['cagr']['p5'])} .. {_p(st['bootstrap']['cagr']['p95'])} | "
          f"random picks {_p(st['random_benchmark']['cagr_mean'])} | universe {_p(st['universe_buy_and_hold']['cagr'])}")
    print(f"  outputs: {out}\n  research ledger: entry {n} (stage_e_run {run_id})")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
