# Odin's Watchlist

A daily **swing-trading assistant** for Indian equities (NSE). Every trading day it scans
thousands of stocks, scores them for the odds of a multi-day up-move, and produces a ranked
shortlist plus an interactive triage **dashboard** — with a per-stock target and stop-loss,
several backtested "lenses", and a self-updating paper-trading ledger that grades the tool on
its own picks.

> **Live demo dashboard:** _(GitHub Pages URL goes here once published — it serves `index.html`,
> a sanitized snapshot with all private data removed.)_

## What it does
1. **Collects** data each day (Chartink chart-pattern screeners, NSE bhavcopy, bulk/block deals)
   into MongoDB and a merged sheet, `odin_DDMMYYYY.csv`.
2. **Scores** each stock 0–100 — led by 20-day **relative strength vs. the market** (the signal
   the backtest found most reliable) plus data-driven chart-pattern weights, volume and agreement.
3. **Plans** every candidate with a volatility-sized **target and stop-loss**.
4. **Triages** via an interactive dashboard (`index.html`): tabs, filters, a sortable table,
   sparklines, a position-size calculator, TradingView links, a "since yesterday" changes view,
   validated lenses (persistence, Wyckoff, Episodic Pivot, emerging), and a paper-ledger panel.
5. **Backtests** honestly — every feature is measured on ~13 months of history with out-of-sample
   checks; ideas that don't beat relative strength are rejected (see the guides).

## The guides (start here)
Read in this order:
1. **`Odins_Watchlist_Beginner_Guide.pdf`** — brand new to the market? Start here. No jargon.
2. **`Odins_Watchlist_Guide.pdf`** — the full how-and-why: pipeline, scoring, backtests, dashboard.
3. **`Odins_Watchlist_UseCases.pdf`** — the playbook: each setup, its edge, and how to trade it.
4. **`Odins_Watchlist_UseCase_Examples.pdf`** — real, dated example trades per setup.

## Prerequisites
- Python 3.10+
- MongoDB running locally (`mongodb://localhost:27017`)
- Google Chrome (Selenium Manager handles ChromeDriver automatically)

## Setup & usage
```bash
pip install -r requirements.txt          # + python-docx, fpdf2 for the guides
mongod                                    # or: brew services start mongodb-community

python run_odin.py                        # today: collect -> score -> ledger -> dashboard
python run_odin.py --yesterday            # previous NSE trading day
python run_odin.py --date 2026-07-08      # a specific date (YYYY-MM-DD or DDMMYYYY)
python run_odin.py --no-downstream        # only build odin_*.csv (skip scoring/dashboard)
```
The optional Telegram feature reads credentials from environment variables
(`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) — never stored in the repo.

## Layout
| Path | Purpose |
|------|---------|
| `run_odin.py` | Daily pipeline entry point (data → score → ledger → dashboard) |
| `find_swing_candidates.py` | The scorer (relative-strength-led, data-driven screener weights) |
| `technical_indicators.py` | Relative strength, ATR, 52-week distance, etc. |
| `make_dashboard.py` | Builds the interactive dashboard |
| `paper_ledger.py` | The paper-trading ledger |
| `backtest_swing_candidates.py`, `optimize_target.py`, `feature_backtests/` | The research |
| `doc_builders/` | Scripts that generate the guides (docx + PDF) |
| `sanitize_dashboard.py` | Makes the public `index.html` from a private `dashboard.html` |
| `config/` | Screener URLs, sector map, data-driven screener weights |

## What's NOT in this repo (by design)
Private data and secrets are excluded (see `.gitignore`): the Telegram session and messages,
the personal paper ledger, the daily market-data CSVs, and the large backtest panels. The
published dashboard (`index.html`) is a **sanitized** snapshot with all private Telegram/ledger
data stripped out.

## Troubleshooting
- **Chartink returns only 20 rows** — the backend API path fetches up to 5000; if it falls back
  to DOM scrape, ensure Chrome performance logging is enabled (default here).
- **Empty delivery data** — NSE bhavcopy publishes only after close on trading days; pass a
  prior date on weekends/holidays.
- **MongoDB connection refused** — start MongoDB before running.

## Disclaimer
Educational software, **not financial advice**. It improves your odds versus picking randomly —
it does not predict the future, and most individual picks do not reach target. Always use a
stop-loss and never risk money you can't afford to lose.
