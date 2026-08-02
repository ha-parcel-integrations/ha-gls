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

**API mechanics live in `docs/api/` (local-only, gitignored)** — the tracktrace
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
- **Service field is `tracking_code`** (suite-wide standard). The old `parcel_no`
  field is a **deprecated alias** — `_resolve_code` accepts either, logs a one-shot
  deprecation warning, **to be removed**. The *stored* dict key stays `parcel_no`
  (`CONF_PARCEL_NO`) — only the service field was renamed; don't conflate them.
- **Options flow = one sectioned form** (`parcels` / `history` / `polling`), not a
  menu. The `remove` multiselect is only in the schema when parcels exist; do
  remove-then-add so re-adding a just-removed number works.
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
  host/culture/postcode-regex. **Only `NL` is mapped** — other GLS countries expose
  no account-less endpoint or gate it behind Cloudflare/registration. Adding a
  country = one `COUNTRIES` entry once a working account-less endpoint is confirmed;
  the setup form links `NEW_COUNTRY_ISSUE_URL`. **Do not switch to the
  registration-gated group REST.** `unique_id` stays the bare postcode (fine while
  NL-only); fold in the country once a second lands.
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
**Events**); this file documents integration decisions, `docs/api/` the API.
