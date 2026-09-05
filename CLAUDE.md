# Working in this repository

Home Assistant custom integration for **GLS Netherlands** parcel tracking.
Distributed via HACS; not part of HA core. Fourth carrier in the suite (with DHL,
DPD, PostNL) — same canonical shape, events and entity set; **mirror DHL when in
doubt**. Account-less (user-entered tracking codes). No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where GLS deviates) |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `carrier-research/gls/api/` (private research repo)** — the tracktrace
endpoint, its `text/plain` body and 204 signalling, the numeric `state` → status
map, the `scans[]` history and the two-identifier lookup. Do not duplicate them
here.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — so
  the `UpdateFailed`-on-total-failure case fails the whole entry (HA retries with
  backoff). From a forwarded platform HA can't catch `ConfigEntryNotReady`.
- **Setup stale-entity cleanup is scoped to `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the button / `last_update` sensor /
  live per-parcel sensors.

## The big divergence: account-less, postcode-keyed hubs

GLS has **no consumer account / feed** — the user enters tracking codes.

- **Setup asks only the postal code** (`async_step_user`), stored as the hub
  default in `entry.options[CONF_POSTAL_CODE]`; `CONF_PARCELS` starts empty. Setup
  does **not** hit the API (the endpoint needs a parcel number).
- **Multiple hubs, one per postcode.** `unique_id = <postcode>` +
  `_abort_if_unique_id_configured` (home + work both work). Device name
  `"GLS (<postcode>)"`. `single_config_entry` is deliberately **absent** (the user
  wanted multiple hubs). The shared `gls.*` services are unloaded only when **no
  other hub is still loaded**. Legacy entries with `unique_id = DOMAIN` are
  migrated to the postcode in `async_setup_entry`.
- **Tracked parcels live in `entry.options[CONF_PARCELS]`** as
  `{parcel_no, postal_code}` dicts, added three ways (options flow, the
  `gls.track_parcel` / `gls.untrack_parcel` services, a Lovelace button), all
  validated the same. Adding takes only the number — the postcode is **always** the
  hub's; the service keeps an optional `postal_code` for the rare
  different-address case.
- **Service field is `tracking_code`** (suite-wide standard); the deprecated
  `parcel_no` alias has been removed from the services (ha-gls#3). The *stored*
  dict key stays `parcel_no` (`CONF_PARCEL_NO`) — that's an internal options
  key, not the service field, and was never part of the alias; don't conflate
  them.
- **Options flow = a two-page menu** (`parcels` / `settings`), not a single
  sectioned form. The `parcels` page edits the whole tracked-code list as one
  multi-value text field (add or remove any number, then save); `settings`
  holds delivered-parcel retention, history and polling.
- **Option changes apply live, no reload.** An **update listener**
  (`_async_options_updated`) retunes `coordinator.update_interval` and calls
  `async_request_refresh()`; the coordinator re-reads options each update, so a
  refresh (not a reload) makes add/remove reflect immediately and avoids the
  config-entry-listener deprecation. **Do not** switch to `async_schedule_reload`.
- **No auth / reauth / sent-shipments coordinator.** The HA-managed session is
  used directly (no per-entry cookie jar — no cookies). Entities are
  **entry-scoped** (like DPD).

## Integration-level carrier decisions

- **Country model** (`CONF_COUNTRY` / `COUNTRIES`): each hub picks a country →
  host/culture (or `group_locale`, see below)/postcode-regex. **NL** uses a
  keyless national GET, **DE** a bearer-POST guest-account with its own
  `countries/de/session.py`, and **BE**, **CZ**, **DK**, **FI**, **HU**,
  **SK**, **AT**, **IE**, **FR**, **LU**, **RS**, **SI**, **HR** and **IT** use
  the keyless pan-EU group leaves
  (`rstt028`/`rstt029`, `countries/group/`). Adding a country = one
  `COUNTRIES` entry once a working account-less endpoint is confirmed; the setup form links
  `NEW_COUNTRY_ISSUE_URL`. `unique_id` stays the bare postcode — fine while
  a postcode is unique per hub regardless of country.
- **`culture` vs `group_locale`.** NL/DE's `culture` is a `nl-NL`-style
  locale plugged into a *national* URL template. The group-country leaves are pan-EU and
  partitioned by consignment record, not by country path — their
  `{ISO2}/{lang}` segment is a locale switch only, so it lives under its own
  `group_locale` key rather than overloading `culture`. A country row
  without `culture` (such as CZ or SK) falls back to `""` in `__init__.py` rather than
  `KeyError`.
- **`CAPABILITIES_BY_VARIANT` holds one frozenset per country** (`"Netherlands"`,
  `"Germany"`, `"Other"` for CZ and any future group-leaf country), not a single
  intersected `CAPABILITIES`. The docs site renders one row per entry. This
  replaced the old intersection model (2026-08-23): under that model NL's full
  field support became invisible on the site the moment a weaker country
  landed — CZ landing shrank the global set even though NL/DE's own support
  hadn't changed. Keep each entry in lockstep with its
  `normalize_parcel_<cc>()`; `KNOWN_CAPABILITIES` is unchanged and every
  entry must still be a subset of it.
- **Per-country code lives in `custom_components/gls/countries/<code>/`** —
  every country is its own package (`__init__.py` holding transport,
  `normalize_parcel_<code>`, `map_parcel_status_<code>`), with an *extra*
  submodule added only where a country needs its own lifecycle handling,
  e.g. DE's `session.py`. Concern-level files — `coordinator.py`,
  `config_flow.py`, `diagnostics.py`, `sensor.py`, etc. — stay top-level and
  dispatch into the country package; they carry no per-country branching
  themselves. **Trigger for a country getting an extra submodule is structural
  divergence from NL (auth model, transport, payload shape, status vocabulary),
  not country count** — decided when DE (bearer-POST, session lifecycle, string
  status enum, non-ISO timestamps) turned out to share almost nothing with NL's
  keyless GET beyond the canonical output shape. This is the suite's first
  multi-country carrier, so the pattern is new — DPD/DHL will likely follow it
  when they expand, but check their own repos' `CLAUDE.md` rather than assuming.
- **Two identifiers both resolve** (long numeric `parcelNo` and short `uniqueNo`)
  — `valid_parcel_no` accepts `^[A-Z0-9]{6,20}$` (not digits-only) and the
  per-parcel sensor's `barcode` always comes from the **response** `parcelNo`, so
  tracking by `uniqueNo` still shows the real number.
- **Multi-collo**: one shipment can list several colli. We track at **shipment
  level** — one sensor per tracked code. Do not split colli into separate sensors.
- **PII**: the recipient's email/address/preference UUIDs are redacted in
  `diagnostics.py`. They still ride in the per-parcel `raw` attribute (user's own
  data, unrecorded) — don't surface elsewhere.
- **`_raw_cache` (parcel_no → last raw payload)**: a transient error or a `204`
  reuses the last good payload so a sensor isn't dropped on a blip; a first-ever
  `204` yields a pending placeholder (`unknown`) so the parcel is still visible.
  `UpdateFailed` only when **every** tracked parcel errored and nothing is cached.
- **`last_success_time` is stamped only when at least one fetch actually
  succeeded** (or nothing is tracked). A poll served entirely from `_raw_cache` is
  not a success — the diagnostic `last_update` sensor exists to reveal that.
- **`weight` + `dimensions` are populated** (GLS provides them, unlike DHL); `text`
  is only formatted when all three sides are known. **History opt-in, default off**
  (built from the `scans[]` already in the response — no extra request).
  Delivered-retention filter is display-only. Events fire exactly as DHL's.

## Entities (same set as DHL, entry-scoped)

`sensor` (incoming summary + per-parcel + next_delivery + en_route_to_parcel_shop
+ awaiting_pickup + delivered_parcels + diagnostic `last_update`), `button`
(refresh), `calendar` (deliveries, read-only, enabled by default), device
triggers.

## Running tests

```
python -m pytest tests/ --cov=custom_components.gls
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. README stays lean/installer-first (device triggers folded into
**Events**); this file documents integration decisions, `carrier-research/gls/api/` the API.
