# Changelog

## 0.1.0

First release.

- Calendars of on-premises Exchange Server 2013–2019/SE as Home Assistant `calendar` entities, via EWS.
- Own calendar plus shared calendars of other mailboxes.
- NTLM (with Extended Protection / channel binding) and Basic authentication, auto-detected.
- Recurring meetings expanded server-side; stable `uid` + `recurrence_id` per occurrence.
- Plain-text descriptions; hides declined and cancelled meetings (configurable).
- Reauthentication, reconfiguration, options, redacted diagnostics.
- English and Russian translations.
