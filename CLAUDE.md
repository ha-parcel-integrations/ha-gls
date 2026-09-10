# Working in this repository

Home Assistant custom integration for **GLS** parcel tracking (16 countries).
Distributed via HACS; not part of HA core. Fourth carrier in the suite (with DHL,
DPD, PostNL) — same canonical shape, events and entity set; **mirror DHL when in
doubt**. Account-less (user-entered tracking codes). No DTO layer.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before touching `api.py`'s dispatch, a `countries/` package, or the DE session |
| Endpoint mechanics, status vocabularies | `carrier-research/gls/api/` (private repo) — the tracktrace endpoint, its `text/plain` body and 204 signalling, the numeric `state` → status map, the `scans[]` history, the two-identifier lookup. **Never** duplicated into this repo |
| Suite-wide conventions | [`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md) |

This file is the short list of things an agent must not get wrong.

## Shared conventions — fetch when relevant

Don't fetch `CONVENTIONS.md` every session — fetch it **before** you act in one
of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where GLS deviates) |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — so
  the `UpdateFailed`-on-total-failure case fails the whole entry (HA retries with
  backoff). From a forwarded platform HA can't catch `ConfigEntryNotReady`.
- **Setup stale-entity cleanup is scoped to `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the button / `last_update` sensor /
  live per-parcel sensors.

## Load-bearing GLS decisions — do not refactor away

**Account-less, postcode-keyed hubs** — GLS has no consumer account or feed.
Setup asks only the postcode + country and does **not** hit the API (the
endpoint needs a parcel number). `unique_id` is the bare postcode;
`single_config_entry` is deliberately **absent** — multiple hubs (home, work)
were the point. The shared `gls.*` services unload only when no other hub is
still loaded.

**Options apply live, never by reload.** An update listener
(`_async_options_updated`) retunes `update_interval` and calls
`async_request_refresh()`; the coordinator re-reads options each update. **Do
not** switch to `async_schedule_reload` — this is the account-less half of the
suite's two options models.

**Service field is `tracking_code`** (suite-wide standard); the deprecated
`parcel_no` alias was removed (ha-gls#3). The *stored* dict key stays
`parcel_no` (`CONF_PARCEL_NO`) — an internal options key, not the service field,
never part of the alias. Don't conflate them.

**Dispatch lives in `api.py`, not the coordinator** — `GlsApiClient.async_get_parcel`
picks NL / DE / group; the coordinator polls a flat pair list and never learns
the country. DPD dispatches in its coordinator instead; **don't "align" the
two.** Concern-level files (`coordinator.py`, `sensor.py`, `diagnostics.py`, …)
stay free of per-country branching — they dispatch into `countries/<code>/`.

**A country gets its own submodule on structural divergence from NL** (auth
model, transport, payload shape, status vocabulary), **not on country count.**
A new group-leaf country is one more `COUNTRIES` row pointing at
`countries/group/` — never a copy of that package.

**`culture` vs `group_locale` are different keys on purpose** — `culture` is a
national URL template's locale; `group_locale` is the pan-EU leaf's
`{ISO2}/{lang}` switch. Don't overload one for the other. A row without
`culture` falls back to `""`, not `KeyError`.

**Germany is the only stateful transport.** GLS keeps the tracked list
server-side, so a re-add answers a bare `409` with no `parcelNumber`; the
per-process `_known_parcel_numbers` cache is seeded from persisted state by
`_prepare_de_poll` before every poll and written back by `_persist_de_state`
after. A `GlsDeSessionError` fails the **whole poll** (nothing can
authenticate), never one parcel. On `pop_reregistered()` every learned
`parcelNumber` is stale and must be dropped. Full lifecycle:
[`ARCHITECTURE.md`](ARCHITECTURE.md).

**`CAPABILITIES_BY_VARIANT` holds one frozenset per country**
(`"Netherlands"` / `"Germany"` / `"Other"`), **not** a single intersected
`CAPABILITIES` — the old intersection model made NL's full support invisible on
the docs site the moment a weaker country landed (replaced 2026-08-23). Keep
each entry in lockstep with its `normalize_parcel_<cc>()`; every entry must stay
a subset of `KNOWN_CAPABILITIES`.

**`_raw_cache` must keep a blip from dropping a sensor** — a transient error or
`204` reuses the last good payload; a first-ever `204` yields a pending
`unknown` placeholder. `UpdateFailed` only when **every** tracked parcel errored
and nothing is cached. Consequently **`last_success_time` is stamped only when a
fetch actually succeeded** — a poll served entirely from cache is not a success,
and the diagnostic `last_update` sensor exists to reveal that.

**Two identifiers both resolve** (long `parcelNo`, short `uniqueNo`) —
`valid_parcel_no` accepts `^[A-Z0-9]{6,20}$`, not digits-only, and the
per-parcel `barcode` always comes from the **response** `parcelNo`.

**Multi-collo tracks at shipment level** — one sensor per tracked code. Do not
split colli into separate sensors.

**PII**: recipient email/address/preference UUIDs are redacted in
`diagnostics.py`. They still ride in the per-parcel `raw` attribute (user's own
data, unrecorded) — don't surface them elsewhere.

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
**Events**); a code change updates the README, `ARCHITECTURE.md` and this file
in the same commit; API mechanics go to `carrier-research/gls/api/`, never here.
