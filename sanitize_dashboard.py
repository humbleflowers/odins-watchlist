"""Produce a PUBLIC, sanitized copy of dashboard.html as index.html.

Strips every piece of PRIVATE data before publishing:
  - all RIGHTWAY / Telegram per-row fields (rw*)
  - the personal paper-ledger (per-row `ledger`, and META ledger/ledgerOpen/alerts)
The public stock picks (symbol, score, price, target/stop -- all derived from
public market data) are kept, so index.html still works as a live demo.

    python sanitize_dashboard.py            # dashboard.html -> index.html
"""
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dashboard.html")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("index.html")

html = SRC.read_text()
ds = html.index("const DATA = ") + len("const DATA = ")
de = html.index("const META = ")
ms = html.index("const META = ") + len("const META = ")
me = html.index(";\n", ms)

data_sub = html[ds:de]
meta_sub = html[ms:me]
data = json.loads(data_sub.rstrip().rstrip("\n").rstrip(";").rstrip())
meta = json.loads(meta_sub.rstrip().rstrip(";"))

# --- strip private fields ---
removed = 0
for row in data:
    for k in list(row):
        if k.startswith("rw") or k == "ledger":
            del row[k]
            removed += 1
for k in ("ledger", "ledgerOpen", "alerts"):
    meta.pop(k, None)
meta["hasGroupData"] = False
meta["rwCount"] = 0
meta["rwAdminCount"] = 0

new_html = html.replace(data_sub, json.dumps(data) + ";\n").replace(meta_sub, json.dumps(meta))
OUT.write_text(new_html, encoding="utf-8")
print(f"Wrote {OUT} — stripped {removed} private field values from {len(data)} rows; "
      f"ledger/alerts removed from META.")
