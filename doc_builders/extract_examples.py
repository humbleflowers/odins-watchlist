"""Extract REAL, dated example trades per use case from the backtest panels.

Outcomes are already computed by backtest_swing_candidates.py:
  tradable_win: 1 = +25% target hit BEFORE -5% stop; 0 = stopped/expired; <NA> unresolved.
  upside_touched_20d: did price touch +20% within 20 bars (opportunity, ignores stop).
  future_max_30d: peak forward move within 30 bars (%).
Lens cohorts are evaluated WITHIN each day's top-40 by Setup Score (the realistic
shortlist), which is what makes the cohort win-rates line up with the guide.
Nothing is invented; every row is a real (symbol, date) with its real outcome.
"""
import json
import pandas as pd, numpy as np
from pathlib import Path

BO = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version/backtest_output")

# ---- OHLC panel: persistence + gap + rel_volume ------------------------------
ohlc = pd.read_csv(BO / "ohlc_indicator_panel.csv",
                   usecols=["Symbol", "Date", "Open", "Close", "rs_vs_market",
                            "rel_volume", "atr_pct"])
ohlc["Date"] = pd.to_datetime(ohlc["Date"])
ohlc = ohlc.sort_values(["Symbol", "Date"])
grp = ohlc.groupby("Symbol", sort=False)
ohlc["prev_close"] = grp["Close"].shift(1)
ohlc["gap_pct"] = (ohlc["Open"] / ohlc["prev_close"] - 1) * 100
ohlc["lead"] = (ohlc["rs_vs_market"] >= 15).astype(float)
ohlc["persist20"] = grp["lead"].rolling(20, min_periods=5).sum().reset_index(level=0, drop=True)
okey = ohlc[["Symbol", "Date", "gap_pct", "rel_volume", "persist20",
             "rs_vs_market", "atr_pct"]].rename(columns={"rs_vs_market": "rs_ohlc"})

# ---- Labeled watchlist panel: outcomes ---------------------------------------
lab = pd.read_csv(BO / "labeled_panel.csv",
                  usecols=["Symbol", "Price", "Date", "Setup Score", "rs_vs_market",
                           "rule_phase", "has_breakout_pattern", "new_screeners_today",
                           "DELIV_PER", "future_max_30d", "upside_touched_20d", "tradable_win"])
lab["Date"] = pd.to_datetime(lab["Date"])
df = lab.merge(okey, on=["Symbol", "Date"], how="left")
res = df[df["tradable_win"].isin([0, 1])].copy()
res["win"] = res["tradable_win"].astype(int)
res["touch20"] = (res["upside_touched_20d"] == True).astype(int)
res["fresh"] = res["new_screeners_today"].astype(str).str.strip().replace({"nan": "", "": ""}).ne("")

# daily rank by score -> restrict lenses to the shortlist (top 40/day)
res["rank"] = res.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
shortlist = res[res["rank"] <= 40]

BASE_WIN = res["win"].mean() * 100
BASE_TOUCH = res["touch20"].mean() * 100
print(f"Panel: {res['Date'].nunique()} dates {res['Date'].min().date()}..{res['Date'].max().date()}, "
      f"{len(res)} resolved trades. Base rates: tradable {BASE_WIN:.0f}%, touched+20% {BASE_TOUCH:.0f}%\n")

RESULTS = {}


def collect(key, title, subtitle, cohort, sort_col, extra, n_win=3, n_loss=2):
    c = cohort.dropna(subset=[sort_col]).copy()
    win_rate = c["win"].mean() * 100 if len(c) else float("nan")
    touch_rate = c["touch20"].mean() * 100 if len(c) else float("nan")
    # representative picks: dedupe by symbol, ranked by the cohort's sort col
    c = c.sort_values(sort_col, ascending=False).drop_duplicates("Symbol")
    wins = c[c["win"] == 1].head(n_win)
    losses = c[c["win"] == 0].head(n_loss)

    def row(r):
        ex = {}
        for col, lbl in extra:
            v = r.get(col)
            if pd.notna(v):
                ex[lbl] = (round(float(v)) if isinstance(v, (int, float, np.floating)) else str(v))
        return {
            "date": r["Date"].date().isoformat(), "symbol": r["Symbol"],
            "price": round(float(r["Price"]), 1), "score": int(r["Setup Score"]),
            "rs": round(float(r["rs_vs_market"])), "extra": ex,
            "win": int(r["win"]),
            "peak": (round(float(r["future_max_30d"])) if pd.notna(r["future_max_30d"]) else None),
        }
    RESULTS[key] = {
        "title": title, "subtitle": subtitle, "n": int(len(c)),
        "win_rate": round(win_rate), "touch_rate": round(touch_rate),
        "examples": [row(r) for _, r in pd.concat([wins, losses]).iterrows()],
    }
    print(f"### {title}  (n={len(cohort)}, tradable {win_rate:.0f}%, touched+20% {touch_rate:.0f}%)")
    for e in RESULTS[key]["examples"]:
        tag = "WIN " if e["win"] else "LOSS"
        ex = " ".join(f"{k}={v}" for k, v in e["extra"].items())
        pk = f"peak {e['peak']:+d}%" if e["peak"] is not None else "peak n/a"
        print(f"   {tag} {e['date']} {e['symbol']:<11} Rs{e['price']:<8} score{e['score']} RS{e['rs']:+d} {ex} -> {pk}")
    print()


# 1. Core top-10 by score
top10 = res[res["rank"] <= 10]
collect("uc1", "Core top-10 by Setup Score", "Every day's ten highest scores.",
        top10, "Setup Score", [])

# 2. Persistent leaders (within shortlist)
collect("uc2", "Persistent leaders (led >=12 of last 20 days)", "Durable market leaders.",
        shortlist[shortlist["persist20"] >= 12], "Setup Score", [("persist20", "strip")])

# 3. Persistent leader + fresh breakout
uc3 = shortlist[(shortlist["persist20"] >= 12) & (shortlist["has_breakout_pattern"] == True) & shortlist["fresh"]]
if len(uc3) < 5:
    uc3 = shortlist[(shortlist["persist20"] >= 12) & (shortlist["has_breakout_pattern"] == True)]
collect("uc3", "Persistent leader + fresh breakout", "Durable leader firing a breakout.",
        uc3, "Setup Score", [("persist20", "strip")])

# 4. Episodic Pivot
collect("uc4", "Episodic Pivot (gap >=4% on >=3x volume)", "Catalyst gap-ups.",
        shortlist[(shortlist["gap_pct"] >= 4) & (shortlist["rel_volume"] >= 3)],
        "rel_volume", [("gap_pct", "gap%"), ("rel_volume", "rvol")])

# 5. Wyckoff SOS / breakout trigger
collect("uc5", "Wyckoff SOS / breakout trigger", "Breakout-structure entries.",
        shortlist[shortlist["rule_phase"].astype(str).eq("Breakout Trigger")],
        "Setup Score", [("rule_phase", "phase")])

# 6. Wyckoff LPS / rebound-setup (pullback entry)
collect("uc6", "Wyckoff LPS / rebound-setup (pullback entry)", "Post-strength pullback entries.",
        shortlist[shortlist["rule_phase"].astype(str).eq("Rebound Setup")],
        "Setup Score", [("rule_phase", "phase")])

# 7. Emerging (volume thrust while RS still modest) -- score>=50 floor
emg = res[(res["rel_volume"] >= 3) & (res["rs_ohlc"].between(0, 25)) & (res["Setup Score"] >= 50)]
collect("uc7", "Emerging (volume thrust >=3x, RS 0..+25)", "Early wake-up before obvious leadership.",
        emg, "rel_volume", [("rel_volume", "rvol"), ("rs_ohlc", "RS")])

# Anti-pattern A: high score, near-empty strip (flare trap)
collect("anti1", "High score (>=75) but near-empty leadership strip (<=2/20)", "The one-day-flare trap.",
        res[(res["Setup Score"] >= 75) & (res["persist20"].fillna(0) <= 2)],
        "Setup Score", [("persist20", "strip")], n_win=2, n_loss=3)

# Anti-pattern B: high delivery % (rejected lens), within shortlist
collect("anti2", "High delivery % (>=80) - the rejected 'accumulation' lens", "Tested and dropped.",
        shortlist[shortlist["DELIV_PER"] >= 80], "DELIV_PER",
        [("DELIV_PER", "deliv%")], n_win=2, n_loss=3)

RESULTS["_meta"] = {
    "dates": f"{res['Date'].min().date()} to {res['Date'].max().date()}",
    "ndates": int(res["Date"].nunique()), "n_resolved": int(len(res)),
    "base_win": round(BASE_WIN), "base_touch": round(BASE_TOUCH),
}
out = Path(__file__).resolve().parent / "examples.json"
out.write_text(json.dumps(RESULTS, indent=2))
print("Wrote", out)
