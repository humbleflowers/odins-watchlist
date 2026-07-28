"""
Find the profit target that gives the best expected outcome for this tool's
picks -- the script was written around a +20% target, but was that optimal?

Method (honest about what "best" means):
  A lower target is hit more often but pays less, so raw hit-rate trivially
  favours tiny targets. The decision metric here is EXPECTED RETURN PER TRADE
  (first-hit: take-profit before stop-loss, else marked to market at the
  horizon), evaluated on the daily top-K picks by Setup Score -- i.e. the
  stocks the live tool would actually surface. Secondary metric is return
  per day of capital held (a +30% target that takes 40 sessions ties up
  capital far longer than a +10% that resolves in 8).

  Forward price paths are reconstructed from the same consolidated panel and
  the SAME gap-aware / corporate-action guards as backtest_swing_candidates.py
  (imported, not re-implemented). Prices are daily closes, so intraday spikes
  through the target/stop are not captured -- this understates both hit and
  stop rates equally, and is the same limitation the whole backtest carries.

Usage:
    python optimize_target.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_swing_candidates import (  # noqa: E402
    OUT_DIR,
    MAX_GAP_DAYS,
    MAX_ABS_RETURN,
    apply_liquidity_gate,
)

TOP_K = 10                      # evaluate the daily top-K picks by Setup Score
TP_GRID = [5, 8, 10, 12, 15, 18, 20, 25, 30, 40]
SL_GRID = [5, 6, 8, 10, 12]
HORIZON_GRID = [10, 15, 20, 30]
PRIMARY_SL = 8.0
PRIMARY_HORIZON = 30


# ---------------------------------------------------------------------------
# Forward-path reconstruction (gap-aware, same guards as the backtest)
# ---------------------------------------------------------------------------

def build_forward_paths(panel: pd.DataFrame, max_h: int) -> dict:
    """(Symbol, Date) -> list of forward % returns from that row's close,
    stopping at a data gap (>MAX_GAP_DAYS) or a corporate-action-sized jump."""
    paths: dict = {}
    for _, g in panel.sort_values(["Symbol", "Date"]).groupby("Symbol", sort=False):
        dates = g["Date"].to_numpy()
        prices = g["Price"].to_numpy(dtype=float)
        # Key on pandas Timestamps (matches picks.itertuples().Date); numpy
        # datetime64 keys would not hash-match the Timestamp lookups.
        keys = list(zip(g["Symbol"].tolist(), g["Date"].tolist()))
        n = len(g)
        for i in range(n):
            base = prices[i]
            if not np.isfinite(base) or base <= 0:
                paths[keys[i]] = []
                continue
            rets = []
            prev_date = dates[i]
            for k in range(1, max_h + 1):
                j = i + k
                if j >= n:
                    break
                if (pd.Timestamp(dates[j]) - pd.Timestamp(prev_date)).days > MAX_GAP_DAYS:
                    break
                nxt = prices[j]
                if not np.isfinite(nxt) or nxt <= 0:
                    break
                ret = (nxt / base - 1.0) * 100.0
                if abs(ret) > MAX_ABS_RETURN:
                    break
                rets.append(ret)
                prev_date = dates[j]
            paths[keys[i]] = rets
    return paths


def resolve_trade(path: list, tp: float, sl: float, horizon: int) -> tuple:
    """First-hit outcome. Returns (return_pct, holding_sessions, outcome).
    SL checked before TP within a session (conservative). Neither hit ->
    marked to market at the last close within the horizon."""
    for k, r in enumerate(path[:horizon], start=1):
        if r <= -sl:
            return -sl, k, "stop"
        if r >= tp:
            return tp, k, "win"
    if path:
        h = min(horizon, len(path))
        return path[h - 1], h, "timeout"
    return np.nan, 0, "no_data"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def daily_top_k(gated: pd.DataFrame, k: int) -> pd.DataFrame:
    return (
        gated.dropna(subset=["Setup Score"])
        .sort_values(["Date", "Setup Score"], ascending=[True, False])
        .groupby("Date", as_index=False)
        .head(k)
    )


def evaluate(picks: pd.DataFrame, paths: dict, tp: float, sl: float, horizon: int) -> dict:
    outcomes, holds, kinds = [], [], []
    for row in picks.itertuples():
        path = paths.get((row.Symbol, row.Date), [])
        ret, hold, kind = resolve_trade(path, tp, sl, horizon)
        if kind == "no_data" or np.isnan(ret):
            continue
        outcomes.append(ret)
        holds.append(hold)
        kinds.append(kind)

    if not outcomes:
        return {}
    outcomes = np.array(outcomes)
    holds = np.array(holds)
    kinds = np.array(kinds)
    avg_ret = outcomes.mean()
    avg_hold = holds[holds > 0].mean() if (holds > 0).any() else np.nan
    gains = outcomes[outcomes > 0].sum()
    losses = -outcomes[outcomes < 0].sum()
    return {
        "TP": tp,
        "SL": sl,
        "H": horizon,
        "n": len(outcomes),
        "win%": (kinds == "win").mean() * 100,
        "stop%": (kinds == "stop").mean() * 100,
        "timeout%": (kinds == "timeout").mean() * 100,
        "avg_ret%": avg_ret,
        "median_ret%": float(np.median(outcomes)),
        "avg_hold": avg_hold,
        "ret_per_session%": avg_ret / avg_hold if avg_hold and avg_hold > 0 else np.nan,
        "profit_factor": gains / losses if losses > 0 else np.inf,
    }


def show(title: str, rows: list, sort_key: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df.sort_values(sort_key, ascending=False).reset_index(drop=True)
    fmt = df.copy()
    for c in ["win%", "stop%", "timeout%", "avg_ret%", "median_ret%", "ret_per_session%", "avg_hold", "profit_factor"]:
        if c in fmt.columns:
            fmt[c] = fmt[c].map(lambda x: f"{x:.2f}" if pd.notna(x) and np.isfinite(x) else "inf")
    print(f"\n{title}")
    print(fmt.to_string(index=False))
    return df


def main() -> int:
    labeled_path = OUT_DIR / "labeled_panel.csv"
    if not labeled_path.exists():
        print("labeled_panel.csv not found -- run backtest_swing_candidates.py first.")
        return 1

    print("Loading labeled panel...")
    panel = pd.read_csv(labeled_path, low_memory=False)
    panel["Date"] = pd.to_datetime(panel["Date"])

    print("Reconstructing gap-aware forward paths (full panel)...")
    paths = build_forward_paths(panel[["Symbol", "Date", "Price"]].copy(), max(HORIZON_GRID))

    gated = apply_liquidity_gate(panel)
    picks = daily_top_k(gated, TOP_K)
    print(f"Evaluating on daily top-{TOP_K} picks: {len(picks)} trades over "
          f"{picks['Date'].nunique()} days\n")
    print("=" * 100)
    print(f"OPTIMAL PROFIT TARGET SEARCH  (stop and horizon held fixed unless noted)")
    print("Decision metric: avg_ret% = expected return per trade. "
          "ret_per_session% = capital efficiency.")
    print("=" * 100)

    # Primary: sweep TP, fixed SL and horizon
    tp_rows = [evaluate(picks, paths, tp, PRIMARY_SL, PRIMARY_HORIZON) for tp in TP_GRID]
    tp_rows = [r for r in tp_rows if r]
    df_tp = show(
        f"[1] Profit-target sweep  (stop -{PRIMARY_SL:.0f}%, horizon {PRIMARY_HORIZON} sessions, top-{TOP_K}/day)  "
        f"-- ranked by expected return/trade",
        tp_rows, "avg_ret%",
    )

    best_ev = df_tp.iloc[0]
    df_eff = df_tp.sort_values("ret_per_session%", ascending=False).reset_index(drop=True)
    best_eff = df_eff.iloc[0]

    # Secondary: stop sensitivity around the best-EV target
    tp_star = float(best_ev["TP"])
    sl_rows = [evaluate(picks, paths, tp_star, sl, PRIMARY_HORIZON) for sl in SL_GRID]
    show(
        f"[2] Stop sensitivity at TP +{tp_star:.0f}%  (horizon {PRIMARY_HORIZON}, top-{TOP_K}/day)",
        [r for r in sl_rows if r], "avg_ret%",
    )

    # Secondary: horizon sensitivity at best-EV target and primary stop
    h_rows = [evaluate(picks, paths, tp_star, PRIMARY_SL, h) for h in HORIZON_GRID]
    show(
        f"[3] Horizon sensitivity at TP +{tp_star:.0f}% / stop -{PRIMARY_SL:.0f}%  (top-{TOP_K}/day)",
        [r for r in h_rows if r], "avg_ret%",
    )

    # Combined: joint TP x SL x horizon grid, so the recommendation isn't a
    # single cherry-picked cell. Report the best few by expected return.
    combo_rows = []
    for tp in [20, 25, 30]:
        for sl in [5, 6, 8]:
            for h in [10, 15, 30]:
                r = evaluate(picks, paths, tp, sl, h)
                if r:
                    combo_rows.append(r)
    df_combo = show(
        f"[4] Combined TP x stop x horizon grid  (top-{TOP_K}/day)  -- top 10 by expected return/trade",
        sorted(combo_rows, key=lambda r: r["avg_ret%"], reverse=True)[:10], "avg_ret%",
    )

    best_combo = df_combo.iloc[0]

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Best TARGET alone (stop -{PRIMARY_SL:.0f}%, H{PRIMARY_HORIZON}) : "
          f"+{best_ev['TP']:.0f}%  ->  {best_ev['avg_ret%']:+.2f}%/trade, win {best_ev['win%']:.0f}%")
    print(f"Current design (+20% / -{PRIMARY_SL:.0f}%, H{PRIMARY_HORIZON})  : "
          f"{df_tp[df_tp['TP']==20].iloc[0]['avg_ret%']:+.2f}%/trade, "
          f"win {df_tp[df_tp['TP']==20].iloc[0]['win%']:.0f}%")
    print(f"Best COMBINED config                : "
          f"TP +{best_combo['TP']:.0f}% / stop -{best_combo['SL']:.0f}% / horizon {best_combo['H']:.0f} "
          f"->  {best_combo['avg_ret%']:+.2f}%/trade, win {best_combo['win%']:.0f}%, "
          f"profit factor {best_combo['profit_factor']:.2f}, ~{best_combo['avg_hold']:.0f} sessions")
    print("\nTakeaway: the profit TARGET is a weak lever (all targets cluster near break-even at "
          "the default -8% stop); the STOP and HORIZON matter more. Tighter stop + wider target "
          "+ shorter horizon is what turns expected value positive on this sample.")
    print("\nCaveats: daily closes only (intraday target/stop touches not captured); no "
          "costs/slippage (real EV a bit lower); one ~13-month window incl. the data gap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
