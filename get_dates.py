"""Trading-day date utilities for NSE bhavcopy downloads."""

from datetime import date, datetime, timedelta

# NSE holidays in ddmmyyyy format — update annually
NSE_HOLIDAYS = {
    "15082025", "27082025", "02102025", "21102025", "22102025",
    "05112025", "25122025",
}


def daterange(start_date: datetime, end_date: datetime):
    """Yield each calendar day from start_date through end_date inclusive."""
    for n in range((end_date - start_date).days + 1):
        yield start_date + timedelta(n)


def exclude_holidays(date_list: list[str]) -> list[str]:
    """Remove known NSE holidays from a list of ddmmyyyy date strings."""
    return [d for d in date_list if d not in NSE_HOLIDAYS]


def is_trading_day(d: date) -> bool:
    """Return True if d is a weekday and not an NSE holiday."""
    return d.weekday() < 5 and d.strftime("%d%m%Y") not in NSE_HOLIDAYS


def previous_trading_day(from_date: date | None = None) -> date:
    """
    Return the most recent NSE trading day before from_date.

    Defaults to the last trading day before today (skips weekends and holidays).
    """
    d = (from_date or date.today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def download_range(start_ddmmyyyy: str, end_ddmmyyyy: str | None = None) -> list[str]:
    """
    Build a list of trading-day date strings (ddmmyyyy) for bhavcopy download.

    Skips weekends and dates in NSE_HOLIDAYS.
    """
    start = datetime.strptime(start_ddmmyyyy, "%d%m%Y")
    end = datetime.strptime(end_ddmmyyyy, "%d%m%Y") if end_ddmmyyyy else start

    dates = []
    for day in daterange(start, end):
        if day.weekday() < 5:
            dates.append(day.strftime("%d%m%Y"))
    return exclude_holidays(dates)
