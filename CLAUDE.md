# Working in this repository

This is a Home Assistant custom integration for **GLS Netherlands** parcel
tracking. Distributed via HACS; not part of HA core. It is the fourth
carrier in the parcel suite (alongside DHL, DPD, PostNL) and follows the
same canonical shape, events and entity set — mirror DHL when in doubt.

## Always consult HA developer documentation

Home Assistant's integration patterns evolve. **Do not rely on memory** —
fetch the canonical page before changing a topic area, and check the
developer blog before introducing anything you only "know" from training.

| When you change | Fetch first |
|---|---|
| Entity properties, naming, lifecycle, attributes | https://developers.home-assistant.io/docs/core/entity/ |
| Config flow, options flow | https://developers.home-assistant.io/docs/config_entries_config_flow_handler |
| DataUpdateCoordinator | https://developers.home-assistant.io/docs/integration_fetching_data |
| Quality scale rules | https://developers.home-assistant.io/docs/core/integration-quality-scale |

## The big divergence: account-less, user-entered tracking codes

Unlike the other carriers, GLS has **no consumer account / feed**. The user
enters tracking codes themselves, so:

- **Setup asks only the postal code.** `async_step_user` collects a single
  postal code (no parcel number) and stores it as the hub default in
  `entry.options[CONF_POSTAL_CODE]`; the entry starts with an empty
  `CONF_PARCELS` list. Setup does **not** hit the API (the endpoint needs a
  parcel number to say anything).
- **Multiple hubs, one per postcode.** `unique_id = <postcode>` +
  `_abort_if_unique_id_configured`, so the same postcode can't be added
  twice but different postcodes (home + work) can. Title/device name is
  **`GLS (<postcode>)`** (postcode read from `entry.options[CONF_POSTAL_CODE]`
  in each `_build_device_info`) so multiple hubs stay distinguishable —
  mirrors the account-in-name pattern of the other carriers. (fable had made
  it single-instance via `single_config_entry`; the user wanted multiple
  hubs, so that manifest flag is removed.) The `gls.*` services are shared
  across hubs, so `async_unload_entry` only calls `async_unload_services`
  when **no other hub is still loaded** — removing them on any unload would
  break the remaining hubs. Entries from before the redesign carried
  `unique_id = DOMAIN`; `async_setup_entry` migrates that to the entry's
  postcode so the per-postcode duplicate guard also covers legacy installs.
- **Tracked parcels live in `entry.options[CONF_PARCELS]`** as a list of
  `{parcel_no, postal_code}` dicts. Added three ways, all validated the same
  (`valid_parcel_no` / `normalize_postcode` in `config_flow.py`): the
  **options flow**, the **`gls.track_parcel` / `gls.untrack_parcel`
  services** (`services.py`), and a Lovelace button that calls the service.
  Adding a parcel takes only its number — the postcode is **always** the
  hub's (`CONF_POSTAL_CODE`); the add form has no postcode field. The service
  keeps an optional `postal_code` for the rare different-address case.
- **Options flow = one sectioned form** (`async_step_init` with
  `data_entry_flow.section`), mirroring the other carriers' section layout —
  here `parcels` (add/remove), `history` (`include_history`), `polling`
  (`refresh_interval`). NOT a menu. The `remove` multiselect is only added to
  the schema when parcels exist. Do the remove-then-add order so re-adding a
  just-removed number works.
- **Option changes apply live, no reload.** An **update listener**
  (`_async_options_updated` in `__init__.py`) retunes `coordinator.update_interval`
  and calls `async_request_refresh()`. The coordinator re-reads `_tracked()`
  and `_include_history` from options every update, so a refresh is enough —
  the summary sensor's `_handle_coordinator_update` spawns/removes per-parcel
  sensors immediately. **Do not** switch this to `async_schedule_reload`: a
  refresh (not a reload) avoids the config-entry-listener deprecation and is
  what makes add/remove reflect in the entities without a manual refresh.
- **No auth / reauth / sent-shipments coordinator.** The HA-managed session
  is used directly (no per-entry cookie jar — there are no cookies).
- Entities are **entry-scoped** (like DPD): unique_id prefix is
  `entry.entry_id`, device identifier `(DOMAIN, entry.entry_id)`, device
  name just `"GLS"`.

## Identifiers & privacy

- **Two identifiers both resolve** on the endpoint: the long numeric
  `parcelNo` (`13290054100304`) and the short alphanumeric tracking ID /
  `uniqueNo` (`00L1B3BX`). So `valid_parcel_no` accepts `^[A-Z0-9]{6,20}$`
  (not digits-only) and `normalize_parcel_no` upper-cases the input. The
  per-parcel sensor's `barcode` always comes from the **response** `parcelNo`,
  so tracking by `uniqueNo` still shows the real parcel number.
- **Multi-collo:** one shipment can list several `parcels[]` (colli). We
  track at **shipment level** — one sensor per tracked code, using the
  top-level `state`/`scans`. Do not split colli into separate sensors.
- **PII:** the payload's `deliveryPreference` block nests the recipient's
  email (under `consignee.contactValues[].value`), address and preference
  UUIDs. It is redacted in `diagnostics.py` (`deliveryPreference` /
  `consignee` / `contactValues` / `houseNumber` in `TO_REDACT`). It still
  rides along in the per-parcel `raw` attribute (the user's own data,
  unrecorded) — do not surface it elsewhere.

## The API

- Public GLS endpoint (`PARCEL_DETAILS_URL` in `const.py`):
  `https://{host}/api/tracktrace/v1/{parcel_no}/postalcode/{postal_code}/details/{culture}`.
  `host` + `culture` come from the hub's **country** (see below), not
  hardcoded. No auth. `200` → JSON (served `text/plain`, so parse with
  `json.loads(await r.text())`), `204` → unknown / not-yet-scanned parcel
  (returns `None`), any other status → `GlsApiError`. `GlsApiClient` takes
  `(session, host, culture)`; `__init__.py` resolves them from the country.
- **Country model (`CONF_COUNTRY` / `COUNTRIES` in `const.py`).** Each hub
  picks a country at setup; the country maps to `{label, host, culture,
  postcode_regex}`. `valid_postcode(value, country)` validates against that
  country's regex. **Only `NL` (`apm.gls.nl`, `nl-NL`) is in the map today**
  — other GLS countries either expose no account-less endpoint or gate it
  behind Cloudflare / API registration (the pan-European
  `gls-group.com/.../rstt001` REST now redirects to `register-api-access`;
  `gls-pakete.de` is Cloudflare-challenged). Adding a country = one entry in
  `COUNTRIES` once a working account-less endpoint is confirmed. The setup
  form links `NEW_COUNTRY_ISSUE_URL` so users can request theirs. Do **not**
  switch to the registration-gated group REST. `unique_id` is still the bare
  postcode (fine while NL-only); fold in the country once a second one lands.

## Coordinator (mirror DHL, adapted)

- Polls **each** tracked parcel and merges them into one list —
  concurrently via one `asyncio.gather` (the endpoint is per-parcel, so
  serial polling scales badly with many tracked codes);
  `coordinator.data` is the active (not-delivered) parcels, `self.delivered`
  the delivered ones. **Multiple parcels are aggregated in the sensors** —
  the summary sensors count/list across all tracked codes, one per-parcel
  sensor per code.
- **`_raw_cache` (parcel_no → last raw payload).** A transient fetch error
  or a `204` reuses the last good payload so a parcel's sensor is not dropped
  on a blip. A first-ever `204` yields a pending placeholder
  (`{"parcelNo": no, "state": None}`) → status `unknown`, so the tracked
  parcel is still visible. `UpdateFailed` only when **every** tracked parcel
  errored and nothing is cached. The cache is pruned to the currently
  tracked numbers at the start of every update, so untracking also frees
  the cached payload.
- **`_dimensions` only formats `text` when all three sides are known** —
  a partial payload keeps the known values but `text: None` (never
  `"30 x None x None cm"`), mirroring DPD's `_augment_dimensions`.
- **`state` → `ParcelStatus`** via `_STATE_MAP` (numeric): `0` registered,
  `1`/`2` in_transit, `3` out_for_delivery, `4` delivered. The same map
  drives history (`map_event_status`). Unmapped non-null state → `unknown`
  (parcel) / `null` (history) plus a **one-shot WARNING** with the
  `issues/new` link (`_unmapped_states_logged`).
- **History is opt-in, default off** (`CONF_INCLUDE_HISTORY`, in the
  `settings` options step) — kept identical to the other suite carriers.
  `normalize_parcel(raw, *, include_history=...)` builds the timeline from
  the `scans[]` array (which is already in the same response, so enabling it
  costs no extra request); when off, `history` is `None`. `raw_status` on a
  history entry is the Dutch `eventReasonDescr`. It is in
  `_unrecorded_attributes` on the per-parcel sensor.
- **Delivered retention** — `_apply_delivered_filter` trims `self.delivered`
  by the `delivered` options section (`days` window or `parcels` count,
  default 7 days), mirroring the other carriers. **Display-only**: parcels
  stay tracked and polled; this only controls the delivered sensor. (A
  per-parcel sensor already disappears on delivery because a delivered parcel
  leaves `coordinator.data` and the summary sensor removes it.)
- **Delivery window** = `deliveryStatus.etaTimestampMin` / `etaTimestampMax`
  (only while not delivered).
- **weight + dimensions are populated** (GLS provides them, unlike DHL).
- **Events** (`gls_parcel_registered` / `_status_changed` / `_delivered` /
  `_delivery_time_changed`) fire exactly as DHL's, including the cached
  `device_id` on every payload, first-refresh suppression, and the silent
  `value → null` ETA transition. They run over the **active + delivered**
  set combined, so the terminal hop is visible: a change **to**
  ``DELIVERED`` fires only `_delivered` (never also `_status_changed`), a
  barcode first seen already-delivered fires nothing, and `registered`
  only fires for not-yet-delivered new barcodes.
- `last_success_time` is only stamped when **at least one fetch actually
  succeeded** (or nothing is tracked). A poll served entirely from
  `_raw_cache` is not a success — the diagnostic sensor exists precisely
  to reveal that situation.
- **First refresh runs in `__init__.py`, before `async_forward_entry_setups`**
  — `async_setup_entry` awaits `coordinator.async_config_entry_first_refresh()`
  before forwarding (not in the `sensor.py` platform). Raising
  `ConfigEntryNotReady` from a *forwarded* platform is too late for HA to
  catch — it logs a warning and half-sets-up the entry. Doing the first
  refresh here lets the `UpdateFailed`-on-total-failure case fail the whole
  entry so HA retries with backoff. Do not move it back into a platform.

## Entities (same set as DHL, entry-scoped)

`sensor` (incoming summary + per-parcel + next_delivery +
en_route_to_parcel_shop + awaiting_pickup + delivered_parcels +
diagnostic `last_update`), `button` (refresh), `calendar` (deliveries,
read-only, enabled by default), device triggers. The setup-time stale-entity
cleanup in `sensor.py` is scoped to `entity_entry.domain == "sensor"` and
excludes the summary/diagnostic unique_ids (`non_parcel_unique_ids`) — do
not drop either guard or it deletes the button / last_update sensor / live
per-parcel sensors.

## Docs / README

The README stays **lean, installer-first** (suite house style): no
`## Buttons` / `## Calendar` sections; the device-trigger option is one
sentence folded into **Events**. CLAUDE.md documents everything.

## Shared conventions

Workflow, commit style, versioning and release notes live in
[`ha-parcel-integrations/.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are not repeated here. In short: single-line commit messages, semver, tags
without a `v` prefix, maintainer-only merges, user-facing release notes.

The structural baseline every carrier repo shares is the
[`ha-carrier-template`](https://github.com/ha-parcel-integrations/ha-carrier-template)
scaffold. One deliberate divergence: GLS keys hubs on **postal code**, so `single_config_entry` is intentionally absent and several hubs are legitimate.

## Running tests

```
python -m pytest tests/ --cov=custom_components.gls
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.
