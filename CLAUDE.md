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
- **Single-instance hub.** `unique_id = DOMAIN`, second entry aborts. One
  **GLS** device holds every tracked parcel.
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

## The API

- Public GLS-NL endpoint (`PARCEL_DETAILS_URL` in `const.py`):
  `https://apm.gls.nl/api/tracktrace/v1/{parcel_no}/postalcode/{postal_code}/details/{culture}`
  with `culture = nl-NL`. No auth. `200` → JSON (served `text/plain`, so
  parse with `json.loads(await r.text())`), `204` → unknown / not-yet-scanned
  parcel (returns `None`), any other status → `GlsApiError`.
- **NL-only.** This is the GLS Netherlands system, keyed on an NL postcode.
  The pan-European `gls-group.com/.../rstt001` REST is now gated behind API
  registration, so it is not usable account-less. Do not switch to it.

## Coordinator (mirror DHL, adapted)

- Polls **each** tracked parcel individually and merges them into one list;
  `coordinator.data` is the active (not-delivered) parcels, `self.delivered`
  the delivered ones. **Multiple parcels are aggregated in the sensors** —
  the summary sensors count/list across all tracked codes, one per-parcel
  sensor per code.
- **`_raw_cache` (parcel_no → last raw payload).** A transient fetch error
  or a `204` reuses the last good payload so a parcel's sensor is not dropped
  on a blip. A first-ever `204` yields a pending placeholder
  (`{"parcelNo": no, "state": None}`) → status `unknown`, so the tracked
  parcel is still visible. `UpdateFailed` only when **every** tracked parcel
  errored and nothing is cached.
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
- **Delivery window** = `deliveryStatus.etaTimestampMin` / `etaTimestampMax`
  (only while not delivered).
- **weight + dimensions are populated** (GLS provides them, unlike DHL).
- **Events** (`gls_parcel_registered` / `_status_changed` /
  `_delivery_time_changed`) fire exactly as DHL's, including the cached
  `device_id` on every payload, first-refresh suppression, and the silent
  `value → null` ETA transition.
- `last_success_time` stamped at the end of every successful update, backing
  the diagnostic sensor.

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

## Running tests

```
python -m pytest tests/ --cov=custom_components.gls
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.
