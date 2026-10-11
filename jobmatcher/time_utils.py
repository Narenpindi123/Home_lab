"""Local calendar dates for display; UTC instants for storage and comparisons."""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

APP_TIMEZONE = os.environ.get("JOB_MATCHER_TIMEZONE", "America/Chicago")
LOCAL_ZONE = ZoneInfo(APP_TIMEZONE)
# Host scrapers wrote untagged Eastern timestamps before the Texas move.
LEGACY_SCRAPER_ZONE = ZoneInfo("America/New_York")


def local_now():
    return datetime.now(LOCAL_ZONE)


def utc_day_bounds(now=None):
    """Return SQLite UTC boundaries for the current local calendar day."""
    now = now or local_now()
    start = now.astimezone(LOCAL_ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return tuple(value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                 for value in (start, end))


def parse_scraped_at(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=LEGACY_SCRAPER_ZONE)
    return stamp


def scrape_is_fresh(stamp, now=None):
    age = (now or datetime.now(timezone.utc)) - stamp.astimezone(timezone.utc)
    return timedelta(0) <= age < timedelta(hours=24)
