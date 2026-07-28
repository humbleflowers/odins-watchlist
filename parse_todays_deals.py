"""Parse NSE bulk/block deal summaries from MongoDB."""

import pandas as pd
from pymongo.collection import Collection

_EMPTY_DEALS_COLS = ["Symbol", "BUY_quantity", "SELL_quantity", "BUY_count", "SELL_count"]


def build_aggregation_pipeline(target_date):
    """Build MongoDB aggregation pipeline for BUY/SELL summary by symbol."""
    return [
        {"$match": {"Date": target_date}},
        {"$group": {
            "_id": {"Symbol": "$Symbol", "Action": "$Buy/Sell"},
            "total_quantity": {"$sum": "$Quantity Traded"},
            "count": {"$sum": 1},
        }},
        {"$group": {
            "_id": "$_id.Symbol",
            "actions": {"$push": {
                "action": "$_id.Action",
                "quantity": "$total_quantity",
                "count": "$count",
            }},
        }},
        {"$project": {
            "_id": 0,
            "Symbol": "$_id",
            "BUY_quantity": _action_sum("BUY", "quantity"),
            "SELL_quantity": _action_sum("SELL", "quantity"),
            "BUY_count": _action_sum("BUY", "count"),
            "SELL_count": _action_sum("SELL", "count"),
        }},
    ]


def _action_sum(action: str, field: str) -> dict:
    """Helper to sum quantities/counts for a given BUY or SELL action."""
    return {
        "$sum": {
            "$map": {
                "input": "$actions",
                "as": "act",
                "in": {"$cond": [
                    {"$eq": ["$$act.action", action]},
                    f"$$act.{field}",
                    0,
                ]},
            }
        }
    }


def fetch_symbol_summary_by_date(collection: Collection, target_date) -> pd.DataFrame:
    """Return BUY/SELL quantity and count per symbol for target_date."""
    results = list(collection.aggregate(build_aggregation_pipeline(target_date)))
    if not results:
        return pd.DataFrame(columns=_EMPTY_DEALS_COLS)
    return pd.DataFrame(results)
