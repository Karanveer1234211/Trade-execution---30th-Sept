#!/usr/bin/env python3
"""
tests/test_soul_meta.py - soul_meta v1. Prints VERIFIED on success.

Run from the open_close folder with the main folder on PYTHONPATH:
    python tests\\test_soul_meta.py

 0. The declaration must match.
 1. The bracket exit equals stage_e_paths.bracket (conservative) on every candidate.
 2. The vectorised Soul personality equals soul_v4's card, candidate by candidate.
 3. POINT IN TIME: scrambling every bar after a date changes no input of that date's candidates.
 4. The rule: veto above 0.30; a plan replaces H5 only with >= 30 bp and its own trap <= 0.30.
 5. The embargo: every fold trains only on candidates that fully exited before its test year.
 6. PLANTED: eight trap-prone stocks that D likes -> the re-ranker learns it, vetoes, and PASSES.
 7. ABSENT: the same traps on random stocks -> nothing to learn -> KEEP D.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import stage_e_execution as EX        # noqa: E402
import stage_e_paths as SP            # noqa: E402
import soul_v4 as SV                  # noqa: E402
import soul_meta as SM                # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL") + f"  {msg}")
    if not cond:
        FAILS.append(msg)


def make_world(planted: bool, seed: int = 3, n_sym: int = 40, n_days: int = 820):
    rng = np.random.default_rng(seed)
    ts = pd.bdate_range("2019-01-01", periods=n_days).values.astype("datetime64[ns]")
    syms = [f"S{i:02d}" for i in range(n_sym)]
    traps = set(syms[:8])
    bars, pr = {}, []
    for s in syms:
        c = np.empty(n_days)
        c[0] = rng.uniform(80, 400)
        o, h, l = np.empty(n_days), np.empty(n_days), np.empty(n_days)
        o[0] = h[0] = l[0] = c[0]
        streak = 0
        for t in range(1, n_days):
            p = c[t - 1]
            lockp = round(p * 0.95 / 0.05) * 0.05
            if planted and s in traps and streak == 0 and rng.random() < 0.02:
                streak = 8                       # PLANTED: trap-prone stocks fall into 8-session lower-circuit streaks
            single = (not planted) and rng.random() < 0.01   # ABSENT: one-day locks on any stock, no pattern
            if streak > 0:
                if streak == 8:
                    o[t] = p * (1 + rng.normal(0, 0.004))
                    h[t], l[t], c[t] = max(o[t], p), lockp, lockp
                else:
                    o[t] = h[t] = l[t] = c[t] = lockp          # frozen at the lower band
                streak -= 1
            elif single:
                o[t] = p * (1 + rng.normal(0, 0.004))
                h[t], l[t], c[t] = max(o[t], p), lockp, lockp
            else:
                drift = 0.0030 if (planted and s in traps) else 0.0004   # trap stocks recover between streaks
                o[t] = p * (1 + rng.normal(drift, 0.006))
                c[t] = o[t] * (1 + rng.normal(0.0002, 0.015))
                h[t], l[t] = max(o[t], c[t]) * (1 + abs(rng.normal(0, 0.004))), min(o[t], c[t]) * (1 - abs(rng.normal(0, 0.004)))
        bars[s] = EX.Bars(ts, o, h, l, c, np.full(n_days, 0.015), {t: i for i, t in enumerate(ts)})
    for t in range(260, n_days - 12):
        fav = traps if planted else set(rng.choice(syms, 8, replace=False))
        for s in syms:
            sc = rng.normal() + (1.2 if s in fav else 0.0)
            pr.append((ts[t], s, sc))
    D = pd.DataFrame(pr, columns=["timestamp", "symbol", "score"])
    E = D.assign(score=D["score"] + rng.normal(0, 0.5, len(D)))
    dims = [f"st_{k}" for k in range(8)] + ["ex_a", "ex_b"]
    S = pd.DataFrame([(t, s) for t in ts for s in syms], columns=["timestamp", "symbol"])
    for d in dims:
        S[d] = rng.uniform(-0.5, 0.5, len(S)).astype("float32")
    profile = json.loads((HERE.parent / "PAPER_DECLARATION.json").read_text())["config"]["soul"]["c_profile"]
    Cf = S[["timestamp", "symbol"]].copy()
    for x in profile:
        Cf[x["feature"]] = rng.normal(size=len(Cf))
    Cf["M_breadth"] = Cf["timestamp"].map({t: rng.uniform() for t in ts})
    cal = ts
    lists = {}
    for t, g in D.groupby("timestamp"):
        lists[np.datetime64(t, "ns")] = list(g.sort_values(["score", "symbol"], ascending=[False, True])["symbol"].head(20))
    picks = pd.DataFrame([(d, s, "D") for d, v in lists.items() for s in v[:3]], columns=["signal_day", "symbol", "engine"])
    picks = pd.concat([picks, picks.assign(engine="ENS")])
    picks["signal_day"] = pd.to_datetime(picks["signal_day"])
    adv = {(d, s): 5e9 for d, v in lists.items() for s in v}
    return {"bars": bars, "D": D, "E": E, "S": S, "Cf": Cf, "cal": cal, "lists": lists, "picks": picks, "adv": adv,
            "profile": profile, "traps": traps}


def pipeline(W, cfg):
    C = SM.candidates(W["D"], W["E"], set(), cfg["candidates_top"])
    O = SV.build_outcomes(W["bars"], 5, cfg["soul"]["target_pct"])
    Dset = SM.build_dataset(C, W["bars"], W["cal"], O, W["S"], W["Cf"], W["picks"], W["profile"], cfg, verbose=False)
    R = SM.walk_forward(Dset, W["cal"], cfg, verbose=False)
    ev = SM.evaluate(R, {"bars": W["bars"], "adv": W["adv"], "cal": W["cal"]}, W["lists"], cfg)
    return C, O, Dset, R, ev


def main():
    print("0. the declaration")
    decl, sha, profile = SM.load_declaration()
    check(decl["stage"] == "soul-meta" and len(profile) == 12, f"settings match the re-ranker declaration (sha {sha})")
    check(SM.CFG["min_order_frac"] == 0.25, "the approved 25% minimum order rule is in force")
    cfg = json.loads(json.dumps(SM.CFG))
    cfg.update(test_years=[2021, 2022], boot_B=2000)
    cfg["soul"]["cross_pool"] = 20000

    print("1-5. mechanics on the planted world")
    W = make_world(True)
    C, O, Dset, R, ev = pipeline(W, cfg)
    worst, n = 0.0, 0
    for _, r in Dset[Dset["buyable"]].head(400).iterrows():
        b = W["bars"][r["symbol"]]
        e = b.index[np.datetime64(pd.Timestamp(r["entry_day"]), "ns")]
        tp, sl = 3 * r["atr"], 2 * r["atr"]
        if not np.isfinite(tp):
            continue
        ret, how, amb = SP.bracket(b, e, 5, tp, sl, "conservative")
        x, kind, raw, fl = SM.bracket_exit(b, e, 5, tp, sl)
        worst, n = max(worst, abs((raw / b.o[e] - 1) - ret)), n + 1
    check(n > 300 and worst < 1e-12, f"bracket exit = stage_e_paths.bracket on {n} candidates (max diff {worst:.1e})")
    soul = SV.Soul(O, W["S"], dict(SM.CFG["soul"]), None, W["bars"], 0.0035, 5)
    worst = 0.0
    for _, r in Dset.sample(40, random_state=1).iterrows():
        p = soul.personality(r["symbol"], r["signal_day"])["shrunk"]
        worst = max(worst, abs(p["net_bp"] / 1e4 - r["pers_net"]), abs(p["win_rate"] - r["pers_win"]),
                    abs(p["mfe"] - r["pers_mfe"]), abs(p["locked_rate"] - r["pers_locked"]))
    check(worst < 1e-9, f"vectorised personality = soul_v4's card on 40 candidates (max diff {worst:.1e})")
    A = pd.Timestamp(W["cal"][600])
    scr = {}
    rng = np.random.default_rng(9)
    for s, b in W["bars"].items():
        fut = b.ts > np.datetime64(A, "ns")
        k = rng.uniform(0.6, 1.4, int(fut.sum()))
        o, h, l, c = b.o.copy(), b.h.copy(), b.l.copy(), b.c.copy()
        o[fut], c[fut] = o[fut] * k, c[fut] * k[::-1]
        h[fut] = np.maximum.reduce([o[fut], c[fut], h[fut] * k])
        l[fut] = np.minimum.reduce([o[fut], c[fut], l[fut] * k])
        scr[s] = EX.Bars(b.ts, o, h, l, c, b.vol20, b.index)
    O2 = SV.build_outcomes(scr, 5, cfg["soul"]["target_pct"])
    D2 = SM.build_dataset(C, scr, W["cal"], O2, W["S"], W["Cf"], W["picks"], W["profile"], cfg, verbose=False)
    feats = SM.feature_columns(Dset)
    m1, m2 = Dset["signal_day"] == A, D2["signal_day"] == A
    a1, a2 = Dset.loc[m1, feats].to_numpy(float), D2.loc[m2, feats].to_numpy(float)
    same = np.allclose(a1, a2, equal_nan=True, rtol=0, atol=1e-12)
    check(m1.sum() == 10 and same, f"POINT IN TIME: scrambling every bar after {A.date()} changes none of its {len(feats)} inputs")
    day = pd.DataFrame({"symbol": ["A", "B", "C"], "score": [3.0, 2.0, 1.0],
                        "ptrap_H5": [0.31, 0.10, 0.10], "val_H5": [0.0, 0.0, 0.0],
                        "ptrap_H3": [0.0, 0.0, 0.0], "val_H3": [0.0, 0.0029, 0.0031],
                        "ptrap_H7": [0.0, 0.0, 0.0], "val_H7": [0.0, 0.0, 0.0],
                        "ptrap_H10": [0.0, 0.0, 0.40], "val_H10": [0.0, 0.0, 0.0100],
                        "ptrap_BRACKET": [0.0, 0.0, 0.0], "val_BRACKET": [0.0, 0.0, 0.0]})
    out = SM.apply_rule(day, 0.30, 0.0030).set_index("symbol")
    check(bool(out.at["A", "veto"]) and not out.at["B", "veto"], "veto: P(trap | H5) 0.31 > 0.30 vetoed, 0.10 kept")
    check(out.at["B", "plan"] == "H5" and out.at["C", "plan"] == "H3",
          "hurdle: +29 bp stays H5; +31 bp switches; a better plan with trap 0.40 is not allowed")
    emb = all(pd.Timestamp(g["train_max_exit"].iloc[0]) < pd.Timestamp(f"{y}-01-01") for y, g in R.groupby("fold"))
    check(len(R) and R["fold"].nunique() == 2 and emb, "embargo: every fold trained only on candidates that exited before its year")

    print("6. PLANTED: trap-prone stocks that D likes")
    P = ev["paired"]
    md, mm = SM.metrics(ev["sims"]["D"]), SM.metrics(ev["sims"]["META"])
    check(ev["decision"] == "PASS" and P["lo_bp"] > 0, f"re-ranker PASSES: {P['mean_bp']:+.1f} bp [{P['lo_bp']:+.1f}, {P['hi_bp']:+.1f}]")
    check(mm["trap rate"] < md["trap rate"], f"trap rate falls: D {md['trap rate']:.1%} -> re-ranker {mm['trap rate']:.1%}")
    vt = R[R["veto"]]
    check(len(vt) and vt["symbol"].isin(W["traps"]).mean() > 0.8, f"vetoes land on the trap-prone stocks ({len(vt)} vetoes)")

    for k in ("D", "META"):
        t = ev["sims"][k]["trades"]
        check(len(t) and float(t["ret"].min()) > -1.0, f"{k}: no trade loses more than 100% (no slivers swamped by fixed costs)")
    print("7. ABSENT: the same traps on random stocks")
    W0 = make_world(False, seed=5)
    *_, ev0 = pipeline(W0, cfg)
    P0 = ev0["paired"]
    check(ev0["decision"] == "KEEP D", f"nothing to learn -> KEEP D: {P0['mean_bp']:+.1f} bp [{P0['lo_bp']:+.1f}, {P0['hi_bp']:+.1f}]")

    print()
    if FAILS:
        print(f"FAILED  {len(FAILS)} check(s):")
        for f in FAILS:
            print("   - " + f)
        return 1
    print(f"VERIFIED  {SM.CODE_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
