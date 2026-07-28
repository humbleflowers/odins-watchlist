"""Download NSE bulk and block deal CSVs."""

import os
from datetime import datetime

import requests

from config import NSE_BULK_DIR, NSE_BLOCK_DIR, NSE_HEADERS


def download_csv(url: str, output_path: str) -> bool:
    """Download a CSV from NSE archives and save to output_path."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    response = requests.get(url, headers=NSE_HEADERS, timeout=30)
    if response.status_code != 200:
        print(f"Failed to download {url} (HTTP {response.status_code})")
        return False
    with open(output_path, "wb") as f:
        f.write(response.content)
    print(f"Downloaded: {output_path}")
    return True


def download_bulk_block_data(date_str: str | None = None) -> dict[str, str]:
    """
    Download today's (or given) NSE bulk and block deal files.

    Args:
        date_str: Date in ddmmyyyy format. Defaults to today.

    Returns:
        Dict with keys 'bulk' and 'block' mapping to local file paths.

    Raises:
        RuntimeError: If either file fails to download.
    """
    date_str = date_str or datetime.today().strftime("%d%m%Y")
    urls = {
        "bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
        "block": "https://nsearchives.nseindia.com/content/equities/block.csv",
    }
    paths = {
        "bulk": str(NSE_BULK_DIR / f"bulk_deals_{date_str}.csv"),
        "block": str(NSE_BLOCK_DIR / f"block_deals_{date_str}.csv"),
    }

    result = {}
    for label, url in urls.items():
        if download_csv(url, paths[label]):
            result[label] = paths[label]

    missing = {"bulk", "block"} - result.keys()
    if missing:
        raise RuntimeError(f"Failed to download: {', '.join(sorted(missing))}")
    return result
