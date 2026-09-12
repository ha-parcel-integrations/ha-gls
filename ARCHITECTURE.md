# Architecture

How `ha-gls` is built and why it is built that way. `CLAUDE.md` is the short
list of things not to get wrong; this file is the reasoning behind it. API
mechanics — endpoints, parameters, status vocabularies — live in the private
`carrier-research/gls/api/` and are never copied here.

Two things drive everything else. GLS has **no consumer account or parcel
feed**, so the user enters tracking codes and a hub is keyed by postcode rather
than by login. And GLS is not one backend but six: a keyless national GET for
the Netherlands, a postcode-enhanced keyless GET for Canada, a keyless JSON
POST for the United States, a keyless national GET for Poland, a stateful
bearer-token POST for Germany, and a keyless pan-EU group index serving
fourteen more countries.

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
    ├── ca/              postcode-enhanced keyless GET + normalize + status map
    ├── us/              keyless POST + newest-of-several-shipments selection
    ├── pl/              keyless national GET + Polish-text event map
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
| **CA** | postcode-enhanced national GET | keyless | `async_get_parcel_ca` |
| **US** | national JSON POST, no postcode | keyless | `async_get_parcel_us` |
| **PL** | national GET, no postcode | keyless | `async_get_parcel_pl` |
| **DE** | bearer POST | anonymous app instance + token | `async_get_parcel_de` |
| **14 group leaves** | pan-EU `rstt028`/`rstt029` | keyless | `async_get_parcel_group` |

Each hub stores its choice in `entry.options[CONF_COUNTRY]`, and `COUNTRIES`
in `const.py` holds 19 rows: `NL`, `CA`, `US`, `PL`, `DE`, and the group leaves `BE`,
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
field; `settings` holds delivered-parcel retention and history. Polling cadence
is not configurable — see **Dynamic polling** below.

An **update listener** (`_async_options_updated`) calls
`async_request_refresh()`, which recomputes the interval too. The
coordinator re-reads options on every update, so a refresh — not a reload —
makes an add or remove reflect immediately, and avoids the config-entry-listener
deprecation. **Do not switch this to `async_schedule_reload`.** This is the
account-less half of the suite's two options models.

There is no auth, no reauth, and no sent-shipments coordinator. The HA-managed
session is used directly, with no per-entry cookie jar because there are no
cookies. Entities are entry-scoped.

### Dynamic polling

There is no user-facing polling interval — a deliberate suite-wide choice, not a
gap. `coordinator.py` recomputes `update_interval` at the end of every refresh
(barcode-based model):

- **Quiet window:** no polling 00:00–06:00 local time, except two daily anchors
  (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight.
- **Full stop:** `update_interval = None` when nothing is tracked or every
  tracked parcel is delivered. Resumes the moment a parcel is added back, via
  the options-flow update listener above.
- **Stagger:** a small, stable per-install offset (hash of the config entry id)
  is added to every computed interval so hubs don't all hit an anchor or tier
  boundary at the same second.

The DE hub is no exception: its stateful session runs inside the same
`_async_update_data`, so it is retimed by the same recompute point.

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

### The US returns several shipments per tracking number

GLS US answers one tracking number with a `shipments[]` array that can hold
**more than one record** — a return-to-sender and the replacement sent
afterwards, for instance — listed oldest first. So `countries/us/` never trusts
array position: it keeps only the records whose `trackingNumber` matches the
tracked code and picks the one with the most recent timestamp (latest transit
event, else delivery date, else ship date). The backend's `0001-01-01` /
`1900-01-01` placeholders parse cleanly, so they are rejected by value —
otherwise a placeholder could win that comparison and become a delivery time.

Its status mapping is **derivation-first**, like DE's: the exact table holds
the delivered literal the carrier's own tracker keys off plus the scan texts
users have reported since, and every other text falls back to what the record's
dates prove (delivery date → delivered, any scan → in transit, ship date alone →
registered). Each unrecognised text still logs once with the
unrecognised-status issue link, which is how the exact table gets filled in
later.

A US scan text can carry the date it schedules — `ARRIVAL SCAN - DELIVERY SCHED
FOR 03/03/2026` — so lookup falls back to the scan type before the ` - `; an
exact table could never match a text with a date in it, and it would log a
fresh unrecognised warning for every new date.

The US is also the one country whose lookup does not use the postcode at all.
The hub still asks for one — every GLS hub does, it is the default for parcels
added later, and dropping the field for one country would fork the setup flow —
but `async_get_parcel_us` never sends it.

### Poland is national, though the group leaf would resolve it

Polish numbering *is* in the pan-EU group index — a real Polish AWB answers
`rstt029` — so PL could have been one more `COUNTRIES` row on
`countries/group/`. It deliberately is not. Measured against the same two real
parcels, the national myGLS route fills more of the canonical shape **without**
a postcode than the group leaf does with one:

| | national myGLS | group `rstt029` |
|---|---|---|
| history | full `eventReasons[]` | none (needs `rstt028` + postcode) |
| `delivered_at` | a real offset-bearing timestamp | localized prose only |
| ParcelShop | drop-off and collection are separate events | reports the drop-off as `DELIVERED` |
| weight | absent | absent (`rstt028` only) |

That last row is the deciding one: the group leaf marked a shop parcel
delivered at the moment GLS dropped it off, six hours before the recipient
collected it, while the national route distinguishes the two. Poland therefore
gets its own transport, and the pan-EU leaf stays its fallback of last resort.

Its status vocabulary comes in two levels that must not be merged. The
**shipment** level (`progressBarIdent`) is the group's machine-code vocabulary,
matched exactly — `DELIVEREDPS` contains the substring `DELIVERED`, so a
`startswith`/`in` test would mark a ParcelShop arrival as delivered. The
**event** level has no code at all, only localized Polish text, so its map
holds exactly the values seen on the wire and unseen wording stays neutral
rather than being guessed from a translation. When the shipment code itself is
unrecognised, the newest event's mapped status stands in — derivation-first,
like DE and US.

One question the capture could not settle: what `progressBarIdent` reports
*while* a parcel waits at a GLS Point (the only shop parcel captured had
already been collected). So a body claiming `DELIVERED` whose newest event is
the drop-off fires a one-shot WARNING rather than being silently corrected —
that log is the answer arriving.

### Capabilities are per country, not intersected

`CAPABILITIES_BY_VARIANT` holds one frozenset per variant — `"Netherlands"`,
`"Germany"`, `"Canada"`, `"United States"`, `"Poland"`, `"Other"` (CZ and every
future group leaf) — and the docs site renders one row per entry.

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
