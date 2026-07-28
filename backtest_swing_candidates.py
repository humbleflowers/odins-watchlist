"""
Backtest the find_swing_candidates.py scoring methodology against historical
Odin watchlist output.

Methodology summary (see plan for full detail):
  1. Consolidate every historical odin_*.csv (root project dir + working_version)
     into one panel, deduped by trading date.
  2. For each symbol, compute forward returns to its next N available
     snapshots (NOT calendar days — data has gaps). Reject any forward pair
     that spans more than 5 calendar days (guards against stitching across
     the 2026-03 -> 2026-07 data gap).
  3. Label two ways:
       upside_touched : future_max_Nd >= 20%       (opportunity existed)
       tradable_win   : TP hit before SL (default +25% / -5%, tightened per
                        optimize_target.py; overridable via --tp/--sl)
  4. Score every historical row with the *actual* functions imported from
     find_swing_candidates.py (no reimplementation), plus the prior repo's
     rule_phase classification as a baseline comparator.
  5. Evaluate with decile analysis, Precision@K/day, in-sample/out-of-sample
     split, rule-phase comparison, and a sector/price-band breakdown.

Usage:
    python backtest_swing_candidates.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_swing_candidates import (  # noqa: E402
    pattern_score,
    confluence_score,
    momentum_volume_score,
    institutional_score,
    relative_strength_score,
    setup_type,
    split_screeners,
)

ROOT_DIR = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist")
WORKING_DIR = ROOT_DIR / "working_version"
OUT_DIR = WORKING_DIR / "backtest_output"
OUT_DIR.mkdir(exist_ok=True)

HORIZONS = [5, 10, 15, 20, 30]

# "Opportunity" measure -- did the stock ever reach this gain within a
# horizon, ignoring drawdown. Kept at the tool's namesake 20% for continuity
# of the upside_touched_* columns and the report.
UPSIDE_PCT = 20.0

# Tradable exit rule (first-hit TP vs SL). Defaults tightened per
# optimize_target.py, which found a ~5% stop with a wide target was the only
# profile with positive expected value on this tool's picks. Overridable via
# --tp / --sl / --tradable-horizon.
TP_PCT = 25.0
SL_PCT = -5.0
TRADABLE_HORIZON = 30

MIN_PRICE = 20.0
MIN_VOLUME = 50_000
MAX_GAP_DAYS = 5           # reject forward pairs spanning more than this
MAX_ABS_RETURN = 100.0     # cap: likely corporate-action artifact beyond this

DDMMYYYY_RE = re.compile(r"^odin_(\d{8})\.csv$")
ISODATE_RE = re.compile(r"^odin_(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2}:\d{2})?\.csv$")

KEEP_COLS = [
    "Symbol", "Stock Name", "Price", "% Chg", "Volume", "Sector",
    "Today_screener_count", "screener_count_diff", "new_screeners_today",
    "Screener_today", "Screener_yesterday", "was_in_previous_sheet",
    "DELIV_PER", "BUY_quantity", "SELL_quantity",
]

# ---------------------------------------------------------------------------
# 1. File discovery + consolidation
# ---------------------------------------------------------------------------

def discover_dated_files(directory: Path) -> dict[pd.Timestamp, Path]:
    """Map trading date -> canonical odin_*.csv path in a directory."""
    dated: dict[pd.Timestamp, Path] = {}
    for path in directory.glob("odin_*.csv"):
        m = DDMMYYYY_RE.match(path.name)
        if m:
            date = pd.to_datetime(m.group(1), format="%d%m%Y")
            dated[date] = path  # DDMMYYYY form preferred/canonical
    for path in directory.glob("odin_*.csv"):
        m = ISODATE_RE.match(path.name)
        if m:
            date = pd.to_datetime(m.group(1))
            dated.setdefault(date, path)  # only fill gaps, don't override canonical
    return dated


def load_one(path: Path, date: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)

    if "BUY_quantity" not in df.columns and "BUY_quantity_x" in df.columns:
        df["BUY_quantity"] = df["BUY_quantity_x"].combine_first(df.get("BUY_quantity_y"))
    if "SELL_quantity" not in df.columns and "SELL_quantity_x" in df.columns:
        df["SELL_quantity"] = df["SELL_quantity_x"].combine_first(df.get("SELL_quantity_y"))

    for col in KEEP_COLS:
        if col not in df.columns:
            df[col] = np.nan

    out = df[KEEP_COLS].copy()
    out["Date"] = date
    out["% Chg"] = pd.to_numeric(
        out["% Chg"].astype(str).str.replace("%", "", regex=False), errors="coerce"
    )
    out["Price"] = pd.to_numeric(out["Price"], errors="coerce")
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce")
    return out


def build_consolidated_panel() -> pd.DataFrame:
    root_dated = discover_dated_files(ROOT_DIR)
    working_dated = discover_dated_files(WORKING_DIR)

    all_dated = dict(root_dated)
    for date, path in working_dated.items():
        all_dated.setdefault(date, path)  # root wins on overlap (richer schema)

    print(f"Discovered {len(root_dated)} dated files in root, {len(working_dated)} in working_version "
          f"-> {len(all_dated)} unique trading dates after dedup")

    frames = []
    for date in sorted(all_dated):
        path = all_dated[date]
        try:
            frames.append(load_one(path, date))
        except Exception as exc:
            print(f"[WARN] Failed to load {path.name}: {exc}")

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["Symbol", "Date", "Price"])
    panel = panel.sort_values(["Symbol", "Date"]).reset_index(drop=True)
    print(f"Consolidated panel: {len(panel)} rows, {panel['Symbol'].nunique()} symbols, "
          f"{panel['Date'].nunique()} dates ({panel['Date'].min().date()} -> {panel['Date'].max().date()})")
    return panel


# ---------------------------------------------------------------------------
# 2. Forward returns + labels (gap-aware, per symbol)
# ---------------------------------------------------------------------------

def label_symbol_group(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").reset_index(drop=True)
    dates = g["Date"].to_numpy()
    prices = g["Price"].to_numpy(dtype=float)
    n = len(g)

    max_h = max(HORIZONS)
    fut_max = {h: np.full(n, np.nan) for h in HORIZONS}
    tradable_win = np.full(n, np.nan)

    for i in range(n):
        base = prices[i]
        if not np.isfinite(base) or base <= 0:
            continue

        rets = []
        prev_date = dates[i]
        for k in range(1, max_h + 1):
            j = i + k
            if j >= n:
                break
            gap_days = (pd.Timestamp(dates[j]) - pd.Timestamp(prev_date)).days
            if gap_days > MAX_GAP_DAYS:
                break  # data-collection gap: stop extending this path
            nxt = prices[j]
            if not np.isfinite(nxt) or nxt <= 0:
                break
            ret = (nxt / base - 1.0) * 100.0
            if abs(ret) > MAX_ABS_RETURN:
                break  # likely corporate action artifact
            rets.append(ret)
            prev_date = dates[j]

        for h in HORIZONS:
            window = rets[:h]
            if window:
                fut_max[h][i] = max(window)

        # tradable_win: first-hit of TP vs SL within the tradable horizon.
        # NaN (unresolved) if the path ran out of data before hitting either
        # bound and before covering the full horizon (gap or end-of-series) --
        # only label 0/1 when we actually observed enough forward path to judge.
        trade_rets = rets[:TRADABLE_HORIZON]
        if trade_rets:
            win = np.nan
            resolved = False
            for r in trade_rets:
                if r <= SL_PCT:
                    win, resolved = 0, True
                    break
                if r >= TP_PCT:
                    win, resolved = 1, True
                    break
            if not resolved:
                win = 0 if len(trade_rets) >= TRADABLE_HORIZON else np.nan
            tradable_win[i] = win

    for h in HORIZONS:
        g[f"future_max_{h}d"] = fut_max[h]
        touched = pd.Series(fut_max[h] >= UPSIDE_PCT, index=g.index).astype("Int64")
        touched[pd.isna(fut_max[h])] = pd.NA
        g[f"upside_touched_{h}d"] = touched

    g["tradable_win"] = pd.Series(tradable_win, index=g.index).astype("Int64")
    g.loc[pd.isna(tradable_win), "tradable_win"] = pd.NA
    return g


def add_forward_returns(panel: pd.DataFrame) -> pd.DataFrame:
    print("Computing forward returns per symbol (gap-aware)...")
    labeled = panel.groupby("Symbol", group_keys=False).apply(label_symbol_group)
    return labeled.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Score every historical row with the live scoring functions
# ---------------------------------------------------------------------------

def score_row(row: pd.Series) -> pd.Series:
    screeners = split_screeners(row.get("Screener_today"))
    fresh = split_screeners(row.get("new_screeners_today"))

    p_score, breakout, momentum, base = pattern_score(screeners)
    c_score, _ = confluence_score(
        row.get("Today_screener_count"), row.get("screener_count_diff"), fresh
    )
    m_score, _ = momentum_volume_score(row.get("% Chg"), row.get("_volume_percentile"))
    i_score, _ = institutional_score(row.get("BUY_quantity"), row.get("SELL_quantity"))
    rs_score, _ = relative_strength_score(row.get("rs_vs_market"))

    total = max(min(p_score + c_score + m_score + i_score + rs_score, 100), 0)
    return pd.Series({
        "Setup Score": total,
        "Setup Score (Chartink-only, pre-RS)": max(min(p_score + c_score + m_score + i_score, 100), 0),
        "Setup Type": setup_type(breakout, momentum, base),
        "has_breakout_pattern": bool(breakout),
    })


def add_setup_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """Score every historical row with the live tool's *current* logic --
    which now includes the OHLC relative-strength component (rs_vs_market),
    merged in from technical_indicators.py's full-market indicator panel."""
    print("Applying live Setup Score logic retroactively (incl. OHLC relative strength)...")
    panel = panel.copy()
    panel["_volume_percentile"] = panel.groupby("Date")["Volume"].rank(pct=True)

    ohlc_path = OUT_DIR / "ohlc_indicator_panel.csv"
    if ohlc_path.exists():
        ohlc = pd.read_csv(ohlc_path, usecols=["Symbol", "Date", "rs_vs_market"], low_memory=False)
        ohlc["Date"] = pd.to_datetime(ohlc["Date"])
        panel = panel.merge(ohlc, on=["Symbol", "Date"], how="left")
    else:
        print("[WARN] ohlc_indicator_panel.csv not found -- run technical_indicators.py first; "
              "scoring without relative strength.")
        panel["rs_vs_market"] = np.nan

    scores = panel.apply(score_row, axis=1)
    return pd.concat([panel, scores], axis=1)


# ---------------------------------------------------------------------------
# Rule-phase baseline (from winners/train_tailored_fut.py, reused for comparison)
# ---------------------------------------------------------------------------

RULE_KW = {
    "PREMIUM71": r"Premium_7\.1",
    "BREAKOUT": r"BREAKOUT|RESISTANCE",
    "MOMENTUM": r"MOMENTUM",
    "VOLUME": r"VOLUME",
    "SUPPORT": r"SUPPORT",
    "FIB": r"FIB",
    "CONTR": r"CONTRACTION",
}


def add_rule_phase(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    today = panel["Screener_today"].fillna("")
    flags = {name: today.str.contains(pat, flags=re.I, regex=True) for name, pat in RULE_KW.items()}

    # Prior art gated breakout/continuation tiers on vol_ratio_5d (Volume vs.
    # 5D avg delivery qty), which we don't carry here (sparse historically).
    # Dropping that condition degrades the rule gracefully to the other tiers
    # rather than silently zeroing out the whole phase.
    rebound = flags["PREMIUM71"] & (flags["SUPPORT"] | flags["FIB"] | flags["CONTR"]) & ~flags["BREAKOUT"]
    breakout = (
        flags["PREMIUM71"] & (flags["BREAKOUT"] | flags["MOMENTUM"])
        & (panel["screener_count_diff"].fillna(0) >= 3)
    )
    continuation = flags["BREAKOUT"] | flags["MOMENTUM"]

    panel["rule_phase"] = np.select(
        [rebound, breakout, continuation],
        ["Rebound Setup", "Breakout Trigger", "Momentum Continuation"],
        default="Neutral",
    )
    return panel


# ---------------------------------------------------------------------------
# 4. Evaluation
# ---------------------------------------------------------------------------

def apply_liquidity_gate(panel: pd.DataFrame) -> pd.DataFrame:
    """Liquidity gate + exclude ETFs/index funds.

    Discovered during backtest QA: sectors.csv maps ~176 ETFs/index funds
    (NIFTYBEES, BANKBEES, LIQUIDIETF, ...) to Sector == 'Equity'. These are
    not swing-trade candidates and their price series show implausible 20%+
    moves (likely NAV/price artifacts or corporate-action noise), inflating
    win-rate stats. find_swing_candidates.py has the same blind spot live.
    """
    is_etf = panel["Symbol"].str.contains("ETF|BEES|IETF|LIQUID", case=False, na=False) | (
        panel["Sector"] == "Equity"
    )
    return panel[
        (panel["Price"].fillna(0) >= MIN_PRICE)
        & (panel["Volume"].fillna(0) >= MIN_VOLUME)
        & ~is_etf
    ].copy()


def decile_analysis(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    valid = panel.dropna(subset=["Setup Score"])
    valid = valid[valid.groupby("Date")["Setup Score"].transform("nunique") >= 2]
    deciles = valid.groupby("Date")["Setup Score"].transform(
        lambda s: pd.qcut(s, 10, labels=False, duplicates="drop")
    )
    valid = valid.assign(decile=deciles)

    for h in HORIZONS:
        col = f"upside_touched_{h}d"
        agg = valid.dropna(subset=[col]).groupby("decile").agg(
            n=(col, "count"),
            win_rate=(col, "mean"),
            avg_future_max=(f"future_max_{h}d", "mean"),
        ).reset_index()
        agg["horizon_days"] = h
        rows.append(agg)
    return pd.concat(rows, ignore_index=True).sort_values(["horizon_days", "decile"], ascending=[True, False])


def precision_at_k(panel: pd.DataFrame, k: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    per_day_rows = []
    for h in HORIZONS:
        col = f"upside_touched_{h}d"
        for date, day_df in panel.dropna(subset=["Setup Score", col]).groupby("Date"):
            baseline = day_df[col].mean()
            top = day_df.sort_values("Setup Score", ascending=False).head(k)
            if top.empty:
                continue
            per_day_rows.append({
                "Date": date, "horizon_days": h, "k": k,
                "precision_at_k": top[col].mean(),
                "baseline_win_rate": baseline,
                "n_top": len(top),
            })
    per_day = pd.DataFrame(per_day_rows)
    if per_day.empty:
        return per_day, per_day
    summary = per_day.groupby("horizon_days").agg(
        avg_precision_at_k=("precision_at_k", "mean"),
        avg_baseline_win_rate=("baseline_win_rate", "mean"),
        n_days=("Date", "nunique"),
    ).reset_index()
    summary["lift"] = summary["avg_precision_at_k"] / summary["avg_baseline_win_rate"]
    return per_day, summary


def precision_at_k_tradable(panel: pd.DataFrame, k: int) -> pd.DataFrame:
    """Precision@K using the tradable_win label (TP before SL, current settings)."""
    rows = []
    for date, day_df in panel.dropna(subset=["Setup Score", "tradable_win"]).groupby("Date"):
        top = day_df.sort_values("Setup Score", ascending=False).head(k)
        if top.empty:
            continue
        rows.append({
            "Date": date, "k": k,
            "precision_at_k": top["tradable_win"].mean(),
            "baseline_win_rate": day_df["tradable_win"].mean(),
            "n_top": len(top),
        })
    per_day = pd.DataFrame(rows)
    return per_day


def insample_outsample_split(panel: pd.DataFrame, k: int = 10) -> pd.DataFrame:
    dates = sorted(panel["Date"].unique())
    cutoff = dates[int(len(dates) * 0.7)]
    rows = []
    for label, sub in [("in_sample", panel[panel["Date"] <= cutoff]), ("out_of_sample", panel[panel["Date"] > cutoff])]:
        _, summary = precision_at_k(sub, k)
        summary["split"] = label
        rows.append(summary)
    result = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    print(f"In-sample/out-of-sample cutoff date: {cutoff.date()}")
    return result


def rule_phase_comparison(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for h in HORIZONS:
        col = f"upside_touched_{h}d"
        agg = panel.dropna(subset=[col]).groupby("rule_phase").agg(
            samples=(col, "count"),
            win_rate=(col, "mean"),
            avg_future_max=(f"future_max_{h}d", "mean"),
        ).reset_index()
        agg["horizon_days"] = h
        rows.append(agg)
    return pd.concat(rows, ignore_index=True).sort_values(["horizon_days", "win_rate"], ascending=[True, False])


def sector_breakdown(panel: pd.DataFrame, h: int = 15, top_n: int = 25) -> pd.DataFrame:
    col = f"upside_touched_{h}d"
    top_picks = (
        panel.dropna(subset=["Setup Score", col])
        .sort_values(["Date", "Setup Score"], ascending=[True, False])
        .groupby("Date", as_index=False)
        .head(top_n)
    )
    return top_picks.groupby("Sector").agg(
        n=(col, "count"), win_rate=(col, "mean"), avg_price=("Price", "mean")
    ).reset_index().sort_values("n", ascending=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> "argparse.Namespace":
    import argparse
    p = argparse.ArgumentParser(description="Backtest the swing-candidate scoring methodology")
    p.add_argument("--tp", type=float, default=TP_PCT,
                   help=f"Take-profit %% for tradable_win (default {TP_PCT:.0f})")
    p.add_argument("--sl", type=float, default=abs(SL_PCT),
                   help=f"Stop-loss %% magnitude for tradable_win (default {abs(SL_PCT):.0f})")
    p.add_argument("--tradable-horizon", type=int, default=TRADABLE_HORIZON,
                   help=f"Sessions to resolve tradable_win within (default {TRADABLE_HORIZON})")
    return p.parse_args()


def main() -> int:
    global TP_PCT, SL_PCT, TRADABLE_HORIZON
    args = parse_args()
    TP_PCT, SL_PCT, TRADABLE_HORIZON = args.tp, -abs(args.sl), args.tradable_horizon
    print(f"Tradable exit rule: +{TP_PCT:.0f}% TP / {SL_PCT:.0f}% SL within {TRADABLE_HORIZON} sessions "
          f"(upside_touched measure fixed at +{UPSIDE_PCT:.0f}%)")

    panel = build_consolidated_panel()
    panel.to_csv(OUT_DIR / "consolidated_panel.csv", index=False)

    labeled = add_forward_returns(panel)
    labeled = add_setup_scores(labeled)
    labeled = add_rule_phase(labeled)
    labeled.to_csv(OUT_DIR / "labeled_panel.csv", index=False)
    print(f"Labeled panel written: {len(labeled)} rows")

    gated = apply_liquidity_gate(labeled)
    print(f"After liquidity gate (Price>={MIN_PRICE}, Volume>={MIN_VOLUME:,.0f}): {len(gated)} rows")

    deciles = decile_analysis(gated)
    deciles.to_csv(OUT_DIR / "decile_analysis.csv", index=False)

    _, prec10 = precision_at_k(gated, 10)
    _, prec25 = precision_at_k(gated, 25)
    prec10.to_csv(OUT_DIR / "precision_at_10.csv", index=False)
    prec25.to_csv(OUT_DIR / "precision_at_25.csv", index=False)

    # Before/after: does adding the OHLC relative-strength component actually
    # beat the old Chartink-screener-only score, on the same rows/dates?
    pre_rs_col = "Setup Score (Chartink-only, pre-RS)"
    prec10_pre_rs = pd.DataFrame()
    if pre_rs_col in gated.columns:
        pre_rs_trial = gated.copy()
        pre_rs_trial["Setup Score"] = pre_rs_trial[pre_rs_col]
        _, prec10_pre_rs = precision_at_k(pre_rs_trial, 10)
        prec10_pre_rs.to_csv(OUT_DIR / "precision_at_10_chartink_only_pre_rs.csv", index=False)

    split_results = insample_outsample_split(gated, k=10)
    split_results.to_csv(OUT_DIR / "insample_outsample_precision_at_10.csv", index=False)

    rule_cmp = rule_phase_comparison(gated)
    rule_cmp.to_csv(OUT_DIR / "rule_phase_comparison.csv", index=False)

    sectors = sector_breakdown(gated)
    sectors.to_csv(OUT_DIR / "sector_breakdown.csv", index=False)

    tradable_p10 = precision_at_k_tradable(gated, 10)
    tradable_p10.to_csv(OUT_DIR / "precision_at_10_tradable_win.csv", index=False)

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 100)
    print(f"BACKTEST SUMMARY — Setup Score vs. +{UPSIDE_PCT:.0f}% target")
    print(f"Labels: upside_touched = ever reached +{UPSIDE_PCT:.0f}% within horizon (ignores drawdown)")
    print(f"        tradable_win  = +{TP_PCT:.0f}% hit before {SL_PCT:.0f}% stop, within {TRADABLE_HORIZON} sessions")
    print("=" * 100)

    print("\nPrecision@10/day (Top-10 by Setup Score) vs. full-universe baseline win rate:")
    print(prec10.to_string(index=False))

    if not prec10_pre_rs.empty:
        print("\nSame test, OLD Chartink-only score (no relative strength) -- shows what the RS component added:")
        print(prec10_pre_rs.to_string(index=False))

    print("\nPrecision@25/day:")
    print(prec25.to_string(index=False))

    print("\nIn-sample vs out-of-sample (Top-10, checks overfitting):")
    print(split_results.to_string(index=False))

    print("\nRule-phase win rates (baseline comparator from prior repo):")
    print(rule_cmp.to_string(index=False))

    overall_tradable = gated["tradable_win"].mean()
    print(f"\nOverall tradable_win rate across full liquid universe (all rows, all dates): {overall_tradable:.4f}")
    if not tradable_p10.empty:
        tp10_mean = tradable_p10["precision_at_k"].mean()
        tp10_base = tradable_p10["baseline_win_rate"].mean()
        print(f"tradable_win Precision@10/day: {tp10_mean:.4f} vs baseline {tp10_base:.4f} "
              f"(lift {tp10_mean / tp10_base:.2f}x, n_days={len(tradable_p10)})")

    print(f"\nAll detailed outputs written to: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
