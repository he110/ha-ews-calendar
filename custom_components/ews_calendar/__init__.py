"""Exchange Calendar (EWS): Microsoft Exchange Server calendars in Home Assistant."""

from __future__ import annotations

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.util.ssl import get_default_context, get_default_no_verify_context

from .client import EwsClient
from .const import CONF_AUTH_METHOD, SUBENTRY_CALENDAR
from .coordinator import EwsCalendarCoordinator, EwsConfigEntry, EwsRuntimeData
from .models import EwsAuthError, EwsError

PLATFORMS: list[Platform] = [Platform.CALENDAR]


def create_client(data) -> EwsClient:
    """Build a client from config entry data (also used by the config flow)."""
    verify = data.get(CONF_VERIFY_SSL, True)
    return EwsClient(
        data[CONF_URL],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_AUTH_METHOD],
        ssl_context=get_default_context() if verify else get_default_no_verify_context(),
    )


async def async_setup_entry(hass: HomeAssistant, entry: EwsConfigEntry) -> bool:
    client = create_client(entry.data)
    # Validate credentials and reachability once, on the account's own calendar.
    # Shared calendars are refreshed independently so one missing permission
    # does not keep the whole account from loading.
    try:
        await client.get_calendar_folder()
    except EwsAuthError as err:
        await client.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except EwsError as err:
        await client.close()
        raise ConfigEntryNotReady(str(err)) from err

    runtime = EwsRuntimeData(client=client)
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_CALENDAR:
            continue
        coordinator = EwsCalendarCoordinator(hass, entry, subentry, client)
        await coordinator.async_refresh()
        runtime.coordinators[subentry_id] = coordinator
    entry.runtime_data = runtime

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EwsConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: EwsConfigEntry) -> None:
    """Options changed or a calendar was added/removed."""
    await hass.config_entries.async_reload(entry.entry_id)
