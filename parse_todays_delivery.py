"""Parse NSE delivery data from MongoDB."""

import pandas as pd
from pymongo.collection import Collection

_DELIVERY_PIPELINE = [
    {"$sort": {"Symbol": 1, "Date": -1}},
    {"$group": {
        "_id": "$Symbol",
        "recent": {"$push": {
            "Date": "$Date",
            "DELIV_QTY": "$DELIV_QTY",
            "DELIV_PER": "$DELIV_PER",
        }},
    }},
    {"$project": {
        "Symbol": "$_id",
        "Latest_Date": {"$arrayElemAt": ["$recent.Date", 0]},
        "DELIV_QTY": {"$arrayElemAt": ["$recent.DELIV_QTY", 0]},
        "DELIV_PER": {"$arrayElemAt": ["$recent.DELIV_PER", 0]},
        "3D_AVG_DELIV_QTY": {"$avg": {"$slice": ["$recent.DELIV_QTY", 3]}},
        "5D_AVG_DELIV_QTY": {"$avg": {"$slice": ["$recent.DELIV_QTY", 5]}},
        "10D_AVG_DELIV_QTY": {"$avg": {"$slice": ["$recent.DELIV_QTY", 10]}},
        "3D_AVG_DELIV_PER": {"$avg": {"$slice": ["$recent.DELIV_PER", 3]}},
        "5D_AVG_DELIV_PER": {"$avg": {"$slice": ["$recent.DELIV_PER", 5]}},
        "10D_AVG_DELIV_PER": {"$avg": {"$slice": ["$recent.DELIV_PER", 10]}},
        "_id": 0,
    }},
]


def calculate_delivery_averages(collection: Collection, target_date) -> pd.DataFrame:
    """Return per-symbol delivery stats filtered to target_date."""
    df = pd.DataFrame(list(collection.aggregate(_DELIVERY_PIPELINE)))
    if df.empty:
        return df
    # pymongo always returns naive datetimes (BSON has no tz concept), but
    # target_date may be tz-aware (e.g. config.date_strings()['dt_utc']).
    # Comparing aware-vs-naive here silently yields zero matches instead of
    # erroring, which is why delivery data went missing from every recent
    # odin_*.csv. Normalize both sides to naive before comparing.
    target_date = pd.Timestamp(target_date).tz_localize(None)
    return df[df["Latest_Date"] == target_date]


def fetch_last_7_days_delivery_data(collection: Collection) -> pd.DataFrame:
    """Pivot delivery quantity and percentage for the last 7 trading dates."""
    recent_dates = sorted(collection.distinct("Date"), reverse=True)[:7]
    cursor = collection.find(
        {"Date": {"$in": recent_dates}},
        {"_id": 0, "Symbol": 1, "Date": 1, "DELIV_QTY": 1, "DELIV_PER": 1},
    )
    df = pd.DataFrame(list(cursor))
    if df.empty:
        return df

    df["Date"] = df["Date"].dt.strftime("%Y%m%d")
    pivot_qty = df.pivot_table(
        index="Symbol", columns="Date", values="DELIV_QTY", aggfunc="sum"
    ).add_suffix("_DELIV_QTY")
    pivot_per = df.pivot_table(
        index="Symbol", columns="Date", values="DELIV_PER", aggfunc="mean"
    ).add_suffix("_DELIV_PER")
    return pd.concat([pivot_qty, pivot_per], axis=1).reset_index()
