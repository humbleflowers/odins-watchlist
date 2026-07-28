"""
Paper-trading ledger for Odin's Watchlist.

Turns the daily dashboard shortlist into a tracked, self-updating paper book so
you can see how the tool's OWN picks actually perform over time - overall and
PER LENS (persistent leader / episodic pivot / emerging / fresh breakout /
RIGHTWAY admin call). This is the measurement backbone: every future idea can
be judged on live picks, not just the historical backtest.

It is honest by construction:
  - Entries are snapshotted from dashboard.html (the actionable shortlist) with
    each pick's entry price, volatility-sized target & stop, and lens tags.
  - Outcomes are resolved from the official delivery bhavcopy (daily high/low),
    first-hit target vs stop, with a 20-trading-day time stop. If both target
    and stop are touched the same day, the STOP is assumed first (pessimistic).
  - Nothing is invented; open trades stay OPEN with an unrealized mark.

Usage (typically run end-of-day, after make_dashboard.py):
    python paper_ledger.py                 # add today's picks, update, report
    python paper_ledger.py --add-only      # just snapshot today's shortlist
    python paper_ledger.py --report        # just print performance
    python paper_ledger.py --dashboard dashboard.html --top 20
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

WORKING_DIR = Path(__file__).resolve().parent
LEDGER = WORKING_DIR / "paper_ledger.csv"
DELIV_DIRS = [WORKING_DIR / "nse_downloads" / "delivery",
              WORKING_DIR.parent / "nse_downloads" / "delivery"]
TIME_STOP_BARS = 20            # exit at close after this many trading days if unresolved
VALID_SERIES = {"EQ", "BE", "BZ"}

LEDGER_COLS = [
    "entry_date", "symbol", "name", "entry_price", "target", "target_pct",
    "stop", "stop_pct", "score", "rs", "tier", "persistent", "ep", "emerging",
    "fresh", "rw_admin", "status", "exit_date", "exit_price", "realized_pct",
    "peak_pct", "days_held", "last_price", "last_update",
]


# --------------------------------------------------------------------------- #
# Reading the dashboard shortlist
# --------------------------------------------------------------------------- #
def load_dashboard(path: Path) -> tuple[list[dict], str]:
    """Extract the embedded DATA array and sheet date from dashboard.html."""
    html = path.read_text()
    s = html.index("const DATA = ") + len("const DATA = ")
    e = html.index("const META = ")
    data = json.loads(html[s:e].rstrip().rstrip(";\n").rstrip(";"))
    m = re.search(r'"sheetDate"\s*:\s*"([^"]+)"', html[e:e + 400])
    sheet = m.group(1) if m else datetime.today().strftime("%d %b %Y")
    return data, sheet


def picks_from_dashboard(data: list[dict], sheet: str, top: int) -> pd.DataFrame:
    d = pd.to_datetime(sheet, errors="coerce")
    entry_date = (d if pd.notna(d) else pd.Timestamp.today()).normalize()
    rows = []
    for r in data[:top]:
        if not r.get("price") or not r.get("target") or not r.get("stop"):
            continue
        rows.append({
            "entry_date": entry_date.date().isoformat(),
            "symbol": r["sym"], "name": r.get("name", ""),
            "entry_price": float(r["price"]),
            "target": float(r["target"]), "target_pct": float(r.get("targetPct") or 0),
            "stop": float(r["stop"]), "stop_pct": float(r.get("stopPct") or 0),
            "score": r.get("score"), "rs": r.get("rs"), "tier": r.get("tier", ""),
            "persistent": bool(r.get("persistent")), "ep": bool(r.get("ep")),
            "emerging": bool(r.get("emerging")), "fresh": bool(r.get("fresh")),
            "rw_admin": bool(r.get("rwAdmin") or r.get("rwTag") or r.get("rwTarget")),
            "status": "OPEN", "exit_date": "", "exit_price": "", "realized_pct": "",
            "peak_pct": 0.0, "days_held": 0, "last_price": float(r["price"]),
            "last_update": "",
        })
    return pd.DataFrame(rows, columns=LEDGER_COLS)


# --------------------------------------------------------------------------- #
# Price history from delivery bhavcopy (for outcome resolution)
# --------------------------------------------------------------------------- #
def _deliv_files() -> dict[pd.Timestamp, Path]:
    out = {}
    for d in DELIV_DIRS:
        for f in glob.glob(str(d / "sec_bhavdata_full_*.csv")):
            m = re.search(r"(\d{8})\.csv$", f)
            if m:
                dt = pd.to_datetime(m.group(1), format="%d%m%Y")
                out.setdefault(dt, Path(f))
    return out


def load_price_paths(since: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """{symbol -> DataFrame[Date, High, Low, Close]} for trading days >= since."""
    files = {dt: p for dt, p in _deliv_files().items() if dt >= since}
    frames = []
    for dt, p in sorted(files.items()):
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        df.columns = [c.strip().upper() for c in df.columns]   # NSE files vary in case / leading spaces
        need = {"SYMBOL", "SERIES", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE"}
        if not need.issubset(df.columns):
            continue
        df["SERIES"] = df["SERIES"].astype(str).str.strip()
        df = df[df["SERIES"].isin(VALID_SERIES)].copy()
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
        df["Date"] = dt
        frames.append(df[["SYMBOL", "Date", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE"]])
    if not frames:
        return {}
    allp = pd.concat(frames, ignore_index=True)
    allp.columns = ["SYMBOL", "Date", "High", "Low", "Close"]
    for c in ("High", "Low", "Close"):
        allp[c] = pd.to_numeric(allp[c], errors="coerce")
    return {s: g.sort_values("Date") for s, g in allp.groupby("SYMBOL")}


def resolve(row: pd.Series, paths: dict[str, pd.DataFrame]) -> dict:
    """Walk the forward path; return updated status/exit/realized/peak fields."""
    g = paths.get(row["symbol"])
    entry_dt = pd.to_datetime(row["entry_date"])
    entry = float(row["entry_price"])
    tgt, stp = float(row["target"]), float(row["stop"])
    upd = {"peak_pct": float(row.get("peak_pct") or 0), "days_held": 0,
           "last_price": entry, "last_update": datetime.today().date().isoformat(),
           "status": "OPEN", "exit_date": "", "exit_price": "", "realized_pct": ""}
    if g is None:
        return upd
    fwd = g[g["Date"] > entry_dt]
    if fwd.empty:
        return upd
    peak = float(row.get("peak_pct") or 0)
    for i, (_, bar) in enumerate(fwd.iterrows(), 1):
        hi, lo, cl = bar["High"], bar["Low"], bar["Close"]
        if pd.notna(hi):
            peak = max(peak, (hi / entry - 1) * 100)
        upd["days_held"] = i
        upd["last_price"] = float(cl) if pd.notna(cl) else upd["last_price"]
        hit_stop = pd.notna(lo) and lo <= stp
        hit_tgt = pd.notna(hi) and hi >= tgt
        if hit_stop:                       # pessimistic: stop first if both touched
            upd.update(status="STOPPED", exit_date=bar["Date"].date().isoformat(),
                       exit_price=round(stp, 2), realized_pct=round((stp / entry - 1) * 100, 1))
            break
        if hit_tgt:
            upd.update(status="TARGET", exit_date=bar["Date"].date().isoformat(),
                       exit_price=round(tgt, 2), realized_pct=round((tgt / entry - 1) * 100, 1))
            break
        if i >= TIME_STOP_BARS:
            upd.update(status="EXPIRED", exit_date=bar["Date"].date().isoformat(),
                       exit_price=round(float(cl), 2) if pd.notna(cl) else "",
                       realized_pct=round((cl / entry - 1) * 100, 1) if pd.notna(cl) else "")
            break
    upd["peak_pct"] = round(peak, 1)
    return upd


# --------------------------------------------------------------------------- #
# Ledger ops
# --------------------------------------------------------------------------- #
def read_ledger() -> pd.DataFrame:
    if LEDGER.exists():
        return pd.read_csv(LEDGER)
    return pd.DataFrame(columns=LEDGER_COLS)


def add_picks(led: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append picks, skipping a symbol that already has an OPEN position."""
    open_syms = set(led[led["status"] == "OPEN"]["symbol"]) if len(led) else set()
    fresh = new[~new["symbol"].isin(open_syms)]
    added = len(fresh)
    out = pd.concat([led, fresh], ignore_index=True)
    print(f"Added {added} new picks ({len(new) - added} skipped - already open).")
    return out


def update_open(led: pd.DataFrame) -> pd.DataFrame:
    open_rows = led[led["status"] == "OPEN"]
    if open_rows.empty:
        print("No open positions to update.")
        return led
    since = pd.to_datetime(open_rows["entry_date"]).min()
    paths = load_price_paths(since)
    n_res = 0
    for idx, row in open_rows.iterrows():
        upd = resolve(row, paths)
        for k, v in upd.items():
            led.at[idx, k] = v
        if upd["status"] != "OPEN":
            n_res += 1
    print(f"Updated {len(open_rows)} open positions; {n_res} newly resolved.")
    return led


def report(led: pd.DataFrame) -> None:
    if led.empty:
        print("Ledger is empty."); return
    closed = led[led["status"].isin(["TARGET", "STOPPED", "EXPIRED"])].copy()
    openp = led[led["status"] == "OPEN"].copy()
    print("\n" + "=" * 60)
    print(f"PAPER LEDGER  -  {len(led)} picks   ({len(openp)} open, {len(closed)} closed)")
    print("=" * 60)
    if len(closed):
        closed["realized_pct"] = pd.to_numeric(closed["realized_pct"], errors="coerce")
        wr = (closed["status"] == "TARGET").mean() * 100
        exp = closed["realized_pct"].mean()
        wins = closed[closed["realized_pct"] > 0]["realized_pct"].sum()
        loss = -closed[closed["realized_pct"] <= 0]["realized_pct"].sum()
        pf = (wins / loss) if loss else float("inf")
        print(f"Closed: target {(closed['status']=='TARGET').sum()}, "
              f"stopped {(closed['status']=='STOPPED').sum()}, "
              f"expired {(closed['status']=='EXPIRED').sum()}")
        print(f"  win rate {wr:.0f}%   avg realized {exp:+.2f}%/trade   profit factor {pf:.2f}")
        print("\n  Per-lens realized performance (closed trades):")
        print(f"    {'lens':<20}{'n':>5}{'win%':>7}{'avg%':>8}")
        for lens, label in [("persistent", "persistent leader"), ("ep", "episodic pivot"),
                            ("emerging", "emerging"), ("fresh", "fresh breakout"),
                            ("rw_admin", "RIGHTWAY admin")]:
            sub = closed[closed[lens].astype(str).isin(["True", "TRUE", "1", "1.0"])]
            if len(sub):
                w = (sub["status"] == "TARGET").mean() * 100
                print(f"    {label:<20}{len(sub):>5}{w:>6.0f}%{sub['realized_pct'].mean():>+7.1f}%")
        plain = closed[~closed[["persistent", "ep", "emerging"]].astype(str)
                       .isin(["True", "TRUE", "1", "1.0"]).any(axis=1)]
        if len(plain):
            print(f"    {'(no lens)':<20}{len(plain):>5}{(plain['status']=='TARGET').mean()*100:>6.0f}%"
                  f"{plain['realized_pct'].mean():>+7.1f}%")
    if len(openp):
        openp["peak_pct"] = pd.to_numeric(openp["peak_pct"], errors="coerce")
        unreal = (pd.to_numeric(openp["last_price"], errors="coerce")
                  / pd.to_numeric(openp["entry_price"], errors="coerce") - 1) * 100
        print(f"\n  Open: {len(openp)}   avg unrealized {unreal.mean():+.2f}%   "
              f"avg peak reached {openp['peak_pct'].mean():+.1f}%")
    print()


def main():
    ap = argparse.ArgumentParser(description="Paper-trading ledger for Odin's Watchlist")
    ap.add_argument("--dashboard", default="dashboard.html", help="dashboard HTML to snapshot")
    ap.add_argument("--top", type=int, default=20, help="how many top shortlist names to record")
    ap.add_argument("--add-only", action="store_true")
    ap.add_argument("--update-only", action="store_true")
    ap.add_argument("--report", action="store_true", help="report only")
    args = ap.parse_args()

    led = read_ledger()
    do_all = not (args.add_only or args.update_only or args.report)

    if args.add_only or do_all:
        dpath = (WORKING_DIR / args.dashboard) if not Path(args.dashboard).is_absolute() else Path(args.dashboard)
        if dpath.exists():
            data, sheet = load_dashboard(dpath)
            led = add_picks(led, picks_from_dashboard(data, sheet, args.top))
        else:
            print(f"[warn] {dpath} not found - skipping add.")
    if args.update_only or do_all:
        led = update_open(led)
    if not args.report or do_all or args.update_only or args.add_only:
        led.to_csv(LEDGER, index=False)
    report(led)


if __name__ == "__main__":
    main()
