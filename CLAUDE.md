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

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — so
  the `UpdateFailed`-on-total-failure case fails the whole entry (HA retries with
  backoff). From a forwarded platform HA can't catch `ConfigEntryNotReady` and
  half-sets-up the entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity cleanup is scoped to `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the button / `last_update` sensor /
  live per-parcel sensors.

## The big divergence: account-less, postcode-keyed hubs

GLS has **no consumer account / feed** — the user enters tracking codes.

- **Setup asks only the postal code** (`async_step_user`), stored as the hub
  default in `entry.options[CONF_POSTAL_CODE]`; `CONF_PARCELS` starts empty.
  Setup does **not** hit the API (the endpoint needs a parcel number).
- **Multiple hubs, one per postcode.** `unique_id = <postcode>` +
  `_abort_if_unique_id_configured` (home + work both work). Device name
  `"GLS (<postcode>)"`. `single_config_entry` is deliberately **absent** (the
  user wanted multiple hubs). The shared `gls.*` services are unloaded only when
  **no other hub is still loaded**. Legacy entries with `unique_id = DOMAIN` are
  migrated to the postcode in `async_setup_entry`.
- **Tracked parcels live in `entry.options[CONF_PARCELS]`** as
  `{parcel_no, postal_code}` dicts, added three ways (options flow, the
  `gls.track_parcel` / `gls.untrack_parcel` services, a Lovelace button), all
  validated by `valid_parcel_no` / `normalize_postcode`. Adding takes only the
  number — the postcode is **always** the hub's; the service keeps an optional
  `postal_code` for the rare different-address case.
- **Service field is `tracking_code`** (suite-wide standard). The old `parcel_no`
  field is a **deprecated alias** — `_resolve_code` accepts either, logs a
  one-shot deprecation warning, **to be removed**. The *stored* dict key stays
  `parcel_no` (`CONF_PARCEL_NO`) — only the service field was renamed; don't
  conflate them.
- **Options flow = one sectioned form** (`parcels` / `history` / `polling`), not
  a menu. The `remove` multiselect is only in the schema when parcels exist; do
  remove-then-add so re-adding a just-removed number works.
- **Option changes apply live, no reload.** An **update listener**
  (`_async_options_updated`) retunes `coordinator.update_interval` and calls
  `async_request_refresh()`; the coordinator re-reads `_tracked()` /
  `_include_history` each update, so a refresh (not a reload) makes add/remove
  reflect immediately and avoids the config-entry-listener deprecation. **Do not**
  switch this to `async_schedule_reload`.
- **No auth / reauth / sent-shipments coordinator.** The HA-managed session is
  used directly (no per-entry cookie jar — no cookies). Entities are
  **entry-scoped** (like DPD): unique_id prefix `entry.entry_id`, device
  identifier `(DOMAIN, entry.entry_id)`.

## Identifiers & privacy

- **Two identifiers both resolve**: the long numeric `parcelNo`
  (`13290054100304`) and the short alphanumeric `uniqueNo` (`00L1B3BX`). So
  `valid_parcel_no` accepts `^[A-Z0-9]{6,20}$` (not digits-only) and
  `normalize_parcel_no` upper-cases. The per-parcel sensor's `barcode` always
  comes from the **response** `parcelNo`, so tracking by `uniqueNo` still shows
  the real number.
- **Multi-collo**: one shipment can list several `parcels[]` (colli). We track at
  **shipment level** — one sensor per tracked code, using the top-level
  `state`/`scans`. Do not split colli into separate sensors.
- **PII**: `deliveryPreference` nests the recipient's email
  (`consignee.contactValues[].value`), address and preference UUIDs — redacted in
  `diagnostics.py` (`deliveryPreference` / `consignee` / `contactValues` /
  `houseNumber` in `TO_REDACT`). It still rides in the per-parcel `raw` attribute
  (user's own data, unrecorded) — don't surface it elsewhere.

## The API & country model

- Public endpoint (`PARCEL_DETAILS_URL`):
  `https://{host}/api/tracktrace/v1/{parcel_no}/postalcode/{postal_code}/details/{culture}`.
  `host` + `culture` come from the hub's **country**, not hardcoded. No auth.
  `200` → JSON (served `text/plain` → `json.loads(await r.text())`), `204` →
  unknown / not-yet-scanned (returns `None`), any other → `GlsApiError`.
- **Country model** (`CONF_COUNTRY` / `COUNTRIES`): each hub picks a country →
  `{label, host, culture, postcode_regex}`; `valid_postcode(value, country)`
  validates against it. **Only `NL` (`apm.gls.nl`, `nl-NL`) is mapped** — other
  GLS countries expose no account-less endpoint or gate it behind
  Cloudflare/registration (the pan-EU `rstt001` REST now redirects to
  `register-api-access`; `gls-pakete.de` is Cloudflare-challenged). Adding a
  country = one `COUNTRIES` entry once a working account-less endpoint is
  confirmed; the setup form links `NEW_COUNTRY_ISSUE_URL`. Do **not** switch to
  the registration-gated group REST. `unique_id` stays the bare postcode (fine
  while NL-only); fold in the country once a second lands.

## Coordinator (mirror DHL, adapted)

- Polls **each** tracked parcel concurrently via one `asyncio.gather` and merges
  them. `coordinator.data` = active parcels, `self.delivered` = delivered ones;
  the summary sensors count/list across all tracked codes, one per-parcel sensor
  per code.
- **`_raw_cache` (parcel_no → last raw payload)**: a transient error or a `204`
  reuses the last good payload so a sensor isn't dropped on a blip. A first-ever
  `204` yields a pending placeholder (status `unknown`) so the parcel is still
  visible. `UpdateFailed` only when **every** tracked parcel errored and nothing
  is cached. Pruned to currently-tracked numbers each update.
- **`state` → `ParcelStatus`** via numeric `_STATE_MAP`: `0` registered, `1`/`2`
  in_transit, `3` out_for_delivery, `4` delivered; same map drives history
  (`map_event_status`). Unmapped non-null state → `unknown` (parcel) / `null`
  (history) + one-shot WARNING with `issues/new` link (`_unmapped_states_logged`).
- **History opt-in, default off** (`CONF_INCLUDE_HISTORY`) — built from the
  `scans[]` already in the response (no extra request); `raw_status` per entry is
  the Dutch `eventReasonDescr`; in `_unrecorded_attributes`.
- **`weight` + `dimensions` are populated** (GLS provides them, unlike DHL).
  `_dimensions` only formats `text` when all three sides are known (never
  `"30 x None x None cm"`). Delivery window =
  `deliveryStatus.etaTimestampMin`/`etaTimestampMax` (only while not delivered).
- **Delivered retention** — `_apply_delivered_filter` trims `self.delivered` by
  the `delivered` options (`days` window or `parcels` count, default 7 days);
  **display-only** (parcels stay tracked and polled).
- **Events** (`gls_parcel_registered` / `_status_changed` / `_delivered` /
  `_delivery_time_changed`) fire exactly as DHL's — cached `device_id`,
  first-refresh suppression, silent `value → null` ETA, run over active+delivered
  combined (change **to** DELIVERED fires only `_delivered`, etc.).
- **`last_success_time` is stamped only when at least one fetch actually
  succeeded** (or nothing is tracked). A poll served entirely from `_raw_cache`
  is not a success — the diagnostic `last_update` sensor exists to reveal that.

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
**Events**); this file documents everything.
