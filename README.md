# Exchange Calendar (EWS) for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/he110/ha-ews-calendar/actions/workflows/validate.yaml/badge.svg)](https://github.com/he110/ha-ews-calendar/actions/workflows/validate.yaml)
[![Tests](https://github.com/he110/ha-ews-calendar/actions/workflows/tests.yaml/badge.svg)](https://github.com/he110/ha-ews-calendar/actions/workflows/tests.yaml)
[![Release](https://img.shields.io/github/v/release/he110/ha-ews-calendar)](https://github.com/he110/ha-ews-calendar/releases)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=he110&repository=ha-ews-calendar&category=integration)
[![Open your Home Assistant instance and start setting up Exchange Calendar.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ews_calendar)

Brings calendars from an **on-premises Microsoft Exchange Server** into Home Assistant as
regular `calendar` entities, using Exchange Web Services (EWS).

It works where the usual workaround does not: many companies block publishing calendars as
ICS links, but keep EWS available — the same API Outlook for Mac and many mobile clients use.

[Русская версия](README.ru.md)

## Features

- Your own calendar, plus **shared calendars** of other mailboxes you can read (a colleague,
  a meeting room, a team mailbox).
- **NTLM** (including **Extended Protection** / channel binding) and **Basic** authentication,
  picked automatically.
- Recurring meetings are expanded by the server; moved single occurrences keep their identity
  (`uid` + `recurrence_id`), so other integrations can follow changes.
- Meeting descriptions as plain text — usually including the Teams/Zoom link.
- Hides meetings you declined and cancelled meetings (both configurable).
- Works with self-signed / internal certificates (optional).
- Reauthentication when the password changes, reconfiguration without re-adding, diagnostics
  that are safe to share.
- Read-only. Nothing is ever written to your mailbox.

## Requirements

| | |
|---|---|
| Exchange | **Exchange Server 2013, 2016, 2019 or Subscription Edition**, on-premises, with EWS reachable from Home Assistant |
| Home Assistant | 2026.2 or newer |
| Not supported | **Exchange Online / Microsoft 365** — Microsoft has disabled password authentication for EWS there; it needs OAuth, which is a different integration |

EWS must be enabled for your mailbox. If it is not, the server answers `403` and the
integration reports it.

## Installation

**HACS (recommended):** click **Open in HACS** above (or HACS → ⋮ → Custom repositories →
`https://github.com/he110/ha-ews-calendar`, category *Integration*), install, restart Home Assistant.

**Manual:** copy `custom_components/ews_calendar` into your `config/custom_components/` and restart.

## Setup

Click **Add integration** above, or go to *Settings → Devices & services → Add integration →
Exchange Calendar (EWS)*.

| Field | |
|---|---|
| Server | Host name (`mail.example.com`) or the full EWS URL (`https://mail.example.com/EWS/Exchange.asmx`) |
| Username | Usually your e-mail address, or `DOMAIN\username` |
| Password | Your mailbox password |
| Authentication | *Automatic* uses NTLM when the server offers it, Basic otherwise |
| Verify SSL certificate | Turn off only for servers with a self-signed or internal certificate |

Your own calendar is added right away. To add another one, open the integration and choose
**Add shared calendar**, then enter that mailbox's e-mail address. Your account needs at least
read access to it (the same permission you would need to open it in Outlook).

> **Before you start:** repeated wrong passwords can lock your account. If the form says the
> password was rejected, check it by signing in to Outlook on the web before trying again.

## Options

*Settings → Devices & services → Exchange Calendar → Configure* — apply to all calendars of the account.

| Option | Default | |
|---|---|---|
| Update interval | 10 min | Minimum 5 |
| Days ahead to keep in cache | 30 | Further dates are fetched on demand, e.g. when browsing the calendar |
| Load event descriptions | on | One extra request per 50 events |
| Hide meetings I declined | on | |
| Hide cancelled meetings | on | Cancelled meetings stay in Outlook until you remove them |

## Using it

Each calendar is a normal Home Assistant calendar: it shows up in the Calendar panel, its state
is `on` while an event is in progress, and it works with the
[calendar trigger](https://www.home-assistant.io/docs/automation/trigger/#calendar-trigger).

```yaml
# Example: a spoken reminder five minutes before a meeting.
triggers:
  - trigger: calendar
    event: start
    offset: "-0:05:00"
    entity_id: calendar.work
conditions:
  - condition: template
    value_template: "{{ not trigger.calendar_event.all_day }}"
actions:
  - action: tts.speak
    target:
      entity_id: tts.home_assistant_cloud
    data:
      media_player_entity_id: media_player.office
      message: "In five minutes: {{ trigger.calendar_event.summary }}"
```

## Security and privacy

- The password is stored in Home Assistant's config entry storage, like the credentials of any
  other integration. **Your company may have a policy about storing corporate credentials on
  personal devices — check it first.**
- With NTLM the password itself is never sent over the network.
- The integration only reads your calendar and talks to your Exchange server only. No data
  leaves your Home Assistant.
- Diagnostics redact the server address, username, password and mailbox addresses.

## Troubleshooting

| Symptom | What to check |
|---|---|
| *The server rejected the username or password* | Sign in to Outlook on the web with the same credentials. Try `DOMAIN\username` instead of the e-mail address. |
| *Could not reach the EWS endpoint* | Open `https://your-server/EWS/Exchange.asmx` in a browser from the same network as Home Assistant: you should get a login prompt. |
| `HTTP 403` in the log | EWS is disabled for your mailbox (administrators: `Get-CASMailbox <user> \| fl EwsEnabled`). |
| *Certificate could not be verified* | Internal CA or self-signed certificate — turn off *Verify SSL certificate*. |
| All-day events on the wrong day | Home Assistant and your mailbox use different time zones. |
| Something else | *Settings → Devices & services → Exchange Calendar → ⋮ → Download diagnostics*, and attach it to an [issue](https://github.com/he110/ha-ews-calendar/issues). |

To see what is going on, enable debug logging:

```yaml
logger:
  logs:
    custom_components.ews_calendar: debug
```

## How it works

A single `FindItem` request with a `CalendarView` returns the events of a time window with
recurrences already expanded by Exchange; descriptions come from batched `GetItem` requests.
NTLM is connection-oriented, so the integration keeps its own single-connection HTTP session:
the handshake and the requests after it travel over the same TCP connection, and Exchange keeps
that connection authenticated between requests.

## Development

```bash
pip install -r requirements_test.txt
pytest tests
```

The tests run the integration inside a real Home Assistant against an in-process fake EWS
server that performs a genuine NTLM handshake.

## License

[MIT](LICENSE)
