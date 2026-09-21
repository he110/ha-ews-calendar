"""End-to-end: account → calendars → events → options → failures → reauth."""

from __future__ import annotations

import datetime as dt

import fake_ews
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ews_calendar.const import DOMAIN

OWN_ENTITY = "calendar.exch_alice"


def when(hours: float) -> dt.datetime:
    return dt_util.now().replace(microsecond=0) + dt.timedelta(hours=hours)


def item(n: int, hours: float, **kw) -> fake_ews.Item:
    start = when(hours)
    kw.setdefault("subject", f"Meeting {n}")
    kw.setdefault("uid", f"U{n}")
    return fake_ews.Item(item_id=f"I{n}", start=start, end=start + dt.timedelta(minutes=30), **kw)


async def add_account(hass: HomeAssistant, url: str, password: str = fake_ews.PASSWORD, **extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "url": url,
            "username": fake_ews.NTLM_LOGIN,
            "password": password,
            "auth_method": "auto",
            "verify_ssl": True,
            **extra,
        },
    )


async def events_of(hass: HomeAssistant, entity_id: str, days: int = 7) -> list[dict]:
    resp = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity_id, "duration": {"days": days}},
        blocking=True,
        return_response=True,
    )
    return resp[entity_id]["events"]


async def test_account_calendar_and_events(hass: HomeAssistant, ews) -> None:
    server, url = ews
    local_midnight = dt_util.start_of_local_day() + dt.timedelta(days=2)
    server.mailboxes[fake_ews.OWN] = [
        item(1, 1, body="Join: https://meet/1", location="Room 1"),
        item(2, 3, my_response="Decline", subject="Declined"),
        item(3, 5, cancelled=True, subject="Cancelled"),
        item(4, 7, item_type="Occurrence", recurrence_id=when(7), uid="SERIES"),
        fake_ews.Item(
            item_id="I5",
            subject="Holiday",
            uid="U5",
            all_day=True,
            start=local_midnight,
            end=local_midnight + dt.timedelta(days=1),
        ),
    ]
    result = await add_account(hass, url)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    entry = result["result"]
    assert entry.data["auth_method"] == "ntlm"  # auto → NTLM when offered
    assert entry.data["url"] == f"{url}/EWS/Exchange.asmx"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(OWN_ENTITY)
    assert state is not None, hass.states.async_entity_ids("calendar")
    assert state.attributes["message"] == "Meeting 1"

    # A "next 30 days" request (the configured horizon) is served with descriptions.
    events = await events_of(hass, OWN_ENTITY, days=30)
    by_summary = {e["summary"]: e for e in events}
    assert set(by_summary) == {"Meeting 1", "Meeting 4", "Holiday"}  # declined + cancelled hidden
    assert by_summary["Meeting 1"]["description"] == "Join: https://meet/1"
    assert by_summary["Meeting 1"]["location"] == "Room 1"
    assert by_summary["Holiday"]["start"] == local_midnight.date().isoformat()

    # Options: show declined, no descriptions → reload applies them.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 15,
            "days_ahead": 30,
            "include_description": False,
            "exclude_declined": False,
            "exclude_cancelled": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    events = await events_of(hass, OWN_ENTITY)
    assert "Declined" in {e["summary"] for e in events}
    assert all("description" not in e for e in events)

    # Outside the cached window → live query.
    server.mailboxes[fake_ews.OWN].append(item(9, 24 * 60, subject="Far away"))
    far = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": OWN_ENTITY, "start_date_time": when(24 * 59), "end_date_time": when(24 * 61)},
        blocking=True,
        return_response=True,
    )
    assert [e["summary"] for e in far[OWN_ENTITY]["events"]] == ["Far away"]


async def test_add_account_errors(hass: HomeAssistant, ews) -> None:
    server, url = ews
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    async def submit(**changes):
        data = {
            "url": url,
            "username": fake_ews.NTLM_LOGIN,
            "password": fake_ews.PASSWORD,
            "auth_method": "auto",
            "verify_ssl": True,
            **changes,
        }
        return await hass.config_entries.flow.async_configure(flow["flow_id"], data)

    assert (await submit(password="wrong"))["errors"] == {"base": "invalid_auth"}
    assert (await submit(url="http://127.0.0.1:9"))["errors"] == {"base": "cannot_connect"}
    server.offer_ntlm = server.offer_basic = False
    assert (await submit())["errors"] == {"base": "no_auth_method"}
    server.offer_ntlm = server.offer_basic = True
    assert (await submit())["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await add_account(hass, url)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_shared_calendar(hass: HomeAssistant, ews) -> None:
    server, url = ews
    server.mailboxes["room@example.com"] = [item(1, 2, subject="Booked")]
    server.denied_mailboxes.add("boss@example.com")
    entry = (await add_account(hass, url))["result"]
    await hass.async_block_till_done()

    async def add(mailbox, name=None):
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "calendar"), context={"source": SOURCE_USER}
        )
        data = {"mailbox": mailbox} | ({"name": name} if name else {})
        return await hass.config_entries.subentries.async_configure(flow["flow_id"], data)

    assert (await add("nobody@example.com"))["errors"] == {"mailbox": "mailbox_not_found"}
    assert (await add("boss@example.com"))["errors"] == {"mailbox": "access_denied"}
    result = await add("room@example.com", "Meeting room")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    events = await events_of(hass, "calendar.meeting_room")
    assert [e["summary"] for e in events] == ["Booked"]
    assert (await add("Room@Example.com"))["reason"] == "already_configured"


async def test_busy_then_recovery(hass: HomeAssistant, ews) -> None:
    server, url = ews
    server.mailboxes[fake_ews.OWN] = [item(1, 1)]
    await add_account(hass, url)
    await hass.async_block_till_done()
    assert hass.states.get(OWN_ENTITY).state != "unavailable"

    server.busy_backoff_ms = 1000
    async_fire_time_changed(hass, dt_util.utcnow() + dt.timedelta(minutes=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(OWN_ENTITY).state == "unavailable"

    server.busy_backoff_ms = None
    async_fire_time_changed(hass, dt_util.utcnow() + dt.timedelta(minutes=22))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(OWN_ENTITY).state != "unavailable"


async def test_password_change_triggers_reauth(hass: HomeAssistant, ews) -> None:
    server, url = ews
    entry = (await add_account(hass, url))["result"]
    await hass.async_block_till_done()

    server.set_password("rotated")
    async_fire_time_changed(hass, dt_util.utcnow() + dt.timedelta(minutes=11))
    await hass.async_block_till_done(wait_background_tasks=True)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"password": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"password": "rotated"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data["password"] == "rotated"
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_to_basic(hass: HomeAssistant, ews) -> None:
    _, url = ews
    entry = (await add_account(hass, url))["result"]
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"url": url, "password": fake_ews.PASSWORD, "auth_method": "basic", "verify_ssl": True},
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["auth_method"] == "basic"
    assert entry.state is ConfigEntryState.LOADED


async def test_diagnostics_redacted(hass: HomeAssistant, ews) -> None:
    _, url = ews
    entry = (await add_account(hass, url))["result"]
    await hass.async_block_till_done()
    from custom_components.ews_calendar.diagnostics import async_get_config_entry_diagnostics

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert fake_ews.PASSWORD not in text and "alice" not in text and "127.0.0.1" not in text
    assert diag["server_version"] == "15.2.1748.39"
    assert diag["calendars"][0]["last_update_success"] is True


async def test_server_down_at_startup_retries(hass: HomeAssistant, ews) -> None:
    server, url = ews
    entry = (await add_account(hass, url))["result"]
    await hass.async_block_till_done()
    server.forbidden = True
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_brand_images_are_served_locally(hass: HomeAssistant) -> None:
    """HA 2026.3+ serves custom integration icons from custom_components/<domain>/brand/."""
    import pytest
    from homeassistant.loader import async_get_custom_components

    integration = (await async_get_custom_components(hass))[DOMAIN]
    if not hasattr(integration, "has_branding"):
        pytest.skip("Local brand images need Home Assistant 2026.3+")
    assert integration.has_branding
