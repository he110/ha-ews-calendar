"""In-memory EWS server for tests.

Emulates what matters about IIS + Exchange:
- a real NTLM handshake (pyspnego's NTLM acceptor), authentication bound to the TCP
  connection, like IIS does — later requests on that connection need no header;
- Basic authentication;
- GetFolder / FindItem with CalendarView / GetItem (bodies), shared mailboxes;
- MaxEntriesReturned with IncludesLastItemInRange=false, error injection.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import re
import tempfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import spnego
from aiohttp import web

DOMAIN = "EXCH"
USER = "alice"
PASSWORD = "s3cret"
NTLM_LOGIN = f"{DOMAIN}\\{USER}"
BASIC_LOGIN = f"{USER}@example.com"
OWN = "alice@example.com"


def _write_ntlm_user_file(password: str) -> str:
    f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".ntlm")  # noqa: SIM115
    f.write(f"{DOMAIN}:{USER}:{password}\n")
    f.close()
    return f.name


@dataclass
class Item:
    item_id: str
    subject: str
    start: dt.datetime
    end: dt.datetime
    uid: str = "UID-1"
    recurrence_id: dt.datetime | None = None
    all_day: bool = False
    location: str = ""
    item_type: str = "Single"
    my_response: str = "Accept"
    cancelled: bool = False
    body: str = ""


@dataclass
class FakeEws:
    password: str = PASSWORD
    offer_ntlm: bool = True
    offer_basic: bool = True
    mailboxes: dict[str, list[Item]] = field(default_factory=lambda: {OWN: []})
    denied_mailboxes: set[str] = field(default_factory=set)
    busy_backoff_ms: int | None = None
    forbidden: bool = False
    # Close every connection right after responding, without "Connection: close" —
    # what IIS does to idle keep-alive connections. The client must not trip over it.
    drop_connections: bool = False
    # Observability for tests.
    handshakes: int = 0
    requests: list[str] = field(default_factory=list)
    _authed_connections: set[int] = field(default_factory=set)
    _contexts: dict[int, object] = field(default_factory=dict)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/EWS/Exchange.asmx", self.handle)
        return app

    def set_password(self, password: str) -> None:
        self.password = password
        self._authed_connections.clear()

    # --- auth -------------------------------------------------------------------

    def _challenge_headers(self) -> list[tuple[str, str]]:
        headers = []
        if self.offer_ntlm:
            headers.append(("WWW-Authenticate", "NTLM"))
        if self.offer_basic:
            headers.append(("WWW-Authenticate", 'Basic realm="example.com"'))
        return headers

    def _unauthorized(self, extra: list[tuple[str, str]] | None = None) -> web.Response:
        resp = web.Response(status=401, text="Unauthorized")
        for name, value in extra or self._challenge_headers():
            resp.headers.add(name, value)
        return resp

    def _authenticate(self, request: web.Request) -> web.Response | None:
        """None → authenticated; otherwise the response to send."""
        conn = id(request.transport)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic ") and self.offer_basic:
            login, _, password = base64.b64decode(header[6:]).decode().partition(":")
            if login in (BASIC_LOGIN, NTLM_LOGIN) and password == self.password:
                return None
            return self._unauthorized()
        if header.startswith("NTLM ") and self.offer_ntlm:
            token = base64.b64decode(header[5:])
            if token[8:12] == b"\x01\x00\x00\x00":  # NEGOTIATE → fresh server context
                os.environ["NTLM_USER_FILE"] = _write_ntlm_user_file(self.password)
                self._contexts[conn] = spnego.server(protocol="ntlm")
            ctx = self._contexts.get(conn)
            if ctx is None:
                return self._unauthorized()
            try:
                out = ctx.step(token)
            except spnego.exceptions.SpnegoError:
                self._contexts.pop(conn, None)
                return self._unauthorized()
            if out:  # CHALLENGE
                return self._unauthorized([("WWW-Authenticate", f"NTLM {base64.b64encode(out).decode()}")])
            self._contexts.pop(conn, None)
            self._authed_connections.add(conn)
            self.handshakes += 1
            return None
        if conn in self._authed_connections:
            return None
        return self._unauthorized()

    # --- EWS --------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.Response:
        if self.drop_connections and request.transport is not None:
            transport = request.transport

            asyncio.get_running_loop().call_later(0.05, transport.close)
        body = await request.text()
        denied = self._authenticate(request)
        if denied is not None:
            return denied
        if self.forbidden:
            return web.Response(status=403, text="Forbidden")
        op = re.search(r"<m:(GetFolder|FindItem|GetItem)\b", body)
        self.requests.append(op.group(1) if op else "?")
        if self.busy_backoff_ms is not None:
            return _xml(_fault_busy(self.busy_backoff_ms), status=500)
        mailbox = (re.search(r"<t:EmailAddress>([^<]+)</t:EmailAddress>", body) or [None, OWN])[1]
        if op is None:
            return _xml(
                _envelope("<s:Fault><faultcode>a:ErrorSchemaValidation</faultcode></s:Fault>"), status=500
            )
        name = op.group(1)
        if name in ("GetFolder", "FindItem"):
            if mailbox in self.denied_mailboxes:
                return _xml(_error_message(name, "ErrorAccessDenied"))
            if mailbox not in self.mailboxes:
                return _xml(_error_message(name, "ErrorNonExistentMailbox"))
        if name == "GetFolder":
            return _xml(_get_folder_response())
        if name == "FindItem":
            m = re.search(r'MaxEntriesReturned="(\d+)" StartDate="([^"]+)" EndDate="([^"]+)"', body)
            limit = int(m.group(1))
            start, end = (dt.datetime.fromisoformat(v.replace("Z", "+00:00")) for v in m.group(2, 3))
            items = sorted(
                (i for i in self.mailboxes[mailbox] if i.start < end and i.end > start),
                key=lambda i: i.start,
            )
            return _xml(_find_response(items[:limit], complete=len(items) <= limit))
        ids = re.findall(r'<t:ItemId Id="([^"]+)"', body)
        by_id = {i.item_id: i for items in self.mailboxes.values() for i in items}
        return _xml(_get_item_response([by_id.get(i) for i in ids]))


# --- XML ------------------------------------------------------------------------

NS = (
    'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)


def _xml(text: str, status: int = 200) -> web.Response:
    return web.Response(status=status, text=text, content_type="text/xml")


def _envelope(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><s:Envelope {NS}><s:Header>'
        '<t:ServerVersionInfo MajorVersion="15" MinorVersion="2" MajorBuildNumber="1748" '
        'MinorBuildNumber="39"/></s:Header>'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def _z(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_message(op: str, code: str) -> str:
    return _envelope(
        f'<m:{op}Response><m:ResponseMessages><m:{op}ResponseMessage ResponseClass="Error">'
        f"<m:MessageText>{code} happened</m:MessageText><m:ResponseCode>{code}</m:ResponseCode>"
        f"</m:{op}ResponseMessage></m:ResponseMessages></m:{op}Response>"
    )


def _fault_busy(backoff_ms: int) -> str:
    return _envelope(
        "<s:Fault><faultcode>a:ErrorServerBusy</faultcode><faultstring>The server cannot service "
        "this request right now.</faultstring><detail><e:ResponseCode "
        'xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">ErrorServerBusy'
        '</e:ResponseCode><t:MessageXml><t:Value Name="BackOffMilliseconds">'
        f"{backoff_ms}</t:Value></t:MessageXml></detail></s:Fault>"
    )


def _get_folder_response() -> str:
    return _envelope(
        '<m:GetFolderResponse><m:ResponseMessages><m:GetFolderResponseMessage ResponseClass="Success">'
        "<m:ResponseCode>NoError</m:ResponseCode><m:Folders><t:CalendarFolder>"
        '<t:FolderId Id="F1"/><t:DisplayName>Calendar</t:DisplayName></t:CalendarFolder>'
        "</m:Folders></m:GetFolderResponseMessage></m:ResponseMessages></m:GetFolderResponse>"
    )


def _item_xml(i: Item) -> str:
    rid = f"<t:RecurrenceId>{_z(i.recurrence_id)}</t:RecurrenceId>" if i.recurrence_id else ""
    cancelled = "<t:IsCancelled>true</t:IsCancelled>" if i.cancelled else ""
    return (
        f'<t:CalendarItem><t:ItemId Id="{i.item_id}" ChangeKey="CK"/>'
        f"<t:Subject>{escape(i.subject)}</t:Subject><t:UID>{i.uid}</t:UID>{rid}"
        f"<t:Start>{_z(i.start)}</t:Start><t:End>{_z(i.end)}</t:End>"
        f"<t:IsAllDayEvent>{'true' if i.all_day else 'false'}</t:IsAllDayEvent>{cancelled}"
        f"<t:LegacyFreeBusyStatus>Busy</t:LegacyFreeBusyStatus><t:Location>{escape(i.location)}</t:Location>"
        f"<t:CalendarItemType>{i.item_type}</t:CalendarItemType>"
        f"<t:MyResponseType>{i.my_response}</t:MyResponseType></t:CalendarItem>"
    )


def _find_response(items: list[Item], complete: bool) -> str:
    return _envelope(
        '<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Success">'
        f'<m:ResponseCode>NoError</m:ResponseCode><m:RootFolder TotalItemsInView="{len(items)}" '
        f'IncludesLastItemInRange="{"true" if complete else "false"}"><t:Items>'
        + "".join(_item_xml(i) for i in items)
        + "</t:Items></m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>"
    )


def _get_item_response(items: list[Item | None]) -> str:
    messages = []
    for i in items:
        if i is None:
            messages.append(
                '<m:GetItemResponseMessage ResponseClass="Error"><m:ResponseCode>ErrorItemNotFound'
                "</m:ResponseCode></m:GetItemResponseMessage>"
            )
            continue
        # Like real occurrences: the returned Id differs from the requested one.
        messages.append(
            '<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
            f'<m:Items><t:CalendarItem><t:ItemId Id="{i.item_id}-returned" ChangeKey="CK"/>'
            f'<t:Body BodyType="Text">{escape(i.body)}</t:Body></t:CalendarItem></m:Items>'
            "</m:GetItemResponseMessage>"
        )
    return _envelope(
        "<m:GetItemResponse><m:ResponseMessages>"
        + "".join(messages)
        + "</m:ResponseMessages></m:GetItemResponse>"
    )
