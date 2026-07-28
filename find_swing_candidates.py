"""
Rank Odin's Watchlist output for short-term swing-trade candidates.

IMPORTANT — what this does and doesn't do:
    The pipeline has no historical OHLC/ATR data, so there is no way to
    compute a real price target. This script does NOT predict "20% upside"
    as a number. Instead it scores each stock on technical-analysis criteria
    that are historically associated with large continuation moves:

      - Breakout patterns (VCP, Darvas box, 52-week breakout, resistance
        breakout, range breakout with volume, strength/RSI extremes)
      - Trend/momentum confirmation (RSI, moving-average pullback entries,
        change-of-polarity strategies)
      - Base-building / support setups (Fib retracement zone, major
        support, EMA-200 support, contraction) — lower urgency, more
        reversal-style
      - Confluence (how many screeners agree today) and freshness (screeners
        that triggered *today* vs. carried over from yesterday)
      - Smart-money confirmation (bulk/block deal BUY vs SELL quantity)
      - 20-session relative strength vs. the market (NIFTYBEES), computed
        from real OHLC bhavcopy history via technical_indicators.py

    Backtested against 158 historical days (see backtest_swing_candidates.py
    and technical_indicators.py): the relative-strength component alone
    showed a far stronger, more durable out-of-sample edge (2.6-3.1x lift)
    than the screener-pattern component (1.1-1.4x out-of-sample) -- weights
    below were rebalanced accordingly, not guessed.

    Each candidate also gets a suggested TARGET and STOP-LOSS: the stop is
    placed by volatility (ATR from technical_indicators.py) and clamped, the
    target set at a fixed reward:risk multiple. Levels are calibrated to
    optimize_target.py, which found that on this tool's picks only a tight
    stop + wide target had positive expected value. These are a starting
    framework, not advice -- see optimize_target.py for the caveats.

    Use the output as a shortlist to chart and validate yourself — not as a
    signal to buy blind. Position sizing and stop-loss discipline are on you.

    When scoring an OLDER odin_*.csv (--file odin_17072026.csv on a later
    date), the output also shows, from the local bhavcopy archive, how each
    pick played out AFTER the signal: the most recent price and % change
    since signal, plus the highest (peak) price reached since the signal, its
    date, and the % gain to that peak. Only bhavcopy rows on/after the signal
    date are used, so a delisted symbol whose data stops early is simply
    skipped rather than compared against a pre-signal date.

Usage:
    python find_swing_candidates.py                       # latest odin_*.csv
    python find_swing_candidates.py --file odin_17072026.csv
    python find_swing_candidates.py --top 15 --min-score 60
    python find_swing_candidates.py --min-price 30 --min-volume 100000
"""

from __future__ import annotations

import argparse
import glob
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import BASE_DIR

# ---------------------------------------------------------------------------
# Screener → technical-setup weighting  (DATA-DRIVEN, from the F5 backtest)
# ---------------------------------------------------------------------------
# These weights are no longer hand-guessed. Each screener's weight is derived
# from its MEASURED +25%/-5% tradable-win rate on the daily top-40 shortlist
# over Jul-2025..Jul-2026, and the ranking is OUT-OF-SAMPLE stable (win-rate
# rank correlation 0.75 between the first and second halves of the window).
# High win-rate screeners score more; screeners at/below the shortlist's
# weakest (~8% win) are dropped to weight 0. Regenerate any time with
# gen_screener_weights.py (writes config/screener_weights.csv, loaded below).
# The embedded dict is the fallback used if that CSV is missing.
SCREENER_WEIGHTS_FALLBACK = {
    "RT_RSI_85": 12, "RSI_IN_ALL_TF_ABOVE_65": 3, "RSI_IN_ALL_TF_65": 12,
    "RT_IPO_STOCKS_SCREENER": 12, "RT_HIGH_STRENGTH": 3, "PREMIUM_3_FOR_MOMENTUM": 11,
    "DARVAS": 11, "RT_52W_BREAKOUT": 10, "SNIPER_IN_PROCESS": 10, "RT_SIDEWAY_BREAKOUT": 8,
    "RT_52W_AWAY": 8, "PREMIUM_7.1": 8, "RT_RANGE_BREAKOUT_WITH_VOLUME": 7,
    "RT_DARVAS_BOX_BREAKOUT_STOCKS": 7, "RT_SNIPER_ENTRY_IN_PROCESS": 3, "RT_COP_IN_DAILY": 3,
    "RT_FOR_MAJOR_RETRACEMENT_MUTLIEYAR_RETEST_AREA": 3, "RT_READY_TO_REBOUND": 6,
    "RT_VCP_TURNAROUND": 7, "RT_15_20_AND_25_AWAY_FROM_52_WEEK_HIGHS": 6,
    "RT_MOMENTUM_MONSTERS": 6, "RT_STOCKS_AT_MAJOR_SUPPORT": 6,
    "RT_SIDEWAYS_BREAKOUT_AFTER_A_STRONG_UPTREND": 6, "RT_52_WEEK_BREAKOUT_SETUPS": 6,
    "RT_MASTER_RESISTANCE_BREAKOUT_SCREENER": 5, "RT_SHORT_TO_MID_SWING_RANGE_HIGH_BREAKOUT": 3,
    "RT_FIB_O.7_TO_0.88_ZONE": 3, "VCP": 2, "RT_DAILY_CONTRACTION": 2, "RT_IPO_SCREENER": 2,
    "RT_COP_STRATEGY": 0, "RT_MAJOR_SUPPORT": 0, "RT_FIB_0.7_TO_0.88_ZONE": 0,
    "RT_SHORT_TO_MID_RANGE_BREAKOUT": 0, "RT_MULTIYEAR_RETEST": 3, "RT_COP_DAILY": 0,
    "GEM_SWING": 3,
}
DEFAULT_SCREENER_WEIGHT = 3   # unseen screener: neutral, don't over-trust


def normalize_screener(name: str) -> str:
    """Match the backtest's normalization: strip trailing _YYYYMMDD date suffix,
    unify separators, uppercase. The odin sheets tag screeners with a date
    suffix and use several separator styles for the same screener."""
    x = re.sub(r"[_\-\s]*\d{6,8}$", "", str(name).strip())
    x = re.sub(r"[\s\-]+", "_", x.strip())
    return x.upper().strip("_")


def _load_screener_weights() -> dict:
    path = BASE_DIR / "config" / "screener_weights.csv"
    try:
        w = pd.read_csv(path)
        return {normalize_screener(r["screener"]): int(r["weight"]) for _, r in w.iterrows()}
    except Exception:
        return dict(SCREENER_WEIGHTS_FALLBACK)


SCREENER_WEIGHTS = _load_screener_weights()


def categorize_screener(norm: str) -> str:
    """Display bucket for a normalized screener name (numeric edge comes from
    the data weights above; this only labels the setup type)."""
    if "AWAY" in norm or "SUPPORT" in norm or "CONTRACTION" in norm or "FIB" in norm \
            or "REBOUND" in norm or "RETRACEMENT" in norm:
        return "base"
    if "BREAKOUT" in norm or "DARVAS" in norm or "52W" in norm or "52_WEEK" in norm \
            or "RESISTANCE" in norm or "RANGE" in norm or "SIDEWAY" in norm \
            or "SNIPER" in norm or "VCP" in norm or "HIGH_STRENGTH" in norm \
            or "MOMENTUM_MONSTER" in norm:
        return "breakout"
    if "RSI" in norm or "MOMENTUM" in norm or "PREMIUM" in norm or "EMA" in norm \
            or "COP" in norm or "MULTIYEAR" in norm or "RETEST" in norm:
        return "momentum"
    return "base"

# Weight caps for each scoring component (sum to 100). Rebalanced from the
# original 55/20/15/10 split after the backtest showed the Chartink-screener
# pattern score has a much weaker out-of-sample edge than real 20-session
# relative strength vs. the market -- see technical_indicators.py.
PATTERN_SCORE_CAP = 30
CONFLUENCE_CAP = 15
MOMENTUM_VOL_CAP = 10
INSTITUTIONAL_CAP = 10
RELATIVE_STRENGTH_CAP = 35

# ---------------------------------------------------------------------------
# Per-stock target & stop-loss levels
# ---------------------------------------------------------------------------
# Informed by optimize_target.py: on this tool's daily top-K picks, the only
# exit profile with positive expected value was a TIGHT stop (~5%) paired with
# a WIDE target (~5x the risk) -- the picks pay off like breakouts (many small
# losses, few large winners), which rewards cutting losers fast and letting
# winners run. Stops are placed by volatility (ATR) so a high-beta name isn't
# shaken out on noise, then clamped into the band the optimization supports;
# the target is set at a fixed reward:risk multiple of the stop distance.
# Multiplier chosen so the MEDIAN stock (~3.5% daily ATR) lands near the
# optimizer's -5% sweet spot; ATR then mainly widens the stop for genuinely
# high-volatility names so they aren't shaken out on noise.
STOP_ATR_MULT = 1.5        # stop distance = 1.5 x daily ATR% ...
STOP_MIN_PCT = 5.0         # ... but never tighter than 5% (optimizer's floor)
STOP_MAX_PCT = 10.0        # ... and never risk more than 10% on one trade
REWARD_RISK = 5.0          # target distance = 5 x the stop distance (~optimizer's best R:R)
TARGET_MIN_PCT = 15.0
TARGET_MAX_PCT = 40.0
STOP_FALLBACK_PCT = 6.0    # used when ATR unavailable (e.g. post-data-gap live dates)
TARGET_FALLBACK_PCT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_odin_date(path: Path) -> "datetime | None":
    m = re.search(r"odin_(\d{8})\.csv$", path.name)
    return datetime.strptime(m.group(1), "%d%m%Y") if m else None


def find_latest_odin_csv() -> Path:
    candidates = glob.glob(str(BASE_DIR / "odin_*.csv"))
    if not candidates:
        raise FileNotFoundError("No odin_*.csv files found. Run run_odin.py first.")

    def parse_date(path: str) -> datetime:
        m = re.search(r"odin_(\d{8})\.csv$", path)
        return datetime.strptime(m.group(1), "%d%m%Y") if m else datetime.min

    return Path(max(candidates, key=parse_date))


def split_screeners(value) -> list[str]:
    if pd.isna(value) or not str(value).strip():
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def pattern_score(screeners: list[str]) -> tuple[int, list[str], list[str], list[str]]:
    """Return (capped score, matched breakout, momentum, base) display names.
    Score sums each DISTINCT screener's data-driven weight; display lists keep
    the original screener strings for readable output."""
    breakout, momentum, base = [], [], []
    seen = set()
    raw = 0
    for s in screeners:
        n = normalize_screener(s)
        if n not in seen:
            seen.add(n)
            raw += SCREENER_WEIGHTS.get(n, DEFAULT_SCREENER_WEIGHT)
        cat = categorize_screener(n)
        (breakout if cat == "breakout" else momentum if cat == "momentum" else base).append(s)
    return min(raw, PATTERN_SCORE_CAP), breakout, momentum, base


def confluence_score(today_count: float, count_diff: float, fresh_screeners: list[str]) -> tuple[int, str]:
    score = 0
    notes = []

    if pd.notna(today_count):
        score += min(int(today_count) * 2, 10)

    if pd.notna(count_diff):
        if count_diff > 0:
            score += 5
            notes.append("strengthening (+screeners vs yesterday)")
        elif count_diff < 0:
            score -= 3
            notes.append("weakening (-screeners vs yesterday)")

    fresh_actionable = [s for s in fresh_screeners
                        if categorize_screener(normalize_screener(s)) in ("breakout", "momentum")]
    if fresh_actionable:
        score += 5
        notes.append(f"fresh trigger today: {', '.join(fresh_actionable)}")

    return max(score, 0), "; ".join(notes)


def momentum_volume_score(pct_chg: float, volume_percentile: float) -> tuple[int, str]:
    score = 0
    notes = []

    if pd.notna(pct_chg):
        if 1 <= pct_chg <= 8:
            score += 7
            notes.append(f"healthy move today ({pct_chg:+.1f}%)")
        elif 8 < pct_chg <= 15:
            score += 4
            notes.append(f"strong move, some chase risk ({pct_chg:+.1f}%)")
        elif pct_chg > 15:
            score += 1
            notes.append(f"CAUTION extended ({pct_chg:+.1f}%)")
        elif pct_chg < 0:
            notes.append(f"down today ({pct_chg:+.1f}%)")

    if pd.notna(volume_percentile):
        score += round(volume_percentile * 8)
        if volume_percentile >= 0.9:
            notes.append("top-decile volume today")
        elif volume_percentile >= 0.75:
            notes.append("elevated volume today")

    return min(score, MOMENTUM_VOL_CAP), "; ".join(notes)


def institutional_score(buy_qty: float, sell_qty: float) -> tuple[int, str]:
    if pd.isna(buy_qty) and pd.isna(sell_qty):
        return 0, ""
    buy_qty = buy_qty or 0
    sell_qty = sell_qty or 0
    if buy_qty > sell_qty:
        return INSTITUTIONAL_CAP, f"bulk/block BUY > SELL ({buy_qty:,.0f} vs {sell_qty:,.0f})"
    if sell_qty > buy_qty:
        return -8, f"CAUTION bulk/block SELL > BUY ({sell_qty:,.0f} vs {buy_qty:,.0f})"
    return 0, ""


def relative_strength_score(rs_vs_market: float) -> tuple[int, str]:
    """rs_vs_market is percentage points of 20-session outperformance vs.
    NIFTYBEES (e.g. +15 means the stock beat the market by 15pp over the
    last month). 1 point per pp of outperformance, capped -- underperformers
    score 0 rather than negative, since this is a ranking score, not a
    penalty system, and the backtest tested it as a positive-only signal."""
    if pd.isna(rs_vs_market):
        return 0, ""
    score = int(min(max(rs_vs_market, 0), RELATIVE_STRENGTH_CAP))
    note = ""
    if rs_vs_market >= 15:
        note = f"strong relative strength vs. market (+{rs_vs_market:.1f}pp/20d)"
    elif rs_vs_market >= 5:
        note = f"outperforming market (+{rs_vs_market:.1f}pp/20d)"
    elif rs_vs_market < 0:
        note = f"CAUTION lagging market ({rs_vs_market:+.1f}pp/20d)"
    return score, note


def risk_levels(entry: float, atr_pct: float) -> dict:
    """Volatility-aware target & stop for one stock. entry is the signal
    price; atr_pct is daily ATR as a % of price (from technical_indicators.py).
    Returns {} when there's no valid entry to anchor to. Falls back to flat
    levels when ATR is unavailable (e.g. recent post-gap dates)."""
    if entry is None or pd.isna(entry) or entry <= 0:
        return {}
    if pd.notna(atr_pct) and atr_pct > 0:
        stop_pct = min(max(STOP_ATR_MULT * atr_pct, STOP_MIN_PCT), STOP_MAX_PCT)
        target_pct = min(max(REWARD_RISK * stop_pct, TARGET_MIN_PCT), TARGET_MAX_PCT)
        basis = "ATR"
    else:
        stop_pct, target_pct, basis = STOP_FALLBACK_PCT, TARGET_FALLBACK_PCT, "flat"
    return {
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "stop_price": entry * (1 - stop_pct / 100),
        "target_price": entry * (1 + target_pct / 100),
        "rr": target_pct / stop_pct,
        "basis": basis,
    }


def setup_type(breakout: list[str], momentum: list[str], base: list[str]) -> str:
    if breakout:
        return "Breakout / Momentum"
    if momentum and not base:
        return "Momentum continuation"
    if base:
        return "Base / Support (early-stage, needs confirmation)"
    return "Mixed / weak signal"


# ---------------------------------------------------------------------------
# Main scoring pipeline
# ---------------------------------------------------------------------------

def score_watchlist(
    df: pd.DataFrame,
    min_price: float,
    min_volume: float,
    rs_lookup: "dict[str, float] | None" = None,
    current_price_lookup: "dict[str, tuple] | None" = None,
    atr_lookup: "dict[str, float] | None" = None,
) -> pd.DataFrame:
    """rs_lookup: optional Symbol -> rs_vs_market (20-session relative
    strength vs. NIFTYBEES) mapping, from technical_indicators.py. When
    omitted, that scoring component contributes 0 for every row -- the tool
    still runs, just without the strongest-tested signal (see main()).

    current_price_lookup: optional Symbol -> (latest_bhavcopy_date,
    latest_bhavcopy_price) mapping, from the most recent bhavcopy available
    overall -- not capped to this odin file's own date. Purely informational:
    when scoring an older odin_*.csv, this shows what a stock has actually
    done *since* that signal, alongside the Setup Score it had *at* that
    signal. Does not affect scoring."""
    df = df.copy()
    rs_lookup = rs_lookup or {}
    current_price_lookup = current_price_lookup or {}
    atr_lookup = atr_lookup or {}

    screener_col = "Screener_today" if "Screener_today" in df.columns else "Screener"
    fresh_col = "new_screeners_today" if "new_screeners_today" in df.columns else None
    count_col = "Today_screener_count" if "Today_screener_count" in df.columns else None
    diff_col = "screener_count_diff" if "screener_count_diff" in df.columns else None

    # Liquidity / quality gate
    df = df[df["Price"].fillna(0) >= min_price]
    df = df[df["Volume"].fillna(0) >= min_volume]
    df = df[df["Sector"].notna()] if "Sector" in df.columns else df

    # Exclude ETFs / index funds: config/sectors.csv maps them to Sector ==
    # "Equity" indistinguishable from ordinary stocks. Confirmed via backtest
    # that they produce implausible price-move stats (likely NAV/price
    # artifacts) and aren't swing-trade candidates anyway.
    is_etf = df["Symbol"].str.contains("ETF|BEES|IETF|LIQUID", case=False, na=False) | (
        df.get("Sector", pd.Series(dtype=str)) == "Equity"
    )
    df = df[~is_etf]
    df = df.reset_index(drop=True)

    if df.empty:
        return df

    df["_volume_percentile"] = df["Volume"].rank(pct=True)

    rows = []
    for _, r in df.iterrows():
        screeners = split_screeners(r.get(screener_col))
        fresh = split_screeners(r.get(fresh_col)) if fresh_col else []

        p_score, breakout, momentum, base = pattern_score(screeners)
        c_score, c_notes = confluence_score(
            r.get(count_col) if count_col else float("nan"),
            r.get(diff_col) if diff_col else float("nan"),
            fresh,
        )
        m_score, m_notes = momentum_volume_score(r.get("% Chg"), r.get("_volume_percentile"))
        i_score, i_notes = institutional_score(r.get("BUY_quantity"), r.get("SELL_quantity"))
        rs_value = rs_lookup.get(r.get("Symbol"), float("nan"))
        rs_score, rs_notes = relative_strength_score(rs_value)

        stats = current_price_lookup.get(r.get("Symbol"))
        cur_date, cur_price, peak_date, peak_price = (stats or (None, float("nan"), None, float("nan")))
        signal_price = r.get("Price")
        pct_since_signal = float("nan")
        pct_to_peak = float("nan")
        if pd.notna(signal_price) and signal_price:
            if pd.notna(cur_price):
                pct_since_signal = (cur_price / signal_price - 1) * 100
            if pd.notna(peak_price):
                pct_to_peak = (peak_price / signal_price - 1) * 100

        levels = risk_levels(signal_price, atr_lookup.get(r.get("Symbol"), float("nan")))

        total = max(min(p_score + c_score + m_score + i_score + rs_score, 100), 0)

        reasoning = "; ".join(n for n in [
            f"patterns: {', '.join(breakout + momentum) or 'none'}",
            c_notes,
            m_notes,
            i_notes,
            rs_notes,
        ] if n)

        rows.append({
            "Symbol": r.get("Symbol"),
            "Stock Name": r.get("Stock Name"),
            "Sector": r.get("Sector"),
            "Price": r.get("Price"),
            "% Chg": r.get("% Chg"),
            "Volume": r.get("Volume"),
            "Setup Score": total,
            "Setup Type": setup_type(breakout, momentum, base),
            "Breakout Patterns": ", ".join(breakout),
            "Momentum Patterns": ", ".join(momentum),
            "Support/Base Patterns": ", ".join(base),
            "Fresh Trigger Today": ", ".join(fresh),
            "RS vs Market (20d)": rs_value,
            "Entry (signal price)": signal_price,
            "Target Price": levels.get("target_price"),
            "Target %": levels.get("target_pct"),
            "Stop Loss Price": levels.get("stop_price"),
            "Stop %": levels.get("stop_pct"),
            "Reward:Risk": levels.get("rr"),
            "Stop Basis": levels.get("basis"),
            "Current Price": cur_price,
            "Current Price Date": cur_date,
            "% Chg Since Signal": pct_since_signal,
            "Peak Price Since Signal": peak_price,
            "Peak Price Date": peak_date,
            "% Chg To Peak": pct_to_peak,
            "Reasoning": reasoning,
            "was_in_previous_sheet": r.get("was_in_previous_sheet"),
        })

    result = pd.DataFrame(rows).sort_values("Setup Score", ascending=False).reset_index(drop=True)
    result.insert(0, "Rank", result.index + 1)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank Odin's Watchlist for swing-trade candidates")
    parser.add_argument("--file", default=None, help="Path to odin_DDMMYYYY.csv (default: latest)")
    parser.add_argument("--top", type=int, default=25, help="How many top candidates to show")
    parser.add_argument("--min-score", type=float, default=50, help="Minimum Setup Score to include")
    parser.add_argument("--min-price", type=float, default=20, help="Minimum price filter (avoid penny stocks)")
    parser.add_argument("--min-volume", type=float, default=50_000, help="Minimum today's volume filter (liquidity)")
    parser.add_argument("--out", default=None, help="Output CSV path (default: swing_candidates_<date>.csv)")
    parser.add_argument(
        "--no-relative-strength", action="store_true",
        help="Skip the OHLC lookup entirely (faster, but drops the relative-strength score "
             "component and the current-price/since-signal columns for older files)",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Skip the automatic RIGHTWAY Telegram delta-fetch at start "
             "(also skippable via ODIN_SKIP_TELEGRAM=1)",
    )
    return parser.parse_args()


def load_relative_strength(src: Path) -> "dict[str, float]":
    """Build Symbol -> 20-session relative-strength-vs-market lookup, as of
    the target date encoded in the odin filename, from real bhavcopy OHLC
    history (technical_indicators.py) -- independent of Chartink."""
    target_date = parse_odin_date(src)

    import technical_indicators as ti
    print(f"Loading OHLC indicators as of "
          f"{target_date.date() if target_date else 'latest available'} (~1 min, full-market history)...")
    latest = ti.latest_indicators_asof(target_date)
    return dict(zip(latest["Symbol"], latest["rs_vs_market"]))


def load_atr_lookup(src: Path) -> "dict[str, float]":
    """Symbol -> daily ATR% as of the odin file's signal date, for placing
    volatility-aware stops. Reuses the same cached indicator snapshot as
    load_relative_strength, so it's free after that call."""
    import technical_indicators as ti
    latest = ti.latest_indicators_asof(parse_odin_date(src))
    return dict(zip(latest["Symbol"], latest["atr_pct"]))


def load_current_price_lookup(src: Path) -> "dict[str, tuple]":
    """Symbol -> (latest_date, latest_price, peak_date, peak_price) covering
    bhavcopy rows on or AFTER the odin file's signal date -- so it shows how
    a past pick actually played out (latest price + the highest it reached)
    without ever comparing against a pre-signal date. Reuses the same cached
    OHLC panel load_relative_strength triggers, so it's nearly free."""
    signal_date = parse_odin_date(src)
    if signal_date is None:
        return {}
    import technical_indicators as ti
    stats = ti.post_signal_price_stats(signal_date)
    return {
        row.Symbol: (row.Latest_Date, row.Latest_Price, row.Peak_Date, row.Peak_Price)
        for row in stats.itertuples()
    }


def main() -> int:
    args = parse_args()

    # Pull any NEW RIGHTWAY Telegram messages first (delta only), so the dashboard
    # built afterwards has the freshest group chatter. Best-effort: never fails
    # the run if Telegram isn't configured or is offline.
    import os
    if not args.no_telegram and not os.environ.get("ODIN_SKIP_TELEGRAM"):
        try:
            from fetch_telegram_delta import fetch_delta
            print("Updating RIGHTWAY Telegram messages (delta)...")
            fetch_delta()
        except Exception as exc:  # noqa: BLE001 - stay non-fatal
            print(f"[telegram] skipped - {exc}")

    src = Path(args.file) if args.file else find_latest_odin_csv()
    print(f"Loading: {src}")

    rs_lookup = {}
    current_price_lookup = {}
    atr_lookup = {}
    if not args.no_relative_strength:
        try:
            rs_lookup = load_relative_strength(src)
            current_price_lookup = load_current_price_lookup(src)
            atr_lookup = load_atr_lookup(src)
        except Exception as exc:
            print(f"[WARN] OHLC lookup failed ({exc}); scoring without relative strength, "
                  f"current-price tracking, or ATR-based stops.")

    df = pd.read_csv(src)
    ranked = score_watchlist(
        df, args.min_price, args.min_volume, rs_lookup, current_price_lookup, atr_lookup
    )

    if ranked.empty:
        print("No candidates passed the liquidity filters.")
        return 1

    shortlist = ranked[ranked["Setup Score"] >= args.min_score].head(args.top)

    out_path = Path(args.out) if args.out else BASE_DIR / f"swing_candidates_{src.stem.replace('odin_', '')}.csv"
    ranked.to_csv(out_path, index=False)
    print(f"Full ranked list ({len(ranked)} stocks) written to: {out_path}")

    print(f"\n{'='*100}")
    print(f"TOP {len(shortlist)} SWING CANDIDATES (score >= {args.min_score}, price >= {args.min_price}, volume >= {args.min_volume:,.0f})")
    print("Screening heuristic only — not a price target. Confirm on chart before entry.")
    print(f"{'='*100}\n")

    signal_date = parse_odin_date(src)

    for _, r in shortlist.iterrows():
        print(f"#{r['Rank']:<3} {r['Symbol']:<15} {str(r['Stock Name'])[:35]:<35} "
              f"Score:{r['Setup Score']:<5.0f} {r['Setup Type']:<35} "
              f"Price:{r['Price']:<10.2f} %Chg:{r['% Chg']:+.2f}")
        print(f"      Sector: {r['Sector']}")
        print(f"      {r['Reasoning']}")

        tgt, stp = r.get("Target Price"), r.get("Stop Loss Price")
        if pd.notna(tgt) and pd.notna(stp):
            basis = " [ATR-based]" if r.get("Stop Basis") == "ATR" else " [flat, no ATR]"
            print(f"      Plan: entry ₹{r['Price']:.2f} | target ₹{tgt:.2f} (+{r['Target %']:.1f}%) | "
                  f"stop ₹{stp:.2f} (-{r['Stop %']:.1f}%) | R:R 1:{r['Reward:Risk']:.1f}{basis}")

        cur_date, cur_price = r.get("Current Price Date"), r.get("Current Price")
        # Only meaningful when scoring an older sheet: the latest post-signal
        # bhavcopy date is genuinely after the signal date.
        if pd.notna(cur_price) and pd.notna(cur_date) and (
            signal_date is None or pd.Timestamp(cur_date) > pd.Timestamp(signal_date)
        ):
            pct = r.get("% Chg Since Signal")
            pct_str = f"{pct:+.1f}%" if pd.notna(pct) else "n/a"
            print(f"      Since signal ({signal_date.date() if signal_date else '?'}): "
                  f"₹{r['Price']:.2f} -> ₹{cur_price:.2f} as of {pd.Timestamp(cur_date).date()} ({pct_str})")

            peak_price, peak_date = r.get("Peak Price Since Signal"), r.get("Peak Price Date")
            to_peak = r.get("% Chg To Peak")
            if pd.notna(peak_price) and pd.notna(peak_date):
                peak_pct_str = f"{to_peak:+.1f}%" if pd.notna(to_peak) else "n/a"
                print(f"      Peak since signal: ₹{peak_price:.2f} on "
                      f"{pd.Timestamp(peak_date).date()} ({peak_pct_str} from signal)")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
