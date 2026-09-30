#!/usr/bin/env python3
"""
tests/test_paper.py - production_models v1 + soul_v4 v1 + paper_pipeline v1. Prints VERIFIED on success.

Run from the open_close folder with the main folder on PYTHONPATH:
    python tests\\test_paper.py

A planted world is frozen, production models are trained, panel_live is created and cut back to an
earlier session T0 ("today"); a watchlist is recorded; then time "passes" (the full panel returns) and
the paper trades are settled.
 1. Production models: fitted on the frozen feature lists in order; a changed byte is refused.
 2. Watchlist: D scores = the production model on panel_live's T0 rows exactly; ensemble =
    0.5 rank(D) + 0.5 rank(C); 10 per engine in score order; ETFs removed; dry runs record nothing.
 3. Ledger: hash chain verified; the same day twice is refused; an edited line is detected.
 4. Soul: a card for every recorded stock; POINT IN TIME - scrambling every bar after T0 changes
    nothing on T0's cards; personality shrinkage and memory weights by hand.
 5. Settlement: the first 3 buyable per engine, entry at the next open, honest exits, 35 bp - all
    re-derived independently; late recordings flagged; the report prints the month-6 rule.
 6. Tracker and log: before the next open every pick waits; two sessions later each traded pick is
    open with its exact unrealised return and MFE / MAE so far; after the exit it is closed with the
    settled net; shadows are followed; the log shows buys, sales and holdings; a holiday re-run of
    the watchlist is a clean no-op.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import research_common as RC          # noqa: E402
import stage_b_screen as SB           # noqa: E402
import stage_c_features as SC         # noqa: E402
import stage_e_execution as EX        # noqa: E402
import freeze_engines as FZ           # noqa: E402
import production_models as PM        # noqa: E402
import soul_v4 as SV                  # noqa: E402
import paper_pipeline as PP           # noqa: E402
import test_stage_d as TD             # noqa: E402

FAILS = []
STATE_COLS = [c for _, c, _ in RC.STOCK_STATE_SPEC] + ["D_drawdown_252", "D_atr_pct_z252"]


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL") + f"  {msg}")
    if not cond:
        FAILS.append(msg)


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


def world(tmp: Path):
    pdir = TD.make_world(tmp)
    pp = pdir / "panel.parquet"
    P = pd.read_parquet(pp)
    rng = np.random.default_rng(11)
    P["label_gap1"] = 0.01 * rng.normal(size=len(P))
    for c in list(SC.PANEL_INPUTS) + STATE_COLS:
        if c not in P:
            P[c] = rng.normal(size=len(P)).astype("float32")
    P.to_parquet(pp, index=False)
    for s, g in P.groupby("symbol"):
        g[["timestamp", "open", "high", "low", "close", "volume"]].to_parquet(tmp / f"{s}_daily.parquet", index=False)
    m = json.loads((pdir / "panel_meta.json").read_text())
    m["built_at"] = (pd.Timestamp.now() - pd.Timedelta(hours=2)).isoformat()
    (pdir / "panel_meta.json").write_text(json.dumps(m))
    _, man_sha = SB.load_manifest()
    RC.ledger_append(pp, {"kind": "panel_audit", "passed": True, "manifest_sha": man_sha})
    _, csha, names = SC.load_declaration()
    X = SC.build(pp, tmp, names, verbose=False)
    (pdir / "stage_c").mkdir()
    X.to_parquet(pdir / "stage_c" / "features_c.parquet", index=False)
    (pdir / "stage_c" / "features_c_meta.json").write_text(json.dumps({"declaration_sha": csha}))
    feats = SB.clean_feature_list(pp)[0]
    E = P[P["label_buyable_o1"] == 1][["timestamp", "symbol", "label_oc_5"]].dropna()
    for rid, fl in (("STAGED_W", feats), ("STAGEDXC_W", names), ("STAGEDXE_W", None)):
        f = pdir / "stage_d" / rid
        f.mkdir(parents=True)
        pr = pd.DataFrame({"timestamp": E["timestamp"], "symbol": E["symbol"], "target": "label_oc_5",
                           "score": rng.normal(size=len(E)), "outcome": E["label_oc_5"]})
        pr.to_parquet(f / "preds.parquet", index=False)
        pr.groupby("timestamp")["outcome"].mean().rename("market").reset_index().assign(target="label_oc_5",
                                                                                       top3_net=0.0).to_csv(f / "daily.csv", index=False)
        (f / "stage_d.json").write_text(json.dumps({"features": fl} if fl else {}))
    RC.ledger_append(pp, {"kind": "stage_d_gate", "experiment_family": "oc_v1", "run_id": "STAGED_W"})
    RC.ledger_append(pp, {"kind": "stage_dx_ens_run", "experiment_family": "oc_v1", "run_id": "STAGEDXE_W",
                          "c_run": "STAGEDXC_W", "d_run": "STAGED_W"})
    quiet(FZ.main, ["--panel", str(pdir)])
    return pdir, pp, feats, names


def main():
    print("0. module origin and the declaration")
    here = HERE.parent.resolve()
    for mo in (PP, PM, SV, FZ, EX, SC, RC):
        check(Path(mo.__file__).resolve().parent == here, f"{mo.__name__} from {Path(mo.__file__).resolve().parent}")
    decl, dsha = PP.load_declaration()
    check(decl["stage"] == "paper" and len(dsha) == 16, f"settings match the paper declaration (sha {dsha})")
    tmp = Path(tempfile.mkdtemp())
    try:
        pdir, pp, feats, names = world(tmp)
        root = tmp
        print("1. production models")
        quiet(PM.main, ["train", "--panel", str(pdir)])
        meta, models = PM.load_verified(pp)
        check(meta["models"]["D"]["features"] == feats and meta["models"]["C"]["features"] == names,
              f"fitted on the frozen feature lists, in order ({len(feats)} D, {len(names)} C)")
        fp = PM.prod_dir(pp) / "D.pkl"
        raw = fp.read_bytes()
        fp.write_bytes(raw + b"\0")
        try:
            PM.load_verified(pp)
            check(False, "a changed production model is refused")
        except SystemExit as e:
            check("changed" in str(e), "a changed production model is refused")
        fp.write_bytes(raw)
        mp = PM.prod_dir(pp) / "PRODUCTION.json"
        mraw = mp.read_text(encoding="utf-8")
        mj = json.loads(mraw)
        check(set(mj["libraries"]) >= {"python", "numpy", "pandas", "sklearn"}, "library versions recorded with the models")
        mj["libraries"]["sklearn"] = "0.0.1"
        mp.write_text(json.dumps(mj), encoding="utf-8")
        try:
            PM.load_verified(pp)
            check(False, "a different scikit-learn version is refused")
        except SystemExit as e:
            check("never upgrade" in str(e), "a different scikit-learn version is refused (pickles are version-bound)")
        mp.write_text(mraw, encoding="utf-8")
        try:
            quiet(PM.main, ["train", "--panel", str(pdir)])
            check(False, "retraining without --rerun-reason is refused")
        except SystemExit as e:
            check("already exist" in str(e), "retraining without --rerun-reason is refused")

        print("2. the watchlist")
        quiet(PP.main, ["init-live", "--root", str(root)])
        live = PP.paths(root)["live"]
        full = pd.read_parquet(live)
        days = np.sort(full["timestamp"].unique())
        T0 = pd.Timestamp(days[-30])
        full[full["timestamp"] <= T0].to_parquet(live, index=False)
        rec_time = T0 + pd.Timedelta(hours=20)
        dry, _ = quiet(PP.cmd_watchlist, root, None, True, rec_time)
        check(not (PP.paths(root)["paper"] / "ledger.jsonl").exists(), "a dry run records nothing")
        try:
            quiet(PP.cmd_watchlist, root, None, False, T0 + pd.Timedelta(days=4))
            check(False, "a watchlist whose entry session already opened is refused")
        except SystemExit as e:
            check("would be LATE" in str(e) and not (PP.paths(root)["paper"] / "ledger.jsonl").exists(),
                  "a watchlist whose entry session already opened is refused, nothing recorded (stale data can't slip in)")
        check(PP.entry_deadline(pd.Timestamp("2026-10-02")) == pd.Timestamp("2026-10-05 09:00"),
              "entry deadline skips the weekend (Friday's list is on time until Monday 09:00)")
        r, _ = quiet(PP.cmd_watchlist, root, None, False, rec_time)
        S, table = r["scores"], r["table"]
        rows = PP.read_day(live, ["symbol"] + feats, T0)
        pred = models["D"].predict(rows[feats].to_numpy(np.float32))
        ref = pd.Series(pred, index=rows["symbol"].astype(str))
        check(np.allclose(S.set_index("symbol")["score_D"].reindex(ref.index).to_numpy(), ref.to_numpy(), atol=0, rtol=0),
              f"D scores = the production model on panel_live's {T0.date()} rows, exactly ({len(ref)} stocks)")
        ens = 0.5 * S["score_D"].rank(pct=True) + 0.5 * S["score_C"].rank(pct=True)
        check(np.allclose(ens, S["score_ENS"]), "ensemble = 0.5 rank(D) + 0.5 rank(C)")
        for eng in ("D", "ENS"):
            g = table[table["engine"] == eng]
            check(len(g) == 10 and list(g["rank"]) == list(range(1, 11)) and bool((np.diff(g["score"].to_numpy()) <= 0).all()),
                  f"{eng}: 10 recorded, ranks 1-10 in score order")
        x = PP.rank_engine(S, "D", {S.sort_values("score_D", ascending=False)["symbol"].iloc[0]}, 10)
        check(S.sort_values("score_D", ascending=False)["symbol"].iloc[0] not in set(x["symbol"]), "ETFs are removed before ranking")

        print("3. the ledger")
        L = PP.read_ledger(PP.paths(root)["paper"])
        check(len(L) == 20 and (L["date"] == str(T0.date())).all(), "20 lines recorded for the day, chain intact")
        try:
            quiet(PP.cmd_watchlist, root, None, False, rec_time)
            check(False, "the same day twice is refused")
        except SystemExit as e:
            check("already recorded" in str(e), "the same day twice is refused")
        n0 = len(PP.read_ledger(PP.paths(root)["paper"]))
        rc, out_ = quiet(PP.main, ["watchlist", "--root", str(root), "--skip-if-recorded"])
        check(rc == 0 and "nothing to record" in out_ and len(PP.read_ledger(PP.paths(root)["paper"])) == n0,
              "a re-run on a day already recorded (holiday) exits cleanly and records nothing")
        lf = PP.paths(root)["paper"] / "ledger.jsonl"
        good = lf.read_text(encoding="utf-8")
        lf.write_text(good.replace(f'"rank": 2', f'"rank": 9', 1), encoding="utf-8")
        try:
            PP.read_ledger(PP.paths(root)["paper"])
            check(False, "an edited ledger line is detected")
        except SystemExit as e:
            check("edited" in str(e), "an edited ledger line is detected")
        lf.write_text(good, encoding="utf-8")

        print("4. Soul")
        cards = r["cards"]
        check(set(cards) == set(table["symbol"]), f"a Soul card for every recorded stock ({len(cards)})")
        cd = next(iter(cards.values()))
        check(cd["personality"]["raw"].get("n", 0) > 50 and cd["memory"]["self"].get("n", 0) == 20
              and cd["memory"]["cross"].get("n", 0) == 50, "cards carry personality, 20 own and 50 cross analogues")
        md = (PP.paths(root)["paper"] / "watchlists" / f"WL_{T0:%Y%m%d}.md").read_text(encoding="utf-8")
        check("Soul cards" in md and "Personality" in md and "Memory" in md and "pre-open" in md, "watchlist file with Soul cards")
        soul = r["soul"]
        R = soul.resolved(T0)
        check(bool((R["exit_day"] <= T0).all()), "Soul uses only trades finished on or before the card's date")
        bars = soul.bars
        rng = np.random.default_rng(5)
        scr = {}
        for s_, b in bars.items():
            fut = b.ts > np.datetime64(T0, "ns")
            k = rng.uniform(0.5, 1.5, size=int(fut.sum()))
            o, h, l, c = b.o.copy(), b.h.copy(), b.l.copy(), b.c.copy()
            o[fut], c[fut] = o[fut] * k, c[fut] * k[::-1]
            h[fut] = np.maximum.reduce([o[fut], c[fut], h[fut] * k])
            l[fut] = np.minimum.reduce([o[fut], c[fut], l[fut] * k])
            scr[s_] = EX.Bars(b.ts, o, h, l, c, b.vol20, b.index)
        soul2 = SV.Soul(SV.build_outcomes(scr), soul.S, PP.CFG["soul"], soul.picks, scr, 0.0035, 5, soul.c_today)
        diffs = 0
        for s_ in cards:
            a_, b_ = SV.card_row(cards[s_]), SV.card_row(soul2.card(s_, T0))
            for k_ in a_:
                va, vb = a_[k_], b_.get(k_)
                if isinstance(va, float) and isinstance(vb, float):
                    if not (np.isnan(va) and np.isnan(vb)) and abs(va - vb) > 1e-12:
                        diffs += 1
                elif va != vb:
                    diffs += 1
        check(diffs == 0, "POINT IN TIME: scrambling every bar after T0 changes nothing on T0's cards (C profile and tug included)")
        ct = soul.c_today.copy()
        ct["symbol"] = ct["symbol"].astype(str)
        ct = ct.set_index("symbol")
        spec = PP.CFG["soul"]["c_profile"]
        worst, favbad = 0.0, 0
        for s_, cd_ in cards.items():
            rows_ = cd_["c_profile"]["rows"]
            if len(rows_) != 12:
                favbad += 1
                continue
            fav = 0
            for r_, sp in zip(rows_, spec):
                pct = ct[sp["feature"]].rank(pct=True)[s_]
                worst = max(worst, abs(r_["pct"] - pct))
                fav += int((pct < 0.5) if sp["favoured"] == "low" else (pct > 0.5))
            favbad += int(fav != cd_["c_profile"]["favoured"])
        check(favbad == 0 and worst < 1e-12, f"C profile: 12 features per card, every percentile re-derived from T0's C features, "
                                             f"favoured counts exact")
        s0 = sorted(cards)[0]
        b0 = bars[s0]
        j0 = int(np.searchsorted(b0.ts, np.datetime64(T0, "ns"), side="right")) - 1
        lo0 = max(1, j0 - 249)
        on0 = b0.o[lo0:j0 + 1] / b0.c[lo0 - 1:j0] - 1
        ss0 = b0.c[lo0:j0 + 1] / b0.o[lo0:j0 + 1] - 1
        tg = cards[s0]["tug"]
        check(abs(tg["overnight_persistence"] - np.corrcoef(on0[:-1], on0[1:])[0, 1]) < 1e-12
              and abs(tg["mean_overnight_bp"] - on0.mean() * 1e4) < 1e-9 and tg["sessions"] == len(on0),
              f"tug of war re-derived by hand from {s0}'s last {len(on0)} sessions")
        check("C profile" in md and "Tug of war" in md and "research on oc_5" in md, "watchlist cards show the C profile and the tug of war")
        st = {"net_bp": 100.0, "win_rate": 0.6, "mfe": 0.05, "mae": -0.03, "locked_rate": 0.0, "hit5_rate": 0.5,
              "overnight_bp": 50.0, "in_session_bp": 50.0}
        mk = {"net_bp": 0.0, "win_rate": 0.5, "mfe": 0.04, "mae": -0.04, "locked_rate": 0.02, "hit5_rate": 0.4,
              "overnight_bp": 30.0, "in_session_bp": -30.0}
        sh = SV._shrink(st, mk, 30.0, 30.0)
        check(abs(sh["net_bp"] - 50.0) < 1e-12 and abs(sh["weight_on_stock"] - 0.5) < 1e-12,
              "shrinkage by hand: 30 effective trades vs K = 30 -> halfway to the market")

        print("6a. tracker before and during the trades")
        full.to_parquet(live, index=False)
        r0, _ = quiet(PP.cmd_settle, root, True, T0)
        TR0 = r0["tracker"]
        check(len(TR0) == 20 and (TR0["status"] == "waiting for the next open").all(),
              "at T0's close all 20 recorded picks wait for the next open")
        T2 = pd.Timestamp(days[-28])
        r2, _ = quiet(PP.cmd_settle, root, True, T2)
        TR2 = r2["tracker"]
        tr2 = TR2[TR2["role"] == "traded"]
        worst = 0.0
        for _, t in tr2.iterrows():
            b = bars[t["symbol"]]
            e = b.index[np.datetime64(pd.Timestamp(t["entry_day"]), "ns")]
            j = b.index[np.datetime64(T2, "ns")]
            worst = max(worst, abs(t["ret"] - (b.c[j] / b.o[e] - 1)), abs(t["mfe"] - (b.h[e:j + 1].max() / b.o[e] - 1)),
                        abs(t["mae"] - (b.l[e:j + 1].min() / b.o[e] - 1)))
        check(len(tr2) == 6 and tr2["status"].str.startswith("open - session 2 of 5").all() and worst < 1e-15,
              f"two sessions in: 6 traded picks open at session 2 of 5, marks and MFE/MAE exact (max diff {worst:.1e})")
        check(int((TR2["role"] == "shadow").sum()) >= 10, "ranks beyond the traded three are followed as shadows")

        print("5. settlement and the report")
        res, _ = quiet(PP.cmd_settle, root)
        tr, dd = res["trades"], res["days"]
        check(len(tr) == 6 and set(tr["engine"]) == {"D", "ENS"}, "3 trades per engine settled")
        worst = 0.0
        for _, t in tr.iterrows():
            b = bars[t["symbol"]]
            e = b.index[np.datetime64(pd.Timestamp(t["entry_day"]), "ns")]
            x, kind, raw, fl = EX.plan_exit(b, e, 5, None)
            worst = max(worst, abs(t["net"] - (raw / b.o[e] - 1 - 0.0035)))
            check(b.ts[e - 1] == np.datetime64(T0, "ns"), f"{t['engine']} {t['symbol']}: entry at the open after T0") if _ == 0 else None
        check(worst < 1e-15, f"every trade re-derived: honest exit - 35 bp (max diff {worst:.1e})")
        sd = dd[dd["status"] == "settled"]
        check(len(sd) == 2 and not sd["late"].any() and sd["excess"].notna().all(), "on-time days settled with their excess")
        uni = json.loads((PP.paths(root)["paper"] / "universe.jsonl").read_text(encoding="utf-8").splitlines()[0])["symbols"]
        Ob = SV.build_outcomes(bars)
        u = Ob[(Ob["signal_day"] == T0) & Ob["symbol"].isin(uni)]
        u = u[np.isfinite(u["r5"]) & u["buyable"]]
        ref_b = u["r5"].mean() - 0.0035
        check(abs(sd["bench_net"].iloc[0] - ref_b) < 1e-15, "benchmark re-derived: buyable non-ETF universe, honest exits, 35 bp")
        uf = PP.paths(root)["paper"] / "universe.jsonl"
        uraw = uf.read_text(encoding="utf-8")
        uj = json.loads(uraw.splitlines()[0])
        uj["symbols"] = uj["symbols"][1:]
        uf.write_text(json.dumps(uj) + "\n", encoding="utf-8")
        try:
            quiet(PP.cmd_settle, root)
            check(False, "an edited universe file is refused")
        except SystemExit as e:
            check("universe.jsonl" in str(e), "an edited universe file is refused (its hash is in the ledger)")
        uf.write_text(uraw, encoding="utf-8")
        L2 = PP.read_ledger(PP.paths(root)["paper"]).copy()
        L2["recorded_at"] = (pd.Timestamp(tr["entry_day"].iloc[0]) + pd.Timedelta(hours=10)).isoformat()
        cal = np.unique(np.concatenate([b.ts for b in bars.values()]))
        _, dd2 = PP.settle_rows(L2, bars, cal, None, {}, 5, 3, 0.0035, "09:00")
        check(dd2["late"].all(), "a watchlist recorded after 09:00 of the entry session is flagged late")
        txt, _ = quiet(PP.cmd_report, root)
        check("Month-6 rule" in txt and "too early" in txt, "report prints the month-6 rule and its status")
        n_miss = int(sum(1 for x in days if pd.Timestamp(x) > T0))
        check(f"NO recorded watchlist: {n_miss}" in txt, f"report lists the {n_miss} sessions after day 1 with no watchlist")
        print("6b. tracker and log after the exits")
        TRf = res["tracker"]
        cl = TRf[(TRf["role"] == "traded")].merge(tr[["engine", "symbol", "net"]], on=["engine", "symbol"], suffixes=("", "_s"))
        check(len(cl) == 6 and (cl["status"] == "closed").all() and float((cl["net"] - cl["net_s"]).abs().max()) < 1e-15,
              "after the exits every traded pick is closed with exactly the settled net")
        paper = PP.paths(root)["paper"]
        lg = (paper / "daily_log.md").read_text(encoding="utf-8")
        tk = (paper / "tracker.md").read_text(encoding="utf-8")
        check("bought at the open" in lg and " sold " in lg and "holding at the close" in lg,
              "daily log shows the buys, the sales and the holdings session by session")
        check("Open positions" in tk and "Last recorded days" in tk and (paper / "tracker.csv").exists(),
              "tracker.md and tracker.csv written")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"FAILED  {len(FAILS)} check(s):")
        for f in FAILS:
            print("   - " + f)
        return 1
    print(f"VERIFIED  {PP.CODE_VERSION} + {PM.CODE_VERSION} + {SV.CODE_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
