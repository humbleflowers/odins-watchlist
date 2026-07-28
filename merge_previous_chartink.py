"""Compare today's Chartink screeners with the previous trading day."""

import pandas as pd
from pymongo.collection import Collection


def get_recent_dates(
    collection: Collection,
    limit: int = 2,
    on_or_before=None,
) -> list:
    """
    Return the most recent unique dates in the Chartink collection.

    Args:
        on_or_before: If set, only consider dates up to this value (for backfills).
    """
    pipeline = []
    if on_or_before is not None:
        pipeline.append({"$match": {"Date": {"$lte": on_or_before}}})
    pipeline.extend([
        {"$group": {"_id": "$Date"}},
        {"$sort": {"_id": -1}},
        {"$limit": limit},
    ])
    return [doc["_id"] for doc in collection.aggregate(pipeline)]


def _screener_count(screener_value) -> int:
    if pd.isna(screener_value) or not str(screener_value).strip():
        return 0
    return str(screener_value).count(",") + 1


def _new_screeners(row) -> str:
    if pd.isna(row["Screener_yesterday"]):
        return row["Screener_today"]
    today_set = {s.strip() for s in str(row["Screener_today"]).split(",")}
    yesterday_set = {s.strip() for s in str(row["Screener_yesterday"]).split(",")}
    added = today_set - yesterday_set
    return ", ".join(sorted(added)) if added else ""


def compare_with_previous_day(collection: Collection, recent_dates: list) -> pd.DataFrame:
    """
    Merge today vs yesterday Chartink data and compute change metrics.

    Requires at least two distinct dates in MongoDB.
    """
    if len(recent_dates) < 2:
        raise ValueError("Need at least two Chartink dates in MongoDB to compare")

    df_today = pd.DataFrame(list(collection.find({"Date": recent_dates[0]})))
    df_yesterday = pd.DataFrame(list(collection.find({"Date": recent_dates[1]})))

    for df in (df_today, df_yesterday):
        df["% Chg"] = pd.to_numeric(
            df["% Chg"].astype(str).str.replace("%", "", regex=False),
            errors="coerce",
        )

    merged = pd.merge(
        df_today, df_yesterday, on="Symbol", how="left", suffixes=("_today", "_yesterday")
    )
    merged["was_in_previous_sheet"] = merged["Stock Name_yesterday"].notna().map(
        {True: "Yes", False: "No"}
    )
    merged["%change_wrt_yesterday"] = merged.apply(
        lambda r: round(r["% Chg_today"] - r["% Chg_yesterday"], 2)
        if pd.notna(r["% Chg_yesterday"]) else "NA",
        axis=1,
    )
    merged["screener_count_today"] = merged["Screener_today"].apply(_screener_count)
    merged["screener_count_yesterday"] = merged["Screener_yesterday"].apply(_screener_count)
    merged["screener_count_diff"] = (
        merged["screener_count_today"] - merged["screener_count_yesterday"]
    )
    merged["new_screeners_today"] = merged.apply(_new_screeners, axis=1)
    merged["Today_screener_count"] = merged["Screener_today"].apply(_screener_count)

    final = merged[
        [
            "Stock Name_today", "Symbol", "% Chg_today", "Price_today", "Volume_today",
            "was_in_previous_sheet", "%change_wrt_yesterday", "Today_screener_count",
            "screener_count_diff", "new_screeners_today", "Screener_today", "Screener_yesterday",
        ]
    ].rename(columns={
        "Stock Name_today": "Stock Name",
        "% Chg_today": "% Chg",
        "Price_today": "Price",
        "Volume_today": "Volume",
    })
    return final
