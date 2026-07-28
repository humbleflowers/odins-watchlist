"""MongoDB connection helper."""

from pymongo.collection import Collection
from pymongo import MongoClient


def connect_mongo(uri: str, db_name: str, collection_name: str) -> Collection:
    """Return a MongoDB collection handle."""
    client = MongoClient(uri)
    return client[db_name][collection_name]
