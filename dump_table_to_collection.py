"""Ingest CSV files into MongoDB collections."""

import pandas as pd
import numpy as np
from pymongo import MongoClient
from pymongo.collection import Collection


def _prepare_chartink(df: pd.DataFrame, file_path: str) -> pd.DataFrame:
    """Add Date and normalize numeric columns for Chartink data."""
    # Date is the folder name: chartink_downloads/2026-07-06/merged_...
    date_str = file_path.replace("\\", "/").split("/")[-2]
    df["Date"] = pd.to_datetime(date_str, format="%Y-%m-%d")
    if "% Chg" not in df.columns and "%Chg" in df.columns:
        df["% Chg"] = pd.to_numeric(df["%Chg"], errors="coerce")
    elif "% Chg" in df.columns:
        df["% Chg"] = pd.to_numeric(df["% Chg"], errors="coerce")
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
    return df


def _prepare_delivery(df: pd.DataFrame) -> pd.DataFrame:
    """Parse delivery bhavcopy dates and numeric columns."""
    df["Date"] = pd.to_datetime(df["Date"].str.strip(), format="%d-%b-%Y")
    df["Date"] = df["Date"].dt.tz_localize("UTC")
    for col in ("DELIV_PER", "DELIV_QTY", "TTL_TRD_QNTY"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].replace("-", np.nan), errors="coerce")
    return df


def _prepare_deals(df: pd.DataFrame) -> pd.DataFrame:
    """Parse bulk/block deal dates; drop NSE placeholder rows when no deals exist."""
    df = df.copy()
    df["Date"] = df["Date"].astype(str).str.strip()
    df = df[~df["Date"].str.upper().isin({"NO RECORDS", "NAN", ""})]
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["Date"])
    if df.empty:
        return df
    df["Date"] = df["Date"].dt.tz_localize("UTC")
    return df


def _replace_dates(collection: Collection, df: pd.DataFrame) -> int:
    """Remove existing MongoDB rows for the same Date value(s) before re-inserting."""
    if df.empty or "Date" not in df.columns:
        return 0
    dates = df["Date"].drop_duplicates().tolist()
    result = collection.delete_many({"Date": {"$in": dates}})
    if result.deleted_count:
        print(f"  Removed {result.deleted_count} existing row(s) for {len(dates)} date(s)")
    return result.deleted_count


def insert_to_collections(uri: str, db_name: str, collection_name: str, file_path: str) -> None:
    """
    Read a CSV, apply type conversions, and insert records into MongoDB.

    Preparation logic is selected based on keywords in the file path.
    """
    df = pd.read_csv(file_path)
    path_lower = file_path.lower()

    if "delivery" in path_lower:
        df = _prepare_delivery(df)
    elif "chartink" in path_lower:
        df = _prepare_chartink(df, file_path)
    elif "bulk" in path_lower or "block" in path_lower:
        df = _prepare_deals(df)

    if df.empty:
        print(f"No records to insert from {file_path} → {db_name}.{collection_name}")
        return

    client = MongoClient(uri)
    collection = client[db_name][collection_name]
    _replace_dates(collection, df)

    records = df.to_dict(orient="records")
    collection.insert_many(records)
    print(f"Inserted {len(records)} rows from {file_path} → {db_name}.{collection_name}")
