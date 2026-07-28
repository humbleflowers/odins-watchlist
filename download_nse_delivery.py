"""Download NSE delivery (bhavcopy) data."""

import os

import pandas as pd
import requests

from config import NSE_DELIVERY_DIR, NSE_HEADERS
from get_dates import download_range


def download_bhavcopy(date_str: str, save_dir: str | os.PathLike) -> str | None:
    """
    Download NSE full bhavcopy for a single date.

    Args:
        date_str: Date in ddmmyyyy format.
        save_dir: Directory to save the CSV.

    Returns:
        Filename on success, None on failure.
    """
    save_dir = os.fspath(save_dir)
    os.makedirs(save_dir, exist_ok=True)
    filename = f"sec_bhavdata_full_{date_str}.csv"
    url = f"https://nsearchives.nseindia.com/products/content/{filename}"
    output_path = os.path.join(save_dir, filename)

    try:
        response = requests.get(url, headers=NSE_HEADERS, timeout=15)
        if response.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(response.content)
            print(f"Downloaded: {filename}")
            return filename
        print(f"Failed for {date_str}: HTTP {response.status_code}")
    except Exception as exc:
        print(f"Error for {date_str}: {exc}")
    return None


def rename_columns(csv_path: str) -> None:
    """Normalize bhavcopy column names and strip whitespace."""
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()
    df = df.rename(columns={"SYMBOL": "Symbol", "DATE1": "Date"})
    for col in ("Date", "Symbol", "SERIES", "DELIV_PER"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    df.to_csv(csv_path, index=False)


def start_download_delivery(date_str: str, save_dir: str | os.PathLike | None = None) -> str:
    """
    Download bhavcopy for the given trading day(s).

    Args:
        date_str: Start date in ddmmyyyy format.
        save_dir: Output directory. Defaults to config NSE_DELIVERY_DIR.

    Returns:
        Path to the last successfully downloaded CSV.

    Raises:
        RuntimeError: If no file was downloaded.
    """
    save_dir = os.fspath(save_dir or NSE_DELIVERY_DIR)
    csv_path = None
    for day in download_range(date_str):
        filename = download_bhavcopy(day, save_dir)
        if filename:
            csv_path = os.path.join(save_dir, filename)
            rename_columns(csv_path)
    if not csv_path:
        raise RuntimeError(f"No NSE delivery bhavcopy downloaded for {date_str}")
    return csv_path
