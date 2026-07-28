"""Fixes: F5 out-of-sample stability (lower threshold), F7 Telegram precedence
(correct 5-column parse), F9 exit engine (datetime-key fix). Uses per-symbol
close arrays for on-demand forward returns - fast, no full-universe loop.
"""
import pandas as pd, numpy as np, csv, re
from pathlib import Path
from collections import defaultdict

BO = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version/backtest_output")
WV = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version")
ROOT = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist")

o = pd.read_csv(BO / "ohlc_indicator_panel.csv", usecols=["Symbol", "Date", "Close", "atr_pct"])
o["Date"] = pd.to_datetime(o["Date"])
o = o.sort_values(["Symbol", "Date"]).reset_index(drop=True)
# per-symbol close array + date->local index
CLOSE, DPOS, ATR0 = {}, {}, {}
for sym, ix in o.groupby("Symbol").indices.items():
    ix = np.sort(ix)
    CLOSE[sym] = o["Close"].values[ix]
    ATR0[sym] = o["atr_pct"].values[ix] / 100.0
    dates = pd.DatetimeIndex(o["Date"].values[ix])
    DPOS[sym] = {d: i for i, d in enumerate(dates)}
TRADE_DATES = np.array(sorted(pd.DatetimeIndex(o["Date"].unique())))


def entry_pos(sym, date):
    """local index of first trading day >= date for sym."""
    dp = DPOS.get(sym)
    if dp is None: return None
    d = pd.Timestamp(date).normalize()
    if d in dp: return dp[d]
    i = np.searchsorted(TRADE_DATES, np.datetime64(d))
    while i < len(TRADE_DATES):
        td = pd.Timestamp(TRADE_DATES[i])
        if td in dp: return dp[td]
        i += 1
    return None


def fwd(sym, date, H=20, TP=0.20, SL=-0.08):
    lp = entry_pos(sym, date)
    if lp is None: return None
    c = CLOSE[sym]; n = len(c)
    if lp >= n - 1: return None
    path = c[lp + 1: lp + 1 + H] / c[lp] - 1.0
    if len(path) == 0: return None
    fmax = float(np.nanmax(path))
    win = np.nan
    for r in path:
        if r <= SL: win = 0.0; break
        if r >= TP: win = 1.0; break
    if np.isnan(win) and len(path) >= H: win = 0.0
    return fmax, win

BASE_TP, BASE_FMAX = 9.0, 7.4  # from stage 2 universe base

# =====================================================================
print("=" * 68); print("F5 screener intelligence - OUT-OF-SAMPLE stability (n>=20/half)")
lab = pd.read_csv(BO / "labeled_panel.csv", usecols=["Symbol", "Date", "Setup Score", "Screener_today", "tradable_win"])
lab["Date"] = pd.to_datetime(lab["Date"])
lab = lab[lab["tradable_win"].isin([0, 1])].copy()
lab["win"] = lab["tradable_win"].astype(int)
lab["rank"] = lab.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
short = lab[lab["rank"] <= 40]
cut = short["Date"].quantile(0.5)

def wr(dsub, minn):
    st = defaultdict(lambda: [0, 0])
    for _, r in dsub.iterrows():
        s = str(r.get("Screener_today") or "")
        if not s or s == "nan": continue
        for nm in [x.strip() for x in s.split(",") if x.strip()]:
            st[nm][0] += r["win"]; st[nm][1] += 1
    return {k: v[0] / v[1] * 100 for k, v in st.items() if v[1] >= minn}

first, second = wr(short[short["Date"] <= cut], 20), wr(short[short["Date"] > cut], 20)
common = sorted(set(first) & set(second))
print(f"  {len(common)} screeners present in both halves")
if len(common) >= 6:
    a = pd.Series({k: first[k] for k in common}); b = pd.Series({k: second[k] for k in common})
    rho = a.rank().corr(b.rank())
    print(f"  rank corr first->second half: {rho:.2f} -> "
          f"{'STABLE (reweight is real)' if rho >= 0.35 else 'UNSTABLE (overfit risk)'}")
    tbl = pd.DataFrame({"H1": a.round(0), "H2": b.round(0)}).sort_values("H1", ascending=False)
    print(tbl.to_string())
print()

# =====================================================================
print("=" * 68); print("F7 Telegram precedence (correct parse)")
rows = []
with open(WV / "telegram_messages.csv") as f:
    rd = csv.reader(f); next(rd)
    for parts in rd:
        if len(parts) < 4: continue
        date_ist, sender = parts[1], parts[2]
        text = " ".join(parts[3:])
        rows.append((date_ist, sender, text))
tgm = pd.DataFrame(rows, columns=["date_ist", "sender", "text"])
tgm["dt"] = pd.to_datetime(tgm["date_ist"], errors="coerce", format="%Y-%m-%d %H:%M")
tgm = tgm.dropna(subset=["dt"])
print(f"  parsed {len(tgm)} messages, {tgm['dt'].min().date()}..{tgm['dt'].max().date()}")

eq = pd.read_csv(ROOT / "EQUITY_L.csv"); eq.columns = eq.columns.str.strip()
SYMS = set(eq["SYMBOL"].astype(str).str.strip().str.upper())
STOP = {"BUY","SELL","SL","TGT","TARGET","CMP","ABOVE","BELOW","HOLD","EXIT","BOOK","PROFIT",
        "LOSS","SHORT","LONG","CALL","PUT","ADD","OK","THE","AND","FOR","NOW","TODAY","STOP",
        "ENTRY","QTY","LOT","NIFTY","BANK","INDEX","FUT","CE","PE","NSE","BSE","DAY","WEEK",
        "SWING","GAP","UP","DOWN","INR","NEW","HIGH","LOW","OPEN","CLOSE","RSI","MA","EMA","COP","VCP"}
pan_end = TRADE_DATES.max()
recs = []
for r in tgm.itertuples():
    md = pd.Timestamp(r.dt).normalize()
    if md > pd.Timestamp(pan_end) - pd.Timedelta(days=35):  # need forward room
        continue
    toks = set(re.findall(r"[A-Z&]{3,}", str(r.text).upper()))
    hits = [t for t in toks if t in SYMS and t not in STOP]
    for s in hits[:3]:
        recs.append((s, md))
print(f"  {len(recs)} symbol-mentions with forward room ({len(set(x[0] for x in recs))} symbols)")
outs = [fwd(s, d) for s, d in recs]
outs = [x for x in outs if x is not None]
if outs:
    fmax = np.array([x[0] for x in outs]) * 100
    winv = np.array([x[1] for x in outs if not np.isnan(x[1])])
    print(f"  matched {len(outs)} mention->forward pairs")
    print(f"  Telegram mentions:  +20/-8 win {winv.mean()*100:4.0f}%   mean fwd-max {fmax.mean():5.1f}%   (base {BASE_TP:.0f}% / {BASE_FMAX:.1f}%)")
    adv = winv.mean() * 100 - BASE_TP
    print(f"  -> edge vs base: {adv:+.0f}pp  ({'EDGE' if adv >= 5 else 'weak/none'})")
else:
    print("  still 0 matches - symbols not in panel window")
print()

# =====================================================================
print("=" * 68); print("F9 Exit engine on top-10 picks (datetime-key fixed)")
picks = lab[lab["rank"] <= 10][["Symbol", "Date"]].drop_duplicates()

def sim(rule, r, atr):
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
            peak = max(peak, x); stop = max(stop, peak - 3 * atr)
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
            if half is None and x >= 0.15: half = 0.15
            if x <= stop:
                return (0.15 + x) / 2 if half is not None else x
        return (0.15 + r[-1]) / 2 if half is not None else r[-1]

R = {k: [] for k in "ABCDE"}; nt = 0
for _, p in picks.iterrows():
    lp = entry_pos(p["Symbol"], p["Date"])
    if lp is None: continue
    c = CLOSE[p["Symbol"]]
    if lp >= len(c) - 3: continue
    seg = c[lp: lp + 41]
    r = seg / seg[0] - 1.0
    atr = ATR0[p["Symbol"]][lp]
    if np.isnan(atr): atr = 0.05
    nt += 1
    for k in "ABCDE": R[k].append(sim(k, r, atr))
print(f"  simulated {nt} trades")
names = {"A": "fixed +25/-5 (current)", "B": "+30/-5 +10bar-stop (doc-best)",
         "C": "ATR chandelier trail", "D": "breakeven-move +35tgt", "E": "scale-out half@15+trail"}
print(f"  {'rule':<32}{'exp/trade':>10}{'win%':>7}")
base = None
for k in "ABCDE":
    a = np.array(R[k]) * 100; exp = a.mean(); wrr = (a > 0).mean() * 100
    if k == "A": base = exp
    tag = "" if k == "A" else f"  ({exp-base:+.2f}pp)"
    print(f"  {names[k]:<32}{exp:>9.2f}%{wrr:>6.0f}%{tag}")
