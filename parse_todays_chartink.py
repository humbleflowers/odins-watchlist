"""Parse Chartink screener data from MongoDB."""

import pandas as pd
from pymongo.collection import Collection


def get_today_chartink_data(collection: Collection, target_date) -> pd.DataFrame:
    """Fetch Chartink records for a specific date from MongoDB."""
    return pd.DataFrame(list(collection.find({"Date": target_date})))
