"""Async EWS client: transport, NTLM/Basic auth, calendar queries. No Home Assistant imports.

NTLM is connection-oriented: both handshake legs must travel over the same TCP
connection, and IIS then keeps the connection authenticated. The client therefore
owns its own aiohttp session with a single-connection pool and serialises requests.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import logging
import ssl
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import spnego
from spnego.channel_bindings import GssChannelBindings

from . import soap
from .models import (
    EwsAuthError,
    EwsConnectionError,
    EwsEvent,
    EwsSslError,
    FolderInfo,
)

_LOGGER = logging.getLogger(__name__)

AUTH_NTLM = "ntlm"
AUTH_BASIC = "basic"
TIMEOUT = aiohttp.ClientTimeout(total=60)
MAX_ENTRIES = 1000
BODY_BATCH = 50
MIN_SPLIT = dt.timedelta(hours=1)
HEADERS = {"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"}


def normalize_url(value: str) -> str:
    """`exchange.example.com` / `https://host` / full EWS URL → full EWS URL."""
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    path = parts.path if parts.path not in ("", "/") else "/EWS/Exchange.asmx"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _auth_schemes(headers: Any) -> list[str]:
    return [v.split(" ", 1)[0].lower() for v in headers.getall("WWW-Authenticate", [])]


class EwsClient:
    """One EWS account. Call `close()` when done."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        auth_method: str,
        *,
        ssl_context: ssl.SSLContext | bool,
        session_factory: Callable[[aiohttp.BaseConnector], aiohttp.ClientSession] | None = None,
    ) -> None:
        self.url = normalize_url(url)
        self._username = username
        self._password = password
        self._auth_method = auth_method
        self._ssl = ssl_context
        # Close idle connections before IIS does (~2 min) to avoid reusing dead ones.
        connector = aiohttp.TCPConnector(limit=1, ssl=ssl_context, keepalive_timeout=30)
        factory = session_factory or (lambda c: aiohttp.ClientSession(connector=c))
        self._session = factory(connector)
        self._lock = asyncio.Lock()
        self._channel_bindings: GssChannelBindings | None = None
        self.server_version: str | None = None

    async def close(self) -> None:
        await self._session.close()

    # --- transport -------------------------------------------------------------

    async def _send(self, body: str, authorization: str | None) -> tuple[int, Any, str]:
        headers = dict(HEADERS)
        if authorization:
            headers["Authorization"] = authorization
        for attempt in (1, 2):
            try:
                async with self._session.post(
                    self.url, data=body.encode(), headers=headers, timeout=TIMEOUT, allow_redirects=False
                ) as resp:
                    # Reading the body fully keeps the (authenticated) connection reusable.
                    return resp.status, resp.headers, await resp.text()
            except aiohttp.ClientConnectorCertificateError as err:
                raise EwsSslError(str(err)) from err
            except aiohttp.ClientConnectorError as err:
                raise EwsConnectionError(f"{type(err).__name__}: {err}") from err
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as err:
                # A pooled keep-alive connection the server has already closed.
                # Retry once on a fresh connection; a fresh one failing is real.
                if attempt == 2:
                    raise EwsConnectionError(f"{type(err).__name__}: {err}") from err
                _LOGGER.debug("Stale connection (%s), retrying", err)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EwsConnectionError(f"{type(err).__name__}: {err}") from err
        raise AssertionError("unreachable")

    async def _fetch_channel_bindings(self) -> GssChannelBindings | None:
        """tls-server-end-point CBT for Extended Protection (RFC 5929)."""
        parts = urlsplit(self.url)
        if parts.scheme != "https":
            return None
        host, port = parts.hostname or "", parts.port or 443
        if isinstance(self._ssl, ssl.SSLContext):
            ctx = self._ssl
        else:
            # Verification disabled by the user: we only need the certificate bytes.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), 30
            )
        except ssl.SSLCertVerificationError as err:
            raise EwsSslError(str(err)) from err
        except (OSError, TimeoutError) as err:
            raise EwsConnectionError(f"{type(err).__name__}: {err}") from err
        try:
            cert = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        finally:
            # Only the certificate is needed; some servers never answer close_notify.
            writer.transport.abort()
        if not cert:
            return None
        digest = hashlib.sha256(cert).digest()
        return GssChannelBindings(application_data=b"tls-server-end-point:" + digest)

    async def _ntlm_handshake(self, body: str) -> tuple[int, Any, str]:
        if self._channel_bindings is None:
            self._channel_bindings = await self._fetch_channel_bindings()
        context = spnego.client(
            self._username,
            self._password,
            hostname=urlsplit(self.url).hostname,
            service="HTTP",
            protocol="ntlm",
            channel_bindings=self._channel_bindings,
        )
        negotiate = base64.b64encode(context.step()).decode()
        status, headers, text = await self._send(body, f"NTLM {negotiate}")
        challenge = next(
            (v[5:] for v in headers.getall("WWW-Authenticate", []) if v.startswith("NTLM ")),
            None,
        )
        if status != 401 or challenge is None:
            if status == 401:
                raise EwsAuthError("Server did not offer an NTLM challenge")
            return status, headers, text
        try:
            authenticate = context.step(base64.b64decode(challenge))
        except spnego.exceptions.SpnegoError as err:
            raise EwsAuthError(f"NTLM: {err}") from err
        return await self._send(body, f"NTLM {base64.b64encode(authenticate).decode()}")

    async def _post_ntlm(self, body: str) -> tuple[int, Any, str]:
        # The pooled connection may still be authenticated from the previous request.
        status, headers, text = await self._send(body, None)
        if status != 401:
            return status, headers, text
        status, headers, text = await self._ntlm_handshake(body)
        if status == 401 and self._channel_bindings is not None:
            # Certificate might have been rotated: refresh the binding once.
            self._channel_bindings = None
            status, headers, text = await self._ntlm_handshake(body)
        return status, headers, text

    async def _post(self, body: str) -> ET.Element:
        async with self._lock:
            if self._auth_method == AUTH_NTLM:
                status, _, text = await self._post_ntlm(body)
            else:
                token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
                status, _, text = await self._send(body, f"Basic {token}")
        if status == 401:
            raise EwsAuthError("HTTP 401")
        if status == 403:
            # Not a password problem: EWS is usually disabled for this mailbox.
            raise EwsConnectionError("HTTP 403: EWS access is forbidden for this account")
        try:
            root = soap.parse_envelope(text)
        except soap.NotSoapError as err:
            raise EwsConnectionError(f"HTTP {status}: not an EWS response") from err
        self.server_version = soap.server_version(root) or self.server_version
        return root

    # --- discovery ---------------------------------------------------------------

    async def detect_auth_methods(self) -> list[str]:
        """Auth schemes the server offers, in our order of preference."""
        async with self._lock:
            status, headers, _ = await self._send(soap.get_folder_request(None), None)
        offered = _auth_schemes(headers) if status == 401 else []
        return [m for m in (AUTH_NTLM, AUTH_BASIC) if m in offered]

    # --- API ---------------------------------------------------------------------

    async def get_calendar_folder(self, mailbox: str | None = None) -> FolderInfo:
        return soap.parse_get_folder(await self._post(soap.get_folder_request(mailbox)))

    async def find_events(
        self,
        start: dt.datetime,
        end: dt.datetime,
        mailbox: str | None = None,
        *,
        with_bodies: bool = False,
    ) -> list[EwsEvent]:
        events = await self._find(start, end, mailbox)
        # A split window can return the same occurrence twice (it overlaps both halves).
        unique = list({e.item_id: e for e in events}.values())
        if with_bodies and unique:
            bodies: dict[str, str] = {}
            ids = [e.item_id for e in unique]
            for i in range(0, len(ids), BODY_BATCH):
                batch = ids[i : i + BODY_BATCH]
                root = await self._post(soap.get_bodies_request(batch))
                bodies |= soap.parse_bodies(root, batch)
            unique = [replace(e, description=bodies.get(e.item_id, "")) for e in unique]
        return sorted(unique, key=lambda e: (e.start, e.end))

    async def _find(self, start: dt.datetime, end: dt.datetime, mailbox: str | None) -> list[EwsEvent]:
        root = await self._post(soap.find_calendar_request(mailbox, start, end, MAX_ENTRIES))
        events, complete = soap.parse_find_calendar(root)
        if complete or end - start <= MIN_SPLIT:
            if not complete:
                _LOGGER.warning("More than %s items between %s and %s", MAX_ENTRIES, start, end)
            return events
        middle = start + (end - start) / 2
        return [*await self._find(start, middle, mailbox), *await self._find(middle, end, mailbox)]
