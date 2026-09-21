import datetime as dt

import pytest
from ec import soap
from ec.client import normalize_url
from ec.models import EwsResponseError, EwsServerBusyError

# Shape taken from a live Exchange 2019 response (content anonymised).
FIND = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Header>
<h:ServerVersionInfo MajorVersion="15" MinorVersion="2" MajorBuildNumber="1748" MinorBuildNumber="39"
 xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header><s:Body>
<m:FindItemResponse xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
 xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><m:ResponseMessages>
<m:FindItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>
<m:RootFolder TotalItemsInView="2" IncludesLastItemInRange="true"><t:Items>
<t:CalendarItem><t:ItemId Id="A1" ChangeKey="x"/><t:Subject>Holiday</t:Subject><t:Sensitivity>Normal</t:Sensitivity>
<t:UID>040000</t:UID><t:RecurrenceId>2026-09-20T21:00:00Z</t:RecurrenceId><t:Start>2026-09-20T21:00:00Z</t:Start>
<t:End>2026-09-21T21:00:00Z</t:End><t:IsAllDayEvent>true</t:IsAllDayEvent><t:LegacyFreeBusyStatus>Free</t:LegacyFreeBusyStatus>
<t:Location/><t:IsRecurring>true</t:IsRecurring><t:CalendarItemType>Occurrence</t:CalendarItemType>
<t:MyResponseType>Accept</t:MyResponseType></t:CalendarItem>
<t:CalendarItem><t:ItemId Id="B2" ChangeKey="y"/><t:Subject> Sync  </t:Subject><t:UID>8144BA</t:UID>
<t:Start>2026-09-25T12:00:00Z</t:Start><t:End>2026-09-25T13:00:00Z</t:End><t:IsAllDayEvent>false</t:IsAllDayEvent>
<t:Location>Room 1</t:Location><t:CalendarItemType>Single</t:CalendarItemType><t:MyResponseType>Tentative</t:MyResponseType>
</t:CalendarItem></t:Items></m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>
</s:Body></s:Envelope>"""


def test_parse_find_calendar():
    root = soap.parse_envelope(FIND)
    events, complete = soap.parse_find_calendar(root)
    assert complete
    holiday, sync = events
    assert holiday.all_day and holiday.item_type == "Occurrence"
    assert holiday.recurrence_id == "2026-09-20T21:00:00Z"
    assert holiday.start == dt.datetime(2026, 9, 20, 21, tzinfo=dt.UTC)
    assert sync.subject == "Sync" and sync.location == "Room 1" and sync.recurrence_id is None
    assert sync.my_response == "Tentative" and not sync.cancelled
    assert soap.server_version(root) == "15.2.1748.39"


def test_busy_fault_with_backoff():
    # Exchange's real format: code in the ".../errors" namespace inside <detail>.
    xml = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
    <faultcode xmlns:a="http://schemas.microsoft.com/exchange/services/2006/types">a:ErrorServerBusy</faultcode>
    <faultstring>Try again later.</faultstring><detail>
    <e:ResponseCode xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">ErrorServerBusy</e:ResponseCode>
    <t:MessageXml xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
    <t:Value Name="BackOffMilliseconds">2500</t:Value></t:MessageXml></detail></s:Fault></s:Body></s:Envelope>"""
    with pytest.raises(EwsServerBusyError) as err:
        soap.parse_envelope(xml)
    assert err.value.backoff == 2.5


def test_fault_code_from_faultcode_when_no_detail():
    xml = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
    <faultcode>a:ErrorSchemaValidation</faultcode><faultstring>bad</faultstring></s:Fault></s:Body></s:Envelope>"""
    with pytest.raises(EwsResponseError) as err:
        soap.parse_envelope(xml)
    assert err.value.code == "ErrorSchemaValidation"


def test_not_soap():
    with pytest.raises(soap.NotSoapError):
        soap.parse_envelope("<html><body>500 URL Rewrite Module Error.</body></html>")


def test_mailbox_is_escaped():
    body = soap.get_folder_request('a&b<"x"@example.com')
    assert "a&amp;b&lt;" in body and "<t:Mailbox>" in body


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mail.example.com", "https://mail.example.com/EWS/Exchange.asmx"),
        ("https://mail.example.com/", "https://mail.example.com/EWS/Exchange.asmx"),
        (" https://mail.example.com/EWS/Exchange.asmx ", "https://mail.example.com/EWS/Exchange.asmx"),
        ("http://10.0.0.5:8080", "http://10.0.0.5:8080/EWS/Exchange.asmx"),
    ],
)
def test_normalize_url(value, expected):
    assert normalize_url(value) == expected


def test_dtd_is_rejected():
    """Entity-expansion payloads never reach the parser."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>&lol2;</s:Body></s:Envelope>'
    )
    with pytest.raises(soap.NotSoapError):
        soap.parse_envelope(bomb)
