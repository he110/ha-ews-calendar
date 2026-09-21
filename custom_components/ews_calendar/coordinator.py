"""Per-calendar data update coordinator."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.calendar import CalendarEvent
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import EwsClient
from .const import (
    CONF_DAYS_AHEAD,
    CONF_EXCLUDE_CANCELLED,
    CONF_EXCLUDE_DECLINED,
    CONF_INCLUDE_DESCRIPTION,
    CONF_MAILBOX,
    CONF_SCAN_INTERVAL,
    DAYS_BEHIND,
    DEFAULT_OPTIONS,
    DOMAIN,
    NO_SUBJECT,
)
from .models import EwsAuthError, EwsError, EwsEvent, EwsServerBusyError

_LOGGER = logging.getLogger(__name__)


@dataclass
class EwsRuntimeData:
    client: EwsClient
    coordinators: dict[str, EwsCalendarCoordinator] = field(default_factory=dict)


type EwsConfigEntry = ConfigEntry[EwsRuntimeData]


def to_calendar_event(event: EwsEvent) -> CalendarEvent:
    """EWS item → Home Assistant CalendarEvent."""
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    if event.all_day:
        # EWS sends midnight in the mailbox time zone, in UTC; read it in HA's zone.
        start = dt_util.as_local(event.start).date()
        end = dt_util.as_local(event.end).date()
        if end <= start:
            end = start + dt.timedelta(days=1)
    else:
        start = dt_util.as_local(event.start)
        end = max(dt_util.as_local(event.end), start)
    return CalendarEvent(
        start=start,
        end=end,
        summary=event.subject or NO_SUBJECT,
        description=event.description or None,
        location=event.location or None,
        uid=event.uid,
        recurrence_id=event.recurrence_id,
    )


def event_filter(options: Mapping[str, Any]):
    exclude_declined = options.get(CONF_EXCLUDE_DECLINED, True)
    exclude_cancelled = options.get(CONF_EXCLUDE_CANCELLED, True)

    def keep(event: EwsEvent) -> bool:
        if exclude_cancelled and event.cancelled:
            return False
        return not (exclude_declined and event.my_response == "Decline")

    return keep


@dataclass
class CalendarWindow:
    start: dt.datetime
    end: dt.datetime
    events: list[CalendarEvent]


class EwsCalendarCoordinator(DataUpdateCoordinator[CalendarWindow]):
    """Fetches a rolling window of one calendar (own or shared mailbox)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EwsConfigEntry,
        subentry: ConfigSubentry,
        client: EwsClient,
    ) -> None:
        options = {**DEFAULT_OPTIONS, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {subentry.title}",
            update_interval=dt.timedelta(minutes=options[CONF_SCAN_INTERVAL]),
            config_entry=entry,
        )
        self.subentry = subentry
        self.client = client
        self.mailbox: str | None = subentry.data.get(CONF_MAILBOX) or None
        self._days_ahead = dt.timedelta(days=options[CONF_DAYS_AHEAD])
        self._with_bodies = options[CONF_INCLUDE_DESCRIPTION]
        self._keep = event_filter(options)

    async def fetch(
        self, start: dt.datetime, end: dt.datetime, *, with_bodies: bool | None = None
    ) -> list[CalendarEvent]:
        """Query EWS directly (also used for ranges outside the cached window)."""
        events = await self.client.find_events(
            start,
            end,
            self.mailbox,
            with_bodies=self._with_bodies if with_bodies is None else with_bodies,
        )
        return [to_calendar_event(e) for e in events if self._keep(e)]

    async def _async_update_data(self) -> CalendarWindow:
        start = dt_util.start_of_local_day() - dt.timedelta(days=DAYS_BEHIND)
        # One spare day: a "next N days" request made a moment after the refresh
        # must still be served from cache (with descriptions) rather than live.
        end = dt_util.now() + self._days_ahead + dt.timedelta(days=1)
        try:
            events = await self.fetch(start, end)
        except EwsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EwsServerBusyError as err:
            raise UpdateFailed(f"Exchange is busy: {err}", retry_after=err.backoff) from err
        except EwsError as err:
            raise UpdateFailed(str(err)) from err
        return CalendarWindow(start=start, end=end, events=events)
