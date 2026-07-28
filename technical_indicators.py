"""
Real OHLC technical-indicator engine, built from raw NSE bhavcopy archives
(nse_downloads/delivery/sec_bhavdata_full_*.csv) rather than trusting
Chartink screener labels ("VCP", "Darvas breakout", ...) at face value.

Covers the whole NSE EQ-series universe per day (~2,380 symbols), not just
the Chartink-filtered watchlist -- same cost, keeps full-market scanning
available later.

Gap-awareness: the same March->July 2026 data-collection hole that affected
the backtest exists here too. Rolling indicators computed naively across it
would silently blend two disconnected periods into one "200-day" window.
Every row carries `bars_since_gap` (consecutive trading-day rows since the
last >5-calendar-day jump for that symbol); indicators are only populated
once enough contiguous history exists since the last gap.

Usage:
    python technical_indicators.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT_DIR = Path("/Users/vinaykusuma/Documents/AlgoTrade/odins_watchlist")
WORKING_DIR = ROOT_DIR / "working_version"
OUT_DIR = WORKING_DIR / "backtest_output"
OUT_DIR.mkdir(exist_ok=True)

BHAV_RE = re.compile(r"^sec_bhavdata_full_(\d{8})\.csv$")
MAX_GAP_DAYS = 5
MARKET_BENCHMARK_SYMBOL = "NIFTYBEES"

# When taking an "as of date X" indicator snapshot, a symbol's most recent
# bhavcopy row must be within this many calendar days of X for the snapshot
# to be trusted. Otherwise the symbol has stopped appearing in the bhavcopy
# (renamed / delisted / suspended) and its last-known indicators are stale --
# returning them would silently apply months-old momentum to a fresh signal.
STALE_TOLERANCE_DAYS = 7

RAW_COLS = ["Symbol", "SERIES", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY", "DELIV_PER"]

# Equity settlement series to keep. EQ = normal rolling settlement; BE =
# Trade-to-Trade (delivery-only, but a fully tradeable ordinary equity --
# ~260 watchlist names incl. stocks that moved to T2T like DEEDEV); BZ =
# Trade-to-Trade under surveillance (riskier, price-band restricted, but
# still in the watchlist so kept rather than silently dropped). Excludes SME
# (SM/ST), government securities (GS/GB) and misc/ETF-ish series.
EQUITY_SERIES = {"EQ", "BE", "BZ"}

# ATR (volatility) window. We let it bootstrap from ATR_MIN_PERIODS bars so a
# usable volatility estimate exists ~1.5 weeks after a data gap instead of
# needing the full 14 -- otherwise every stock falls back to a flat stop for
# weeks after a gap. It converges to the true 14-bar ATR by bar 14.
ATR_WINDOW = 14
ATR_MIN_PERIODS = 7

# indicator -> minimum contiguous bars-since-gap required to trust it
WINDOW_REQS = {
    "atr14": ATR_MIN_PERIODS, "atr_pct": ATR_MIN_PERIODS,
    "sma20": 20, "sma50": 50, "sma150": 150, "sma200": 200,
    "pct_from_52w_high": 20, "pct_from_52w_low": 20,
    "range_contraction_ratio": 20, "rel_volume": 20,
    "ret_20d": 21,
}
TREND_TEMPLATE_MIN_BARS = 220  # sma200 + a 20-bar slope check


# ---------------------------------------------------------------------------
# 1. Consolidate the OHLC panel
# ---------------------------------------------------------------------------

def discover_bhavcopy_files() -> dict[pd.Timestamp, Path]:
    """Map trading date -> bhavcopy path, root dir preferred on duplicate dates."""
    dated: dict[pd.Timestamp, Path] = {}
    for directory in (ROOT_DIR / "nse_downloads" / "delivery", WORKING_DIR / "nse_downloads" / "delivery"):
        if not directory.exists():
            continue
        for path in directory.glob("sec_bhavdata_full_*.csv"):
            m = BHAV_RE.match(path.name)
            if not m:
                continue
            date = pd.to_datetime(m.group(1), format="%d%m%Y")
            dated.setdefault(date, path)
    return dated


def load_bhavcopy(path: Path, date: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    for col in RAW_COLS:
        if col not in df.columns:
            df[col] = np.nan

    df = df[df["SERIES"].astype(str).str.strip().isin(EQUITY_SERIES)]
    out = df[["Symbol", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY", "DELIV_PER"]].copy()
    out = out.rename(columns={
        "OPEN_PRICE": "Open", "HIGH_PRICE": "High", "LOW_PRICE": "Low",
        "CLOSE_PRICE": "Close", "TTL_TRD_QNTY": "Volume",
    })
    out["Symbol"] = out["Symbol"].astype(str).str.strip()
    out["Date"] = date
    for col in ["Open", "High", "Low", "Close", "Volume", "DELIV_PER"]:
        out[col] = pd.to_numeric(
            out[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
    return out


def build_ohlc_panel() -> pd.DataFrame:
    dated = discover_bhavcopy_files()
    print(f"Discovered {len(dated)} bhavcopy dates")

    frames = []
    for date in sorted(dated):
        try:
            frames.append(load_bhavcopy(dated[date], date))
        except Exception as exc:
            print(f"[WARN] Failed to load {dated[date].name}: {exc}")

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["Symbol", "Date", "Close"])
    panel = panel[panel["Close"] > 0]
    # One row per symbol-date (a stock trades in a single series per day; guard
    # against any edge-case duplicate so forward-return indexing stays valid).
    panel = panel.drop_duplicates(subset=["Symbol", "Date"], keep="last")
    panel = panel.sort_values(["Symbol", "Date"]).reset_index(drop=True)
    print(f"OHLC panel: {len(panel)} rows, {panel['Symbol'].nunique()} symbols, "
          f"{panel['Date'].nunique()} dates ({panel['Date'].min().date()} -> {panel['Date'].max().date()})")
    return panel


# ---------------------------------------------------------------------------
# 2 & 3. Per-symbol indicators (gap-aware)
# ---------------------------------------------------------------------------

def compute_indicators_for_symbol(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").reset_index(drop=True)

    gap_days = g["Date"].diff().dt.days.fillna(0)
    gap_marker = (gap_days > MAX_GAP_DAYS).cumsum()
    g["bars_since_gap"] = g.groupby(gap_marker).cumcount() + 1

    close, high, low = g["Close"], g["High"], g["Low"]
    prev_close = close.shift(1)

    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    # On the first bar of each contiguous segment, prev_close is either NaN or
    # a stale pre-gap close -- the gap-jump term would blow up the true range.
    # Use just the intra-day range there so the bootstrapped ATR isn't poisoned.
    true_range = true_range.mask(g["bars_since_gap"] == 1, high - low)

    g["atr14"] = true_range.rolling(ATR_WINDOW, min_periods=ATR_MIN_PERIODS).mean()
    g["atr_pct"] = g["atr14"] / close * 100

    g["sma20"] = close.rolling(20).mean()
    g["sma50"] = close.rolling(50).mean()
    g["sma150"] = close.rolling(150).mean()
    g["sma200"] = close.rolling(200).mean()

    roll_high_252 = close.rolling(252, min_periods=20).max()
    roll_low_252 = close.rolling(252, min_periods=20).min()
    g["pct_from_52w_high"] = (close / roll_high_252 - 1) * 100
    g["pct_from_52w_low"] = (close / roll_low_252 - 1) * 100

    sma200_rising = g["sma200"] > g["sma200"].shift(20)
    g["trend_template_pass"] = (
        (close > g["sma50"]) & (g["sma50"] > g["sma150"]) & (g["sma150"] > g["sma200"])
        & sma200_rising
        & (g["pct_from_52w_high"] >= -25)
        & (g["pct_from_52w_low"] >= 30)
    ).astype("boolean")  # nullable bool dtype so NaN-masking below doesn't warn/coerce

    atr5 = true_range.rolling(5).mean()
    atr20 = true_range.rolling(20).mean()
    g["range_contraction_ratio"] = atr5 / atr20

    vol20 = g["Volume"].rolling(20).mean()
    g["rel_volume"] = g["Volume"] / vol20

    g["ret_20d"] = close / close.shift(20) - 1
    # NOTE: delivery-% accumulation (deliv_ratio vs. the stock's own norm) was
    # built and backtested here and REJECTED -- it did not add lift on top of
    # relative strength, and within the RS-top-10 the high-delivery bucket
    # slightly underperformed. High delivery signals long-term accumulation,
    # not short-term momentum continuation. Left out on purpose (same verdict
    # as the Minervini trend template). See the exploration notes.

    # Null out anything computed on less contiguous history than it needs --
    # this is what stops a "200-day SMA" from silently spanning the data gap.
    for col, req in WINDOW_REQS.items():
        g.loc[g["bars_since_gap"] < req, col] = np.nan
    g.loc[g["bars_since_gap"] < TREND_TEMPLATE_MIN_BARS, "trend_template_pass"] = np.nan

    return g


def add_indicators(panel: pd.DataFrame) -> pd.DataFrame:
    print("Computing per-symbol technical indicators (gap-aware)...")
    out = panel.groupby("Symbol", group_keys=False).apply(compute_indicators_for_symbol)
    return out.sort_values(["Symbol", "Date"]).reset_index(drop=True)


def add_relative_strength(panel: pd.DataFrame, symbol: str = MARKET_BENCHMARK_SYMBOL) -> pd.DataFrame:
    """rs_vs_market = symbol's trailing-20-session return minus the market
    benchmark's, using the benchmark's own OHLC row from this same panel.
    NIFTYBEES tracks the Nifty 50 -- it's excluded from being scored as a
    trade *candidate* elsewhere, but its price series is a legitimate,
    already-available market-benchmark input."""
    bench = panel[panel["Symbol"] == symbol][["Date", "ret_20d"]].rename(
        columns={"ret_20d": "benchmark_ret_20d"}
    )
    if bench.empty:
        print(f"[WARN] Benchmark symbol {symbol} not found in panel; rs_vs_market left NaN")
        panel["benchmark_ret_20d"] = np.nan
        panel["rs_vs_market"] = np.nan
        return panel

    panel = panel.merge(bench, on="Date", how="left")
    panel["rs_vs_market"] = (panel["ret_20d"] - panel["benchmark_ret_20d"]) * 100
    return panel


# ---------------------------------------------------------------------------
# Live lookup -- what find_swing_candidates.py calls at runtime
# ---------------------------------------------------------------------------

_INDICATOR_CACHE: dict[str, pd.DataFrame] = {}
_RAW_PANEL_CACHE: "pd.DataFrame | None" = None


def _get_raw_panel() -> pd.DataFrame:
    """build_ohlc_panel(), cached in-process -- so a single run that needs
    both an as-of-date indicator snapshot and the latest-overall price
    (see latest_price_overall) only pays the ~15s panel-build cost once."""
    global _RAW_PANEL_CACHE
    if _RAW_PANEL_CACHE is None:
        _RAW_PANEL_CACHE = build_ohlc_panel()
    return _RAW_PANEL_CACHE


def latest_indicators_asof(target_date: "pd.Timestamp | str | None" = None) -> pd.DataFrame:
    """Per-symbol indicator snapshot as of the most recent bhavcopy on or
    before target_date (defaults to the latest available date overall).

    Rebuilds the full historical OHLC panel each call (~1 min for the whole
    market) -- acceptable for a once-a-day batch tool. Cached in-process so
    a single run of find_swing_candidates.py only pays that cost once even
    if it looks up indicators more than once.
    """
    cache_key = str(target_date)
    if cache_key in _INDICATOR_CACHE:
        return _INDICATOR_CACHE[cache_key]

    panel = _get_raw_panel()
    if target_date is not None:
        target_date = pd.Timestamp(target_date)
        panel = panel[panel["Date"] <= target_date]
        if panel.empty:
            raise ValueError(f"No bhavcopy data on or before {target_date.date()}")

    indicators = add_indicators(panel)
    indicators = add_relative_strength(indicators)

    latest = indicators.sort_values(["Symbol", "Date"]).groupby("Symbol", as_index=False).tail(1)

    # Drop stale snapshots: if a symbol's most recent row is far older than
    # the as-of date, it has dropped out of the bhavcopy and its indicators
    # (relative strength etc.) are not "as of" the target date at all.
    if target_date is not None:
        age_days = (target_date - latest["Date"]).dt.days
        latest = latest[age_days <= STALE_TOLERANCE_DAYS]

    _INDICATOR_CACHE[cache_key] = latest
    return latest


def post_signal_price_stats(signal_date: "pd.Timestamp | str") -> pd.DataFrame:
    """Per-symbol price action AFTER a signal date, from the bhavcopy archive.

    Only considers rows on or after signal_date, so it can never compare
    against a pre-signal date (the bug that made a delisted symbol look like
    it fell 57% "since" a signal, when really its data just stopped months
    earlier). Symbols with no bhavcopy on/after signal_date are omitted.

    Columns per symbol:
        Latest_Date, Latest_Price   -- most recent close on/after the signal
        Peak_Date,   Peak_Price     -- highest close reached since the signal
                                       (earliest date if the peak repeats)
    """
    signal_date = pd.Timestamp(signal_date)
    panel = _get_raw_panel()
    fwd = panel[panel["Date"] >= signal_date].sort_values(["Symbol", "Date"])
    if fwd.empty:
        return pd.DataFrame(columns=["Symbol", "Latest_Date", "Latest_Price", "Peak_Date", "Peak_Price"])

    latest = fwd.groupby("Symbol", as_index=False).tail(1)[["Symbol", "Date", "Close"]].rename(
        columns={"Date": "Latest_Date", "Close": "Latest_Price"}
    )
    # idxmax gives the first occurrence of the max close per symbol
    peak_idx = fwd.groupby("Symbol")["Close"].idxmax()
    peak = fwd.loc[peak_idx, ["Symbol", "Date", "Close"]].rename(
        columns={"Date": "Peak_Date", "Close": "Peak_Price"}
    )
    return latest.merge(peak, on="Symbol", how="left")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def spot_check(indicators: pd.DataFrame, symbol: str = "RELIANCE") -> None:
    sub = indicators[indicators["Symbol"] == symbol]
    if sub.empty:
        symbol = indicators.loc[indicators["bars_since_gap"].idxmax(), "Symbol"]
        sub = indicators[indicators["Symbol"] == symbol]
    print(f"\nSpot check ({symbol}) -- verify SMA/ATR by eye against Close:")
    cols = ["Date", "Close", "sma50", "sma200", "atr_pct", "trend_template_pass", "bars_since_gap"]
    print(sub[cols].tail(8).to_string(index=False))

    # confirm bars_since_gap actually resets across the known March->July 2026 hole
    around_gap = sub[(sub["Date"] >= "2026-06-15") & (sub["Date"] <= "2026-07-15")]
    if not around_gap.empty:
        print(f"\nGap-boundary check ({symbol}) -- bars_since_gap should reset to a small number "
              f"right after the gap, not continue a pre-gap streak:")
        print(around_gap[["Date", "bars_since_gap", "sma200"]].to_string(index=False))


def validate_against_backtest(indicators: pd.DataFrame) -> None:
    """Light signal check: do real-OHLC-based rankings beat the existing
    Chartink-screener-based Setup Score, using the exact same evaluation
    functions already built and reported on in backtest_swing_candidates.py."""
    from backtest_swing_candidates import apply_liquidity_gate, precision_at_k

    labeled_path = OUT_DIR / "labeled_panel.csv"
    labeled = pd.read_csv(labeled_path, low_memory=False)
    labeled["Date"] = pd.to_datetime(labeled["Date"])
    gated = apply_liquidity_gate(labeled)

    ind_cols = ["trend_template_pass", "range_contraction_ratio", "rel_volume", "rs_vs_market"]
    # labeled_panel.csv itself carries rs_vs_market (the backtest merges it in
    # for scoring) -- drop any overlapping columns from the labels side so the
    # merge keeps clean, unsuffixed indicator columns.
    gated = gated.drop(columns=[c for c in ind_cols if c in gated.columns])
    merged = gated.merge(
        indicators[["Symbol", "Date"] + ind_cols],
        on=["Symbol", "Date"], how="inner",
    )
    print(f"Rows with both backtest labels and trusted OHLC indicators: {len(merged)} "
          f"(of {len(gated)} liquidity-gated backtest rows)")
    if merged.empty:
        print("[WARN] No overlap between the OHLC panel and the backtest panel -- skipping validation.")
        return

    candidate_scores = {
        "trend_template + contraction + rel_volume": (
            merged["trend_template_pass"].fillna(False).astype(int) * 10
            + (merged["range_contraction_ratio"] < 0.7).fillna(False).astype(int) * 5
            + merged["rel_volume"].clip(upper=5).fillna(0)
        ),
        "rs_vs_market (20-session relative strength)": merged["rs_vs_market"],
    }

    for name, score in candidate_scores.items():
        trial = merged.copy()
        trial["Setup Score"] = score
        _, prec10 = precision_at_k(trial, 10)
        print(f"\n=== Candidate ranking: {name} ===")
        print(prec10.to_string(index=False) if not prec10.empty else "insufficient overlapping data")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    panel = build_ohlc_panel()
    indicators = add_indicators(panel)
    indicators = add_relative_strength(indicators)

    out_path = OUT_DIR / "ohlc_indicator_panel.csv"
    indicators.to_csv(out_path, index=False)
    print(f"Indicator panel written: {out_path} ({len(indicators)} rows)")

    trusted200 = (indicators["bars_since_gap"] >= 200).mean()
    print(f"Rows with >=200 contiguous bars (trusted SMA200/trend template): {trusted200:.1%}")
    trend_col = indicators["trend_template_pass"].dropna()
    if not trend_col.empty:
        print(f"trend_template_pass rate (among trusted rows): {trend_col.mean():.2%}")

    spot_check(indicators)

    print("\n" + "=" * 100)
    print("VALIDATION -- does a real-OHLC ranking beat the existing (Chartink-screener-based) Setup Score?")
    print("=" * 100)
    try:
        validate_against_backtest(indicators)
    except FileNotFoundError:
        print("[WARN] backtest_output/labeled_panel.csv not found; run backtest_swing_candidates.py first.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
