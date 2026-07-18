# Automatic parcel tracking from e-mail (IMAP → GLS)

Companion guide for [`track_parcels_from_email.yaml`](track_parcels_from_email.yaml): watch your mailbox(es) for shipping e-mails, extract the GLS parcel number, and register it with `gls.track_parcel` — fully automatic, no extra custom component required.

GLS is a **code-based** carrier: it has no account inbox, so every parcel must be registered by its parcel number before the integration can follow it. This recipe automates exactly that step.

**How it works, in one line:** the core [IMAP integration](https://www.home-assistant.io/integrations/imap/) fires an `imap_content` event for every new e-mail (including the body); the automation extracts the parcel number — a cheap regex first, an optional AI fallback for everything else — and calls `gls.track_parcel`.

```
new e-mail ──imap_content──▶ automation ──▶ regex match? ──▶ gls.track_parcel
                                     │
                                     └──▶ no match, but looks like a shipping mail
                                          ──▶ ai_task.generate_data (optional)
                                              ──▶ parcel number
```

## Prerequisites

- This integration, with the `gls.track_parcel` action available (fields `parcel_no`, optional `postal_code`).
- The core **IMAP** integration (ships with Home Assistant, no HACS needed).
- *(Optional but recommended)* an **AI Task** entity (e.g. Anthropic/Claude, Google, OpenAI) for the fallback path. Without it, simply delete the `else:` block — the regex path works standalone.

## Step 1 — IMAP entries

Add **Settings → Devices & services → Add integration → IMAP** for every account you want to watch:

| Field | Value |
|---|---|
| Server | `imap.gmail.com` (Gmail) — mind the hostname, it is **not** `imap.google.com` |
| Port | `993` |
| Username | your address |
| Password | see the Gmail note below |
| Charset | `utf-8` |
| Folder | `INBOX` (or a label/subfolder — see below) |

Then open the entry's **Configure** (options) and set:

- **Message data to include in the event**: enable **text** (the automation needs the body!)
- **Max message size**: raise it to `30000` — carrier mails are long and the default cuts them off before the parcel number appears.
- Leave *search* on `UnSeen UnDeleted` and *push* enabled (IMAP IDLE → events arrive within seconds).

**Multiple mailboxes / accounts:** each IMAP entry is one account × folder combination. Add the same account again with a different folder to watch labels (Gmail labels appear as IMAP folders). All entries fire the *same* `imap_content` event, so **one automation covers all of them**.

**Gmail note:** since May 2025 Google blocks plain-password IMAP logins ("less secure apps"). Use an **app password** instead (requires 2-step verification): <https://myaccount.google.com/apppasswords>.

## Step 2 — the automation

Paste [`track_parcels_from_email.yaml`](track_parcels_from_email.yaml) and adapt the notify action, the keyword list and the AI entity to your setup.

### About the parcel-number regex

```
(?<![0-9])[0-9]{11,14}(?![0-9])        — only applied to GLS mails
```

GLS parcel numbers are long digit runs, which is a *weak* signal on its own — so the example only applies it when the mail actually looks like GLS (`'gls' in sender or subject`). Two further notes:

- **Phone numbers collide.** An 11-digit Dutch phone number in international notation (`31…`) sits inside the 11–14 digit range; the `reject('match', '31[0-9]{9}$')` filter drops it. **Adapt the country code to yours.**
- **The short tracking ID is not matched.** GLS also accepts a short alphanumeric ID (e.g. `00L1B3BX`) alongside the long numeric parcel number — both resolve to the same shipment. It is deliberately *not* in the regex: a 6–20 character alphanumeric pattern would match half the words in an e-mail. Use the long number, or let the AI fallback handle it.

### `postal_code` is optional

`gls.track_parcel` takes an optional `postal_code`; when omitted the integration falls back to the postcode configured on the GLS hub. The example omits it, which is right for parcels coming to your own address. Pass it explicitly only for parcels going elsewhere.

### Design notes

- **Regex first, AI second.** Mails straight from the carrier match the regex and never touch the AI. The AI fallback earns its keep on the messy cases: forwarded mails, webshop confirmations, unfamiliar layouts. In a real-world test it pulled the correct GLS parcel number out of a forwarded mail while ignoring a second code embedded in the T&T links, a sender reference and several dates.
- **Duplicates are harmless:** calling `track_parcel` twice for the same number is a no-op, and the `initial` condition already suppresses re-triggers of the same message.
- **`mode: queued`** so a burst of mails (mailbox sync) is processed one by one instead of being dropped.

## Pitfalls we hit so you don't have to

1. **Jinja eats backslashes in string literals.** A template stored as `regex_findall('\b[0-9]{11,14}')` silently becomes a **backspace character** (`\b` is a string escape), so the regex never matches — no error anywhere. That's why every pattern is backslash-free: `(?<![0-9])`/`(?![0-9])` lookarounds instead of `\b`, `[0-9]` instead of `\d`.
2. **The `initial` event flag means the opposite of what you might expect.** In the IMAP integration `initial: true` = *first time this message is seen* (new mail); `false` = a duplicate trigger of the same message. So the condition must **require** `initial`, not exclude it.
3. **Raise the max message size.** With the default the body is truncated before the parcel number appears in most carrier mails. `30000` is plenty.
4. **Enable "text" in the event options.** Without it the event has headers only and there is nothing to extract.
5. **Gmail = app password.** Plain passwords stopped working on Google IMAP in May 2025; app passwords (with 2FA) are the supported route. And the host is `imap.gmail.com`.
6. **Digit-run regexes match phone numbers and dates too.** Hence the GLS-mail gate plus the phone-number reject — do not loosen both at once.

## Testing without waiting for a real parcel

Fire a fake event and watch the automation trace (Settings → Automations → your automation → Traces):

```bash
curl -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  http://YOUR_HA:8123/api/events/imap_content \
  -d '{"sender":"noreply@gls-netherlands.com","subject":"Your GLS parcel",
       "text":"Parcel number: 12345678901234","initial":true,"folder":"INBOX","username":"test"}'
```

Then `gls.untrack_parcel` the test number afterwards. For a full end-to-end test, forward a real shipping mail to the watched mailbox — it must arrive **unread**.
