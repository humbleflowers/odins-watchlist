# Feature backtests (Jul 2026)

Backtests of candidate features against the historical panels in `../backtest_output/`.
Decisive test: does a feature separate winners **among already-strong names** (top-40
shortlist), not just re-rank by strength? Window: Jul 2025 – Jul 2026, ~316k resolved trades.

## Scripts
| Script | Tests |
|--------|-------|
| `bt_features.py` | F1 regime/breadth, F2 sector RS, F3 delivery-trend, F4 volatility squeeze, F5 screener intelligence (in-sample) |
| `bt_features2.py` | F5 OOS, F6 EP follow-through, F7 telegram precedence, F8 smart-money deals |
| `bt_fix.py` | Fixed F5-OOS (normalized names), F7 (correct 5-col parse), F9 exit engine |
| `bt_exit.py` | F9 exit-rule expectancy (fixed vs trailing vs scale-out) |
| `gen_screener_weights.py` | Generates `../config/screener_weights.csv` from the F5 result |

Run any with `python <script>.py` (needs pandas/numpy and the panels in `../backtest_output/`).

## Verdicts
| Feature | Verdict | Evidence |
|---|---|---|
| **F5 Screener intelligence** | ✅ **BUILT** | Per-screener win rates OOS-stable (rank corr **0.75**). Reweighted the pattern score + dropped dead screeners. → `config/screener_weights.csv`, used by `find_swing_candidates.py`. |
| **F6 EP follow-through** | ✅ **BUILT** | EP that closes top-half of range wins **19% vs 12%** faded (+7pp). EP↑ badge now requires the gap to hold. → `make_dashboard.py`. |
| F2 Sector RS | 🟡 marginal | +2pp only. Not built. |
| F7 Telegram precedence | 🟡 weak | +3pp (12% vs 9% base) over 13,540 real pairs. Kept as confluence display only. |
| F1 Regime/breadth | ❌ reject | A risk-on filter would *hurt* (low-breadth entries won more). |
| F3 Delivery trend | ❌ reject | −3pp. Confirms delivery adds nothing beyond strength. |
| F4 Volatility squeeze | ❌ reject | −2pp. |
| F8 Smart-money deals | ❌ reject | −6pp. Bulk/block buys don't precede moves (HFT filtered). |
| F9 Trailing-stop exit | ❌ reject | Current fixed +25/−5 already near-optimal (+1.73%/trade); trailing & scale-out cut ~2pp. |

## Implemented alongside
- `../paper_ledger.py` — paper-trading ledger; snapshots the daily dashboard shortlist,
  resolves outcomes vs the delivery bhavcopy, reports realized P&L per lens. (Not a backtest —
  the live measurement backbone for future ideas.)
