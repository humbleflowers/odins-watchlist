"""
Build a self-contained HTML triage dashboard from the latest swing-candidates CSV.

Reads swing_candidates_DDMMYYYY.csv (latest by date, or --file), joins the NSE
settlement series (to flag BE/BZ Trade-to-Trade names), computes a conviction
tier per pick from the evidence-backed triage checklist, and writes a single
dashboard.html with everything embedded -- open it in any browser, no server.

Tier checklist (each pick scored on six checks; documented in the page's help):
  1. Relative strength >= +15pp vs market      (strongest tested signal)
  2. Fresh breakout trigger fired TODAY
  3. Volume confirmation (elevated / top-decile)
  4. Healthy move today (+1..8%, not extended)
  5. Not lagging the market (RS >= 0)
  6. Mainstream EQ series (not BE/BZ trade-to-trade)
  Tier A = 5+ checks incl. RS>=15 and fresh breakout; Tier B = 3-4; Tier C = 0-2.

Usage:
    python make_dashboard.py                 # latest swing_candidates_*.csv
    python make_dashboard.py --file swing_candidates_21072026.csv --out dashboard.html
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_swing_candidates import normalize_screener, categorize_screener  # noqa: E402
from technical_indicators import discover_bhavcopy_files  # noqa: E402

WORKING_DIR = Path(__file__).resolve().parent
TOP_N = 60  # rows embedded in the page

# --- Wyckoff phase mapping (screener names -> Wyckoff structural events) ---
# SOS  = Sign of Strength / "jumping the creek": breakout from a trading range.
# SUP  = support/retracement zone where a Last Point of Support (LPS) forms.
# BASE = volatility contraction = the "cause" building inside the range.
WYCKOFF_SOS_SCREENERS = {
    "RT_SIDEWAY_BREAKOUT", "RT_Range_Breakout_with_Volume",
    "RT_Master_Resistance_Breakout_Screener", "DARVAS", "RT_52W_BREAKOUT",
    "RT_SHORT_TO_MID_RANGE_BREAKOUT",
}
WYCKOFF_SUPPORT_SCREENERS = {"RT_MAJOR_SUPPORT", "RT_Fib_0.7_to_0.88_Zone"}
WYCKOFF_BASE_SCREENERS = {"RT_Daily_Contraction", "VCP"}


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def find_latest_candidates() -> Path:
    files = glob.glob(str(WORKING_DIR / "swing_candidates_*.csv"))
    if not files:
        raise FileNotFoundError("No swing_candidates_*.csv found -- run find_swing_candidates.py first.")

    def d(p):
        m = re.search(r"swing_candidates_(\d{8})\.csv$", p)
        return datetime.strptime(m.group(1), "%d%m%Y") if m else datetime.min

    return Path(max(files, key=d))


def load_persistence(sheet_date) -> tuple[dict, list]:
    """Per-symbol RS-leadership history over the trailing 20 sheets, from the
    OHLC panel. Validated: within the RS-top-10, persistent leaders (leader on
    >=15 of prior 20) hit target ~24% vs ~15% for freshly-emerged ones -- so
    persistence is a real secondary conviction signal, surfaced here as a
    20-day presence strip + cohort flags (not folded into the score, which RS
    already ranks well)."""
    panel = WORKING_DIR / "backtest_output" / "ohlc_indicator_panel.csv"
    if not panel.exists():
        return {}, []
    ind = pd.read_csv(panel, usecols=["Symbol", "Date", "rs_vs_market", "Open", "High",
                                      "Low", "Close", "rel_volume", "sma50"],
                      low_memory=False)
    ind["Date"] = pd.to_datetime(ind["Date"])
    dates = sorted(d for d in ind["Date"].unique() if d <= pd.Timestamp(sheet_date))[-20:]
    if not dates:
        return {}, []
    sub = ind[ind["Date"].isin(dates)]
    latest = dates[-1]
    per = {}
    for sym, grp in sub.groupby("Symbol"):
        grp = grp.sort_values("Date")
        dmap = dict(zip(grp["Date"], grp["rs_vs_market"]))
        # 2 = RS-leader (>=15), 1 = present but not leading, 0 = absent/unknown
        strip = [(2 if (d in dmap and pd.notna(dmap[d]) and dmap[d] >= 15)
                  else 1 if (d in dmap and pd.notna(dmap[d])) else 0) for d in dates]
        leader_days = sum(1 for x in strip if x == 2)
        streak = 0
        for x in reversed(strip):
            if x == 2:
                streak += 1
            else:
                break
        prior_leader = sum(1 for x in strip[:-1] if x == 2)

        # Episodic Pivot on the latest available date: gap-up vs prior close, on
        # elevated volume. Validated as a secondary lens (EP within RS-top-10
        # hit ~23% vs ~17%). Live threshold: gap >= 4% and volume >= 3x normal.
        # FOLLOW-THROUGH (F6, backtested +7pp): an EP that CLOSES in the top half
        # of the day's range (held the gap) hit target ~19% vs ~12% for one that
        # faded to the bottom half. So a held gap is the actionable EP; a faded
        # gap is flagged separately and downgraded.
        gap = rvol = np.nan
        close_pos = np.nan
        rows = grp[grp["Date"] == latest]
        if not rows.empty:
            r0 = rows.iloc[0]
            prev = grp[grp["Date"] < latest]
            if not prev.empty and pd.notna(r0["Open"]):
                pc = prev.iloc[-1]["Close"]
                if pd.notna(pc) and pc > 0:
                    gap = (r0["Open"] / pc - 1) * 100
            rvol = r0["rel_volume"]
            hi, lo, cl = r0.get("High"), r0.get("Low"), r0.get("Close")
            if pd.notna(hi) and pd.notna(lo) and pd.notna(cl) and hi > lo:
                close_pos = (cl - lo) / (hi - lo)
        ep_raw = bool(pd.notna(gap) and gap >= 4 and pd.notna(rvol) and rvol >= 3)
        held = bool(pd.notna(close_pos) and close_pos >= 0.5)
        # actionable EP requires follow-through; if close_pos couldn't be computed
        # (missing OHLC), fall back to the raw gap so we don't silently drop it.
        ep = bool(ep_raw and (held or pd.isna(close_pos)))
        ep_fade = bool(ep_raw and pd.notna(close_pos) and not held)

        # Emerging / pre-ignition: a volume THRUST (>=3x) while relative
        # strength is rising but not yet extreme (0 < RS <= 25). Validated at
        # ~1.5x the base rate -- an EARLIER but lower-confidence entry than a
        # confirmed leader (~3x). Would have flagged STALLION on its first
        # thrust day, well before the obvious breakout.
        rs_vals = [dmap.get(d) for d in dates]
        rs_latest = rs_vals[-1] if rs_vals else None
        rs_5ago = rs_vals[-6] if len(rs_vals) >= 6 else (rs_vals[0] if rs_vals else None)
        rs_rising = (rs_latest is not None and rs_5ago is not None and rs_latest > rs_5ago)
        emerging = bool(pd.notna(rvol) and rvol >= 3 and rs_latest is not None
                        and 0 < rs_latest <= 25 and rs_rising)

        spark = [round(float(c), 2) for c in grp["Close"].tolist() if pd.notna(c)][-20:]

        per[sym] = {
            "strip": strip, "leaderDays": leader_days, "streak": streak,
            "persistent": leader_days >= 12, "newLeader": strip[-1] == 2 and prior_leader <= 2,
            "gap": round(float(gap), 1) if pd.notna(gap) else None,
            "relVol": round(float(rvol), 1) if pd.notna(rvol) else None,
            "closePos": round(float(close_pos), 2) if pd.notna(close_pos) else None,
            "ep": ep, "epFade": ep_fade, "emerging": emerging, "spark": spark,
        }

    # Market context (breadth): share of the whole universe above its 50-DMA on
    # the latest date, plus the benchmark's own 20-day drift. Shown as CONTEXT
    # only -- the backtest found a breadth FILTER would hurt, so we never gate on it.
    market = {}
    latest_rows = sub[sub["Date"] == latest]
    ab = latest_rows["Close"] > latest_rows["sma50"]
    valid = latest_rows["sma50"].notna() & latest_rows["Close"].notna()
    if valid.any():
        market = {
            "breadth": round(float((ab & valid).sum()) / float(valid.sum()) * 100),
            "universe": int(valid.sum()),
            "date": latest.strftime("%d %b %Y"),
        }
    return per, [d.strftime("%d %b") for d in dates], market


# Admin posts come from the broadcast channel (no individual sender -> numeric
# src). Those carry the structured "CMP / TARGET" calls; everyone else is
# member chatter. The admin's "important stock" markers, from the real data:
_ADMIN_SRC = re.compile(r"^-?\d+$")
_CONVICTION = re.compile(r"POSITIONAL|MULTIBAGGER|LONG\s*TERM|FOCUS", re.I)


def _extract_target(text: str):
    """Final numeric target from an admin call: 'TARGET-260 AND 300+' -> 300,
    'TARGET-2X' -> '2x'. Returns (final_target, is_multiplier)."""
    m = re.search(r"TARGET", text, re.I)
    if not m:
        return None, False
    tail = text[m.start(): m.start() + 130]
    nums = [float(x) for x in re.findall(r"\b([0-9]{2,6})\b", tail)]
    nums = [n for n in nums if n >= 10]
    if nums:
        return max(nums), False
    mult = re.search(r"([2-9])\s*X\b", tail, re.I)
    return (mult.group(1) + "x" if mult else None), bool(mult)


def load_group_mentions(sheet_date, syms: list, window_days: int = 45) -> dict:
    """Symbol -> mention stats from the Telegram corpus (telegram_messages.csv),
    for the given dashboard stocks only. Per-symbol exact ticker search avoids
    the free-text extraction noise. Separates ADMIN posts (the broadcast
    channel, which carries the real calls + targets + conviction tags) from
    member chatter.

    HONEST NOTE: this channel is heavily promotional/post-hoc (often posts
    winners AFTER they move), so a mention is 'they're talking about it', not
    a validated edge. The admin target is the admin's stated goal, not ours."""
    path = WORKING_DIR / "telegram_messages.csv"
    if not path.exists() or not syms:
        return {}
    msgs = pd.read_csv(path, header=None, skiprows=1,
                       names=["mid", "date", "src", "text", "gid"], dtype=str)
    msgs["dt"] = pd.to_datetime(msgs["date"], errors="coerce")
    sheet = pd.Timestamp(sheet_date)
    win = msgs[(msgs["dt"] >= sheet - pd.Timedelta(days=window_days)) & (msgs["dt"] <= sheet)]
    if win.empty:
        return {}
    pat = re.compile(r"(?<![A-Z0-9])(" + "|".join(re.escape(s) for s in syms) + r")(?![A-Z0-9])")
    out: dict = {}

    # Pass 1 -- recent mentions & buzz (any sender, the given window).
    for row in win.itertuples():
        hits = set(pat.findall(str(getattr(row, "text", "") or "")))
        for sym in hits:
            d = out.setdefault(sym, _blank())
            d["mentions"] += 1
            if d["last"] is None or row.dt > d["last"]:
                d["last"] = row.dt
            if _ADMIN_SRC.match(str(getattr(row, "src", "") or "")):
                d["adminMentions"] += 1

    # Pass 2 -- the admin's actual CALL (target / conviction). Search ALL admin
    # history (a positional call is referenced for months; the target-bearing
    # message is usually older than the recent brag posts). Keep the most
    # recent admin message that actually carries a target or a conviction tag.
    admin_all = msgs[msgs["src"].fillna("").str.match(r"^-?\d+$") & (msgs["dt"] <= sheet)]
    for row in admin_all.itertuples():
        text = str(getattr(row, "text", "") or "")
        if "TARGET" not in text.upper() and not _CONVICTION.search(text):
            continue
        hits = set(pat.findall(text)) & set(out)
        if not hits:
            continue
        target, _ = _extract_target(text)
        conv = _CONVICTION.search(text)
        tag = conv.group(0).upper() if conv else None
        for sym in hits:
            d = out[sym]
            if d["callDt"] is None or row.dt > d["callDt"]:
                d["callDt"] = row.dt
                d["target"] = target
                d["tag"] = tag

    for d in out.values():
        d["daysSince"] = int((sheet - d["last"]).days) if d["last"] is not None else None
        d["last"] = d["last"].strftime("%d %b") if d["last"] is not None else None
        d["callDays"] = int((sheet - d["callDt"]).days) if d["callDt"] is not None else None
        d["callDate"] = d["callDt"].strftime("%d %b '%y") if d["callDt"] is not None else None
    return out


def _blank() -> dict:
    return {"mentions": 0, "adminMentions": 0, "last": None,
            "callDt": None, "target": None, "tag": None}


def load_series_map() -> dict:
    """Symbol -> settlement series from the most recent bhavcopy file."""
    dated = discover_bhavcopy_files()
    latest = dated[max(dated)]
    df = pd.read_csv(latest, usecols=lambda c: c.strip() in ("Symbol", "SERIES"))
    df.columns = df.columns.str.strip()
    return dict(zip(df["Symbol"].astype(str).str.strip(),
                    df["SERIES"].astype(str).str.strip()))


def f(v, nd=2):
    """JSON-safe rounded float or None."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    return round(v, nd)


def load_prev_sheet(sheet_dt: datetime, current_path: Path) -> dict:
    """{symbol -> (rank, score, name)} from the most recent swing_candidates_*.csv
    BEFORE the current one -- powers the 'since yesterday' diff."""
    best_dt, best_path = None, None
    for fp in glob.glob(str(WORKING_DIR / "swing_candidates_*.csv")):
        p = Path(fp)
        if p.name == current_path.name:
            continue
        m = re.search(r"swing_candidates_(\d{8})\.csv$", p.name)
        if not m:
            continue
        dt = datetime.strptime(m.group(1), "%d%m%Y")
        if dt < sheet_dt and (best_dt is None or dt > best_dt):
            best_dt, best_path = dt, p
    if best_path is None:
        return {}
    try:
        pdf = pd.read_csv(best_path, usecols=lambda c: c.strip() in
                          ("Symbol", "Rank", "Setup Score", "Stock Name"))
    except Exception:
        return {}
    out = {}
    for r in pdf.to_dict(orient="records"):
        s = str(r.get("Symbol", ""))
        if s:
            out[s] = (int(r.get("Rank", 0) or 0), f(r.get("Setup Score"), 0),
                      str(r.get("Stock Name") or "")[:44])
    out["__date__"] = best_dt.strftime("%d %b")
    return out


def load_ledger_context() -> dict:
    """Read paper_ledger.csv -> per-symbol status, the open-position list, and
    today's alerts (recently resolved, or open & near target/stop)."""
    path = WORKING_DIR / "paper_ledger.csv"
    empty = {"bySym": {}, "open": [], "alerts": []}
    if not path.exists():
        return empty
    try:
        d = pd.read_csv(path)
    except Exception:
        return empty
    if d.empty:
        return empty
    by_sym, open_rows, alerts = {}, [], []
    last_update = str(d["last_update"].dropna().max()) if "last_update" in d.columns else ""
    for r in d.to_dict(orient="records"):
        sym = str(r.get("symbol", ""))
        status = str(r.get("status", ""))
        entry = r.get("entry_price"); last = r.get("last_price")
        unreal = round((last / entry - 1) * 100, 1) if (pd.notna(entry) and pd.notna(last) and entry) else None
        info = {"status": status, "entryDate": str(r.get("entry_date", "")),
                "unreal": unreal, "peak": f(r.get("peak_pct"), 1),
                "days": int(r.get("days_held") or 0), "realized": f(r.get("realized_pct"), 1)}
        by_sym[sym] = info
        if status == "OPEN":
            tgt, stp = r.get("target"), r.get("stop")
            open_rows.append({
                "sym": sym, "name": str(r.get("name") or "")[:40],
                "entryDate": info["entryDate"], "entry": f(entry), "target": f(tgt),
                "stop": f(stp), "last": f(last), "unreal": unreal, "peak": info["peak"],
                "days": info["days"],
                "persistent": bool(r.get("persistent")), "ep": bool(r.get("ep")),
            })
            if pd.notna(last) and pd.notna(tgt) and pd.notna(stp) and last:
                if (tgt - last) / last <= 0.03:
                    alerts.append({"sym": sym, "kind": "near-target",
                                   "detail": f"{unreal:+.1f}% held, ~{round((tgt/last-1)*100)}% to target"})
                elif (last - stp) / last <= 0.03:
                    alerts.append({"sym": sym, "kind": "near-stop",
                                   "detail": f"{unreal:+.1f}%, near stop"})
        elif status in ("TARGET", "STOPPED", "EXPIRED") and str(r.get("exit_date", "")) and \
                str(r.get("last_update", "")) == last_update:
            alerts.append({"sym": sym, "kind": status.lower(),
                           "detail": f"{info['realized']:+.1f}% on {r.get('exit_date')}"})
    return {"bySym": by_sym, "open": open_rows, "alerts": alerts}


def build_rows(csv_path: Path) -> tuple[list, dict]:
    df = pd.read_csv(csv_path)
    series_map = load_series_map()
    total_gated = len(df)

    dm = re.search(r"swing_candidates_(\d{8})\.csv$", csv_path.name)
    sheet_dt = datetime.strptime(dm.group(1), "%d%m%Y") if dm else datetime.now()
    persist, strip_dates, market = load_persistence(sheet_dt)
    prev = load_prev_sheet(sheet_dt, csv_path)
    ledger_ctx = load_ledger_context()
    prev_syms = {k for k in prev if not k.startswith("__")}
    top_syms = [str(s) for s in df.head(TOP_N)["Symbol"].dropna().tolist()]
    mentions = load_group_mentions(sheet_dt, top_syms)

    out = []
    for rec in df.head(TOP_N).to_dict(orient="records"):
        sym = str(rec.get("Symbol", ""))
        reasoning = str(rec.get("Reasoning") or "")
        fresh_list = [s.strip() for s in str(rec.get("Fresh Trigger Today") or "").split(",") if s.strip()]
        rs = rec.get("RS vs Market (20d)")
        rs = None if pd.isna(rs) else float(rs)
        chg = rec.get("% Chg")
        chg = None if pd.isna(chg) else float(chg)
        series = series_map.get(sym, "?")
        pdata = persist.get(sym, {})

        fresh_breakout = any(categorize_screener(normalize_screener(s)) == "breakout" for s in fresh_list)
        vol_ok = ("top-decile volume" in reasoning) or ("elevated volume" in reasoning)
        inst = "bulk/block BUY > SELL" in reasoning
        healthy = chg is not None and 1 <= chg <= 8
        extended = chg is not None and chg > 15
        lagging = rs is not None and rs < 0
        t2t = series in ("BE", "BZ")
        rs_strong = rs is not None and rs >= 15

        # Wyckoff phase detection from the stock's screener membership
        all_screeners = set()
        for col in ("Breakout Patterns", "Momentum Patterns", "Support/Base Patterns"):
            all_screeners.update(s.strip() for s in str(rec.get(col) or "").split(",") if s.strip())
        fresh_set = set(fresh_list)
        sos_today = bool(fresh_set & WYCKOFF_SOS_SCREENERS)          # the "jump" is happening now
        at_support = bool(all_screeners & WYCKOFF_SUPPORT_SCREENERS)  # in an LPS zone
        contracting = bool(all_screeners & WYCKOFF_BASE_SCREENERS)    # cause building

        # SOS entry: range breakout firing today, on volume, leading, not yet extended
        wyckoff_sos = (sos_today and vol_ok and not extended and rs is not None and rs >= 5)
        # LPS entry: a leader resting quietly at support (low-volume backup), not breaking out today
        wyckoff_lps = (at_support and not sos_today and not extended
                       and rs is not None and rs >= 5
                       and chg is not None and -4 <= chg <= 3)
        if wyckoff_sos:
            phase = "SOS"
        elif wyckoff_lps:
            phase = "LPS"
        elif contracting and not sos_today:
            phase = "Base"
        else:
            phase = ""

        checks = sum([rs_strong, fresh_breakout, vol_ok,
                      healthy and not extended, not lagging, not t2t])
        if checks >= 5 and rs_strong and fresh_breakout:
            tier = "A"
        elif checks >= 3:
            tier = "B"
        else:
            tier = "C"

        out.append({
            "rank": int(rec.get("Rank", 0)),
            "sym": sym,
            "name": str(rec.get("Stock Name") or "")[:44],
            "sector": str(rec.get("Sector") or ""),
            "score": f(rec.get("Setup Score"), 0),
            "rs": f(rs, 1),
            "chg": f(chg, 2),
            "price": f(rec.get("Price")),
            "target": f(rec.get("Target Price")),
            "targetPct": f(rec.get("Target %"), 1),
            "stop": f(rec.get("Stop Loss Price")),
            "stopPct": f(rec.get("Stop %"), 1),
            "rr": f(rec.get("Reward:Risk"), 1),
            "basis": str(rec.get("Stop Basis") or ""),
            "series": series,
            "tier": tier,
            "checks": int(checks),
            "fresh": fresh_breakout,
            "freshList": ", ".join(fresh_list),
            "volOk": vol_ok,
            "inst": inst,
            "healthy": healthy,
            "extended": extended,
            "lagging": lagging,
            "t2t": t2t,
            "wSOS": wyckoff_sos,
            "wLPS": wyckoff_lps,
            "phase": phase,
            "strip": pdata.get("strip", []),
            "leaderDays": pdata.get("leaderDays", 0),
            "streak": pdata.get("streak", 0),
            "persistent": pdata.get("persistent", False),
            "newLeader": pdata.get("newLeader", False),
            "ep": pdata.get("ep", False),
            "epFade": pdata.get("epFade", False),
            "emerging": pdata.get("emerging", False),
            "gap": pdata.get("gap"),
            "relVol": pdata.get("relVol"),
            "closePos": pdata.get("closePos"),
            "rwMentions": mentions.get(sym, {}).get("mentions", 0),
            "rwDays": mentions.get(sym, {}).get("daysSince"),
            "rwLast": mentions.get(sym, {}).get("last"),
            "rwAdmin": mentions.get(sym, {}).get("adminMentions", 0),
            "rwTarget": mentions.get(sym, {}).get("target"),
            "rwTag": mentions.get(sym, {}).get("tag"),
            "rwCallDate": mentions.get(sym, {}).get("callDate"),
            "rwCallDays": mentions.get(sym, {}).get("callDays"),
            "patterns": str(rec.get("Breakout Patterns") or ""),
            "reasoning": reasoning,
            "spark": pdata.get("spark", []),
            "isNew": bool(prev_syms) and sym not in prev_syms,
            "rankDelta": (prev[sym][0] - int(rec.get("Rank", 0))) if sym in prev_syms and prev[sym][0] else None,
            "scoreDelta": (round(f(rec.get("Setup Score"), 0) - prev[sym][1])
                           if sym in prev_syms and prev[sym][1] is not None
                           and rec.get("Setup Score") is not None else None),
            "ledger": ledger_ctx["bySym"].get(sym),
        })

    m = re.search(r"swing_candidates_(\d{8})\.csv$", csv_path.name)
    sheet_date = datetime.strptime(m.group(1), "%d%m%Y").strftime("%d %b %Y") if m else "?"
    top10 = [r for r in out if r["rank"] <= 10 and r["rs"] is not None]
    meta = {
        "sheetDate": sheet_date,
        "totalGated": int(total_gated),
        "shown": len(out),
        "tierA": sum(1 for r in out if r["tier"] == "A"),
        "tierB": sum(1 for r in out if r["tier"] == "B"),
        "freshCount": sum(1 for r in out if r["fresh"]),
        "medianRsTop10": f(pd.Series([r["rs"] for r in top10]).median(), 1) if top10 else None,
        "persistentCount": sum(1 for r in out if r["persistent"]),
        "newLeaderCount": sum(1 for r in out if r["newLeader"]),
        "epCount": sum(1 for r in out if r["ep"]),
        "emergingCount": sum(1 for r in out if r["emerging"]),
        "rwCount": sum(1 for r in out if r["rwMentions"] > 0),
        "rwAdminCount": sum(1 for r in out if r["rwAdmin"] > 0 or r["rwTarget"] or r["rwTag"]),
        "hasGroupData": bool(mentions),
        "stripDates": strip_dates,
        "generated": datetime.now().strftime("%d %b %Y %H:%M"),
        "market": market,
        "ledgerOpen": ledger_ctx["open"],
        "alerts": ledger_ctx["alerts"],
    }
    # 'Since yesterday' diff: new entrants, names that dropped off, and movers.
    shown_syms = {r["sym"] for r in out}
    dropped = [{"sym": s, "name": prev[s][2], "score": prev[s][1], "rank": prev[s][0]}
               for s in prev_syms if s not in shown_syms]
    dropped.sort(key=lambda x: x["rank"] or 999)
    movers = sorted([r for r in out if r.get("scoreDelta")],
                    key=lambda r: abs(r["scoreDelta"]), reverse=True)[:12]
    meta["diff"] = {
        "prevDate": prev.get("__date__"),
        "newCount": sum(1 for r in out if r["isNew"]),
        "dropped": dropped[:20],
        "movers": [{"sym": r["sym"], "scoreDelta": r["scoreDelta"], "rankDelta": r["rankDelta"],
                    "score": r["score"]} for r in movers],
    }
    return out, meta


# ---------------------------------------------------------------------------
# Page template (tokens __DATA__, __META__ substituted; no f-string braces issues)
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Odin's Watchlist — Daily Triage</title>
<style>
  :root{
    --paper:#F2F0EA; --raised:#FBFAF6; --ink:#1C1B17; --soft:#57544B; --faint:#8B8778;
    --line:#DCD7C8; --line2:#C4BEAB; --accent:#A6721F; --accent-ink:#7A5417; --wash:#EFE2C6;
    --good:#0ca30c; --warnc:#fab219; --serious:#ec835a; --critical:#d03b3b;
    --good-wash:#e3f2e3; --warn-wash:#fdf3d8; --crit-wash:#f8e2e2; --chip:#ECE9DE;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    --serif:ui-serif,"Iowan Old Style",Palatino,Georgia,serif;
  }
  :root[data-theme="dark"]{
    --paper:#16171B; --raised:#1D1E23; --ink:#EAE7DC; --soft:#B5B1A2; --faint:#7C7969;
    --line:#302F2F; --line2:#423F3C; --accent:#D4A24C; --accent-ink:#E9C077; --wash:#332A16;
    --good-wash:#17301a; --warn-wash:#33290f; --crit-wash:#331616; --chip:#2A2B31;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --paper:#16171B; --raised:#1D1E23; --ink:#EAE7DC; --soft:#B5B1A2; --faint:#7C7969;
      --line:#302F2F; --line2:#423F3C; --accent:#D4A24C; --accent-ink:#E9C077; --wash:#332A16;
      --good-wash:#17301a; --warn-wash:#33290f; --crit-wash:#331616; --chip:#2A2B31;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5}
  .wrap{max-width:1200px;margin:0 auto;padding:1.6rem 1.4rem 4rem}

  header.bar{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;border-bottom:1px solid var(--line2);padding-bottom:.9rem;margin-bottom:1.2rem}
  header.bar h1{font-family:var(--serif);font-size:1.5rem;margin:0;font-weight:600}
  header.bar .date{font-family:var(--mono);font-size:.8rem;color:var(--accent-ink);letter-spacing:.08em;text-transform:uppercase}
  header.bar .spacer{flex:1}
  .helpbtn{width:2rem;height:2rem;border-radius:50%;border:1px solid var(--line2);background:var(--raised);
    color:var(--accent-ink);font-family:var(--serif);font-size:1.05rem;font-weight:700;cursor:pointer}
  .helpbtn:hover{background:var(--wash)}
  .helpbtn:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line2);
    border:1px solid var(--line2);border-radius:4px;overflow:hidden;margin-bottom:1.1rem}
  .tile{background:var(--raised);padding:.8rem 1rem}
  .tile .v{font-family:var(--mono);font-size:1.45rem;color:var(--accent-ink)}
  .tile .k{font-size:.74rem;color:var(--soft);margin-top:.1rem}

  .filters{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin-bottom:1rem}
  .filters .lbl{font-size:.72rem;color:var(--faint);text-transform:uppercase;letter-spacing:.07em;margin-right:.15rem}
  .tog{border:1px solid var(--line2);background:var(--raised);color:var(--soft);border-radius:999px;
    padding:.28rem .7rem;font-size:.8rem;cursor:pointer}
  .tog[aria-pressed="true"]{background:var(--wash);color:var(--accent-ink);border-color:var(--accent)}
  select,input[type="search"]{border:1px solid var(--line2);background:var(--raised);color:var(--ink);
    border-radius:4px;padding:.28rem .5rem;font-size:.8rem;font-family:var(--sans)}
  .count{margin-left:auto;font-family:var(--mono);font-size:.78rem;color:var(--faint)}

  .panel{background:var(--raised);border:1px solid var(--line2);border-radius:4px;margin-bottom:1.1rem;overflow:hidden}
  .panel .cap{font-family:var(--mono);font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;
    color:var(--faint);padding:.6rem .9rem;border-bottom:1px solid var(--line)}

  /* RS ladder */
  .ladder{padding:.7rem .9rem .9rem}
  .lrow{display:grid;grid-template-columns:92px 1fr;gap:.6rem;align-items:center;height:24px;cursor:pointer;border-radius:3px}
  .lrow:hover{background:var(--wash)}
  .lrow .s{font-family:var(--mono);font-size:.78rem;text-align:right;color:var(--ink)}
  .ltrack{position:relative;height:14px}
  .ltrack .zero{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--line2)}
  .lbar{position:absolute;top:1px;height:12px;border-radius:0 4px 4px 0;background:var(--accent)}
  .lbar.neg{border-radius:4px 0 0 4px;background:var(--faint);opacity:.55}
  .lval{position:absolute;top:-1px;font-family:var(--mono);font-size:.72rem;color:var(--soft);white-space:nowrap}

  .tblwrap{overflow-x:auto}
  table{border-collapse:collapse;width:100%;font-size:.85rem;min-width:900px}
  th,td{text-align:left;padding:.5rem .65rem;white-space:nowrap}
  thead th{font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);
    border-bottom:1px solid var(--line2);position:sticky;top:0;background:var(--raised)}
  thead th.sortable{cursor:pointer}
  thead th.sortable:hover{color:var(--accent-ink)}
  thead th .arr{font-size:.6rem}
  td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
  tbody tr.main{cursor:pointer;border-bottom:1px solid var(--line)}
  tbody tr.main:hover{background:var(--wash)}
  td .nm{display:block;font-size:.68rem;color:var(--faint);max-width:190px;overflow:hidden;text-overflow:ellipsis}
  .pos{color:var(--good)} .negv{color:var(--critical)} .neg{color:var(--critical)}
  .pnl{margin:0 0 1rem}
  .pnlhead{font-weight:600;font-size:.9rem;margin:.1rem 0 .4rem}
  .pnlhead span{color:var(--soft);font-weight:400;font-size:.82rem}

  .chip{display:inline-block;font-family:var(--mono);font-size:.68rem;font-weight:700;border-radius:3px;
    padding:.1rem .42rem;border:1px solid transparent}
  .tA{background:var(--good-wash);color:var(--good);border-color:var(--good)}
  .tB{background:var(--warn-wash);color:var(--accent-ink);border-color:var(--warnc)}
  .tC{background:var(--chip);color:var(--soft);border-color:var(--line2)}
  .b{display:inline-block;font-size:.66rem;border-radius:3px;padding:.08rem .38rem;margin-right:.22rem;
    background:var(--chip);color:var(--soft)}
  .b.good{background:var(--good-wash);color:var(--good)}
  .b.warn{background:var(--warn-wash);color:var(--accent-ink)}
  .b.crit{background:var(--crit-wash);color:var(--critical)}

  .ph{display:inline-block;font-family:var(--mono);font-size:.66rem;font-weight:700;border-radius:3px;
    padding:.08rem .4rem;border:1px solid transparent;letter-spacing:.02em}
  .phSOS{background:var(--good-wash);color:var(--good);border-color:var(--good)}
  .phLPS{background:var(--warn-wash);color:var(--accent-ink);border-color:var(--warnc)}
  .phBase{background:var(--chip);color:var(--soft);border-color:var(--line2)}
  .phNone{color:var(--faint)}
  .sep{width:1px;align-self:stretch;background:var(--line2);margin:0 .15rem}

  /* 20-day RS-leadership presence strip */
  .strip{display:inline-flex;gap:1px;align-items:center}
  .strip i{width:4px;height:13px;border-radius:1px;background:var(--line)}
  .strip i.on{background:var(--line2)}
  .strip i.lead{background:var(--accent)}
  .strip .ld{font-family:var(--mono);font-size:.7rem;color:var(--soft);margin-left:.35rem}
  .b.new{background:var(--good-wash);color:var(--good);font-weight:700}

  /* relative-volume figure (vs the stock's own 20-day average) */
  .rv{font-family:var(--mono);font-variant-numeric:tabular-nums}
  .rv.lo{color:var(--faint)}                 /* below normal - weak for a breakout */
  .rv.mid{color:var(--soft)}
  .rv.hi{color:var(--accent-ink);font-weight:700}  /* 3x+ - a real volume surge */

  /* RIGHTWAY group mentions */
  .rw{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.8rem}
  .rw.recent{color:var(--accent-ink);font-weight:700}  /* mentioned in last 3 days */
  .rw.mid{color:var(--soft)}
  .rw.stale{color:var(--faint)}
  .rw.none{color:var(--line-strong)}

  tr.detail td{background:var(--paper);border-bottom:1px solid var(--line2);white-space:normal;
    font-size:.82rem;color:var(--soft);padding:.7rem 1rem}
  tr.detail .plan{font-family:var(--mono);font-size:.8rem;color:var(--ink);margin-bottom:.35rem}
  tr.detail .plan b{color:var(--accent-ink)}

  footer{margin-top:1.6rem;color:var(--faint);font-size:.76rem;border-top:1px solid var(--line);padding-top:.8rem}

  /* Help modal */
  .backdrop{position:fixed;inset:0;background:rgba(20,18,12,.5);display:none;z-index:40}
  .modal{position:fixed;top:5%;bottom:5%;left:50%;transform:translateX(-50%);width:min(680px,92vw);z-index:50;display:none;
    background:var(--raised);border:1px solid var(--line2);border-radius:6px;overflow:auto;padding:1.2rem 1.4rem}
  .open .backdrop,.open .modal{display:block}
  .modal h2{font-family:var(--serif);margin:0 0 .3rem;font-size:1.25rem}
  .modal h3{font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-ink);margin:1.1rem 0 .4rem}
  .modal dl{margin:0}
  .modal dt{font-weight:650;font-size:.85rem;margin-top:.5rem}
  .modal dd{margin:0 0 .2rem;font-size:.82rem;color:var(--soft)}
  .modal .x{position:sticky;float:right;top:0;border:1px solid var(--line2);background:var(--paper);
    border-radius:4px;padding:.2rem .6rem;cursor:pointer;color:var(--soft);font-size:.8rem}
  @media (prefers-reduced-motion: no-preference){
    .modal{animation:pop .14s ease-out}
    @keyframes pop{from{opacity:0;transform:translateX(-50%) translateY(6px)}to{opacity:1;transform:translateX(-50%)}}
  }

  /* ---- added: toolbar, tabs, pages, market strip, alerts, sparkline, marks ---- */
  .toolbar{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.2rem 0 .6rem}
  .toolbar .grp{display:flex;gap:.35rem;align-items:center;border:1px solid var(--line2);
    border-radius:6px;padding:.25rem .45rem;background:var(--raised)}
  .toolbar label{font-size:.72rem;color:var(--soft)}
  .toolbar input[type="number"]{width:5.5rem;border:1px solid var(--line2);background:var(--paper);
    color:var(--ink);border-radius:4px;padding:.15rem .35rem;font-family:var(--mono);font-size:.8rem}
  .tbtn{border:1px solid var(--line2);background:var(--paper);color:var(--soft);border-radius:4px;
    padding:.2rem .55rem;cursor:pointer;font-size:.78rem}
  .tbtn:hover{color:var(--ink)}
  .tabs{display:flex;gap:.25rem;border-bottom:1px solid var(--line2);margin:.2rem 0 .8rem}
  .tab{border:none;background:none;color:var(--soft);padding:.45rem .8rem;cursor:pointer;font-size:.85rem;
    border-bottom:2px solid transparent;margin-bottom:-1px}
  .tab:hover{color:var(--ink)}
  .tab.active{color:var(--accent-ink);border-bottom-color:var(--accent);font-weight:650}
  .tab .cnt{font-family:var(--mono);font-size:.72rem;color:var(--faint);margin-left:.3rem}
  .pagehide{display:none}
  .mstrip{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;font-size:.8rem;color:var(--soft);
    border:1px solid var(--line2);background:var(--raised);border-radius:6px;padding:.4rem .7rem;margin:.2rem 0 .5rem}
  .mstrip b{color:var(--ink)}
  .mbar{display:inline-block;width:120px;height:8px;border-radius:4px;background:var(--line);overflow:hidden;vertical-align:middle}
  .mbar i{display:block;height:100%;background:var(--accent)}
  .alerts{display:flex;flex-wrap:wrap;gap:.4rem;margin:.2rem 0 .6rem}
  .alert{font-size:.78rem;border-radius:5px;padding:.3rem .6rem;border:1px solid var(--line2)}
  .alert.near-target,.alert.target{background:var(--good-wash);color:var(--good);border-color:var(--good)}
  .alert.near-stop,.alert.stopped{background:var(--crit-wash);color:var(--critical);border-color:var(--critical)}
  .alert.expired{background:var(--chip);color:var(--soft)}
  .alert b{font-family:var(--mono)}
  /* sparkline */
  .spark{width:74px;height:22px;vertical-align:middle}
  .spark path{fill:none;stroke:var(--accent);stroke-width:1.4}
  .spark.dn path{stroke:var(--critical)}
  /* row extras */
  .star{cursor:pointer;color:var(--line-strong);font-size:1rem;line-height:1;user-select:none}
  .star.watch{color:var(--warnc)} .star.taken{color:var(--good)}
  .sec{display:block;font-size:.68rem;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
  .tv{font-size:.66rem;color:var(--accent-ink);border:1px solid var(--line2);border-radius:3px;
    padding:0 .25rem;margin-left:.35rem;text-decoration:none}
  .tv:hover{background:var(--wash)}
  .mv{font-family:var(--mono);font-size:.7rem;margin-left:.3rem}
  .mv.up{color:var(--good)} .mv.dn{color:var(--critical)}
  .shares{display:block;font-size:.66rem;color:var(--accent-ink);font-family:var(--mono)}
  .ledb{font-size:.66rem;border:1px solid var(--line2);border-radius:3px;padding:0 .3rem;margin-left:.3rem;font-family:var(--mono)}
  .ledb.up{color:var(--good);border-color:var(--good)} .ledb.dn{color:var(--critical);border-color:var(--critical)}
  /* score-composition bar */
  .brk{display:flex;height:12px;border-radius:3px;overflow:hidden;margin:.35rem 0;max-width:420px;border:1px solid var(--line2)}
  .brk i{height:100%}
  .brk .c0{background:var(--accent)} .brk .c1{background:#6f8fb0} .brk .c2{background:#c7a15a}
  .brk .c3{background:#7fae7f} .brk .c4{background:#b58bbf}
  .brklegend{font-size:.68rem;color:var(--soft);display:flex;gap:.7rem;flex-wrap:wrap}
  .brklegend span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:.25rem;vertical-align:baseline}
  /* generic table for the extra pages */
  .wtable{width:100%;border-collapse:collapse;font-size:.82rem}
  .wtable th{text-align:left;color:var(--soft);font-weight:600;border-bottom:1px solid var(--line2);padding:.4rem .5rem;font-size:.75rem}
  .wtable td{border-bottom:1px solid var(--line);padding:.4rem .5rem;font-family:var(--mono);font-variant-numeric:tabular-nums}
  .wtable td.txt{font-family:inherit}
  .emptynote{color:var(--faint);font-size:.85rem;padding:1rem 0}
  body.compact .tile{padding:.5rem .7rem}
  body.compact td, body.compact th{padding-top:.25rem;padding-bottom:.25rem}
  body.compact .tile .v{font-size:1.15rem}
  .colhide-sec .sec, .colhide-spark .spark, .colhide-spark th.cspark, .colhide-spark td.cspark{display:none}
  @media print{
    .toolbar,.tabs,.filters,.helpbtn,.backdrop,.modal,.star,.tv{display:none!important}
    .page,.pagehide{display:block!important}
    body{color:#000}
  }
</style>

<div class="wrap" id="app">
  <header class="bar">
    <h1>Odin's Watchlist — Daily Triage</h1>
    <span class="date" id="sheetDate"></span>
    <span class="spacer"></span>
    <button class="helpbtn" id="helpBtn" aria-label="Help: what everything means" title="What everything means">?</button>
  </header>

  <div id="mstrip" class="mstrip"></div>
  <div id="alerts" class="alerts"></div>

  <div class="tabs" id="tabs">
    <button class="tab active" data-page="shortlist">Shortlist</button>
    <button class="tab" data-page="watchlist">Watchlist<span class="cnt" id="wcnt"></span></button>
    <button class="tab" data-page="changes">Changes<span class="cnt" id="ccnt"></span></button>
    <button class="tab" data-page="ledger">Ledger<span class="cnt" id="lcnt"></span></button>
  </div>

  <div class="toolbar">
    <div class="grp"><label for="capital">Capital Rs</label><input type="number" id="capital" min="0" step="1000" placeholder="e.g. 200000">
      <label for="riskpct">Risk %</label><input type="number" id="riskpct" min="0" step="0.1" placeholder="1"></div>
    <span style="flex:1"></span>
    <button class="tbtn" id="exportBtn" title="Download the current view as CSV">Export CSV</button>
    <button class="tbtn" id="densityBtn" title="Toggle compact rows">Density</button>
    <button class="tbtn" id="themeBtn" title="Toggle light / dark">Theme</button>
    <button class="tbtn" id="colBtn" title="Show / hide sparkline &amp; sector">Columns</button>
  </div>

  <div id="page-shortlist">
  <div class="tiles" id="tiles"></div>

  <div id="pnl" class="pnl"></div>

  <div class="filters" id="filters">
    <span class="lbl">Setup</span>
    <button class="tog" data-setup="" aria-pressed="true">Any</button>
    <button class="tog" data-setup="sos" aria-pressed="false">Wyckoff SOS</button>
    <button class="tog" data-setup="lps" aria-pressed="false">Wyckoff LPS</button>
    <button class="tog" data-setup="ep" aria-pressed="false">Episodic Pivot</button>
    <span class="sep"></span>
    <span class="lbl">Leaders</span>
    <button class="tog" data-k="persist" aria-pressed="false">Persistent</button>
    <button class="tog" data-k="newlead" aria-pressed="false">New today</button>
    <button class="tog" data-k="emerg" aria-pressed="false">Emerging</button>
    <button class="tog" data-k="rwonly" aria-pressed="false">In RIGHTWAY</button>
    <button class="tog" data-k="rwadmin" aria-pressed="false">Admin call</button>
    <span class="sep"></span>
    <span class="lbl">Tier</span>
    <button class="tog" data-k="tA" aria-pressed="true">A</button>
    <button class="tog" data-k="tB" aria-pressed="true">B</button>
    <button class="tog" data-k="tC" aria-pressed="false">C</button>
    <span class="lbl">Require</span>
    <button class="tog" data-k="fresh" aria-pressed="false">Fresh breakout</button>
    <button class="tog" data-k="vol" aria-pressed="false">Volume confirmed</button>
    <span class="lbl">Hide</span>
    <button class="tog" data-k="noext" aria-pressed="true">Extended &gt;15%</button>
    <button class="tog" data-k="nolag" aria-pressed="true">Market laggards</button>
    <button class="tog" data-k="not2t" aria-pressed="false">T2T (BE/BZ)</button>
    <select id="minRs" aria-label="Minimum relative strength">
      <option value="">RS: any</option><option value="5">RS ≥ +5</option>
      <option value="15">RS ≥ +15</option><option value="25">RS ≥ +25</option>
    </select>
    <select id="secf" aria-label="Sector filter"><option value="">Sector: all</option></select>
    <input type="search" id="q" placeholder="symbol…" aria-label="Search symbol">
    <span class="count" id="count"></span>
  </div>

  <div class="panel">
    <div class="cap">Relative strength vs market — top 10 shown (pp over 20 sessions) · click a bar to jump to its row</div>
    <div class="ladder" id="ladder"></div>
  </div>

  <div class="panel">
    <div class="cap">Candidates · click a row for reasoning &amp; the trade plan · click column heads to sort</div>
    <div class="tblwrap">
      <table id="tbl" aria-label="Swing candidates">
        <thead><tr>
          <th title="mark: click once = watching, twice = taken"></th>
          <th class="num sortable" data-s="rank">#<span class="arr"></span></th>
          <th>Tier</th><th>Wyckoff</th><th>Stock</th>
          <th class="num sortable" data-s="score">Score<span class="arr"></span></th>
          <th class="num sortable" data-s="rs">RS 20d<span class="arr"></span></th>
          <th class="sortable" data-s="leaderDays">20-day leadership<span class="arr"></span></th>
          <th class="num sortable" data-s="chg">%Chg<span class="arr"></span></th>
          <th class="num sortable" data-s="relVol">Vol<span class="arr"></span></th>
          <th class="cspark">Trend</th>
          <th class="num">Price</th><th class="num">Target</th><th class="num">Stop</th>
          <th class="num">R:R</th>
          <th class="num sortable" data-s="rwMentions">RW<span class="arr"></span></th>
          <th>Signals</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
  </div><!-- /page-shortlist -->

  <section id="page-watchlist" class="pagehide"></section>
  <section id="page-changes" class="pagehide"></section>
  <section id="page-ledger" class="pagehide"></section>

  <footer>
    Scores and levels are a screening framework, not advice. Backtested expectation for daily top-10:
    roughly 1 in 6 picks hits target before the stop. Set the stop before you buy.
    Generated <span id="gen"></span>.
  </footer>
</div>

<div class="backdrop" id="backdrop"></div>
<div class="modal" role="dialog" aria-modal="true" aria-labelledby="helpTitle" id="modal">
  <button class="x" id="closeBtn">Close ✕</button>
  <h2 id="helpTitle">What everything means</h2>
  <p style="font-size:.84rem;color:var(--soft)">Every number here comes from the project's own backtest of ~13 months of history. Nothing is a prediction — it's a triage aid.</p>

  <h3>The tiles</h3>
  <dl>
    <dt>Passing liquidity gate</dt><dd>Stocks on today's sheet after removing penny stocks (&lt;₹20), thin traders (&lt;50k shares) and ETFs. The dashboard embeds the top-scored slice of these.</dd>
    <dt>Tier A / Tier B</dt><dd>Conviction tiers from the six-check list below. A is rare by design.</dd>
    <dt>Persistent leaders</dt><dd>Stocks that have been market leaders (RS ≥ +15) on 12+ of the last 20 sheets — durable strength. Backtested, this cohort hit target ~24% of the time vs ~15% for freshly-emerged leaders.</dd>
    <dt>New leaders today</dt><dd>Stocks entering the leader cohort for the first time today — fresh ideas, a tiny daily shortlist.</dd>
    <dt>Median RS, top 10</dt><dd>The middle relative-strength value among the 10 highest-scored picks — a quick read on how strong today's crop is.</dd>
  </dl>

  <h3>Table columns</h3>
  <dl>
    <dt># (Rank)</dt><dd>Position by Setup Score. The backtested edge concentrates in roughly the top 10 — rank matters more than raw score differences.</dd>
    <dt>Tier</dt><dd>A / B / C conviction chip — see “The six checks” below.</dd>
    <dt>Wyckoff</dt><dd>The stock's inferred Wyckoff phase — <b>SOS</b>, <b>LPS</b>, <b>Base</b> or “–” — read from which screeners it triggered. See “Wyckoff phases &amp; the Setup filter” below.</dd>
    <dt>Score</dt><dd>The 0–100 Setup Score: relative strength (35 pts max), chart patterns (30), screener agreement &amp; freshness (15), day's move &amp; volume (10), institutional buying (10).</dd>
    <dt>RS 20d</dt><dd>Percentage points the stock beat the market (NIFTYBEES) over the last 20 sessions. The single strongest tested signal — out-of-sample its top picks won ~3x more often than random. Higher is better; ≥ +15 is “strong”.</dd>
    <dt>20-day leadership</dt><dd>A strip of the last 20 sheets — each bar is one sheet, <b>filled (amber)</b> = the stock was a market leader (RS ≥ +15) that day, faint = present but not leading, empty = absent. The <b>“N/20”</b> counts leader-days. A solid amber block = a durable leader; a sparse or just-starting strip = fresh or sporadic. This is the persistence signal, validated as a real conviction edge (see “Persistence &amp; leadership” below).</dd>
    <dt>%Chg</dt><dd>Today's move. +1..8% is the healthy zone; above +15% is chase territory (flagged “ext”).</dd>
    <dt>Vol</dt><dd>Today's volume vs. THIS stock's own 20-day average — e.g. 2.4x means it traded 2.4 times its normal volume. Dim = below 1x (weak — a breakout on light volume is prone to fail); bold amber = 3x+ (a real volume surge). This is different from the “vol” badge, which compares against OTHER stocks today; this Vol figure compares the stock against itself, and is the one to check when judging whether a breakout has real force behind it.</dd>
    <dt>Target / Stop</dt><dd>The suggested exit prices, sized to this stock's own volatility (ATR): stop ≈ 1.5×ATR clamped to −5..−10%, target = 5× the stop distance (capped +40%). The backtest found the tight stop — not the target — is what makes the edge profitable.</dd>
    <dt>R:R</dt><dd>Reward-to-risk of that plan (target % ÷ stop %). Typically 4–5.</dd>
    <dt>RW</dt><dd>How many times your RIGHTWAY Telegram group mentioned this stock in the last 45 days (blank = not in recent buzz). A “^” = the ADMIN (broadcast channel) has called it at some point — hover to see when. A “*” in bold amber = the admin flagged it important (POSITIONAL / MULTIBAGGER / LONG TERM / FOCUS) <i>recently</i> (within ~4 months). Expand the row to see the admin's target and the date it was called. See “RIGHTWAY group mentions” below — and read the caveat.</dd>
    <dt>Signals</dt><dd>Compact badges — see next section.</dd>
  </dl>

  <h3>Signal badges</h3>
  <dl>
    <dt>NEW★</dt><dd>Emerged as a market leader (RS ≥ +15) for the first time today — a fresh idea worth a look.</dd>
    <dt>EP↑</dt><dd>Episodic Pivot — gapped up ≥ 4% on ≥ 3× normal volume (a catalyst / news move). Hover for the exact gap and volume. See the Setup filter section.</dd>
    <dt>EMRG↗</dt><dd>Emerging — a volume thrust (≥ 3× normal) while relative strength is rising but not yet extreme (0 to +25). An EARLY, lower-confidence entry — catching the move before it's an obvious leader. See “Emerging” below.</dd>
    <dt>fresh</dt><dd>A breakout-type screener (52-week breakout, resistance break, VCP, Darvas…) triggered today. Fresh beats stale.</dd>
    <dt>vol</dt><dd>Today's volume was elevated or top-decile compared to OTHER stocks today (cross-sectional). For the stock-vs-its-own-normal figure, use the Vol column instead — that's the more useful one for judging a breakout.</dd>
    <dt>inst</dt><dd>Institutions bought more than they sold today (bulk/block deals). Rare but meaningful.</dd>
    <dt>ext</dt><dd>Caution: already up &gt;15% today — you'd be chasing.</dd>
    <dt>lag</dt><dd>Caution: negative relative strength — the market is beating this stock.</dd>
    <dt>T2T</dt><dd>Caution: BE/BZ settlement series (delivery-only Trade-to-Trade; BZ adds surveillance). Tradeable but jumpier, thinner, and their backtest stats flatter reality.</dd>
  </dl>

  <h3>The six checks behind the tier</h3>
  <dl>
    <dt>Checklist</dt><dd>1) RS ≥ +15 &nbsp;2) fresh breakout today &nbsp;3) volume confirmed &nbsp;4) healthy move (+1..8%, not extended) &nbsp;5) not lagging the market &nbsp;6) mainstream EQ series.</dd>
    <dt>Tier A</dt><dd>5+ checks including strong RS AND a fresh breakout — today's highest-conviction setups.</dd>
    <dt>Tier B</dt><dd>3–4 checks — worth a look, one or two things missing.</dd>
    <dt>Tier C</dt><dd>0–2 checks — on the sheet, but weak by this playbook. Off by default in the Tier filter to keep the list short.</dd>
  </dl>

  <h3>Wyckoff phases &amp; the Setup filter</h3>
  <p style="font-size:.82rem;color:var(--soft)">This tool finds stocks already leading and breaking out, which maps onto the <b>later</b> half of a Wyckoff cycle (Phases D–E: the breakout and markup) — not the early accumulation Spring. Each stock's phase is inferred from which screeners it fired:</p>
  <dl>
    <dt>SOS — Sign of Strength</dt><dd>A range/resistance breakout (Sideways-breakout, Range-breakout-with-volume, Master-resistance, Darvas, 52-week) firing <b>today</b>, on volume, while leading the market and not yet extended. Wyckoff's “jumping the creek” — the actionable breakout entry.</dd>
    <dt>LPS — Last Point of Support</dt><dd>A leader (positive RS) resting quietly at support (Major-support or the 0.7–0.88 Fib zone) on a flat/down day — i.e. a low-volume backup after a prior breakout. The safer, lower-chase Wyckoff entry.</dd>
    <dt>Base — cause building</dt><dd>Volatility contracting (Daily-contraction or VCP) with no breakout yet — the range is still forming. A watch-list, not an entry.</dd>
    <dt>Setup filter: Any / Wyckoff SOS / Wyckoff LPS</dt><dd>One click. <b>SOS</b> also sets the companion filters (require fresh + volume, hide extended/laggards, RS ≥ +5). <b>LPS</b> reconfigures them for a quiet backup (fresh &amp; volume OFF, RS ≥ +5, shows Tier C too, since a resting stock may score lower). You can still fine-tune afterward.</dd>
    <dt>The manual step Wyckoff still needs</dt><dd>No filter confirms a phase — on the chart, verify a real prior trading range (the “cause”), light volume on pullbacks/Spring vs. expanding volume on the breakout, and that the pullback holds above the breakout level.</dd>
  </dl>

  <h3>Episodic Pivot (the Setup filter's 4th option)</h3>
  <p style="font-size:.82rem;color:var(--soft)">A stockbee-style catalyst move: the stock <b>gaps up ≥ 4% on ≥ 3× normal volume</b> — usually earnings or news igniting it out of a quiet base. Unlike relative strength (which measures “is it already strong”), this measures “did something just happen today,” so it's genuinely different information. We tested it: standalone, EP names hit +20% about 2.5× more often than random; and within the RS-top-10, the ones that also just had an EP hit target ~24% vs ~17% for the rest. Clicking the preset shows gap-ups on volume and — unlike the other lenses — leaves “Extended &gt;15%” OFF, because a big gap is the whole point.</p>
  <dl>
    <dt>Follow-through (why some gaps don't get the badge)</dt><dd>We backtested a refinement: an EP that <b>closes in the top half of its day's range</b> (it HELD the gap) hit target ~19%, versus only ~12% for one that <b>faded to the bottom half</b> — a +7pp edge. So the EP↑ badge now fires only for gaps that held; a gap that faded is not flagged as actionable. Hold quality is the tell that the catalyst has real buyers behind it.</dd>
    <dt>Reading it</dt><dd>Best when the gap comes off a tight base (check the chart) rather than after the stock has already run for weeks. A gap on the 1st day of a move &gt; a gap on day 20.</dd>
  </dl>

  <h3>Persistence &amp; leadership (the 20-day strip)</h3>
  <p style="font-size:.82rem;color:var(--soft)">A single day tells you a stock is strong <i>today</i>; the strip tells you whether it has <i>stayed</i> strong. We tested this: among the daily top-10 by relative strength, the ones that had led on 15+ of the prior 20 sheets went on to hit target <b>~24%</b> of the time, versus <b>~15%</b> for names that had only just emerged. Persistence is a real, second-order conviction signal — so it's shown as context and as cohort filters, but deliberately NOT folded into the score (relative strength already ranks these names to the top; persistence is how you choose <i>between</i> them).</p>
  <dl>
    <dt>Persistent leader</dt><dd>Led (RS ≥ +15) on 12+ of the last 20 sheets — a durable trend you can lean on. The higher-win cohort.</dd>
    <dt>New leader (NEW★)</dt><dd>First day in the leader cohort — earlier, less confirmed, but a fresh idea before it's obvious.</dd>
    <dt>Reading it with price</dt><dd>Persistence is only bullish alongside strength. A stock that's persistently on the list but NOT leading (a faint, breakout-less strip) is dead money, not a base — the strip makes that visible.</dd>
  </dl>

  <h3>Emerging (catching a move early)</h3>
  <p style="font-size:.82rem;color:var(--soft)">The persistence cohorts catch stocks that are ALREADY strong. Emerging tries to catch them a step earlier: a stock printing a <b>volume thrust (3x+ its own normal)</b> while its relative strength is <b>rising but still modest (0 to +25)</b> — the moment a quiet stock wakes up, before it's an obvious leader.</p>
  <dl>
    <dt>What we found when we tested it</dt><dd>Honestly: catching stocks BEFORE strength shows up (buying the quiet pullback) is NO better than random — you'd be front-running the only signal that works. But catching the thrust itself, with RS accelerating, hits target about <b>1.5x the base rate</b>. That's real, but roughly HALF the edge of a confirmed leader (~3x). So Emerging is an earlier, lower-confidence entry, not a better one.</dd>
    <dt>How to use it</dt><dd>A watch-and-size tool: earlier means more upside if right, but more failures, so size smaller and keep the stop tight. It's the cohort STALLION would have appeared in on its first thrust day, well before the obvious breakout.</dd>
  </dl>

  <h3>RIGHTWAY group mentions</h3>
  <p style="font-size:.82rem;color:var(--soft)">Cross-references your dashboard picks against what your RIGHTWAY Telegram channel has been talking about (last 45 days). The value is CONFLUENCE: when the tool independently flags a strong setup AND the group is also on it, that's a second, unrelated vote. The RW column and the “In RIGHTWAY” filter show it.</p>
  <dl>
    <dt>Admin vs. members (the “^” and “*”)</dt><dd>Most mentions are member chatter (noise). A <b>“^”</b> means the ADMIN — the broadcast channel that posts the actual calls — called it (hover for the date; many are old). A <b>“*”</b> means the admin flagged it important with a conviction tag (POSITIONAL PICK / MULTIBAGGER / LONG TERM / FOCUS) <i>recently</i>. The admin call carries more weight than random member buzz — use the “Admin call” filter to isolate every stock the admin has ever called (with a target or conviction tag).</dd>
    <dt>The admin's target</dt><dd>When the admin posted a target (e.g. “TARGET-260 AND 300+”), expand the row to see it. A <b>recent</b> call shows “admin target Rs X (+Y% from here)” or “(already reached)”; an <b>old</b> call (&gt;6 months) shows “admin's old target Rs X” with the date — because the entry it was based on is long gone, the live upside math would be misleading. Note: most admin calls in this data are 9-24 months old, so treat them as historical context, not live targets. That's the ADMIN'S stated goal, not the tool's — the tool's own volatility-based target/stop are still the ones with a backtest behind them.</dd>
    <dt>Read it honestly</dt><dd>This channel is heavily promotional and often posts winners AFTER they've already moved — so a mention means “they're discussing it,” NOT that it's a good buy. About 43 of 60 dashboard names get mentioned, so a plain mention isn't selective; the admin “^”/“*” markers are far more meaningful than raw count. A proper backtest of whether the admin's calls actually PRECEDE moves is the planned next step.</dd>
    <dt>How to use it</dt><dd>Best as a confirming nudge on a stock the tool already likes — especially “strong setup + fresh ADMIN call” — never as a reason to buy something the tool doesn't rate.</dd>
  </dl>

  <h3>Filters</h3>
  <dl>
    <dt>Setup (Any / Wyckoff SOS / Wyckoff LPS / Episodic Pivot)</dt><dd>The setup lens — Wyckoff sections above, EP section just above.</dd>
    <dt>In RIGHTWAY / Admin call</dt><dd>“In RIGHTWAY” = mentioned by anyone in the group (last 45d). “Admin call” = the tighter, higher-value set the admin channel actually called.</dd>
    <dt>Leaders: Persistent / New today / Emerging</dt><dd>“Persistent” = the durable-leader set; “New today” = fresh entrants to leadership; “Emerging” = the early volume-thrust cohort (see sections above).</dd>
    <dt>Tier A/B/C</dt><dd>Show or hide each conviction tier. C is off by default to keep the list short.</dd>
    <dt>Fresh breakout / Volume confirmed</dt><dd>Require that badge — the “only show me the textbook setups” switches.</dd>
    <dt>Hide: Extended &gt;15%</dt><dd>Drops same-day spikes (on by default — chase risk).</dd>
    <dt>Hide: Market laggards</dt><dd>Drops negative-RS names (on by default — the evidence says buy leaders).</dd>
    <dt>Hide: T2T (BE/BZ)</dt><dd>Drops Trade-to-Trade series names entirely (off by default; they're flagged either way).</dd>
    <dt>RS ≥ …</dt><dd>Minimum relative-strength floor.</dd>
  </dl>

  <h3>How to use this page (60 seconds)</h3>
  <dl>
    <dt>1.</dt><dd>Scan the tiles — is today a strong crop or a weak one?</dd>
    <dt>2.</dt><dd>Look at Tier A first; expand rows to read the reasoning and the plan.</dd>
    <dt>3.</dt><dd>For the 2–4 you like, open the actual chart and judge the entry vs support/resistance yourself.</dd>
    <dt>4.</dt><dd>If you trade one: use the printed stop, exactly. Expect ~1 in 6 winners; size so a losing streak is boring.</dd>
  </dl>

  <h3>Honest caveats</h3>
  <dl>
    <dt>Not predictions</dt><dd>Backtested on one ~13-month window, daily closes only, no brokerage/slippage. BE/BZ names test better than you'd realistically capture. Trust direction, not decimals.</dd>
  </dl>
</div>

<script>
const DATA = __DATA__;
const META = __META__;

const LS="odin_dash_v1";
function loadSaved(){try{return JSON.parse(localStorage.getItem(LS))||{}}catch(e){return {}}}
const saved=loadSaved();
const st=Object.assign({setup:"",persist:false,newlead:false,emerg:false,rwonly:false,rwadmin:false,tA:true,tB:true,tC:false,fresh:false,vol:false,
            noext:true,nolag:true,not2t:false,minRs:"",q:"",sortKey:"rank",sortDir:1,openSym:null,
            page:"shortlist",sector:"",capital:"",risk:"",theme:"",density:"",colSpark:true,colSec:true,showDismissed:false}, saved.st||{});
let marks=saved.marks||{};          // sym -> "watch" | "taken"
let dismissed=saved.dismissed||{};  // sym -> 1
function persist(){try{localStorage.setItem(LS,JSON.stringify({st:Object.assign({},st,{openSym:null}),marks,dismissed}))}catch(e){}}

if(st.theme) document.documentElement.dataset.theme=st.theme;
document.body.classList.toggle("compact", st.density==="compact");
function applyColHide(){document.body.classList.toggle("colhide-spark",!st.colSpark);document.body.classList.toggle("colhide-sec",!st.colSec);}
applyColHide();

document.getElementById("sheetDate").textContent = "sheet: " + META.sheetDate;
document.getElementById("gen").textContent = META.generated;

function tvLink(sym){return `<a class="tv" href="https://www.tradingview.com/chart/?symbol=NSE%3A${encodeURIComponent(sym)}" target="_blank" rel="noopener" title="Open ${sym} on TradingView" onclick="event.stopPropagation()">TV</a>`;}
function sparkSvg(a){
  if(!a||a.length<2) return "";
  const mn=Math.min(...a),mx=Math.max(...a),rng=(mx-mn)||1,W=74,H=22,p=2;
  const pts=a.map((v,i)=>{const x=p+i/(a.length-1)*(W-2*p);const y=H-p-(v-mn)/rng*(H-2*p);return x.toFixed(1)+","+y.toFixed(1);});
  return `<svg class="spark ${a[a.length-1]<a[0]?'dn':''}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="M${pts.join(" L")}"/></svg>`;
}
function sharesFor(r){
  const cap=parseFloat(st.capital),risk=parseFloat(st.risk);
  if(!cap||!risk||r.price==null||r.stop==null||r.stop>=r.price) return "";
  const n=Math.floor(cap*risk/100/(r.price-r.stop));
  if(!isFinite(n)||n<=0) return "";
  return `<span class="shares" title="risk ${risk}% of Rs ${cap.toLocaleString('en-IN')} at this stop">${n.toLocaleString('en-IN')} sh</span>`;
}
function moverArr(r){
  if(r.rankDelta==null||r.rankDelta===0) return "";
  const up=r.rankDelta>0;
  return `<span class="mv ${up?'up':'dn'}" title="rank moved ${up?'+':''}${r.rankDelta} vs prev sheet">${up?'^':'v'}${Math.abs(r.rankDelta)}</span>`;
}
function ledBadge(r){
  if(!r.ledger) return "";
  const L=r.ledger;
  if(L.status==="OPEN"){const u=L.unreal==null?0:L.unreal; return `<span class="ledb ${u>=0?'up':'dn'}" title="open paper position since ${L.entryDate}, ${L.days}d held, peak ${L.peak}%">held ${u>=0?'+':''}${u}%</span>`;}
  const cls=L.status==="TARGET"?"up":L.status==="STOPPED"?"dn":"";
  const rz=L.realized==null?0:L.realized;
  return `<span class="ledb ${cls}" title="paper position closed ${L.status}">${L.status.toLowerCase()} ${rz>=0?'+':''}${rz}%</span>`;
}
function markStar(r){
  const m=marks[r.sym]||"";
  const dis=dismissed[r.sym]?'<span class="mv dn" title="dismissed">x</span>':'';
  return `<span class="star ${m}" data-star="${r.sym}" title="click: watching -> taken -> clear · shift+click: dismiss">${m?'*':'☆'}</span>${dis}`;
}
function breakdownBar(r){
  const rs=Math.max(0,Math.min(35, r.rs==null?0:r.rs));
  const pat=Math.min(30,(r.patterns?r.patterns.split(",").filter(Boolean).length:0)*8+(r.fresh?6:0));
  const fresh=r.fresh?15:(r.newLeader?6:0), vol=r.volOk?10:0, inst=r.inst?10:0;
  const parts=[["RS",rs,"c0"],["Patterns",pat,"c1"],["Freshness",fresh,"c2"],["Volume",vol,"c3"],["Big-player",inst,"c4"]];
  const tot=parts.reduce((s,p)=>s+p[1],0)||1;
  const bar=parts.map(p=>`<i class="${p[2]}" style="width:${p[1]/tot*100}%" title="${p[0]} ~${Math.round(p[1])}"></i>`).join("");
  const leg=parts.filter(p=>p[1]>0).map(p=>`${p[0]} ~${Math.round(p[1])}`).join(" · ");
  return `<div class="brk">${bar}</div><div class="brklegend">${leg} — indicative composition (relative strength is the real driver; exact internal weights differ).</div>`;
}

function tiles(){
  const t=[["Passing liquidity gate",META.totalGated],["Tier A picks",META.tierA],
           ["Persistent leaders",META.persistentCount],
           ["New leaders today",META.newLeaderCount],
           ["Emerging (early)",META.emergingCount],
           ["In RIGHTWAY (admin)",META.hasGroupData?(META.rwCount+" ("+META.rwAdminCount+")"):"–"],
           ["Median RS, top 10",META.medianRsTop10==null?"–":("+"+META.medianRsTop10+"pp")]];
  document.getElementById("tiles").innerHTML =
    t.map(x=>`<div class="tile"><div class="v">${x[1]}</div><div class="k">${x[0]}</div></div>`).join("");
}

function pnl(){
  const el=document.getElementById("pnl"); const L=META.ledger;
  if(!L||(L.closed===0&&L.open===0)){el.style.display="none";return;}
  const cell=(v,k,cls)=>`<div class="tile"><div class="v ${cls||''}">${v}</div><div class="k">${k}</div></div>`;
  const t=[];
  if(L.closed>0){
    const sign=L.avgRealized>=0?"+":"";
    t.push(cell(L.winRate+"%","hit target ("+L.closed+" closed)"));
    t.push(cell(sign+L.avgRealized+"%","avg realized/trade", L.avgRealized>=0?"pos":"neg"));
    if(L.profitFactor!=null) t.push(cell(L.profitFactor,"profit factor", L.profitFactor>=1?"pos":"neg"));
    t.push(cell(L.target+"/"+L.stopped+"/"+L.expired,"tgt / stop / expired"));
    (L.lens||[]).forEach(x=>t.push(cell(x.win+"%",x.label+" ("+x.n+")")));
  }
  if(L.open>0){
    const u=L.openUnreal; const s=u==null?"":((u>=0?"+":"")+u+"% unreal");
    t.push(cell(L.open,"open"+(s?" · "+s:""), u==null?"":(u>=0?"pos":"neg")));
  }
  el.innerHTML=`<div class="pnlhead">Paper ledger <span>— how the tool's own tracked picks are doing (paper, +25%/-5% rules)</span></div>`
    +`<div class="tiles">`+t.join("")+`</div>`;
}

function visible(){
  let v = DATA.filter(r=>{
    if(st.setup==="sos"&&!r.wSOS) return false;
    if(st.setup==="lps"&&!r.wLPS) return false;
    if(st.setup==="ep"&&!r.ep) return false;
    if(st.persist&&!r.persistent) return false;
    if(st.newlead&&!r.newLeader) return false;
    if(st.emerg&&!r.emerging) return false;
    if(st.rwonly&&!r.rwMentions) return false;
    if(st.rwadmin&&!(r.rwAdmin||r.rwTarget||r.rwTag)) return false;
    if(r.tier==="A"&&!st.tA) return false;
    if(r.tier==="B"&&!st.tB) return false;
    if(r.tier==="C"&&!st.tC) return false;
    if(st.fresh&&!r.fresh) return false;
    if(st.vol&&!r.volOk) return false;
    if(st.noext&&r.extended) return false;
    if(st.nolag&&r.lagging) return false;
    if(st.not2t&&r.t2t) return false;
    if(st.minRs!==""&&(r.rs==null||r.rs<+st.minRs)) return false;
    if(st.q&&!r.sym.toLowerCase().includes(st.q)) return false;
    if(st.sector&&r.sector!==st.sector) return false;
    if(!st.showDismissed&&dismissed[r.sym]) return false;
    return true;
  });
  const k=st.sortKey,d=st.sortDir;
  v.sort((a,b)=>{const x=a[k]??-1e9,y=b[k]??-1e9;return x<y?-d:x>y?d:0;});
  return v;
}

function badge(r){
  let out="";
  if(r.fresh) out+='<span class="b good">fresh</span>';
  if(r.volOk) out+='<span class="b good">vol</span>';
  if(r.inst)  out+='<span class="b good">inst</span>';
  if(r.newLeader) out+='<span class="b new">NEW★</span>';
  if(r.ep) out+=`<span class="b good" title="Episodic Pivot: gapped +${r.gap}% on ${r.relVol}x volume and HELD (closed top-half of range)${r.closePos!=null?` — close ${Math.round(r.closePos*100)}% up the range`:''}">EP↑</span>`;
  if(r.emerging&&!r.ep) out+=`<span class="b warn" title="Emerging: volume thrust (${r.relVol}x) with RS rising but not yet extreme - an early, lower-confidence entry">EMRG↗</span>`;
  if(r.extended) out+='<span class="b crit">ext</span>';
  if(r.lagging)  out+='<span class="b crit">lag</span>';
  if(r.t2t)      out+='<span class="b warn">T2T</span>';
  return out||'<span class="b">–</span>';
}
function rwCell(r){
  const hasCall = r.rwAdmin||r.rwTarget||r.rwTag;
  if(!r.rwMentions && !hasCall) return '<span class="rw none">-</span>';
  const recentBuzz = r.rwDays!=null&&r.rwDays<=3;
  const recentConv = r.rwTag && r.rwCallDays!=null && r.rwCallDays<=120;   // admin flagged it important, recently
  let extra="", tip;
  tip = r.rwMentions ? `RIGHTWAY mentioned this ${r.rwMentions}x in the last 45 days; last ${r.rwLast||"?"}`
                     : "Not in recent buzz";
  if(hasCall){ extra="^"; tip+= r.rwCallDate?` | admin called it ${r.rwCallDate}`:" | admin call"; if(r.rwTag) tip+=` (${r.rwTag})`; }
  if(recentConv){ extra="*"; }                     // recent conviction wins the marker
  else if(!hasCall && recentBuzz) extra="*";
  const cls = recentConv ? "rw recent"
            : hasCall ? "rw mid"
            : recentBuzz ? "rw recent"
            : (r.rwDays!=null&&r.rwDays<=14 ? "rw mid" : "rw stale");
  return `<span class="${cls}" title="${tip}">${r.rwMentions||""}${extra}</span>`;
}
function rwCallInfo(r){
  let s="";
  const old = r.rwCallDays!=null && r.rwCallDays>180;   // call is >6 months old -> historical, not a live target
  if(r.rwTarget!=null){
    const tn=Number(r.rwTarget);
    if(isNaN(tn)){
      s+= old?` - admin's old target <b>${r.rwTarget}</b>`:` - admin target <b>${r.rwTarget}</b>`;   // e.g. "2x"
    } else if(r.price && tn>=r.price*0.4 && tn<=r.price*15){   // plausible price target only
      if(old) s+=` - admin's old target <b>Rs ${tn}</b>`;            // historical: entry long gone, no live upside math
      else if(tn<=r.price) s+=` - admin target <b>Rs ${tn}</b> (already reached)`;
      else s+=` - admin target <b>Rs ${tn}</b> (+${((tn/r.price-1)*100).toFixed(0)}% from here)`;
    }
    // else: implausible number (e.g. a '10' from '10%'/'10 days') -> not a real price target, skip
  }
  if(r.rwTag) s+=` <span class="b new">${r.rwTag}</span>`;
  if(r.rwCallDate) s+=` <span style="color:var(--faint)">(admin called ${r.rwCallDate})</span>`;
  return s;
}
function volCell(r){
  if(r.relVol==null) return '<span class="rv lo">-</span>';
  const c=r.relVol>=3?"hi":r.relVol<1?"lo":"mid";
  return `<span class="rv ${c}" title="today's volume vs this stock's own 20-day average">${r.relVol}x</span>`;
}
function stripCell(r){
  if(!r.strip||!r.strip.length) return '<span class="strip"><span class="ld">–</span></span>';
  const cls=x=>x===2?"lead":x===1?"on":"";
  const cells=r.strip.map(x=>`<i class="${cls(x)}"></i>`).join("");
  return `<span class="strip" title="last 20 sheets · filled = RS-leader day">${cells}<span class="ld">${r.leaderDays}/20</span></span>`;
}
function phaseChip(r){
  if(r.phase==="SOS") return '<span class="ph phSOS" title="Sign of Strength: range breakout today, on volume">SOS</span>';
  if(r.phase==="LPS") return '<span class="ph phLPS" title="Last Point of Support: leader resting at support">LPS</span>';
  if(r.phase==="Base") return '<span class="ph phBase" title="Cause building: volatility contracting inside a range">Base</span>';
  return '<span class="ph phNone">–</span>';
}
const money=v=>v==null?"–":"₹"+v.toLocaleString("en-IN");
const sgn=(v,s)=>v==null?"–":(v>0?"+":"")+v.toFixed(s??1);

function ladder(v){
  const top=v.slice().sort((a,b)=>(b.score??0)-(a.score??0)).slice(0,10)
             .filter(r=>r.rs!=null);
  const el=document.getElementById("ladder");
  if(!top.length){el.innerHTML='<div style="color:var(--faint);font-size:.8rem">No RS data in current view.</div>';return;}
  // one shared scale for both signs (honest magnitudes); zero-line shifts
  // right only when negatives are actually present
  const mx=Math.max(...top.map(r=>Math.abs(r.rs)),1);
  const hasNeg=top.some(r=>r.rs<0);
  const zero=hasNeg?32:1, span=hasNeg?30:84;   // % of track
  el.innerHTML=top.map(r=>{
    const w=Math.abs(r.rs)/mx*span, neg=r.rs<0;
    const barLeft=neg?(zero-w):zero;
    const lblLeft=neg?(zero+1.5):(zero+w+1.5);
    return `<div class="lrow" data-sym="${r.sym}" title="open row">
      <span class="s">${r.sym}</span>
      <span class="ltrack"><span class="zero" style="left:${zero}%"></span>
        <span class="lbar ${neg?"neg":""}" style="left:${barLeft}%;width:${w}%"></span>
        <span class="lval" style="left:${lblLeft}%">${sgn(r.rs)}pp</span>
      </span></div>`;
  }).join("");
  el.querySelectorAll(".lrow").forEach(b=>b.addEventListener("click",()=>{
    st.openSym=b.dataset.sym;render();
    const row=document.querySelector(`tr[data-sym="${b.dataset.sym}"]`);
    if(row) row.scrollIntoView({block:"center"});
  }));
}

function rows(v){
  const tb=document.getElementById("rows");
  tb.innerHTML=v.map(r=>{
    const chgCls=r.chg==null?"":(r.chg>=0?"pos":"negv");
    let html=`<tr class="main" data-sym="${r.sym}">
      <td>${markStar(r)}</td>
      <td class="num">${r.rank}</td>
      <td><span class="chip t${r.tier}">${r.tier}·${r.checks}/6</span></td>
      <td>${phaseChip(r)}</td>
      <td>${r.sym}${tvLink(r.sym)}${r.isNew?'<span class="b new" title="new entrant vs previous sheet">NEW</span>':''}${moverArr(r)}${ledBadge(r)}<span class="nm">${r.name}</span><span class="sec">${r.sector||""}</span></td>
      <td class="num">${r.score??"–"}</td>
      <td class="num">${sgn(r.rs)}</td>
      <td>${stripCell(r)}</td>
      <td class="num ${chgCls}">${sgn(r.chg,2)}%</td>
      <td class="num">${volCell(r)}</td>
      <td class="cspark">${sparkSvg(r.spark)}</td>
      <td class="num">${money(r.price)}${sharesFor(r)}</td>
      <td class="num">${money(r.target)}</td>
      <td class="num">${money(r.stop)}</td>
      <td class="num">${r.rr==null?"–":"1:"+r.rr.toFixed(1)}</td>
      <td class="num">${rwCell(r)}</td>
      <td>${badge(r)}</td></tr>`;
    if(st.openSym===r.sym){
      html+=`<tr class="detail"><td colspan="17">
        <div style="margin-bottom:.35rem"><b>Score composition:</b> ${breakdownBar(r)}</div>
        <div class="plan">Plan: entry <b>${money(r.price)}</b> → target <b>${money(r.target)}</b> (+${r.targetPct??"?"}%) ·
          stop <b>${money(r.stop)}</b> (−${r.stopPct??"?"}%) · R:R 1:${r.rr??"?"} · ${r.basis==="ATR"?"volatility-sized":"flat fallback"}</div>
        <div><b>Sector:</b> ${r.sector} · <b>Series:</b> ${r.series} ${r.freshList?` · <b>Fired today:</b> ${r.freshList}`:""}</div>
        <div style="margin-top:.3rem"><b>Leadership:</b> ${stripCell(r)} &nbsp; ${r.persistent?"persistent leader (led ≥12 of last 20 sheets — historically ~24% hit rate vs ~15% for fresh)":r.newLeader?"newly emerged as a leader today":r.leaderDays?("led "+r.leaderDays+" of the last 20 sheets"):"not a recent market leader"}</div>
        ${(r.rwMentions||r.rwAdmin||r.rwTarget||r.rwTag)?`<div style="margin-top:.3rem"><b>RIGHTWAY:</b> ${r.rwMentions?`mentioned ${r.rwMentions}x in 45d, last ${r.rwLast} (${r.rwDays}d ago)`:"not in recent 45d buzz"}${r.rwAdmin?` - <b>admin also mentioned it ${r.rwAdmin}x recently</b>`:""}${rwCallInfo(r)}. Note: this channel often posts winners after they move - a mention is 'they're talking about it', not a proven edge.</div>`:""}
        <div style="margin-top:.3rem">${r.reasoning}</div>
      </td></tr>`;
    }
    return html;
  }).join("");
  tb.querySelectorAll("tr.main").forEach(tr=>tr.addEventListener("click",()=>{
    st.openSym = st.openSym===tr.dataset.sym?null:tr.dataset.sym; render();
  }));
  tb.querySelectorAll(".star").forEach(s=>s.addEventListener("click",e=>{
    e.stopPropagation();
    const sym=s.dataset.star;
    if(e.shiftKey){ if(dismissed[sym]) delete dismissed[sym]; else dismissed[sym]=1; }
    else { const cur=marks[sym]||""; const nx=cur===""?"watch":cur==="watch"?"taken":""; if(nx) marks[sym]=nx; else delete marks[sym]; }
    persist(); render();
  }));
}

// ---------- market-context strip, alerts, tab counts ----------
function marketStrip(){
  const el=document.getElementById("mstrip"); const M=META.market;
  if(!M||M.breadth==null){el.style.display="none";return;}
  const tone=M.breadth>=55?"strong":M.breadth<=35?"weak":"mixed";
  el.innerHTML=`<span><b>Market breadth</b> ${M.breadth}% above 50-DMA <span class="mbar"><i style="width:${M.breadth}%"></i></span> (${tone}, ${M.universe} stocks, ${M.date})</span>`
    +`<span style="color:var(--faint)">context only — a breadth filter tested WORSE, so we never gate on it.</span>`;
}
function alertsBanner(){
  const el=document.getElementById("alerts"); const A=(META.alerts||[]);
  if(!A.length){el.style.display="none";return;}
  el.innerHTML=A.map(a=>`<span class="alert ${a.kind}"><b>${a.sym}</b> ${a.kind.replace('-',' ')} — ${a.detail}</span>`).join("");
}
function tabCounts(){
  const w=Object.keys(marks).length;
  const d=(META.diff?((META.diff.newCount||0)+(META.diff.dropped||[]).length):0);
  const l=(META.ledgerOpen||[]).length;
  document.getElementById("wcnt").textContent=w?w:"";
  document.getElementById("ccnt").textContent=d?d:"";
  document.getElementById("lcnt").textContent=l?l:"";
}

// ---------- extra pages ----------
function renderWatchlist(){
  const el=document.getElementById("page-watchlist");
  const rowsM=DATA.filter(r=>marks[r.sym]);
  if(!rowsM.length){el.innerHTML='<div class="emptynote">Nothing marked yet. On the Shortlist, click a row\'s star: once = watching, twice = taken.</div>';return;}
  const body=rowsM.map(r=>`<tr><td>${markStar(r)}</td><td class="txt">${r.sym}${tvLink(r.sym)} <span style="color:var(--soft)">${r.name}</span>${ledBadge(r)}</td>
    <td>${marks[r.sym]}</td><td>${r.score??"–"}</td><td>${sgn(r.rs)}</td><td>${sparkSvg(r.spark)}</td>
    <td>${money(r.price)}</td><td>${money(r.target)}</td><td>${money(r.stop)}</td><td>${sharesFor(r)||"–"}</td></tr>`).join("");
  el.innerHTML=`<div class="panel"><div class="cap">Your marked names (★ watching / taken) — saved in this browser.</div>
    <table class="wtable"><thead><tr><th></th><th>Stock</th><th>Mark</th><th>Score</th><th>RS</th><th>Trend</th><th>Price</th><th>Target</th><th>Stop</th><th>Size</th></tr></thead><tbody>${body}</tbody></table></div>`;
  el.querySelectorAll(".star").forEach(s=>s.addEventListener("click",e=>{e.stopPropagation();const sym=s.dataset.star;
    if(e.shiftKey){if(dismissed[sym])delete dismissed[sym];else dismissed[sym]=1;}
    else{const c=marks[sym]||"";const n=c===""?"watch":c==="watch"?"taken":"";if(n)marks[sym]=n;else delete marks[sym];}
    persist();render();}));
}
function renderChanges(){
  const el=document.getElementById("page-changes"); const D=META.diff;
  if(!D||!D.prevDate){el.innerHTML='<div class="emptynote">No previous sheet found to compare against.</div>';return;}
  const news=DATA.filter(r=>r.isNew).sort((a,b)=>(a.rank-b.rank));
  const newTbl=news.length?`<table class="wtable"><thead><tr><th>#</th><th>Stock</th><th>Score</th><th>RS</th><th>Signals</th></tr></thead><tbody>`+
    news.map(r=>`<tr><td>${r.rank}</td><td class="txt">${r.sym}${tvLink(r.sym)} <span style="color:var(--soft)">${r.name}</span></td><td>${r.score}</td><td>${sgn(r.rs)}</td><td class="txt">${badge(r)}</td></tr>`).join("")+`</tbody></table>`:'<div class="emptynote">No new entrants.</div>';
  const drop=(D.dropped||[]);
  const dropTbl=drop.length?`<table class="wtable"><thead><tr><th>Stock</th><th>Prev #</th><th>Prev score</th></tr></thead><tbody>`+
    drop.map(x=>`<tr><td class="txt">${x.sym}${tvLink(x.sym)} <span style="color:var(--soft)">${x.name}</span></td><td>${x.rank||"–"}</td><td>${x.score??"–"}</td></tr>`).join("")+`</tbody></table>`:'<div class="emptynote">Nothing dropped off.</div>';
  const mv=(D.movers||[]);
  const mvTbl=mv.length?`<table class="wtable"><thead><tr><th>Stock</th><th>Score Δ</th><th>Rank Δ</th><th>Score</th></tr></thead><tbody>`+
    mv.map(x=>`<tr><td class="txt">${x.sym}${tvLink(x.sym)}</td><td class="${x.scoreDelta>=0?'':''}" style="color:${x.scoreDelta>=0?'var(--good)':'var(--critical)'}">${x.scoreDelta>0?'+':''}${x.scoreDelta}</td><td>${x.rankDelta==null?'–':(x.rankDelta>0?'+':'')+x.rankDelta}</td><td>${x.score}</td></tr>`).join("")+`</tbody></table>`:'<div class="emptynote">No score movers.</div>';
  el.innerHTML=`<div class="panel"><div class="cap">Since the previous sheet (${D.prevDate}) — ${D.newCount} new, ${drop.length} dropped.</div>
    <h3 style="margin:.4rem 0">New entrants</h3>${newTbl}
    <h3 style="margin:1rem 0 .4rem">Dropped off</h3>${dropTbl}
    <h3 style="margin:1rem 0 .4rem">Biggest score movers</h3>${mvTbl}</div>`;
}
function renderLedger(){
  const el=document.getElementById("page-ledger"); const L=META.ledger, O=(META.ledgerOpen||[]);
  let head="";
  if(L){head=`<div class="pnlhead">Paper ledger — ${L.closed} closed, ${L.open} open`+(L.winRate!=null?` · ${L.winRate}% hit target · avg ${L.avgRealized>=0?'+':''}${L.avgRealized}%/trade`:"")+`</div>`;}
  const body=O.length?`<table class="wtable"><thead><tr><th>Stock</th><th>Since</th><th>Days</th><th>Entry</th><th>Last</th><th>Unreal</th><th>Peak</th><th>Target</th><th>Stop</th><th>Lens</th></tr></thead><tbody>`+
    O.map(o=>`<tr><td class="txt">${o.sym}${tvLink(o.sym)}</td><td>${o.entryDate}</td><td>${o.days}</td><td>${money(o.entry)}</td><td>${money(o.last)}</td>
      <td style="color:${(o.unreal||0)>=0?'var(--good)':'var(--critical)'}">${o.unreal==null?'–':(o.unreal>=0?'+':'')+o.unreal+'%'}</td>
      <td>${o.peak==null?'–':'+'+o.peak+'%'}</td><td>${money(o.target)}</td><td>${money(o.stop)}</td>
      <td class="txt">${o.persistent?'persistent ':''}${o.ep?'EP':''}</td></tr>`).join("")+`</tbody></table>`
    :'<div class="emptynote">No open paper positions yet. They are snapshotted from the shortlist each day the pipeline runs.</div>';
  el.innerHTML=`<div class="panel">${head}${body}</div>`;
}

// ---------- page routing + main render ----------
function setPage(p){
  st.page=p; persist();
  ["shortlist","watchlist","changes","ledger"].forEach(x=>{
    const sec=document.getElementById("page-"+x); if(sec) sec.classList.toggle("pagehide",x!==p);
  });
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t.dataset.page===p));
  render();
}
function render(){
  marketStrip(); alertsBanner(); tabCounts();
  if(st.page==="shortlist"){
    const v=visible();
    const hid=Object.keys(dismissed).length;
    document.getElementById("count").textContent=`showing ${v.length} of ${META.shown}`+(hid&&!st.showDismissed?` · ${hid} hidden (shift+click ★ to dismiss; toggle with 'd')`:"");
    document.querySelectorAll("thead th.sortable").forEach(th=>{
      th.querySelector(".arr").textContent = th.dataset.s===st.sortKey?(st.sortDir===1?" ▲":" ▼"):"";
    });
    ladder(v); rows(v);
  } else if(st.page==="watchlist") renderWatchlist();
  else if(st.page==="changes") renderChanges();
  else if(st.page==="ledger") renderLedger();
}

function syncControls(){
  document.querySelectorAll(".tog[data-k]").forEach(b=>b.setAttribute("aria-pressed",String(!!st[b.dataset.k])));
  document.querySelectorAll(".tog[data-setup]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.setup===st.setup)));
  document.getElementById("minRs").value=st.minRs;
  document.getElementById("secf").value=st.sector;
  document.getElementById("q").value=st.q;
  document.getElementById("capital").value=st.capital;
  document.getElementById("riskpct").value=st.risk;
}

// sector dropdown options
(function(){
  const secs=[...new Set(DATA.map(r=>r.sector).filter(Boolean))].sort();
  const sel=document.getElementById("secf");
  secs.forEach(s=>{const o=document.createElement("option");o.value=s;o.textContent=s.length>28?s.slice(0,27)+"…":s;sel.appendChild(o);});
})();

document.querySelectorAll(".tog[data-k]").forEach(b=>b.addEventListener("click",()=>{
  st[b.dataset.k]=!st[b.dataset.k]; syncControls(); persist(); render();
}));
const SETUP_PRESETS={
  "":   {},
  sos:  {fresh:true, vol:true, noext:true, nolag:true, minRs:"5", tC:false},
  lps:  {fresh:false, vol:false, noext:true, nolag:true, minRs:"5", tC:true},
  ep:   {fresh:false, vol:false, noext:false, nolag:true, minRs:"5", tC:true},
};
document.querySelectorAll(".tog[data-setup]").forEach(b=>b.addEventListener("click",()=>{
  st.setup=b.dataset.setup; Object.assign(st, SETUP_PRESETS[st.setup]||{}); syncControls(); persist(); render();
}));
document.getElementById("minRs").addEventListener("change",e=>{st.minRs=e.target.value;persist();render();});
document.getElementById("secf").addEventListener("change",e=>{st.sector=e.target.value;persist();render();});
document.getElementById("q").addEventListener("input",e=>{st.q=e.target.value.trim().toLowerCase();render();});
document.querySelectorAll("thead th.sortable").forEach(th=>th.addEventListener("click",()=>{
  const k=th.dataset.s;
  if(st.sortKey===k) st.sortDir*=-1; else {st.sortKey=k; st.sortDir = k==="rank"?1:-1;}
  persist(); render();
}));
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>setPage(t.dataset.page)));
document.getElementById("capital").addEventListener("input",e=>{st.capital=e.target.value;persist();render();});
document.getElementById("riskpct").addEventListener("input",e=>{st.risk=e.target.value;persist();render();});
document.getElementById("themeBtn").addEventListener("click",()=>{
  const cur=document.documentElement.dataset.theme;
  const nx=cur==="dark"?"light":"dark"; document.documentElement.dataset.theme=nx; st.theme=nx; persist();
});
document.getElementById("densityBtn").addEventListener("click",()=>{
  st.density=st.density==="compact"?"":"compact"; document.body.classList.toggle("compact",st.density==="compact"); persist();
});
document.getElementById("colBtn").addEventListener("click",()=>{
  // cycle: both -> no spark -> no sector -> both
  if(st.colSpark&&st.colSec){st.colSpark=false;} else if(!st.colSpark&&st.colSec){st.colSpark=true;st.colSec=false;} else {st.colSpark=true;st.colSec=true;}
  applyColHide(); persist();
});
document.getElementById("exportBtn").addEventListener("click",()=>{
  const v=st.page==="shortlist"?visible():DATA.filter(r=>marks[r.sym]);
  const cols=["rank","sym","name","sector","score","rs","chg","relVol","price","target","stop","rr","phase","persistent","ep","emerging","isNew"];
  const esc=x=>{const s=(x==null?"":String(x)).replace(/"/g,'""');return /[",\n]/.test(s)?`"${s}"`:s;};
  const csv=[cols.join(",")].concat(v.map(r=>cols.map(c=>esc(r[c])).join(","))).join("\n");
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download="odin_"+(META.sheetDate||"today").replace(/ /g,"_")+"_"+st.page+".csv"; a.click();
});

const modalOpen=o=>document.body.classList.toggle("open",o);
document.getElementById("helpBtn").addEventListener("click",()=>modalOpen(true));
document.getElementById("closeBtn").addEventListener("click",()=>modalOpen(false));
document.getElementById("backdrop").addEventListener("click",()=>modalOpen(false));

// keyboard: Esc close · / search · 1-4 tabs · t theme · d dismissed · j/k rows · Enter expand
let cursor=-1;
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){modalOpen(false);return;}
  const tag=(e.target.tagName||"").toLowerCase();
  if(tag==="input"||tag==="select"||tag==="textarea") return;
  if(e.key==="/"){e.preventDefault();document.getElementById("q").focus();return;}
  if(e.key==="t"){document.getElementById("themeBtn").click();return;}
  if(e.key==="d"){st.showDismissed=!st.showDismissed;persist();render();return;}
  if(["1","2","3","4"].includes(e.key)){setPage(["shortlist","watchlist","changes","ledger"][+e.key-1]);return;}
  if(st.page!=="shortlist") return;
  const v=visible();
  if(e.key==="j"||e.key==="k"){
    cursor=Math.max(0,Math.min(v.length-1,cursor+(e.key==="j"?1:-1)));
    const row=document.querySelector(`tr.main[data-sym="${v[cursor].sym}"]`);
    if(row){row.scrollIntoView({block:"center"});row.style.outline="2px solid var(--accent)";setTimeout(()=>row.style.outline="",600);}
  }
  if(e.key==="Enter"&&cursor>=0&&v[cursor]){st.openSym=st.openSym===v[cursor].sym?null:v[cursor].sym;render();}
});

syncControls(); tiles(); pnl(); setPage(st.page||"shortlist");
</script>
"""


# Normalize "fancy" typographic Unicode to plain ASCII so the page renders
# identically regardless of the viewer's font/encoding (avoids em-dashes,
# curly quotes, rupee sign etc. showing as boxes or garbled characters).
# HTML entities (&amp; &lt; &gt;) are deliberately NOT touched -- those are
# required HTML escaping and render correctly.
ASCII_MAP = {
    "—": " - ", "–": "-",                     # em / en dash
    "‘": "'", "’": "'", "“": '"', "”": '"',  # curly quotes
    "≥": ">=", "≤": "<=", "−": "-", "±": "+/-",
    "×": "x", "✕": "x", "✖": "x",         # multiplication / close X
    "≈": "~", "…": "...", "→": "->", "↑": "^",
    "★": "*", "▲": "^", "▼": "v",         # star, sort triangles
    "₹": "Rs ", "·": "-", " ": " ", "‑": "-",
    "÷": "/", "•": "-", "↗": "^", "↘": "v", "→": "->",
}


def to_ascii(s: str) -> str:
    for k, v in ASCII_MAP.items():
        s = s.replace(k, v)
    return s


def ledger_summary() -> "dict | None":
    """Compact performance summary of paper_ledger.csv for the dashboard P&L
    panel. Returns None if the ledger doesn't exist yet (e.g. first ever run)."""
    path = WORKING_DIR / "paper_ledger.csv"
    if not path.exists():
        return None
    try:
        d = pd.read_csv(path)
    except Exception:
        return None
    if d.empty:
        return None
    truthy = ["True", "TRUE", "1", "1.0"]
    closed = d[d["status"].isin(["TARGET", "STOPPED", "EXPIRED"])].copy()
    openp = d[d["status"] == "OPEN"].copy()
    out = {"total": int(len(d)), "open": int(len(openp)), "closed": int(len(closed))}
    if len(closed):
        rp = pd.to_numeric(closed["realized_pct"], errors="coerce")
        wins, loss = rp[rp > 0].sum(), -rp[rp <= 0].sum()
        out["winRate"] = round((closed["status"] == "TARGET").mean() * 100)
        out["avgRealized"] = round(float(rp.mean()), 2)
        out["profitFactor"] = round(float(wins / loss), 2) if loss else None
        out["target"] = int((closed["status"] == "TARGET").sum())
        out["stopped"] = int((closed["status"] == "STOPPED").sum())
        out["expired"] = int((closed["status"] == "EXPIRED").sum())
        lens = []
        for key, label in [("persistent", "Persistent"), ("ep", "EP"), ("emerging", "Emerging")]:
            sub = closed[closed[key].astype(str).isin(truthy)]
            if len(sub):
                lens.append({"label": label, "n": int(len(sub)),
                             "win": round((sub["status"] == "TARGET").mean() * 100)})
        out["lens"] = lens
    if len(openp):
        unreal = (pd.to_numeric(openp["last_price"], errors="coerce")
                  / pd.to_numeric(openp["entry_price"], errors="coerce") - 1) * 100
        out["openUnreal"] = round(float(unreal.mean()), 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the triage dashboard HTML")
    ap.add_argument("--file", default=None, help="swing_candidates_*.csv (default: latest)")
    ap.add_argument("--out", default=str(WORKING_DIR / "dashboard.html"))
    args = ap.parse_args()

    src = Path(args.file) if args.file else find_latest_candidates()
    print(f"Source: {src.name}")
    rows, meta = build_rows(src)
    meta["ledger"] = ledger_summary()
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(rows, ensure_ascii=False))
            .replace("__META__", json.dumps(meta, ensure_ascii=False)))
    html = to_ascii(html)
    Path(args.out).write_text(html, encoding="utf-8", newline="\n")
    print(f"Dashboard written: {args.out}  ({len(rows)} rows embedded, "
          f"tier A={meta['tierA']}, B={meta['tierB']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
