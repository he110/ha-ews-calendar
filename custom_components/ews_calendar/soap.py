"""Building EWS SOAP requests and parsing responses. No Home Assistant imports."""

from __future__ import annotations

import contextlib
import datetime as dt
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .models import EwsEvent, EwsResponseError, EwsServerBusyError, FolderInfo

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS = {"s": NS_SOAP, "t": NS_T, "m": NS_M}

# Exchange 2013 SP1 is the oldest schema that has RecurrenceId; 2016/2019/SE accept it.
SERVER_VERSION = "Exchange2013_SP1"
MAX_DESCRIPTION = 4000

CALENDAR_PROPERTIES = (
    "item:Subject",
    "calendar:Start",
    "calendar:End",
    "calendar:IsAllDayEvent",
    "calendar:Location",
    "calendar:UID",
    "calendar:RecurrenceId",
    "calendar:IsCancelled",
    "calendar:CalendarItemType",
    "calendar:MyResponseType",
    "calendar:LegacyFreeBusyStatus",
)


def _envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{NS_SOAP}" xmlns:t="{NS_T}" xmlns:m="{NS_M}">'
        f'<soap:Header><t:RequestServerVersion Version="{SERVER_VERSION}"/></soap:Header>'
        f"<soap:Body>{body}</soap:Body></soap:Envelope>"
    )


def _calendar_folder(mailbox: str | None) -> str:
    if not mailbox:
        return '<t:DistinguishedFolderId Id="calendar"/>'
    return (
        '<t:DistinguishedFolderId Id="calendar"><t:Mailbox>'
        f"<t:EmailAddress>{escape(mailbox)}</t:EmailAddress>"
        "</t:Mailbox></t:DistinguishedFolderId>"
    )


def _ews_time(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_folder_request(mailbox: str | None) -> str:
    return _envelope(
        "<m:GetFolder><m:FolderShape><t:BaseShape>Default</t:BaseShape></m:FolderShape>"
        f"<m:FolderIds>{_calendar_folder(mailbox)}</m:FolderIds></m:GetFolder>"
    )


def find_calendar_request(mailbox: str | None, start: dt.datetime, end: dt.datetime, max_entries: int) -> str:
    props = "".join(f'<t:FieldURI FieldURI="{p}"/>' for p in CALENDAR_PROPERTIES)
    return _envelope(
        '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>'
        f"<t:AdditionalProperties>{props}</t:AdditionalProperties></m:ItemShape>"
        f'<m:CalendarView MaxEntriesReturned="{max_entries}" '
        f'StartDate="{_ews_time(start)}" EndDate="{_ews_time(end)}"/>'
        f"<m:ParentFolderIds>{_calendar_folder(mailbox)}</m:ParentFolderIds></m:FindItem>"
    )


def get_bodies_request(item_ids: list[str]) -> str:
    ids = "".join(f"<t:ItemId Id={quoteattr(i)}/>" for i in item_ids)
    return _envelope(
        "<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>"
        "<t:BodyType>Text</t:BodyType><t:AdditionalProperties>"
        '<t:FieldURI FieldURI="item:Body"/></t:AdditionalProperties></m:ItemShape>'
        f"<m:ItemIds>{ids}</m:ItemIds></m:GetItem>"
    )


# --- Parsing -----------------------------------------------------------------


class NotSoapError(ValueError):
    """The body is not a SOAP envelope (HTML error page, proxy message, ...)."""


def parse_envelope(text: str) -> ET.Element:
    """Parse a response and raise on SOAP faults."""
    # EWS never sends a DTD. Refusing one up front shuts out entity-expansion and
    # external-entity tricks from a malicious or compromised server, without
    # pulling in defusedxml.
    if "<!DOCTYPE" in text[:2048].upper() or "<!ENTITY" in text.upper():
        raise NotSoapError("DTD is not allowed in EWS responses")
    try:
        root = ET.fromstring(text)  # noqa: S314 — DTD rejected above
    except ET.ParseError as err:
        raise NotSoapError(str(err)) from err
    if root.tag != f"{{{NS_SOAP}}}Envelope":
        raise NotSoapError(root.tag)
    fault = root.find("s:Body/s:Fault", NS)
    if fault is not None:
        # Exchange puts the code in the ".../errors" namespace inside <detail>;
        # fall back to faultcode ("a:ErrorServerBusy") without its prefix.
        code = (
            fault.findtext(".//{*}ResponseCode", default="")
            or (fault.findtext("faultcode", default="SoapFault").rpartition(":")[2])
        )
        message = fault.findtext("faultstring", default="")
        _raise_for_code(code, message, fault)
    return root


def _raise_for_code(code: str, message: str, element: ET.Element) -> None:
    if code == "ErrorServerBusy":
        backoff = None
        for value in element.iter(f"{{{NS_T}}}Value"):
            if value.get("Name") == "BackOffMilliseconds" and value.text:
                with contextlib.suppress(ValueError):
                    backoff = int(value.text) / 1000
        raise EwsServerBusyError(message, backoff)
    raise EwsResponseError(code, message)


def _check_messages(root: ET.Element) -> list[ET.Element]:
    """Return response messages, raising on the first ResponseClass="Error"."""
    messages = root.findall("s:Body/*/m:ResponseMessages/*", NS)
    for message in messages:
        if message.get("ResponseClass") == "Error":
            _raise_for_code(
                message.findtext("m:ResponseCode", default="Error", namespaces=NS),
                message.findtext("m:MessageText", default="", namespaces=NS),
                message,
            )
    return messages


def server_version(root: ET.Element) -> str | None:
    info = root.find("s:Header/t:ServerVersionInfo", NS)
    if info is None:
        return None
    parts = [info.get(k) for k in ("MajorVersion", "MinorVersion", "MajorBuildNumber", "MinorBuildNumber")]
    return ".".join(p for p in parts if p) or None


def parse_get_folder(root: ET.Element) -> FolderInfo:
    [message] = _check_messages(root)
    name = message.findtext(".//t:DisplayName", default="", namespaces=NS)
    return FolderInfo(display_name=name, server_version=server_version(root))


def _parse_time(value: str) -> dt.datetime:
    # EWS returns UTC ("2026-09-21T08:00:00Z"); fromisoformat handles the offset form too.
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def parse_find_calendar(root: ET.Element) -> tuple[list[EwsEvent], bool]:
    """Return (events, includes_last_item_in_range)."""
    [message] = _check_messages(root)
    folder = message.find("m:RootFolder", NS)
    includes_last = folder is None or folder.get("IncludesLastItemInRange", "true") == "true"
    events: list[EwsEvent] = []
    for item in message.findall("m:RootFolder/t:Items/t:CalendarItem", NS):
        item_id = item.find("t:ItemId", NS)
        start = item.findtext("t:Start", namespaces=NS)
        end = item.findtext("t:End", namespaces=NS)
        if item_id is None or not start or not end:
            continue
        events.append(
            EwsEvent(
                item_id=item_id.get("Id", ""),
                uid=item.findtext("t:UID", namespaces=NS) or None,
                recurrence_id=item.findtext("t:RecurrenceId", namespaces=NS) or None,
                subject=(item.findtext("t:Subject", namespaces=NS) or "").strip(),
                start=_parse_time(start),
                end=_parse_time(end),
                all_day=_bool(item.findtext("t:IsAllDayEvent", namespaces=NS)),
                location=(item.findtext("t:Location", namespaces=NS) or "").strip(),
                item_type=item.findtext("t:CalendarItemType", namespaces=NS),
                my_response=item.findtext("t:MyResponseType", namespaces=NS),
                free_busy=item.findtext("t:LegacyFreeBusyStatus", namespaces=NS),
                cancelled=_bool(item.findtext("t:IsCancelled", namespaces=NS)),
            )
        )
    return events, includes_last


def parse_bodies(root: ET.Element, requested_ids: list[str]) -> dict[str, str]:
    """Requested ItemId → plain-text body (trimmed).

    Matched by position: GetItem answers in request order, and for occurrences the
    returned ItemId is not guaranteed to be byte-identical to the requested one.
    Items that failed individually are skipped.
    """
    bodies: dict[str, str] = {}
    messages = root.findall("s:Body/*/m:ResponseMessages/*", NS)
    for requested, message in zip(requested_ids, messages, strict=False):
        if message.get("ResponseClass") == "Error":
            continue
        item = message.find("m:Items/*", NS)
        if item is None:
            continue
        text = (item.findtext("t:Body", namespaces=NS) or "").replace("\r\n", "\n").strip()
        bodies[requested] = text[:MAX_DESCRIPTION]
    return bodies
