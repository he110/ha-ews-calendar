"""Diagnostics: safe to paste into a public GitHub issue."""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_MAILBOX
from .coordinator import EwsConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_URL, CONF_MAILBOX}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EwsConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    calendars = []
    for subentry_id, coordinator in runtime.coordinators.items():
        window = coordinator.data
        events = window.events if window else []
        calendars.append(
            {
                "subentry_id": subentry_id,
                "shared_mailbox": bool(coordinator.mailbox),
                "last_update_success": coordinator.last_update_success,
                "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
                "window": [window.start.isoformat(), window.end.isoformat()] if window else None,
                "events": len(events),
                "all_day_events": sum(1 for e in events if e.all_day),
                "recurring_instances": sum(1 for e in events if e.recurrence_id),
                "with_description": sum(1 for e in events if e.description),
                "duration_minutes": dict(
                    Counter(
                        int((e.end_datetime_local - e.start_datetime_local).total_seconds() // 60)
                        for e in events
                        if not e.all_day
                    ).most_common(5)
                ),
            }
        )
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "url_scheme": urlsplit(entry.data[CONF_URL]).scheme,
            "options": dict(entry.options),
        },
        "server_version": runtime.client.server_version,
        "calendars": calendars,
    }
