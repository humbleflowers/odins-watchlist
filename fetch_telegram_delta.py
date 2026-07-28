"""
Best-effort DELTA fetch of the RIGHTWAY Telegram messages.

A thin, SAFE wrapper around telegram_fetch.py, meant to be called from other
scripts (find_swing_candidates.py runs it automatically). Unlike telegram_fetch
run directly, this never crashes or exits non-zero on the normal "not
configured / offline / not logged in" cases -- it just prints a one-line note
and returns, so it can never break the pipeline that calls it.

It only pulls messages newer than the last saved id (telegram_state.json) and
appends them to telegram_messages.csv -- exactly the same delta behaviour as
`python telegram_fetch.py --prefix RIGHTWAY`, just made non-fatal.

Standalone:
    python fetch_telegram_delta.py                 # delta-fetch RIGHTWAY groups
    python fetch_telegram_delta.py --prefix RGHTWAY --quiet
Env:
    TELEGRAM_API_ID / TELEGRAM_API_HASH   (required; from my.telegram.org)
    TELEGRAM_GROUP_PREFIX                  (optional; default "RIGHTWAY")
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

WORKING_DIR = Path(__file__).resolve().parent
DEFAULT_PREFIX = os.environ.get("TELEGRAM_GROUP_PREFIX", "RIGHTWAY")


def _count_rows(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def fetch_delta(prefix: str | None = None, quiet: bool = False) -> int:
    """Pull new messages for every group whose title starts with `prefix`.
    Returns the number of new messages appended, or -1 if skipped (not
    configured / telethon missing / offline / stale login). Never raises."""
    prefix = prefix or DEFAULT_PREFIX

    def note(msg: str) -> None:
        if not quiet:
            print(f"[telegram] {msg}")

    if not (os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH")):
        note("skipped - set TELEGRAM_API_ID / TELEGRAM_API_HASH to enable "
             "(one-time login via `python telegram_fetch.py --list`).")
        return -1

    try:
        import telegram_fetch as tf
    except Exception as exc:  # noqa: BLE001 - stay non-fatal for the caller
        note(f"skipped - could not import telegram_fetch ({exc}).")
        return -1

    before = _count_rows(tf.MESSAGES_CSV)
    try:
        client = tf.get_client()           # may SystemExit if telethon missing
        with client:
            client.get_me()                # verifies the saved session
            tf.fetch_by_prefix(client, prefix)
    except SystemExit as exc:              # telegram_fetch uses sys.exit on config errors
        note(f"skipped - {exc}")
        return -1
    except Exception as exc:               # network / auth / anything else
        note(f"skipped - {type(exc).__name__}: {exc}")
        return -1

    added = max(0, _count_rows(tf.MESSAGES_CSV) - before)
    note(f"added {added} new message(s) from '{prefix}' groups.")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description="Delta-fetch RIGHTWAY Telegram messages (best-effort).")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="group-title prefix (default RIGHTWAY)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    fetch_delta(args.prefix, args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
