# GLS Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-gls.svg)](https://github.com/ha-parcel-integrations/ha-gls/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your GLS parcels in the Netherlands. No GLS account is needed — you enter the tracking number and delivery postal code yourself, just like the gls-info.nl website.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track multiple GLS parcels by tracking number — no account
- Add parcels from the options, a service, or a dashboard button
- Per-parcel sensor per tracked shipment, with full status details as attributes
- Incoming, next-delivery, en-route and awaiting-pickup summary sensors
- Delivered-parcels sensor and an optional per-parcel status history timeline
- Weight and dimensions where GLS provides them
- Automatic lifecycle management — per-parcel sensors appear and disappear
  as parcels move through delivery

## Requirements

- Home Assistant 2024.7 or newer
- A GLS parcel delivered to a supported country. Only the **Netherlands**
  is available today; the setup form links to a GitHub issue for
  [requesting another country](https://github.com/ha-parcel-integrations/ha-gls/issues/new?title=Add%20country%3A%20%3Cyour%20country%3E&labels=enhancement)

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add this repository URL and select category **Integration**
3. Search for **GLS** and install it
4. Restart Home Assistant

### Manual

1. Copy the `gls` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **GLS**
3. Pick your **country** (Netherlands for now) and enter your **delivery
   postal code** (the one parcels usually go to)
4. Click **Submit**

That's it — setup only asks for the country and postal code. The postcode
becomes the default for parcels you add later, so adding a parcel usually
only needs its number.

You can add **multiple hubs** — one per delivery postal code (e.g. home and
work). Each hub is its own **GLS (postcode)** device with its own parcels.

### Adding parcels

A **GLS** hub holds your tracked parcels. Add them any of three ways — new
per-parcel sensors appear immediately, no restart or manual refresh needed:

- **Options** — integration card → **Configure** → **Parcels** → **Add a parcel**.
- **Service** — call `gls.track_parcel` with a `parcel_no` (and optional
  `postal_code`, which picks the hub when you run several). `gls.untrack_parcel`
  removes one.
- **Dashboard** — a text field + button that calls the service. See
  [`examples/dashboards/add_parcel_card.yaml`](examples/dashboards/add_parcel_card.yaml).

You can use either identifier GLS gives out: the long parcel number
(e.g. `13290054100304`) or the short tracking ID (e.g. `00L1B3BX`). Find
them in the GLS track & trace mail/SMS or on gls-info.nl.

## Options

Click **Configure** on the integration card. One form, four sections:

| Section | Description |
|---|---|
| Parcels | Add a parcel by its tracking number (it uses the hub postal code), or remove tracked parcels. |
| Delivered parcels | Keep delivered parcels in the delivered sensor for the last N **days**, or keep only the N most recent (**parcels**). Default: 7 days. Parcels stay tracked — this only controls the sensor. |
| Parcel history | Add a per-parcel status history attribute. **Off by default.** |
| Polling | How often GLS is checked: **15 / 30 / 60 / 120 / 240 minutes** (default 30). |

## Removal

Standard HA removal applies: **Settings → Devices & Services → GLS → ⋮ →
Delete**. Nothing is stored on GLS' side.

## Sensors

Each hub is a **GLS (postcode)** device. The entities below show the
friendly-name pattern (with multiple hubs each carries its own postcode):

| Friendly name | Description |
|---|---|
| `GLS (postcode) Incoming parcels` | Number of active (not-yet-delivered) tracked parcels |
| `GLS (postcode) Parcel <number>` | Canonical status of a single tracked parcel |
| `GLS (postcode) Next delivery` | Earliest expected delivery datetime |
| `GLS (postcode) En route to ParcelShop` | Active parcels still in transit to a GLS ParcelShop |
| `GLS (postcode) Awaiting pickup` | Parcels that have arrived at a ParcelShop and are ready to collect |
| `GLS (postcode) Delivered parcels` | Recently delivered tracked parcels (retention configurable) |
| `GLS (postcode) Last successful update` | Diagnostic timestamp of the last successful poll |

Every parcel exposed on a sensor attribute uses a carrier-agnostic shape:

| Key | Type | Meaning |
|---|---|---|
| `carrier` | string | `"GLS"` |
| `barcode` | string | Parcel tracking number |
| `sender` | string \| null | Sender name |
| `receiver` | string \| null | Recipient name |
| `status` | `ParcelStatus` | Canonical status — see the [status reference](#parcel-status-reference) |
| `raw_status` | string \| null | Original GLS status description (Dutch) |
| `delivered` | bool | Whether the parcel has been delivered |
| `delivered_at` | ISO 8601 \| null | Delivery moment, if known |
| `planned_from` | ISO 8601 \| null | Expected delivery window start |
| `planned_to` | ISO 8601 \| null | Expected delivery window end |
| `pickup` | bool | Destined for a ParcelShop rather than a home address |
| `pickup_point` | string \| null | ParcelShop name when `pickup` is true |
| `url` | string \| null | Deep link to the parcel's tracking page |
| `weight` | float \| null | Parcel weight in kilograms |
| `dimensions` | dict \| null | `{length, width, height, text}` in centimeters |
| `history` | list \| null | Ordered status timeline (oldest → newest), each `{timestamp, status, raw_status}`. `null` unless the **status history** option is enabled — see [Options](#options). |
| `raw` | dict | The original GLS API payload |

## Parcel status reference

`status` on every parcel is one of the canonical `ParcelStatus` values
below — use these in automations rather than GLS' raw Dutch strings.

| `status` | Meaning | GLS state |
|---|---|---|
| `registered` | GLS was notified of the parcel | 0 (Aangekondigd bij GLS) |
| `in_transit` | In GLS' network | 1, 2 (ontvangen / op depot) |
| `out_for_delivery` | On the delivery vehicle today | 3 (Onderweg - geladen voor aflevering) |
| `at_pickup_point` | Arrived at a ParcelShop, ready to collect | (mapped once observed) |
| `delivered` | Handed over | 4 (Afgeleverd) |
| `returning` | On the way back to the sender | (mapped once observed) |
| `problem` | Carrier reports an exception | (mapped once observed) |
| `unknown` | A state we have not mapped yet | anything else — logged once at warning level with a ready-to-paste issue link |

## Events

The coordinator fires events on the HA event bus when something interesting
happens to a parcel, so automations can react without polling per-parcel
sensors.

| Event | When | Payload |
|---|---|---|
| `gls_parcel_registered` | A new parcel appears in the active list | The full parcel dict (see the table above) |
| `gls_parcel_status_changed` | A parcel's canonical `status` value changes | Same payload plus `old_status` and `new_status` |
| `gls_parcel_delivery_time_changed` | A parcel's expected delivery time changes to a new value | Same payload plus `old_planned_from`, `new_planned_from`, `old_planned_to`, `new_planned_to` |

Every payload also carries a `device_id`. Events do not fire for parcels
that were already tracked when HA first started.

If you build automations in the UI, these same events are also available as
no-code **device triggers** (**Settings → Automations → Create → Add trigger
→ Device**).

See [`examples/automations/`](examples/automations/) for ready-to-paste
event-driven automations.

## Services

| Service | Description |
|---|---|
| `gls.track_parcel` | Start tracking a parcel — `parcel_no` (required) and `postal_code` (optional, defaults to the hub postal code). |
| `gls.untrack_parcel` | Stop tracking a parcel — `parcel_no`. |

## Examples

Ready-to-paste automations and dashboard snippets live in
[`examples/`](examples/), including a [card that adds a parcel from a
dashboard](examples/dashboards/add_parcel_card.yaml).

## Debugging

To capture the raw GLS API response, enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.gls: debug
```

Restart Home Assistant, wait for the next poll (or press the **Refresh**
button), and check **Settings → System → Logs**.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `cannot_connect` during setup | GLS is unreachable; check your network |
| A parcel shows `unknown` | GLS has not scanned it yet, or its state is not mapped — check the logs for a ready-to-paste issue link |
| Sensors not updating | Check **Settings → System → Logs** for `gls` entries |

## Related integrations

Tracking parcels from other Dutch carriers:

| Integration | Description |
|---|---|
| [ha-dhl-nl](https://github.com/ha-parcel-integrations/ha-dhl-nl) | DHL eCommerce NL parcel tracker. |
| [ha-dpd](https://github.com/ha-parcel-integrations/ha-dpd) | DPD parcel tracker. |
| [ha-postnl](https://github.com/ha-parcel-integrations/ha-postnl) | PostNL parcel tracker. |
| [ha-dragonfly](https://github.com/ha-parcel-integrations/ha-dragonfly) | Dragonfly Shipping parcel tracker — no account, you enter tracking numbers yourself. |
| [ha-parcel-aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) | Rolls up counts and next-delivery timestamps from all installed carrier integrations into a single set of sensors. |

## Disclaimer

This is an independent, community-built project with no affiliation,
endorsement, or connection to GLS or any of its subsidiaries. The GLS
tracking endpoint used here is undocumented (reverse-engineered from the
public gls-info.nl site) and may change without notice. Installing this
integration may breach GLS' Terms of Service. You take any risk that
follows. No warranty (see [LICENSE](LICENSE)).

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

MIT
