"""
Parse Telegram group messages (telegram_messages.csv) into structured stock
recommendations, using the NSE symbol list (EQUITY_L.csv) to pull tickers out
of free text.

Extracts, per message:
    date, caller, symbol, direction (BUY/SELL/''), entry, target, sl, raw_text

Symbol detection is heuristic -- exact ticker-token match plus a conservative
company-name match. It WILL need tuning once we see the group's real phrasing
(callers write "buy senco 380 tgt 420 sl 365", "ACCUMULATE xyz", "book abc",
etc.). Run it, eyeball the output, and we tighten the rules.

Usage:
    python parse_telegram_recs.py                 # -> telegram_recs.csv
    python parse_telegram_recs.py --show 30       # print a sample too
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

WORKING_DIR = Path(__file__).resolve().parent
ROOT = WORKING_DIR.parent
MESSAGES_CSV = WORKING_DIR / "telegram_messages.csv"
EQUITY_L = ROOT / "EQUITY_L.csv"
OUT_CSV = WORKING_DIR / "telegram_recs.csv"

# Tokens that are valid NSE tickers OR look like them but are really trading /
# English words -- excluded to cut false positives. Extend as we see real data.
STOPWORDS = {
    "BUY", "SELL", "SL", "TGT", "TARGET", "CMP", "ABOVE", "BELOW", "HOLD", "EXIT",
    "BOOK", "PROFIT", "LOSS", "SHORT", "LONG", "CALL", "PUT", "ADD", "OK", "THE",
    "AND", "FOR", "NOW", "TODAY", "STOP", "ENTRY", "QTY", "LOT", "NIFTY", "BANK",
    "BANKNIFTY", "INDEX", "FUT", "CE", "PE", "NSE", "BSE", "ATH", "ATL", "DAY",
    "WEEK", "SWING", "POS", "INTRADAY", "BTST", "STBT", "GAP", "UP", "DOWN",
    "RS", "INR", "PER", "PVT", "LTD", "NEW", "OLD", "HIGH", "LOW", "OPEN", "CLOSE",
}
BUY_WORDS = re.compile(r"\b(buy|accumulate|add|long|enter|entry)\b", re.I)
SELL_WORDS = re.compile(r"\b(sell|exit|book|short|profit\s*book)\b", re.I)
NUM = r"([0-9]{2,6}(?:\.[0-9]{1,2})?)"
RE_TARGET = re.compile(r"(?:tgt|target|t\d?)[\s:=@-]*" + NUM, re.I)
RE_SL = re.compile(r"(?:sl|stop\s*loss|stoploss)[\s:=@-]*" + NUM, re.I)
RE_ENTRY = re.compile(r"(?:above|entry|cmp|@|around|near)[\s:=-]*" + NUM, re.I)


def load_symbols() -> tuple[set, dict]:
    df = pd.read_csv(EQUITY_L)
    df.columns = df.columns.str.strip()
    symbols = set(df["SYMBOL"].astype(str).str.strip().str.upper())
    # first significant word of each company name -> symbol (for name mentions)
    name_map = {}
    for _, r in df.iterrows():
        name = str(r["NAME OF COMPANY"]).upper()
        first = re.sub(r"[^A-Z0-9]", "", name.split()[0]) if name.split() else ""
        if len(first) >= 5 and first not in STOPWORDS:
            name_map.setdefault(first, str(r["SYMBOL"]).strip().upper())
    return symbols, name_map


def find_symbols(text: str, symbols: set, name_map: dict) -> list[str]:
    tokens = re.findall(r"[A-Za-z&]{3,}", text.upper())
    hits = []
    for t in tokens:
        t = t.strip("&")
        if t in STOPWORDS or len(t) < 3:
            continue
        if t in symbols:
            hits.append(t)
        elif t in name_map:
            hits.append(name_map[t])
    # dedupe preserving order
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h); out.append(h)
    return out


def num(m):
    return float(m.group(1)) if m else None


def parse(show: int) -> None:
    if not MESSAGES_CSV.exists():
        raise SystemExit(f"{MESSAGES_CSV.name} not found -- run telegram_fetch.py first.")
    symbols, name_map = load_symbols()
    msgs = pd.read_csv(MESSAGES_CSV)

    rows = []
    for r in msgs.itertuples():
        text = str(getattr(r, "text", "") or "")
        syms = find_symbols(text, symbols, name_map)
        if not syms:
            continue
        direction = "BUY" if BUY_WORDS.search(text) else ("SELL" if SELL_WORDS.search(text) else "")
        target = num(RE_TARGET.search(text))
        sl = num(RE_SL.search(text))
        entry = num(RE_ENTRY.search(text))
        for sym in syms:  # one row per detected symbol
            rows.append({
                "date": getattr(r, "date_ist", ""),
                "caller": getattr(r, "sender", ""),
                "symbol": sym,
                "direction": direction,
                "entry": entry, "target": target, "sl": sl,
                "message_id": getattr(r, "message_id", ""),
                "raw_text": text[:200],
            })

    recs = pd.DataFrame(rows)
    recs.to_csv(OUT_CSV, index=False)
    print(f"Parsed {len(msgs)} messages -> {len(recs)} recommendation rows "
          f"({recs['symbol'].nunique() if len(recs) else 0} unique symbols, "
          f"{recs['caller'].nunique() if len(recs) else 0} callers) -> {OUT_CSV.name}")
    if show and len(recs):
        cols = ["date", "caller", "symbol", "direction", "entry", "target", "sl"]
        print(recs[cols].head(show).to_string(index=False))
        print("\nSanity-check these against the raw messages; tell me the misses "
              "and I'll tighten the symbol/level rules.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Parse Telegram messages into stock recs")
    ap.add_argument("--show", type=int, default=20, help="Print N parsed rows")
    args = ap.parse_args()
    parse(args.show)
