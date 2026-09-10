# Architecture

How `ha-gls` is built and why it is built that way. `CLAUDE.md` is the short
list of things not to get wrong; this file is the reasoning behind it. API
mechanics — endpoints, parameters, status vocabularies — live in the private
`carrier-research/gls/api/` and are never copied here.

Two things drive everything else. GLS has **no consumer account or parcel
feed**, so the user enters tracking codes and a hub is keyed by postcode rather
than by login. And GLS is not one backend but three: a keyless national GET for
the Netherlands, a stateful bearer-token POST for Germany, and a keyless pan-EU
group index serving fourteen more countries.

This is the suite's first multi-country carrier, so the country-package pattern
originated here. DPD followed it later with a different dispatch point — check
each repo rather than assuming they match.

## Project layout

```
custom_components/gls/
├── __init__.py          setup, transport construction, first refresh
├── api.py               GlsApiClient — the per-country dispatcher (thin)
├── const.py             COUNTRIES, GROUP_LEAF_COUNTRIES, ParcelStatus, CAPABILITIES_BY_VARIANT, GlsApiError
├── coordinator.py       poll loop, _raw_cache, DE state persistence, event firing
├── parcels.py           shared pure helpers (filters, sort) — no I/O, no HA objects
├── timeutils.py         timestamp parsing shared across countries
├── config_flow.py       postcode + country setup, two-page options menu
├── sensor.py            summary, per-parcel, pickup and diagnostic sensors
├── button.py            refresh button
├── calendar.py          read-only deliveries calendar
├── services.py          gls.track_parcel / gls.untrack_parcel (shared across hubs)
├── device.py            device registry helpers
├── device_trigger.py    device automation triggers
├── diagnostics.py       redacted diagnostics
└── countries/
    ├── nl/              keyless national GET + normalize + status map
    ├── de/              bearer POST: __init__.py (transport) + session.py (lifecycle)
    └── group/           pan-EU rstt028/rstt029 leaves + normalize + status map
```

`GlsApiError` is defined in `const.py`, not `api.py`, so a country package can
raise it without an import cycle back through the dispatcher. It is re-exported
from `api.py` so `from .api import GlsApiClient, GlsApiError` and existing test
patch targets keep working.

## Transports and dispatch

**The dispatch point is `GlsApiClient.async_get_parcel` in `api.py`**, not the
coordinator. The coordinator polls a flat list of `(parcel_no, postal_code)`
pairs and never learns which country it is talking to. (DPD puts its dispatch
in the coordinator instead; do not "align" the two — GLS's per-parcel fetch has
no account-level list call to branch on.)

| Country | Transport | Auth | Entry point |
|---|---|---|---|
| **NL** | national GET | keyless | `async_get_parcel_nl` |
| **DE** | bearer POST | anonymous app instance + token | `async_get_parcel_de` |
| **14 group leaves** | pan-EU `rstt028`/`rstt029` | keyless | `async_get_parcel_group` |

Each hub stores its choice in `entry.options[CONF_COUNTRY]`, and `COUNTRIES`
in `const.py` holds 16 rows: `NL`, `DE`, and the group leaves `BE`,
`CZ`, `DK`, `FI`, `HU`, `SK`, `AT`, `IE`, `FR`, `LU`, `RS`, `SI`, `HR`, `IT`.
Each row carries a host, a postcode regex, and either a `culture` or a
`group_locale` (below). Constructing a `country="DE"` client without a
`GlsDeSession` raises `RuntimeError`, not `GlsApiError` — that is a wiring bug,
not a runtime API failure.

## Key design decisions

### Account-less, postcode-keyed hubs

Setup asks only for a postal code and a country (`async_step_user`) and does
**not** hit the API — the endpoint needs a parcel number, so there is nothing to
validate against. The postcode is stored as the hub default in
`entry.options[CONF_POSTAL_CODE]`; `CONF_PARCELS` starts empty.

**Multiple hubs, one per postcode.** `unique_id` is the bare postcode plus
`_abort_if_unique_id_configured`, so home and work can both be added. Device
name is `"GLS (<postcode>)"`. `single_config_entry` is deliberately **absent** —
multiple hubs were the point. The shared `gls.*` services are unloaded only when
no other hub is still loaded. Legacy entries whose `unique_id` was `DOMAIN` are
migrated to the postcode in `async_setup_entry`.

`unique_id` stays the bare postcode even though hubs now carry a country. That
is fine while a postcode is unique per hub regardless of country; revisit only
if two countries' postcode formats ever collide.

**Tracked parcels live in `entry.options[CONF_PARCELS]`** as
`{parcel_no, postal_code}` dicts, added three ways — the options flow, the
`gls.track_parcel` / `gls.untrack_parcel` services, and a Lovelace button — all
validated identically. Adding takes only the number; the postcode is **always**
the hub's. The service keeps an optional `postal_code` for the rare
different-address case.

The service field is `tracking_code` (the suite-wide standard); the deprecated
`parcel_no` alias was removed from the services (ha-gls#3). The *stored* dict
key stays `parcel_no` (`CONF_PARCEL_NO`) — an internal options key, never the
service field, and never part of the alias. Do not conflate the two.

### Options apply live, never by reload

The options flow is a two-page menu (`parcels` / `settings`), not one sectioned
form. `parcels` edits the whole tracked-code list as a single multi-value text
field; `settings` holds delivered-parcel retention, history and polling.

An **update listener** (`_async_options_updated`) retunes
`coordinator.update_interval` and calls `async_request_refresh()`. The
coordinator re-reads options on every update, so a refresh — not a reload —
makes an add or remove reflect immediately, and avoids the config-entry-listener
deprecation. **Do not switch this to `async_schedule_reload`.** This is the
account-less half of the suite's two options models.

There is no auth, no reauth, and no sent-shipments coordinator. The HA-managed
session is used directly, with no per-entry cookie jar because there are no
cookies. Entities are entry-scoped.

### `culture` vs `group_locale`

NL's and DE's `culture` is an `nl-NL`-style locale plugged into a *national* URL
template. The group leaves are pan-EU and partitioned by consignment record
rather than by country path — their `{ISO2}/{lang}` segment is a locale switch
only. Overloading `culture` for both would conflate "which national backend" with
"which language", so the group leaves carry their own `group_locale` key
instead. A country row without `culture` (CZ, SK) falls back to `""` in
`__init__.py` rather than raising `KeyError`.

### Per-country packages

Per-country code lives in `custom_components/gls/countries/<code>/` — each
country is its own package holding its transport, `normalize_parcel_<code>` and
`map_parcel_status_<code>`, with an *extra* submodule only where a country needs
its own lifecycle handling (`countries/de/session.py`). Concern-level files —
`coordinator.py`, `config_flow.py`, `diagnostics.py`, `sensor.py` — stay
top-level and dispatch into the country package; they carry no per-country
branching themselves.

**The trigger for a country getting an extra submodule is structural divergence
from NL** — auth model, transport, payload shape, status vocabulary — **not
country count.** That was decided when DE (bearer POST, session lifecycle,
string status enum, non-ISO timestamps) turned out to share almost nothing with
NL's keyless GET beyond the canonical output shape.

`countries/group/` is a package rather than a flat `countries/group.py` purely
for structural symmetry with `countries/de/`; being keyless and sessionless, it
needs no lifecycle submodule, same as NL.

### Germany is stateful — the only transport that is

DE is an anonymous *guest account*, not a login, and `GlsDeSession` owns one
config entry's `appInstanceId` and bearer token.

- **`appInstanceId`** is minted once in the config flow via `async_register` and
  persisted in `entry.data[CONF_DE_APP_INSTANCE_ID]`. A fresh session instance
  always starts with no cached token, so its first `async_get_token` refreshes
  via `validate` even though the id itself is not new.
- **The token refreshes inside `TOKEN_REFRESH_MARGIN`** of its expiry rather
  than on failure.
- **GLS keeps the tracked-parcel list server-side.** A parcel must be POSTed
  before it can be read, and re-adding one that is already tracked answers a
  bare `409` carrying no `parcelNumber`. So the transport keeps a per-process
  `_known_parcel_numbers` cache mapping tracking reference → `parcelNumber`.
- **That cache does not survive a restart**, which is why the coordinator's
  `_prepare_de_poll` seeds it from the `parcelNumber` persisted per tracked
  parcel (`CONF_DE_PARCEL_NUMBER`) before every poll. It only ever `setdefault`s,
  never clobbering a value already learned this session; the cache stays owned
  by `countries/de/__init__.py`.
- **`_persist_de_state` runs after the poll** and writes back any newly-learned
  `parcelNumber`. It goes through `async_update_entry`, which — once the update
  listener is registered, i.e. after the first refresh — triggers one extra
  refresh like any other option edit.
- **A recovery re-register resets the carrier side.** `pop_reregistered()` is
  called once per poll; `True` means the server-side list was wiped under a new
  `appInstanceId`, so every learned `parcelNumber` is stale. Both the in-process
  cache and the persisted values are dropped and every tracked parcel is
  re-POSTed on the next poll, with a `WARNING` explaining why.
- **A `GlsDeSessionError` fails the whole poll**, not one parcel. A
  token-refresh or identity failure means nothing can authenticate, so the
  coordinator raises `UpdateFailed` rather than caching a per-parcel error.

### The group leaves: two endpoints, one fallback

`rstt028` is the only call per poll in the common case — it carries everything
`rstt029` does plus history, weight and references. `rstt029` is an AWB-only
fallback used **only** on an `E609` (AWB right, postcode wrong), returning the
same `progressBar` shape so status mapping is unaffected.

- **`E800`** means the AWB is not in the group index — the group equivalent of
  NL's `204`. It usually means the leaf does not carry the consignment, but it
  can also hit a parcel that is still live, so it warns once per parcel rather
  than dropping it.
- **The maintenance page is an HTML `200`**, indistinguishable from a real
  response by status code alone, so a non-JSON body raises
  `GlsGroupMaintenanceError` (a `GlsApiError` subclass) and warns once.

Nothing in the package is hardcoded to a country: host, `group_locale`, the
tracking-URL template and the preferred `type=` value all come from
`COUNTRIES[country]`. **A new group-leaf country is one more `COUNTRIES` row
pointing at this same package, never a copy of it.**

The package carries an unusually dense set of one-shot warnings — unmapped
status and event codes, postcode mismatch, unexpected top-level keys, history
out of order, unparseable timestamps, odd weight formats, unexpected info types,
unexpected 5xx. That is the pre-1.0 discipline from `CONVENTIONS.md` applied to
a payload family observed on far fewer real parcels than NL's.

### Capabilities are per country, not intersected

`CAPABILITIES_BY_VARIANT` holds one frozenset per variant — `"Netherlands"`,
`"Germany"`, `"Other"` (CZ and every future group leaf) — and the docs site
renders one row per entry.

This replaced an intersected single `CAPABILITIES` on 2026-08-23. Under that
model NL's full field support went invisible on the site the moment a weaker
country landed: CZ arriving shrank the global set even though NL's and DE's own
support had not changed. Keep each entry in lockstep with its matching
`normalize_parcel_<cc>()`. `KNOWN_CAPABILITIES` is unchanged, and every entry
must still be a subset of it.

### Two identifiers both resolve

A parcel can be tracked by the long numeric `parcelNo` or the short `uniqueNo`,
so `valid_parcel_no` accepts `^[A-Z0-9]{6,20}$` rather than digits only. The
per-parcel sensor's `barcode` always comes from the **response** `parcelNo`, so
tracking by `uniqueNo` still shows the real number.

### Multi-collo tracks at shipment level

One shipment can list several colli. There is one sensor per tracked code, at
shipment level. **Do not split colli into separate sensors.**

### `_raw_cache` keeps a blip from dropping a sensor

`_raw_cache` maps `parcel_no` → last raw payload. A transient error or a `204`
reuses the last good payload so a sensor is not dropped mid-delivery; a
first-ever `204` yields a pending placeholder (`unknown`) so the parcel is still
visible. The cache is trimmed each poll to the still-tracked set, so it stays
bounded. `UpdateFailed` is raised only when **every** tracked parcel errored and
nothing is cached.

Because of that cache, **`last_success_time` is stamped only when at least one
fetch actually succeeded** (or nothing is tracked). A poll served entirely from
`_raw_cache` is not a success — the diagnostic `last_update` sensor exists
precisely to reveal that.

### Fields and PII

`weight` and `dimensions` are populated (GLS provides them, unlike DHL); `text`
is formatted only when all three sides are known. History is opt-in and default
off, built from the `scans[]` already in the response — no extra request. The
delivered-retention filter is display-only. Events fire exactly as DHL's.

The recipient's email, address and preference UUIDs are redacted in
`diagnostics.py`. They still ride in the per-parcel `raw` attribute — the user's
own data, unrecorded — and must not be surfaced anywhere else.

## Entities

Same set as DHL, entry-scoped: `sensor` (incoming summary, per-parcel,
`next_delivery`, `en_route_to_parcel_shop`, `awaiting_pickup`,
`delivered_parcels`, diagnostic `last_update`), `button` (refresh), `calendar`
(deliveries, read-only, enabled by default), and device triggers.

## Adding a country

1. Confirm a working **account-less** endpoint exists for it. The setup form
   links `NEW_COUNTRY_ISSUE_URL` for requests that have not cleared this.
2. If its numbering lives in the pan-EU group index, add one `COUNTRIES` row
   with `host` + `group_locale` + postcode regex, pointing at
   `countries/group/`. Nothing else is needed.
3. If it diverges structurally from NL (auth model, transport, payload shape,
   status vocabulary), give it its own `countries/<cc>/` package with a
   `normalize_parcel_<cc>` and `map_parcel_status_<cc>`, plus a `session.py`
   only if it needs lifecycle handling. Add a branch in
   `GlsApiClient.async_get_parcel`.
4. Add a `CAPABILITIES_BY_VARIANT` entry describing exactly which canonical
   fields the new normalizer populates — or reuse `"Other"` if it matches the
   group-leaf set.
5. Add its language to `translations/`.
