"""
Odin's Watchlist — daily pipeline.

Downloads Chartink screeners, NSE delivery/bulk/block data, ingests into
MongoDB, merges with sector mapping, and writes odin_DDMMYYYY.csv.

Usage:
    python run_odin.py                  # today
    python run_odin.py --yesterday      # previous trading day
    python run_odin.py --date 2026-07-08
    python run_odin.py --date 08072026
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import traceback
from functools import reduce

import pandas as pd

from config import (
    BASE_DIR,
    CHARTINK_CONFIG,
    CHARTINK_DOWNLOADS,
    CHROMEDRIVER_PATH,
    COL_BLOCK,
    COL_BULK,
    COL_CHARTINK,
    COL_DELIVERY,
    DB_NAME,
    MONGO_URI,
    NSE_DELIVERY_DIR,
    SECTORS_CSV,
    date_strings,
    parse_date_arg,
    today_strings,
)
from connect_to_mongo import connect_mongo
from download_chartink_screeners import process_chartink_download
from download_nse_deals import download_bulk_block_data
from download_nse_delivery import start_download_delivery
from dump_table_to_collection import insert_to_collections
from get_dates import previous_trading_day
from merge_previous_chartink import compare_with_previous_day, get_recent_dates
from parse_todays_deals import fetch_symbol_summary_by_date
from parse_todays_delivery import (
    calculate_delivery_averages,
    fetch_last_7_days_delivery_data,
)


def _step(label: str) -> None:
    print(f"\n{'='*60}\n{label}\n{'='*60}")


def download_and_ingest(dates: dict) -> None:
    """Download all data sources and insert into MongoDB."""
    iso, ddmmyyyy = dates["iso"], dates["ddmmyyyy"]

    _step(f"1/3  Chartink screeners ({iso})")
    merged = process_chartink_download(
        CHROMEDRIVER_PATH,
        str(CHARTINK_CONFIG),
        str(CHARTINK_DOWNLOADS),
        iso,
    )
    insert_to_collections(MONGO_URI, DB_NAME, COL_CHARTINK, merged)

    _step(f"2/3  NSE delivery bhavcopy ({ddmmyyyy})")
    delivery_file = start_download_delivery(ddmmyyyy, str(NSE_DELIVERY_DIR))
    insert_to_collections(MONGO_URI, DB_NAME, COL_DELIVERY, delivery_file)

    _step(f"3/3  NSE bulk & block deals ({ddmmyyyy})")
    deals = download_bulk_block_data(ddmmyyyy)
    insert_to_collections(MONGO_URI, DB_NAME, COL_BULK, deals["bulk"])
    insert_to_collections(MONGO_URI, DB_NAME, COL_BLOCK, deals["block"])


def fetch_and_merge(dates: dict) -> pd.DataFrame:
    """Query MongoDB, merge all sources, filter, and attach sector data."""
    ddmmyyyy = dates["ddmmyyyy"]
    target_dt = dates["dt_utc"]
    chartink_cutoff = dates["dt_naive"]

    _step(f"Fetching data from MongoDB ({dates['iso']})")

    chartink_col = connect_mongo(MONGO_URI, DB_NAME, COL_CHARTINK)
    recent = get_recent_dates(chartink_col, limit=2, on_or_before=chartink_cutoff)
    if len(recent) < 2:
        raise ValueError(
            f"Need at least two Chartink dates on or before {dates['iso']} in MongoDB"
        )
    chartink_df = compare_with_previous_day(chartink_col, recent)
    print(f"Chartink: {len(chartink_df)} symbols ({recent[0]} vs {recent[1]})")

    delivery_col = connect_mongo(MONGO_URI, DB_NAME, COL_DELIVERY)
    delivery_df = calculate_delivery_averages(delivery_col, target_dt)
    last_week = fetch_last_7_days_delivery_data(delivery_col)
    if not last_week.empty:
        delivery_df = pd.merge(delivery_df, last_week, on="Symbol", how="left")
    print(f"Delivery: {len(delivery_df)} symbols")

    bulk_df = fetch_symbol_summary_by_date(
        connect_mongo(MONGO_URI, DB_NAME, COL_BULK), target_dt
    )
    print(f"Bulk deals: {len(bulk_df)} symbols")

    block_df = fetch_symbol_summary_by_date(
        connect_mongo(MONGO_URI, DB_NAME, COL_BLOCK), target_dt
    )
    print(f"Block deals: {len(block_df)} symbols")

    dfs = [df for df in (chartink_df, delivery_df, bulk_df, block_df) if not df.empty]
    if not dfs:
        raise RuntimeError("No data available to merge")

    merged = reduce(
        lambda left, right: pd.merge(left, right, on="Symbol", how="outer"), dfs
    )

    if "% Chg" in merged.columns:
        merged = merged[merged["% Chg"].notna() & (merged["% Chg"] != "")]

    sectors = pd.read_csv(SECTORS_CSV)
    merged = pd.merge(merged, sectors, on="Symbol", how="inner")

    output = BASE_DIR / f"odin_{ddmmyyyy}.csv"
    merged.to_csv(output, index=False)
    print(f"\nOutput written: {output} ({len(merged)} rows)")
    return merged


def _run_script(script: str, *script_args: str) -> bool:
    """Run a downstream script with the same interpreter. Returns True on success."""
    cmd = [sys.executable, str(BASE_DIR / script), *script_args]
    print(f"\n$ {' '.join(cmd[1:])}")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print(f"[warn] {script} exited {result.returncode}")
        return False
    return True


def run_downstream(dates: dict) -> None:
    """Score the sheet, refresh the paper ledger, build the dashboard (with the
    P&L panel), then snapshot today's shortlist into the ledger. Each step is
    best-effort: a failure is reported but does not fail the data pipeline."""
    ddmmyyyy = dates["ddmmyyyy"]
    odin_csv = f"odin_{ddmmyyyy}.csv"

    _step("Scoring candidates (find_swing_candidates.py)")
    if not _run_script("find_swing_candidates.py", "--file", odin_csv):
        print("[warn] scoring failed - skipping dashboard & ledger.")
        return

    _step("Refreshing paper ledger — open positions (paper_ledger.py --update-only)")
    _run_script("paper_ledger.py", "--update-only")

    _step("Building dashboard with P&L panel (make_dashboard.py)")
    if not _run_script("make_dashboard.py"):
        print("[warn] dashboard build failed - skipping ledger snapshot.")
        return

    _step("Snapshotting today's shortlist into the ledger (paper_ledger.py --add-only)")
    _run_script("paper_ledger.py", "--add-only")


def resolve_dates(args: argparse.Namespace) -> dict:
    """Determine target pipeline date from CLI arguments."""
    if args.yesterday:
        target = previous_trading_day()
        label = "previous trading day"
    elif args.date:
        target = parse_date_arg(args.date)
        label = "specified date"
    else:
        return today_strings()

    dates = date_strings(target)
    print(f"Target: {label} → {dates['iso']} ({dates['ddmmyyyy']})")
    return dates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Odin's Watchlist daily pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python run_odin.py                      run for today
  python run_odin.py --yesterday          run for last NSE trading day
  python run_odin.py --date 2026-07-08    run for a specific date
  python run_odin.py --date 08072026      same, DDMMYYYY format
        """,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--yesterday",
        action="store_true",
        help="Download data for the previous NSE trading day (skips weekends/holidays)",
    )
    group.add_argument(
        "--date",
        metavar="DATE",
        help="Target date as YYYY-MM-DD or DDMMYYYY",
    )
    parser.add_argument(
        "--no-downstream",
        action="store_true",
        help="Only build odin_*.csv; skip scoring, dashboard, and paper ledger",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dates = resolve_dates(args)
    print(f"Odin's Watchlist — {dates['iso']}")

    try:
        download_and_ingest(dates)
        fetch_and_merge(dates)
        if not args.no_downstream:
            run_downstream(dates)
        return 0
    except Exception as exc:
        print(traceback.format_exc())
        print(f"\nPipeline failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
