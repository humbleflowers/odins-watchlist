"""
Backfill missing NSE bhavcopy files in the local archive.

Why: the OHLC indicator engine (technical_indicators.py) is only as good as
the contiguous price history behind it. The archive has a 4-month collection
gap (2026-03-13 -> 2026-07-06) plus two small May/June-2025 holes, and starts
only at 2025-05-09 -- so long-window indicators (SMA200, Minervini trend
template, stable 52-week levels) are unusable for almost every row. NSE keeps
historical bhavcopies online, so the fix is mechanical: download the missing
trading days.

What it does:
  1. Discovers every bhavcopy date already present (root + working_version
     delivery dirs, same discovery as technical_indicators.py).
  2. Targets every missing weekday from --extend-back-to (default: the
     earliest existing date, i.e. interior gaps only) through the latest
     existing date.
  3. Downloads each via the same download_bhavcopy() the daily pipeline uses,
     into the ROOT archive dir, and applies the same rename_columns()
     normalization so files are byte-compatible with pipeline output.
  4. Optionally ingests each file into MongoDB (nse_delivery collection) via
     the pipeline's own insert_to_collections (date-deduped), so delivery-%
     features benefit too. --no-mongo to skip.

Failures are expected for exchange holidays (NSE publishes nothing those
days -> HTTP 404); they're reported but not retried.

Usage:
    python backfill_bhavcopy.py --dry-run                     # show the plan
    python backfill_bhavcopy.py                               # fill interior gaps
    python backfill_bhavcopy.py --extend-back-to 2024-07-01   # + deepen history
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_nse_delivery import download_bhavcopy, rename_columns  # noqa: E402
from technical_indicators import discover_bhavcopy_files, ROOT_DIR  # noqa: E402

ROOT_DELIVERY_DIR = ROOT_DIR / "nse_downloads" / "delivery"


def missing_weekdays(existing: set, start: pd.Timestamp, end: pd.Timestamp) -> list:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in existing:
            days.append(d)
        d += timedelta(days=1)
    return days


def summarize_ranges(dates: list) -> str:
    """Compact 'a..b (n)' run-length summary of a sorted date list."""
    if not dates:
        return "(none)"
    runs, run_start, prev = [], dates[0], dates[0]
    for d in dates[1:]:
        if (d - prev).days > 7:  # new run if more than a week apart
            runs.append((run_start, prev))
            run_start = d
        prev = d
    runs.append((run_start, prev))
    return ", ".join(f"{a.date()}..{b.date()}" for a, b in runs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill missing NSE bhavcopy files")
    ap.add_argument("--extend-back-to", default=None, metavar="YYYY-MM-DD",
                    help="Also fetch history earlier than the current archive start "
                         "(unlocks SMA200/trend-template for earlier backtest dates)")
    ap.add_argument("--delay", type=float, default=0.75,
                    help="Seconds between requests (politeness; default 0.75)")
    ap.add_argument("--no-mongo", action="store_true",
                    help="Skip MongoDB ingestion; only write CSV files")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan, download nothing")
    args = ap.parse_args()

    existing = set(discover_bhavcopy_files().keys())
    if not existing:
        print("No existing bhavcopy files found -- nothing to anchor a backfill to.")
        return 1
    lo, hi = min(existing), max(existing)

    start = pd.Timestamp(args.extend_back_to) if args.extend_back_to else lo
    targets = missing_weekdays(existing, start, hi)

    print(f"Archive now: {len(existing)} dates, {lo.date()} -> {hi.date()}")
    print(f"Backfill window: {start.date()} -> {hi.date()}")
    print(f"Missing weekdays to attempt: {len(targets)}")
    print(f"  ranges: {summarize_ranges(targets)}")
    if args.dry_run or not targets:
        return 0

    insert = None
    if not args.no_mongo:
        try:
            from dump_table_to_collection import insert_to_collections
            from config import MONGO_URI, DB_NAME, COL_DELIVERY
            insert = lambda p: insert_to_collections(MONGO_URI, DB_NAME, COL_DELIVERY, p)  # noqa: E731
        except Exception as exc:
            print(f"[WARN] Mongo ingestion unavailable ({exc}); continuing files-only.")

    ok, failed = [], []
    for i, d in enumerate(targets, 1):
        ddmmyyyy = d.strftime("%d%m%Y")
        filename = download_bhavcopy(ddmmyyyy, ROOT_DELIVERY_DIR)
        if filename:
            path = str(ROOT_DELIVERY_DIR / filename)
            try:
                rename_columns(path)  # normalize SYMBOL->Symbol etc., matching pipeline files
            except Exception as exc:
                print(f"[WARN] normalize failed for {filename}: {exc}")
            if insert:
                try:
                    insert(path)
                except Exception as exc:
                    print(f"[WARN] Mongo insert failed for {filename}: {exc}")
            ok.append(d)
        else:
            failed.append(d)
        if i % 25 == 0:
            print(f"  ... {i}/{len(targets)} attempted ({len(ok)} downloaded)")
        time.sleep(args.delay)

    print("\n" + "=" * 78)
    print(f"Downloaded {len(ok)} files; {len(failed)} dates unavailable "
          f"(expected for exchange holidays).")
    if failed:
        print(f"Unavailable: {summarize_ranges(sorted(failed))}")
    print("Next: rerun technical_indicators.py to rebuild the indicator panel "
          "on the extended history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
