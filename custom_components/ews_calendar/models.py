"""Data model and errors. No Home Assistant imports."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class EwsEvent:
    """A calendar item as returned by EWS FindItem/CalendarView.

    Times are timezone-aware UTC. For all-day events EWS returns midnight in the
    mailbox time zone, expressed in UTC; converting to a date is the caller's job.
    """

    item_id: str
    uid: str | None
    recurrence_id: str | None
    subject: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool
    location: str = ""
    item_type: str | None = None  # Single / Occurrence / Exception / RecurringMaster
    my_response: str | None = None  # Accept / Tentative / Decline / Organizer / ...
    free_busy: str | None = None  # Free / Tentative / Busy / OOF / WorkingElsewhere
    cancelled: bool = False
    description: str = ""


@dataclass(frozen=True)
class FolderInfo:
    display_name: str
    server_version: str | None


class EwsError(Exception):
    """Base class for all errors talking to EWS."""


class EwsAuthError(EwsError):
    """Credentials were rejected."""


class EwsConnectionError(EwsError):
    """Network problem, timeout or a non-SOAP HTTP error."""


class EwsSslError(EwsConnectionError):
    """TLS certificate could not be verified."""


class EwsResponseError(EwsError):
    """EWS returned an error ResponseCode (e.g. ErrorNonExistentMailbox)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class EwsServerBusyError(EwsResponseError):
    """ErrorServerBusy: the server asks to back off for `backoff` seconds."""

    def __init__(self, message: str, backoff: float | None) -> None:
        super().__init__("ErrorServerBusy", message)
        self.backoff = backoff
