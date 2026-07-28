"""Backtest candidate features. The decisive test (per the project's own rule):
a feature only ADDS edge if it separates outcomes AMONG already-strong names -
not if it merely re-ranks by strength. So for each feature we split the daily
top-40 shortlist into feature-POSITIVE vs feature-NEGATIVE and compare the
+25%/-5% tradable-win rate and the touched-+20% rate. A feature 'wins' if POS
clearly beats NEG and beats the shortlist average.
"""
import pandas as pd, numpy as np
from pathlib import Path

BO = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version/backtest_output")
ROOT = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist")

# ---------- OHLC panel: universe-wide features -------------------------------
o = pd.read_csv(BO / "ohlc_indicator_panel.csv",
                usecols=["Symbol", "Date", "Close", "sma50", "DELIV_PER",
                         "atr_pct", "range_contraction_ratio", "rel_volume",
                         "rs_vs_market", "benchmark_ret_20d"])
o["Date"] = pd.to_datetime(o["Date"])
o = o.sort_values(["Symbol", "Date"])

# breadth: % of universe above 50DMA, per date
o["above50"] = (o["Close"] > o["sma50"]).astype(float)
breadth = o.groupby("Date")["above50"].mean().rename("breadth")
bench20 = o.groupby("Date")["benchmark_ret_20d"].first().rename("bench20")

# delivery trend: today's DELIV_PER vs its own 20d avg
g = o.groupby("Symbol", sort=False)
o["deliv_avg20"] = g["DELIV_PER"].transform(lambda s: s.rolling(20, min_periods=5).mean())
o["deliv_ratio"] = o["DELIV_PER"] / o["deliv_avg20"]

# sector momentum
sec = pd.read_csv(ROOT / "working_version" / "config" / "sectors.csv", usecols=[0, 1],
                  names=["Symbol", "Sector"], header=0)
sec = sec.dropna().drop_duplicates("Symbol")
o = o.merge(sec, on="Symbol", how="left")
secmom = (o.dropna(subset=["Sector"]).groupby(["Date", "Sector"])["rs_vs_market"]
          .median().rename("sector_rs").reset_index())
# rank sectors within each date -> percentile (1 = strongest sector)
secmom["sector_pct"] = secmom.groupby("Date")["sector_rs"].rank(pct=True)
o = o.merge(secmom[["Date", "Sector", "sector_rs", "sector_pct"]], on=["Date", "Sector"], how="left")

okey = o[["Symbol", "Date", "deliv_ratio", "atr_pct", "range_contraction_ratio",
          "rel_volume", "sector_pct", "sector_rs", "Sector"]]

# ---------- Labeled watchlist panel: outcomes --------------------------------
lab = pd.read_csv(BO / "labeled_panel.csv",
                  usecols=["Symbol", "Date", "Setup Score", "rs_vs_market",
                           "Screener_today", "tradable_win", "upside_touched_20d"])
lab["Date"] = pd.to_datetime(lab["Date"])
df = lab.merge(okey, on=["Symbol", "Date"], how="left").merge(breadth, on="Date", how="left").merge(bench20, on="Date", how="left")
res = df[df["tradable_win"].isin([0, 1])].copy()
res["win"] = res["tradable_win"].astype(int)
res["touch"] = (res["upside_touched_20d"] == True).astype(int)
res["rank"] = res.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
short = res[res["rank"] <= 40].copy()
top10 = res[res["rank"] <= 10].copy()

BW, BT = res["win"].mean() * 100, res["touch"].mean() * 100
print(f"Resolved trades: {len(res):,} over {res['Date'].nunique()} dates. "
      f"Base: win {BW:.0f}% touch {BT:.0f}%. "
      f"Shortlist(top40): win {short['win'].mean()*100:.0f}% touch {short['touch'].mean()*100:.0f}%. "
      f"Top10: win {top10['win'].mean()*100:.0f}% touch {top10['touch'].mean()*100:.0f}%.\n")

VERDICTS = []


def split_test(name, universe, mask, note=""):
    u = universe.dropna(subset=[])
    pos = u[mask]
    neg = u[~mask]
    if len(pos) < 40 or len(neg) < 40:
        print(f"[skip] {name}: too few samples (pos={len(pos)}, neg={len(neg)})")
        VERDICTS.append((name, "n/a", len(pos), None, None, note))
        return
    pw, pt = pos["win"].mean() * 100, pos["touch"].mean() * 100
    nw, nt = neg["win"].mean() * 100, neg["touch"].mean() * 100
    base = u["win"].mean() * 100
    edge = pw - nw
    verdict = "EDGE" if (edge >= 4 and pw > base) else ("weak" if edge >= 1.5 else "none")
    print(f"### {name}")
    print(f"    POS n={len(pos):<5} win {pw:4.0f}%  touch {pt:4.0f}%   |   NEG n={len(neg):<5} win {nw:4.0f}%  touch {nt:4.0f}%   "
          f"|  Δwin {edge:+.0f}pp  -> {verdict}")
    if note:
        print(f"    {note}")
    print()
    VERDICTS.append((name, verdict, len(pos), round(pw), round(nw), note))


# ===== Feature 1: Market regime / breadth =====
# split ALL top10 trades by whether the entry date was a high-breadth (risk-on) day
med_b = res["breadth"].median()
split_test("F1 Regime/breadth (entry on risk-ON day, breadth>median) - within TOP10",
           top10, top10["breadth"] > med_b,
           note=f"tests timing: does entering on strong-breadth days help? (median breadth={med_b:.0%})")
split_test("F1b Regime: benchmark 20d uptrend (bench20>0) - within TOP10",
           top10, top10["bench20"] > 0)

# ===== Feature 2: Sector RS (leader in a leading sector) =====
split_test("F2 Sector RS (stock in top-quartile sector) - within TOP40",
           short, short["sector_pct"] >= 0.75,
           note="new axis: group strength vs single-stock strength")

# ===== Feature 3: Delivery TREND (not level) =====
split_test("F3 Delivery trend (deliv today > 1.3x its 20d avg) - within TOP40",
           short, short["deliv_ratio"] >= 1.3,
           note="delivery LEVEL was rejected; this tests the CHANGE")

# ===== Feature 4: Volatility squeeze / VCP coil =====
# tight = low range_contraction_ratio (more contracted) & low atr
q = short["range_contraction_ratio"].quantile(0.33)
split_test("F4 Volatility squeeze (range_contraction in tightest third) - within TOP40",
           short, short["range_contraction_ratio"] <= q,
           note=f"'coiled spring' pre-breakout (tight<= {q:.2f})")

# ===== Feature 5: Screener intelligence =====
# per-screener forward win rate, across the whole shortlist
from collections import defaultdict
sc_stats = defaultdict(lambda: [0, 0])  # screener -> [wins, n]
for _, r in short.iterrows():
    s = str(r.get("Screener_today") or "")
    if not s or s == "nan":
        continue
    for name in [x.strip() for x in s.split(",") if x.strip()]:
        sc_stats[name][0] += r["win"]
        sc_stats[name][1] += 1
rows = [(k, v[0] / v[1] * 100, v[1]) for k, v in sc_stats.items() if v[1] >= 60]
rows.sort(key=lambda x: -x[1])
print("### F5 Screener intelligence - per-screener tradable-win rate (n>=60), within TOP40")
print(f"    (shortlist avg win {short['win'].mean()*100:.0f}%)  BEST:")
for k, wr, n in rows[:6]:
    print(f"      {wr:4.0f}%  n={n:<5} {k[:60]}")
print("    WORST:")
for k, wr, n in rows[-5:]:
    print(f"      {wr:4.0f}%  n={n:<5} {k[:60]}")
spread = rows[0][1] - rows[-1][1] if rows else 0
print(f"    -> spread best-vs-worst screener: {spread:.0f}pp "
      f"({'EDGE (reweight worth it)' if spread >= 8 else 'weak'})\n")
VERDICTS.append(("F5 Screener intelligence", "EDGE" if spread >= 8 else "weak",
                 len(rows), round(rows[0][1]) if rows else None, round(rows[-1][1]) if rows else None,
                 f"best-worst spread {spread:.0f}pp"))

print("\n===== SUMMARY (feature | verdict | POSwin% vs NEGwin%) =====")
for name, v, n, pw, nw, note in VERDICTS:
    print(f"  {v:5} | {name}  (pos n={n}, win {pw} vs {nw})")
