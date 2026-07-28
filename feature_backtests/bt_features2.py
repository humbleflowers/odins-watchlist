"""Stage 2 backtests: F5 out-of-sample stability, EP follow-through,
Telegram precedence, Smart-money (bulk/block) precedence, and a trailing-stop
exit engine vs the fixed target/stop baseline.
"""
import pandas as pd, numpy as np, glob, re
from pathlib import Path
from collections import defaultdict

BO = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version/backtest_output")
WV = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version")
ROOT = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist")

print("Loading OHLC panel (Open/High/Low/Close)...")
o = pd.read_csv(BO / "ohlc_indicator_panel.csv",
                usecols=["Symbol", "Date", "Open", "High", "Low", "Close", "atr_pct",
                         "rel_volume", "rs_vs_market"])
o["Date"] = pd.to_datetime(o["Date"])
o = o.sort_values(["Symbol", "Date"]).reset_index(drop=True)

# ---- forward 20-bar features per (Symbol,Date): fwd max return + TP/SL label ----
print("Computing forward returns (20 bars, +20%/-8% first-hit)...")
o["ret1"] = o.groupby("Symbol")["Close"].pct_change().shift(-1)  # next-day ret placeholder
# Build per-symbol close arrays and compute forward max + tp/sl label
fwd_max = np.full(len(o), np.nan)
tpsl = np.full(len(o), np.nan)     # 1 = +20 before -8, 0 = otherwise resolved, nan unresolved
H, TP, SL = 20, 0.20, -0.08
for sym, idx in o.groupby("Symbol").indices.items():
    idx = np.sort(idx)
    c = o["Close"].values[idx]
    n = len(c)
    for i in range(n):
        hi = min(i + H, n - 1)
        if hi <= i:
            continue
        path = c[i + 1:hi + 1] / c[i] - 1.0
        if len(path):
            gi = idx[i]
            fwd_max[gi] = np.nanmax(path)
            win = np.nan
            for r in path:
                if r <= SL:
                    win = 0; break
                if r >= TP:
                    win = 1; break
            if np.isnan(win) and len(path) >= H:
                win = 0
            tpsl[gi] = win
o["fwd_max20"] = fwd_max
o["tpsl"] = tpsl
lookup = o.set_index(["Symbol", "Date"])[["fwd_max20", "tpsl"]]
BASE_FWD = o["fwd_max20"].mean() * 100
BASE_TP = np.nanmean(o["tpsl"]) * 100
print(f"Universe base: mean fwd-max-20 {BASE_FWD:.1f}%, +20%-before-8% rate {BASE_TP:.0f}%\n")

# =====================================================================
# F5 out-of-sample stability of per-screener edge
# =====================================================================
print("=" * 70)
print("F5 (screener intelligence) OUT-OF-SAMPLE stability check")
lab = pd.read_csv(BO / "labeled_panel.csv",
                  usecols=["Symbol", "Date", "Setup Score", "Screener_today", "tradable_win"])
lab["Date"] = pd.to_datetime(lab["Date"])
lab = lab[lab["tradable_win"].isin([0, 1])].copy()
lab["win"] = lab["tradable_win"].astype(int)
lab["rank"] = lab.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
short = lab[lab["rank"] <= 40]
cut = short["Date"].quantile(0.5)


def screener_winrates(dfsub):
    st = defaultdict(lambda: [0, 0])
    for _, r in dfsub.iterrows():
        s = str(r.get("Screener_today") or "")
        if not s or s == "nan":
            continue
        for name in [x.strip() for x in s.split(",") if x.strip()]:
            st[name][0] += r["win"]; st[name][1] += 1
    return {k: v[0] / v[1] * 100 for k, v in st.items() if v[1] >= 30}


first = screener_winrates(short[short["Date"] <= cut])
second = screener_winrates(short[short["Date"] > cut])
common = sorted(set(first) & set(second))
if len(common) >= 8:
    a = pd.Series({k: first[k] for k in common})
    b = pd.Series({k: second[k] for k in common})
    rho = a.rank().corr(b.rank())
    print(f"  {len(common)} screeners in both halves. Rank correlation first->second half: {rho:.2f}")
    print(f"  -> {'STABLE (edge persists, reweight is real)' if rho >= 0.35 else 'UNSTABLE (likely overfit - do NOT hard-reweight)'}")
    # show a few
    tbl = pd.DataFrame({"1st half win%": a.round(0), "2nd half win%": b.round(0)}).sort_values("1st half win%", ascending=False)
    print(tbl.head(6).to_string())
    print(tbl.tail(4).to_string())
else:
    print("  too few common screeners for OOS test")
print()

# =====================================================================
# F6 Episodic Pivot follow-through (gap held vs faded)
# =====================================================================
print("=" * 70)
print("F6 EP follow-through: does a gap that CLOSES STRONG beat one that fades?")
o["prev_close"] = o.groupby("Symbol")["Close"].shift(1)
o["gap_pct"] = (o["Open"] / o["prev_close"] - 1) * 100
ep = o[(o["gap_pct"] >= 4) & (o["rel_volume"] >= 3) & o["tpsl"].notna()].copy()
# follow-through = closed in top half of the day's range (held the gap)
ep["close_pos"] = (ep["Close"] - ep["Low"]) / (ep["High"] - ep["Low"]).replace(0, np.nan)
held = ep[ep["close_pos"] >= 0.5]
faded = ep[ep["close_pos"] < 0.5]
for lbl, d in [("ALL EP", ep), ("EP held (close top-half)", held), ("EP faded (close bot-half)", faded)]:
    if len(d):
        print(f"  {lbl:28} n={len(d):<5} +20/-8 win {d['tpsl'].mean()*100:4.0f}%  mean fwd-max {d['fwd_max20'].mean()*100:5.1f}%")
delta = held["tpsl"].mean() * 100 - faded["tpsl"].mean() * 100 if len(held) and len(faded) else 0
print(f"  -> follow-through Δ: {delta:+.0f}pp  ({'EDGE' if delta >= 5 else 'weak'})\n")

# =====================================================================
# F7 Telegram precedence: do mentions PRECEDE a move?
# =====================================================================
print("=" * 70)
print("F7 Telegram precedence: forward move AFTER a mention vs universe base")
eq = pd.read_csv(ROOT / "EQUITY_L.csv")
eq.columns = eq.columns.str.strip()
symbols = set(eq["SYMBOL"].astype(str).str.strip().str.upper())
STOP = {"BUY","SELL","SL","TGT","TARGET","CMP","ABOVE","BELOW","HOLD","EXIT","BOOK","PROFIT",
        "LOSS","SHORT","LONG","CALL","PUT","ADD","OK","THE","AND","FOR","NOW","TODAY","STOP",
        "ENTRY","QTY","LOT","NIFTY","BANK","INDEX","FUT","CE","PE","NSE","BSE","DAY","WEEK",
        "SWING","GAP","UP","DOWN","INR","NEW","HIGH","LOW","OPEN","CLOSE","RSI"}
msgs = pd.read_csv(WV / "telegram_messages.csv")
msgs["dt"] = pd.to_datetime(msgs["date_ist"], errors="coerce").dt.normalize()
# map each trading date to itself; we look up first trading day >= mention date
trade_dates = np.array(sorted(o["Date"].unique()))
def next_td(d):
    i = np.searchsorted(trade_dates, np.datetime64(d))
    return pd.Timestamp(trade_dates[i]) if i < len(trade_dates) else pd.NaT
rows = []
admin_re = re.compile(r"^-?\d+$")
for r in msgs.itertuples():
    if pd.isna(r.dt): continue
    text = str(getattr(r, "text", "") or "").upper()
    toks = set(re.findall(r"[A-Z&]{3,}", text))
    hits = [t for t in toks if t in symbols and t not in STOP]
    if not hits: continue
    is_admin = bool(admin_re.match(str(getattr(r, "sender", "") or "")))
    td = next_td(r.dt)
    for s in hits[:3]:
        rows.append((s, td, is_admin))
tg = pd.DataFrame(rows, columns=["Symbol", "Date", "admin"]).dropna()
tg = tg.merge(lookup, on=["Symbol", "Date"], how="left").dropna(subset=["fwd_max20"])
print(f"  {len(tg)} mention->forward pairs matched ({tg['Symbol'].nunique()} symbols)")
for lbl, d in [("ALL mentions", tg), ("ADMIN posts only", tg[tg["admin"]]), ("member posts", tg[~tg["admin"]])]:
    if len(d):
        print(f"  {lbl:20} n={len(d):<6} +20/-8 win {d['tpsl'].mean()*100:4.0f}%  mean fwd-max {d['fwd_max20'].mean()*100:5.1f}%   (base {BASE_TP:.0f}% / {BASE_FWD:.1f}%)")
adv = tg[tg["admin"]]["tpsl"].mean() * 100 - BASE_TP if len(tg[tg["admin"]]) else 0
print(f"  -> admin-call edge vs base: {adv:+.0f}pp  ({'EDGE' if adv >= 5 else 'weak/none'})\n")

# =====================================================================
# F8 Smart-money (bulk/block) precedence, HFT desks filtered out
# =====================================================================
print("=" * 70)
print("F8 Smart-money bulk/block BUYS (HFT desks removed) -> forward move")
HFT = ["GRAVITON","IRAGE","QE SECURITIES","NK SECURITIES","HRTI","JUNOMONETA","MUSIGMA",
       "MICROCURVES","ALPHAGREP","QUANT BROKING","TOWER RESEARCH","OPTIVER","JANE STREET",
       "PROGRESSIVE","SS CORPORATE SECUR","AISHWARYA GLOBAL"]
def is_hft(name):
    n = str(name).upper()
    return any(h in n for h in HFT)
deal_rows = []
for f in glob.glob(str(WV / "nse_downloads" / "bulk" / "*.csv")) + glob.glob(str(WV / "nse_downloads" / "block" / "*.csv")):
    try: d = pd.read_csv(f)
    except: continue
    d.columns = [c.strip() for c in d.columns]
    if "Symbol" not in d.columns: continue
    bs = [c for c in d.columns if c.lower().startswith("buy")]
    cl = [c for c in d.columns if "client" in c.lower()]
    dc = [c for c in d.columns if c.lower() == "date"]
    if not (bs and cl and dc): continue
    for r in d.itertuples(index=False):
        rd = dict(zip(d.columns, r))
        if str(rd[bs[0]]).strip().upper().startswith("B") and not is_hft(rd[cl[0]]):
            deal_rows.append((str(rd["Symbol"]).strip().upper(), rd[dc[0]]))
dl = pd.DataFrame(deal_rows, columns=["Symbol", "d"]).dropna()
dl["dt"] = pd.to_datetime(dl["d"], errors="coerce", dayfirst=True).dt.normalize()
dl = dl.dropna(subset=["dt"])
dl["Date"] = dl["dt"].map(next_td)
dl = dl.merge(lookup, on=["Symbol", "Date"], how="left").dropna(subset=["fwd_max20"])
print(f"  {len(dl)} genuine-fund buy->forward pairs ({dl['Symbol'].nunique()} symbols)")
if len(dl):
    print(f"  Smart-money buys      +20/-8 win {dl['tpsl'].mean()*100:4.0f}%  mean fwd-max {dl['fwd_max20'].mean()*100:5.1f}%   (base {BASE_TP:.0f}% / {BASE_FWD:.1f}%)")
    adv2 = dl["tpsl"].mean() * 100 - BASE_TP
    print(f"  -> edge vs base: {adv2:+.0f}pp  ({'EDGE' if adv2 >= 5 else 'weak/none'})\n")

print("Stage 2 done.")
