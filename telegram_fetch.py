"""
End-of-day Telegram group reader (Telethon).

Reads new messages from a group/channel you're a member of, using YOUR OWN
account via Telegram's MTProto API. Works even when the group has "restrict
saving / export" enabled -- that only blocks the app's Export button, not a
client reading messages your account can already see. (Media may be
restricted; message text comes through, which is what we parse.)

Credentials are read from environment variables so nothing sensitive is ever
written to a file:
    export TELEGRAM_API_ID=1234567
    export TELEGRAM_API_HASH=your_api_hash_here        # from my.telegram.org

First run (interactive, in YOUR terminal -- do the login yourself):
    python telegram_fetch.py --list          # log in once, then list your groups
Then, each end of day (non-interactive after the first login):
    python telegram_fetch.py --prefix RIGHTWAY   # ALL groups whose title starts with RIGHTWAY
    python telegram_fetch.py --group -1001234567890   # or a single group by id
    python telegram_fetch.py --group "My Stock Group"  # or by name

Image messages: the caption text (where the stock name usually is) IS captured
even when the tip has no target/SL. Images with no caption at all are skipped
(nothing to read). Forum "topics" inside a group are all read together.

Appends new messages to telegram_messages.csv and remembers the last message
id per group (telegram_state.json) so re-runs only pull what's new.

Install once:  pip install telethon
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKING_DIR = Path(__file__).resolve().parent
SESSION = str(WORKING_DIR / "odin_tg")           # -> odin_tg.session (keep private)
MESSAGES_CSV = WORKING_DIR / "telegram_messages.csv"
STATE_FILE = WORKING_DIR / "telegram_state.json"
IST = timezone(timedelta(hours=5, minutes=30))


def get_client():
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("Telethon not installed. Run:  pip install telethon")

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        sys.exit(
            "Set your credentials first (from my.telegram.org):\n"
            "  export TELEGRAM_API_ID=1234567\n"
            "  export TELEGRAM_API_HASH=your_api_hash_here\n"
            "then rerun. They are read from the environment and never stored."
        )
    return TelegramClient(SESSION, int(api_id), api_hash,
                          connection_retries=5, retry_delay=2, timeout=30)


def reset_session() -> None:
    for suffix in (".session", ".session-journal"):
        p = Path(SESSION + suffix)
        if p.exists():
            p.unlink()
    print("Session reset -- next run will do a fresh login.")


def list_dialogs(client) -> None:
    print(f"{'id':>16}  type      name")
    print("-" * 60)
    for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            kind = "channel" if d.is_channel and not d.is_group else "group"
            print(f"{d.id:>16}  {kind:8}  {d.name}")
    print("\nCopy the id of your stock group and run:\n"
          "  python telegram_fetch.py --group <id>")


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def resolve_group(client, group: str):
    """Accept a numeric id or a name substring."""
    try:
        return client.get_entity(int(group))
    except (ValueError, TypeError):
        pass
    for d in client.iter_dialogs():
        if (d.is_group or d.is_channel) and group.lower() in (d.name or "").lower():
            return d.entity
    sys.exit(f"Group '{group}' not found. Run --list to see available groups.")


def fetch(client, group: str) -> None:
    fetch_entity(client, resolve_group(client, group), group)


def fetch_by_prefix(client, prefix: str) -> None:
    """Fetch from EVERY group/channel whose title starts with `prefix`
    (case-insensitive) -- covers RIGHTWAY and any RIGHTWAY-* subgroups."""
    pre = prefix.strip().upper()
    matched = [d for d in client.iter_dialogs()
               if (d.is_group or d.is_channel) and (d.name or "").upper().startswith(pre)]
    if not matched:
        print(f"No groups found whose title starts with '{prefix}'. Try --list.")
        return
    print(f"Matched {len(matched)} group(s) starting with '{prefix}': "
          + ", ".join(d.name for d in matched))
    for d in matched:
        fetch_entity(client, d.entity, str(d.id))


def fetch_entity(client, entity, key) -> None:
    key = str(getattr(entity, "id", key))
    state = load_state()
    last_id = state.get(key, 0)

    new_rows = []
    max_id = last_id
    for msg in client.iter_messages(entity, min_id=last_id, reverse=True):
        text = (msg.message or "").replace("\r", " ").strip()
        if not text:
            continue  # skip media-only / empty messages
        sender = ""
        try:
            s = msg.sender
            sender = (getattr(s, "username", None) or
                      " ".join(filter(None, [getattr(s, "first_name", ""),
                                             getattr(s, "last_name", "")])) or
                      str(msg.sender_id))
        except Exception:
            sender = str(msg.sender_id)
        dt = msg.date.astimezone(IST).strftime("%Y-%m-%d %H:%M")
        new_rows.append([msg.id, dt, sender, text])
        max_id = max(max_id, msg.id)

    if new_rows:
        exists = MESSAGES_CSV.exists()
        with MESSAGES_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["message_id", "date_ist", "sender", "text"])
            for r in new_rows:
                r.append(key)  # group id column
                w.writerow(r)
        state[key] = max_id
        save_state(state)
    print(f"Fetched {len(new_rows)} new message(s) from group {key} "
          f"(up to id {max_id}). Total appended to {MESSAGES_CSV.name}.")


def main() -> int:
    ap = argparse.ArgumentParser(description="End-of-day Telegram group reader")
    ap.add_argument("--list", action="store_true", help="List your groups/channels (log in once)")
    ap.add_argument("--group", help="Target a single group by id (preferred) or name substring")
    ap.add_argument("--prefix", help="Fetch ALL groups whose title starts with this (e.g. RIGHTWAY)")
    ap.add_argument("--reset", action="store_true", help="Delete the saved session and log in fresh")
    args = ap.parse_args()

    if args.reset:
        reset_session()

    from telethon.errors.common import AuthKeyNotFound
    try:
        client = get_client()
        with client:
            me = client.get_me()
            print(f"Logged in as {getattr(me, 'username', None) or me.first_name}")
            if args.list:
                list_dialogs(client)
            elif args.prefix:
                fetch_by_prefix(client, args.prefix)
            elif args.group:
                fetch(client, args.group)
            else:
                list_dialogs(client)
    except AuthKeyNotFound:
        reset_session()
        print("Stale session detected and cleared. Please run the command again "
              "to do a fresh login.")
        return 1
    except (ConnectionError, OSError) as exc:
        print(f"Could not reach Telegram ({exc}).\n"
              "This is usually a network hiccup or a firewall/ISP blocking Telegram.\n"
              "Try again in a moment, or from a different network (e.g. phone hotspot). "
              "Also check your Mac's clock is set to automatic -- MTProto is time-sensitive.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
