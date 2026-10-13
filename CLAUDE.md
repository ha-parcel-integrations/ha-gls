# Working in this repository

Home Assistant custom integration for **GLS** parcel tracking (23 countries).
Distributed via HACS; not part of HA core. Fourth carrier in the suite (with DHL,
DPD, PostNL) — same canonical shape, events and entity set; **mirror DHL when in
doubt**. Account-less (user-entered tracking codes). No DTO layer.

Three places hold the knowledge, and they do not overlap:

| What | Where |
|---|---|
| How this integration is built, and why it is built that way | [`ARCHITECTURE.md`](ARCHITECTURE.md) — read it before touching `api.py`'s dispatch, a `countries/` package, or the DE session |
| Endpoint mechanics, status vocabularies | `carrier-research/gls/api/` (private repo). **Never** duplicated into this repo |
| Suite-wide conventions | [`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md) |

This file is the short list of things an agent must not get wrong.

## Shared conventions — fetch when relevant

Don't fetch `CONVENTIONS.md` every session — fetch it **before** you act in one
of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where GLS deviates) |
| consider "fixing" a lint/pattern the skill flags (inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
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
(`_async_options_updated`) calls `async_request_refresh()`; the coordinator
re-reads options each update. **Do not** switch to `async_schedule_reload` —
this is the account-less half of the suite's two options models.

**Polling cadence is not configurable — don't add the option back.** The
Section 2.1 dynamic-polling algorithm always runs: `update_interval` is
recomputed at the end of every refresh (quiet window 00:00–06:00 with two
anchors, hot 15 min / mid 45 min, full stop when nothing is tracked or
everything is delivered, plus a per-hub stagger). The old `refresh_interval`
dropdown (Phase 1) is gone; see [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Service field is `tracking_code`** (suite-wide standard); the deprecated
`parcel_no` alias was removed (ha-gls#3). The *stored* dict key stays
`parcel_no` (`CONF_PARCEL_NO`) — an internal options key, not the service field,
never part of the alias. Don't conflate them.

**Dispatch lives in `api.py`, not the coordinator** — `GlsApiClient.async_get_parcel`
picks NL / CA / US / PL / DE / ES+PT / group; the coordinator polls a flat pair list and never learns
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
after. Status stays derivation-first; the `_STATUS_TEXT_MAP` fallback is
exact-match on the pinned `de-DE` text and only takes user-confirmed wording —
never a translation or a substring. A `GlsDeSessionError` fails the **whole poll** (nothing can
authenticate), never one parcel. On `pop_reregistered()` every learned
`parcelNumber` is stale and must be dropped. Full lifecycle:
[`ARCHITECTURE.md`](ARCHITECTURE.md).

**The US lookup ignores the postcode, and the hub still asks for one.** GLS US
is keyed on the tracking number alone; the postcode field stays required so
every GLS hub behaves identically (and stays the default for parcels added
later). Don't "fix" that by branching the setup flow per country. Its
`shipments[]` can hold **several records for one number**, oldest first — pick
the newest by timestamp, never by array position, and reject the
`0001-01-01`/`1900-01-01` placeholders by value. Status mapping is
derivation-first, like DE's: only the delivered literal is mapped exactly,
anything else is derived from the record's own dates and logged once.

**Poland is national on purpose, though the group leaf resolves it.** A real
Polish AWB does answer `rstt029`, so a `COUNTRIES` row on `countries/group/`
would "work" — don't make that change. The national route gives history and an
offset-bearing `delivered_at` without a postcode, and it separates a GLS Point
drop-off from the recipient collecting the parcel; the group leaf calls the
drop-off `DELIVERED`. Two status levels, never merged: `progressBarIdent` is
the group's machine vocabulary (**match exactly** — `DELIVEREDPS` contains
`DELIVERED`), while events carry only Polish text, mapped from captured values
only. An unrecognised shipment code falls back to the newest event's status.

**Spain and Portugal are one signed transport, and the postcode is half the
lookup key.** `countries/es/signing.py` stays **pure** — a signature bug and a
rejected key look alike on the wire, so the signature must be provable without
a network. The signed path **excludes** the URL's `/api/v5` prefix. **Re-stamp
and re-sign inside the request function, never above a retry loop**: a reused
timestamp is rejected. The three failure branches are distinguishable and must
stay reported apart — `404` + the body's own `notFound` is the *only* absence,
`401 InvalidCredentialsError` is clock skew, `403 ForbiddenError` is the key;
anything else, and HTML on a `200`, is a transport failure. **`incidence` is
mapped on its sentence, not its code** (the code covers an ordinary missed
delivery as much as a refusal), which is why `Accept-Language` is pinned and
never follows the user's HA locale, and why the redirect sentence is an
anchored prefix and never a substring test. **`agency` is the destination
depot and is present on every shipment** — `pickup_point` is gated on a GLS
Point *scan*, not on a field's presence, or every Spanish parcel gets a
phantom one. Timestamps are true UTC instants: do **not** apply the
wall-clock reconstruction the group leaves need. ES/PT are the only rows
besides NL whose `tracking_url` carries the postcode, and the generic GROUP
fallback must **not** be used for them — Spanish consignments are not in the
pan-EU index.

**`barcode` is the code the user tracked — a deliberate ES divergence from the
suite's "always take the response's own number" rule; don't align it with PL
or NL.** Those return the same identifier the user holds; Spain answers a
14-digit parcel barcode with a 10-digit `expeditionCode`, the *shipment*
number, which the recipient never sees. Both forms resolve, the per-parcel
sensor's `unique_id` is built from this field, so switching would rename every
Spanish entity to a number its owner cannot recognise, for nothing.
`expeditionCode` is an **int** on the wire and stands in only when the user's
own code is missing.

**Three real parcels confirmed the surface (2026-10-12)**, Portugal's among
them — so PT is a confirmed contract, not just a shared host. `pickup_point`
and `weight` graduated out of `PENDING_CAPABILITIES_BY_VARIANT` on them;
`delivery_window` is still pending, because all three were delivered and
`state.deliveryDate` has only ever been seen agreeing with the actual. Still
unconfirmed: a *moving* parcel, and eleven of the eighteen state codes.

**`incidence` is read by its sentence because the code genuinely does not
decide** — the redirect wording has been seen under both `incidence` and
`picked`. Four sentences are mapped from real parcels; only the redirect is
not a `PROBLEM`.

**One transport, two countries — so nothing here may say "Spain" to a user.**
The log lines read "GLS Spain/Portugal" because a Portuguese parcel goes
through this exact code, and a Spain-only warning reads as the wrong-country
bug it isn't. **PT also has its own consumer link host**
(`mygls.gls-portugal.pt`): the Spanish host does not resolve a Portuguese
consignment, and because the page is a Flutter SPA it answers `200` either
way, so getting that host wrong fails *silently* — a status check cannot
verify these links, only a browser can. Don't collapse the two `tracking_url`
rows.

**History ties are not reversed.** Three observed pairs share a minute (the
agency scan and the van loading); the carrier states no order, and reversing
them prints "Out for delivery" before "At delivery agency".

**France and Italy are tracked on the group leaf, and each takes a second
endpoint for one job only.** Both are strictly additive — a failure degrades
to exactly the pre-existing behaviour — and **neither is a transport**; don't
"finish" them into one.
- **`countries/fr/` translates, it does not track.** It resolves the
  8-character alphanumeric reference both group leaves reject (`404
  E206`/`E800`) into the numeric AWB they index. A plain numeric code **never**
  reaches it, which is what keeps today's French parcels on today's path with
  no extra request. Only `numeroGp` is the leaf's identifier — three forms come
  back in one reply and picking another looks exactly like an outage. This is
  **not** the shipped `E609` → `rstt029` fallback, which covers a postcode
  mismatch. A full French transport was considered and **rejected**: its status
  vocabulary is one value wide (`LIV` only). Don't re-propose it on payload
  richness alone.
- **`countries/it/` supplies history only, and only fills a gap.** Italy keeps
  `progressBar.statusInfo` — a complete machine vocabulary — because
  `rstt030`'s history carries **no event code at all**, only a localized
  sentence. A migration would trade reliable status for history; **don't make
  it.** `rstt030` rows need no translation (`date`/`time`/`evtDscr` is the
  group builder's own shape) and have no `evtNo`, so per-event `status` is
  `None` on purpose. An `rstt028` history is **never** replaced — it carries
  per-event codes the national rows do not. `type=NAT` is pinned
  unconditionally: omitting it answers `400` with **HTML**. `rstt030` is
  published for `IT` alone (eleven other paths `403`) — it is not a
  country-expansion lever.

**`E800` is not absence, and Romania is why.** `rstt028` is keyed on the group
`tuNo`; a Romanian label code carries one more check digit than that, so it
answers `E800` on the detail leaf while `rstt029` resolves it by AWB alone.
Both fall back to `rstt029`, and the `E800` warning fires **only when the
fallback disagrees** — a genuinely absent parcel answers both leaves and must
stay silent. **Don't "simplify" `E800` back to an immediate `None`**: that is
what made Romanian parcels unfindable. The cost is one extra request per poll
for a parcel that really is absent, which is accepted.

**The group leaf reports its own coverage gaps, and that is deliberate.**
`rstt028` is captured for CZ alone (`GROUP_RSTT028_CONFIRMED`), so a `200`
from any other group country warns **once per country** with a pre-filled
`country_coverage.yml` link. It is a WARNING although nothing is wrong: an
INFO is invisible in a default HA log, and only someone receiving a parcel in
that country can ever confirm the detail leaf — the call needs their own
postcode. **Add the country to `GROUP_RSTT028_CONFIRMED` when a report lands**;
that is the off switch, not a log-level change. Neither this warning nor the
`E800` one prefills the tracking number — the template asks for it in its own
optional field, where the user can decline.

**`CAPABILITIES_BY_VARIANT` holds one frozenset per country**
(`"Netherlands"` / `"Germany"` / `"Canada"` / `"United States"` / `"Poland"` /
`"Spain"` / `"Other"`), **not** a single intersected
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
`valid_parcel_no` accepts any non-empty code unvalidated (the two shapes
differ too much, and neither client-side format is confirmed, to gate on one
guessed regex), and the per-parcel `barcode` always comes from the
**response** `parcelNo`.

**Multi-collo tracks at shipment level** — one sensor per tracked code. Do not
split colli into separate sensors.

**PII**: recipient email/address/preference UUIDs are redacted in
`diagnostics.py`. They still ride in the per-parcel `raw` attribute (user's own
data, unrecorded) — don't surface them elsewhere.

## Entities (same set as DHL, entry-scoped)

`sensor` (incoming summary + per-parcel + next_delivery + en_route_to_pickup_point
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
