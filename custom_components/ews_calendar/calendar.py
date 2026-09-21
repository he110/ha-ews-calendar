"""Calendar entities: one per configured calendar (config subentry)."""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EwsCalendarCoordinator, EwsConfigEntry
from .models import EwsError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EwsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        async_add_entities([EwsCalendarEntity(coordinator)], config_subentry_id=subentry_id)


class EwsCalendarEntity(CoordinatorEntity[EwsCalendarCoordinator], CalendarEntity):
    """A read-only Exchange calendar."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: EwsCalendarCoordinator) -> None:
        super().__init__(coordinator)
        subentry = coordinator.subentry
        self._attr_unique_id = subentry.subentry_id
        parts = urlsplit(coordinator.client.url)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Microsoft",
            model="Exchange calendar",
            sw_version=coordinator.client.server_version,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"{parts.scheme}://{parts.netloc}/owa/",
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next upcoming event."""
        if self.coordinator.data is None:
            return None
        now = dt_util.now()
        upcoming = [e for e in self.coordinator.data.events if e.end_datetime_local > now]
        return min(upcoming, key=lambda e: e.start_datetime_local, default=None)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> list[CalendarEvent]:
        window = self.coordinator.data
        if window is not None and window.start <= start_date and end_date <= window.end:
            return [
                e
                for e in window.events
                if e.start_datetime_local < end_date and e.end_datetime_local > start_date
            ]
        # Outside the cached window (e.g. browsing another month in the UI).
        try:
            return await self.coordinator.fetch(start_date, end_date)
        except EwsError as err:
            raise HomeAssistantError(f"Exchange request failed: {err}") from err
