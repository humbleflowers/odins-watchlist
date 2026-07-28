"""Shared configuration for the Odin's Watchlist pipeline."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Base directory (working_version/)
BASE_DIR = Path(__file__).resolve().parent

# MongoDB
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "odins_watchlist"

# Collections
COL_CHARTINK = "chartink_screeners"
COL_DELIVERY = "nse_delivery"
COL_BULK = "nse_bulk"
COL_BLOCK = "nse_block"

# Paths
CONFIG_DIR = BASE_DIR / "config"
CHARTINK_CONFIG = CONFIG_DIR / "chartink_screeners_url.yaml"
SECTORS_CSV = CONFIG_DIR / "sectors.csv"
CHARTINK_DOWNLOADS = BASE_DIR / "chartink_downloads"
NSE_DELIVERY_DIR = BASE_DIR / "nse_downloads" / "delivery"
NSE_BULK_DIR = BASE_DIR / "nse_downloads" / "bulk"
NSE_BLOCK_DIR = BASE_DIR / "nse_downloads" / "block"

# Chartink
CHROMEDRIVER_PATH = None  # None = Selenium Manager auto-downloads matching ChromeDriver

# NSE request headers
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.nseindia.com",
}


def date_strings(target: date | None = None) -> dict:
    """
    Return common date representations for a pipeline run.

    Keys:
        date:     datetime.date object
        iso:      YYYY-MM-DD (Chartink folder, display)
        ddmmyyyy: DDMMYYYY (NSE filenames, output CSV)
        dt_utc:   midnight UTC datetime (NSE delivery/deals in MongoDB)
        dt_naive: midnight naive datetime (Chartink dates in MongoDB)
    """
    d = target or date.today()
    midnight = datetime.min.time()
    return {
        "date": d,
        "iso": d.strftime("%Y-%m-%d"),
        "ddmmyyyy": d.strftime("%d%m%Y"),
        "dt_utc": datetime.combine(d, midnight).replace(tzinfo=timezone.utc),
        "dt_naive": datetime.combine(d, midnight),
    }


def today_strings() -> dict:
    """Return date representations for today."""
    return date_strings()


def yesterday_strings() -> dict:
    """Return date representations for the previous calendar day."""
    return date_strings(date.today() - timedelta(days=1))


def parse_date_arg(value: str) -> date:
    """Parse a CLI date string in YYYY-MM-DD or DDMMYYYY format."""
    value = value.strip()
    if len(value) == 10 and value[4] == "-":
        return datetime.strptime(value, "%Y-%m-%d").date()
    if len(value) == 8 and value.isdigit():
        return datetime.strptime(value, "%d%m%Y").date()
    raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD or DDMMYYYY.")
