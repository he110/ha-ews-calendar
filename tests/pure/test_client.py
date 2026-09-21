import datetime as dt

import fake_ews
import pytest
from ec import client as client_mod
from ec.client import AUTH_BASIC, AUTH_NTLM, EwsClient
from ec.models import (
    EwsAuthError,
    EwsConnectionError,
    EwsResponseError,
    EwsServerBusyError,
)

NOW = dt.datetime(2026, 9, 21, 12, tzinfo=dt.UTC)


def item(n, hours, **kw):
    start = NOW + dt.timedelta(hours=hours)
    return fake_ews.Item(
        item_id=f"I{n}",
        subject=f"Event {n}",
        start=start,
        end=start + dt.timedelta(minutes=30),
        uid=f"U{n}",
        **kw,
    )


def make(url, method=AUTH_NTLM, password=fake_ews.PASSWORD, login=None):
    login = login or (fake_ews.NTLM_LOGIN if method == AUTH_NTLM else fake_ews.BASIC_LOGIN)
    return EwsClient(url, login, password, method, ssl_context=False)


async def test_ntlm_handshake_then_connection_reuse(ews):
    server, url = ews
    server.mailboxes[fake_ews.OWN] = [item(1, 1)]
    c = make(url)
    try:
        folder = await c.get_calendar_folder()
        assert folder.display_name == "Calendar"
        assert c.server_version == "15.2.1748.39"
        await c.find_events(NOW, NOW + dt.timedelta(days=1))
        await c.find_events(NOW, NOW + dt.timedelta(days=1))
        assert server.handshakes == 1  # IIS-style: the connection stays authenticated
    finally:
        await c.close()


async def test_ntlm_wrong_password(ews):
    _, url = ews
    c = make(url, password="nope")
    try:
        with pytest.raises(EwsAuthError):
            await c.get_calendar_folder()
    finally:
        await c.close()


async def test_basic(ews):
    server, url = ews
    server.offer_ntlm = False
    c = make(url, AUTH_BASIC)
    try:
        assert (await c.get_calendar_folder()).display_name == "Calendar"
    finally:
        await c.close()
    bad = make(url, AUTH_BASIC, password="nope")
    try:
        with pytest.raises(EwsAuthError):
            await bad.get_calendar_folder()
    finally:
        await bad.close()


async def test_detect_auth_methods(ews):
    server, url = ews
    c = make(url)
    try:
        assert await c.detect_auth_methods() == [AUTH_NTLM, AUTH_BASIC]
        server.offer_ntlm = False
        assert await c.detect_auth_methods() == [AUTH_BASIC]
    finally:
        await c.close()


async def test_events_bodies_matched_by_position(ews):
    server, url = ews
    server.mailboxes[fake_ews.OWN] = [item(1, 1, body="Join: https://meet/1"), item(2, 2, body="")]
    c = make(url)
    try:
        events = await c.find_events(NOW, NOW + dt.timedelta(days=1), with_bodies=True)
    finally:
        await c.close()
    assert [e.description for e in events] == ["Join: https://meet/1", ""]


async def test_window_split_when_server_truncates(ews, monkeypatch):
    server, url = ews
    server.mailboxes[fake_ews.OWN] = [item(n, n * 5) for n in range(1, 13)]
    monkeypatch.setattr(client_mod, "MAX_ENTRIES", 3)
    c = make(url)
    try:
        events = await c.find_events(NOW, NOW + dt.timedelta(days=3))
    finally:
        await c.close()
    assert [e.item_id for e in events] == [f"I{n}" for n in range(1, 13)]
    assert server.requests.count("FindItem") > 1


async def test_shared_mailbox_errors(ews):
    server, url = ews
    server.denied_mailboxes.add("boss@example.com")
    c = make(url)
    try:
        with pytest.raises(EwsResponseError) as err:
            await c.get_calendar_folder("nobody@example.com")
        assert err.value.code == "ErrorNonExistentMailbox"
        with pytest.raises(EwsResponseError) as err:
            await c.get_calendar_folder("boss@example.com")
        assert err.value.code == "ErrorAccessDenied"
    finally:
        await c.close()


async def test_busy_and_forbidden(ews):
    server, url = ews
    c = make(url)
    try:
        server.busy_backoff_ms = 1500
        with pytest.raises(EwsServerBusyError) as err:
            await c.find_events(NOW, NOW + dt.timedelta(days=1))
        assert err.value.backoff == 1.5
        server.busy_backoff_ms = None
        server.forbidden = True
        with pytest.raises(EwsConnectionError):
            await c.get_calendar_folder()
    finally:
        await c.close()


async def test_unreachable():
    c = EwsClient("http://127.0.0.1:9", "u", "p", AUTH_BASIC, ssl_context=False)
    try:
        with pytest.raises(EwsConnectionError):
            await c.get_calendar_folder()
    finally:
        await c.close()


async def test_retries_once_on_connection_closed_by_server(ews):
    """IIS closes idle keep-alive connections; reusing one must not fail the request."""
    import asyncio

    server, url = ews
    server.drop_connections = True
    c = make(url, AUTH_BASIC)
    try:
        for _ in range(3):
            assert (await c.get_calendar_folder()).display_name == "Calendar"
            await asyncio.sleep(0.1)  # let the server drop the pooled connection
    finally:
        await c.close()
