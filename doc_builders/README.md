# Document builders

These scripts GENERATE the Word documents in `working_version/`. The `.docx` files are
build output — edit these scripts, not the Word files.

Requires: `pip install python-docx pandas numpy`

| Script | Produces | Notes |
|--------|----------|-------|
| `build_guide_docx.py` | `Odins_Watchlist_Guide.docx` | The full plain-English guide (pipeline, scoring, backtest, exit study, dashboard, lenses, Telegram/RIGHTWAY, glossary, FAQ). |
| `build_usecases_docx.py` | `Odins_Watchlist_UseCases.docx` | The trading playbook — one use case per lens with finding, expected gain, how to trade. |
| `extract_examples.py` | `examples.json` | Pulls REAL dated example trades per use case from `../backtest_output/labeled_panel.csv` + `ohlc_indicator_panel.csv`. Run this first. |
| `build_examples_docx.py` | `Odins_Watchlist_UseCase_Examples.docx` | Renders `examples.json` into the worked-examples doc. |
| `build_beginner_docx.py` | `Odins_Watchlist_Beginner_Guide.docx` | Plain-English primer for someone brand new to the market — concepts, the best setups to trade, traps, and a dictionary. |

## Regenerate everything

```bash
cd doc_builders
python build_guide_docx.py
python build_usecases_docx.py
python extract_examples.py       # refresh example trades (needs backtest_output panels)
python build_examples_docx.py
python build_beginner_docx.py
python docx_to_pdf.py            # convert every guide .docx -> .pdf (fpdf2; no Word/LibreOffice)
```

All scripts write their `.docx` to `working_version/` via an absolute OUT path near the
bottom of each file — adjust there if you move the project.

The example-trade numbers come from the backtest window in `../backtest_output/` (currently
2025-07-11 → 2026-07-20). Re-run `backtest_swing_candidates.py` to refresh those panels, then
re-run `extract_examples.py` + `build_examples_docx.py` to update the worked examples.
