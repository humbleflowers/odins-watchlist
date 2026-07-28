"""Generate DATA-DRIVEN screener weights from the F5 backtest (OOS-stable,
rank-corr 0.75). Weight is derived from each normalized screener's measured
+25%/-5% tradable-win rate on the daily top-40 shortlist. Rules:
  - normalize names (strip _YYYYMMDD suffix, uppercase, unify separators)
  - weight = clamp(round((winrate - FLOOR) * SCALE), 0, CAPW)
      FLOOR=8%  (screeners at/below the shortlist's weakest are dropped -> 0)
      SCALE=0.9, CAPW=12 (no single screener dominates; tames the RSI_85 outlier)
  - only trust screeners with n>=MINN; thinner ones get a neutral small weight
Outputs config/screener_weights.csv and prints a ready-to-paste dict.
"""
import pandas as pd, numpy as np, re
from pathlib import Path
from collections import defaultdict

WV = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist/working_version")
BO = WV / "backtest_output"
FLOOR, SCALE, CAPW, MINN, NEUTRAL = 8.0, 0.9, 12, 120, 3

lab = pd.read_csv(BO / "labeled_panel.csv", usecols=["Date", "Screener_today", "Setup Score", "tradable_win"])
lab["Date"] = pd.to_datetime(lab["Date"])
lab = lab[lab["tradable_win"].isin([0, 1])].copy()
lab["win"] = lab["tradable_win"].astype(int)
lab["rank"] = lab.groupby("Date")["Setup Score"].rank(ascending=False, method="first")
short = lab[lab["rank"] <= 40]


def norm(x):
    x = re.sub(r'[_\-\s]*\d{6,8}$', '', x.strip())
    x = re.sub(r'[\s\-]+', '_', x.strip())
    return x.upper().strip("_")


st = defaultdict(lambda: [0, 0])
for _, r in short.iterrows():
    s = str(r.get("Screener_today") or "")
    if not s or s == "nan":
        continue
    for nm in {norm(x) for x in s.split(",") if x.strip()}:
        st[nm][0] += r["win"]; st[nm][1] += 1

rows = []
for k, (w, n) in st.items():
    if n < 30:
        continue
    wr = w / n * 100
    weight = int(np.clip(round((wr - FLOOR) * SCALE), 0, CAPW)) if n >= MINN else NEUTRAL
    rows.append((k, round(wr, 1), n, weight))
rows.sort(key=lambda x: -x[1])

out = pd.DataFrame(rows, columns=["screener", "win_rate", "n", "weight"])
(WV / "config" / "screener_weights.csv").write_text(out.to_csv(index=False))
print("Saved config/screener_weights.csv\n")
print(out.to_string(index=False))
print("\n# ---- paste-ready dict (n>=120 -> data weight; else neutral 3) ----")
print("SCREENER_WEIGHTS = {")
for k, wr, n, wt in rows:
    print(f'    "{k}": {wt},   # {wr:.0f}% win, n={n}')
print("}")
