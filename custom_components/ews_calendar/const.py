"""Constants for the Exchange Calendar (EWS) integration."""

from __future__ import annotations

DOMAIN = "ews_calendar"

CONF_AUTH_METHOD = "auth_method"
CONF_MAILBOX = "mailbox"

AUTH_AUTO = "auto"

SUBENTRY_CALENDAR = "calendar"

# Options.
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DAYS_AHEAD = "days_ahead"
CONF_INCLUDE_DESCRIPTION = "include_description"
CONF_EXCLUDE_DECLINED = "exclude_declined"
CONF_EXCLUDE_CANCELLED = "exclude_cancelled"

DEFAULT_SCAN_INTERVAL = 10  # minutes
MIN_SCAN_INTERVAL = 5
DEFAULT_DAYS_AHEAD = 30
DEFAULT_OPTIONS = {
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_DAYS_AHEAD: DEFAULT_DAYS_AHEAD,
    CONF_INCLUDE_DESCRIPTION: True,
    CONF_EXCLUDE_DECLINED: True,
    CONF_EXCLUDE_CANCELLED: True,
}

# How far back the cached window reaches, so "today" and yesterday's
# all-day events are always served from cache.
DAYS_BEHIND = 1

NO_SUBJECT = "(no subject)"
