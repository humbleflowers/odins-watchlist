"""F9 - Exit engine backtest. On the daily top-10 picks, simulate several exit
rules over the forward daily path and compare per-trade EXPECTANCY (avg % result
per trade) and hit/loss structure. Uses close-to-close on the entry price.
Rules:
  A fixed        : +25% target / -5% stop (current tool default)
  B doc-best     : +30% target / -5% stop / 10-bar time stop
  C ATR-trail    : initial -1.5*ATR stop, trail a 3*ATR chandelier from peak close, wide +40% cap
  D breakeven    : -6% stop; after +8% move stop to entry; +35% target
  E scale-out    : exit half at +15%, trail remainder with 3*ATR chandelier
Horizon cap: 40 trading bars.
"""
import pandas as pd, numpy as np
from pathlib import Path

BO = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version/backtest_output")
HCAP = 40

o = pd.read_csv(BO / "ohlc_indicator_panel.csv",
                usecols=["Symbol", "Date", "High", "Low", "Close", "atr_pct"])
o["Date"] = pd.to_datetime(o["Date"])
o = o.sort_values(["Symbol", "Date"]).reset_index(drop=True)
idxmap = {s: np.sort(ix) for s, ix in o.groupby("Symbol").indices.items()}
CL = o["Close"].values; AT = o["atr_pct"].values / 100.0
posmap = {}  # (symbol)->{date_ordinal:local_i}
for s, ix in idxmap.items():
    dd = o["Date"].values[ix]
    posmap[s] = {d: i for i, d in enumerate(dd)}

lab = pd.read_csv(BO / "labeled_panel.csv", usecols=["Symbol", "Date", "Setup Score"])
lab["Date"] = pd.to_datetime(lab["Date"])
lab["rank"] = lab.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
picks = lab[lab["rank"] <= 10][["Symbol", "Date"]].drop_duplicates()


def path_for(sym, date):
    ix = idxmap.get(sym)
    if ix is None: return None
    lp = posmap[sym].get(np.datetime64(date))
    if lp is None: return None
    seg = ix[lp: lp + HCAP + 1]
    if len(seg) < 3: return None
    closes = CL[seg]
    atr = AT[seg[0]] if not np.isnan(AT[seg[0]]) else 0.05
    entry = closes[0]
    return closes / entry - 1.0, atr  # forward return path (r[0]=0), atr fraction


def sim(rule, r, atr):
    n = len(r)
    if rule == "A":
        for x in r[1:]:
            if x <= -0.05: return -0.05
            if x >= 0.25: return 0.25
        return r[-1]
    if rule == "B":
        for i, x in enumerate(r[1:], 1):
            if x <= -0.05: return -0.05
            if x >= 0.30: return 0.30
            if i >= 10: return x
        return r[-1]
    if rule == "C":
        peak = 0.0; stop = -1.5 * atr
        for x in r[1:]:
            peak = max(peak, x)
            stop = max(stop, peak - 3 * atr)
            if x <= stop: return x
            if x >= 0.40: return 0.40
        return r[-1]
    if rule == "D":
        stop = -0.06; moved = False
        for x in r[1:]:
            if x >= 0.08 and not moved: stop = 0.0; moved = True
            if x <= stop: return stop
            if x >= 0.35: return 0.35
        return r[-1]
    if rule == "E":
        half = None; peak = 0.0; stop = -0.06
        for x in r[1:]:
            peak = max(peak, x); stop = max(stop, peak - 3 * atr)
            if half is None and x >= 0.15:
                half = 0.15  # booked half here
            if x <= stop:
                rest = x
                return (0.15 + rest) / 2 if half is not None else rest
        rest = r[-1]
        return (0.15 + rest) / 2 if half is not None else rest


res = {k: [] for k in "ABCDE"}
nt = 0
for _, p in picks.iterrows():
    pf = path_for(p["Symbol"], p["Date"])
    if pf is None: continue
    r, atr = pf
    nt += 1
    for k in "ABCDE":
        res[k].append(sim(k, r, atr))

print(f"Simulated {nt} top-10 trades over <= {HCAP} bars.\n")
names = {"A": "fixed +25/-5 (current)", "B": "+30/-5 +10bar-timestop (doc-best)",
         "C": "ATR chandelier trail", "D": "breakeven-move +35 tgt", "E": "scale-out half@+15 + trail"}
print(f"{'rule':<34}{'exp/trade':>10}{'win%':>7}{'avg win':>9}{'avg loss':>9}")
base = None
for k in "ABCDE":
    a = np.array(res[k]) * 100
    exp = a.mean(); wr = (a > 0).mean() * 100
    aw = a[a > 0].mean() if (a > 0).any() else 0
    al = a[a <= 0].mean() if (a <= 0).any() else 0
    if k == "A": base = exp
    tag = "" if k == "A" else f"  ({exp-base:+.2f}pp vs current)"
    print(f"{names[k]:<34}{exp:>9.2f}%{wr:>6.0f}%{aw:>8.1f}%{al:>8.1f}%{tag}")
print("\nHigher exp/trade = better. A positive delta vs current = worth implementing.")
