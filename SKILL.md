---
name: alexa-account-device-api
description: Use when you need to list, audit or bulk-delete the smart-home devices registered in an Amazon Alexa account — typically to clean up orphaned or duplicated devices left behind when a skill is disabled/re-enabled, a hub is replaced, or a home is moved out of. Covers reusing an existing Home Assistant `alexa_media` session instead of logging in again, and the one mistake that logs the account out.
---

# Alexa account device API

The Alexa app has no bulk operations. An account that has accumulated hundreds or
thousands of stale smart-home devices — after a skill is toggled, a bridge is
re-paired, or a household moves — can only be cleaned one tap at a time. The
account API can do it in one pass.

These endpoints are **undocumented and reverse-engineered**. They can change
without notice. Treat a schema error on the list call as expected maintenance,
not as a broken tool.

## 1. Get a session without logging in again

If Home Assistant's `alexa_media` integration (HACS) is set up, it already holds a
valid session. Reuse it **read-only**:

```
<config>/.storage/alexa_media.<email>.cookies
```

```json
{"format": "alexapy.cookies", "version": 1, "cookies": [{"name": "...", "value": "...", "domain": "..."}]}
```

Build a `Cookie:` header from the cookies whose domain contains `amazon.`, and send
the value of the `csrf` cookie **also as a `csrf:` header** — the API rejects writes
without it. Plain `urllib` is enough; no dependencies needed.

### The mistake that logs the account out

**Never construct an `alexapy.AlexaLogin` outside the integration, and never call
`finalize_login()` or `save_cookiefile()`.** Doing so writes that object's own
(empty) jar over the integration's cookie file. Observed in practice: the file went
from ~4.5 KB to 58 bytes and the account was logged out.

Recovery, if it happens — do this **before any restart**:

```
action: homeassistant.reload_config_entry   # target the alexa_media entry
```

Unloading the entry runs `close_connections()`, which writes the live, valid jar
back to disk before setup reads the file again. A restart first would load the
empty jar and lose the session for good.

So: open the cookie file `"r"`, never `"w"`. If you are writing a tool, put that in
a comment at the top so the next person does not "helpfully" add a refresh path.

## 1b. There is no login flow — and should not be

Amazon sign-in is MFA-protected and actively anti-scripting (CAPTCHA, device
registration, rotating challenges). Do not build one into a tool like this: it is
`alexapy`-sized complexity, it breaks whenever Amazon changes the flow, and the
library that does implement it is exactly what clobbers cookie files.

Let a human clear MFA in a browser once, then consume the session:

- **Reuse the `alexa_media` jar** (above). While the integration runs it re-stamps
  the session periodically — observed: 13 cookies, ~1 year expiry, file re-written
  the same day — so it rarely needs manual renewal.
- **Or import a browser cookie export** (Netscape `cookies.txt` for `amazon.com`).
  Write the converted jar somewhere the integration does not own, mode `0600`, and
  make the importer *refuse* paths that look like HA's `.storage`.

The `csrf` cookie is set by the Alexa web app, not the storefront — an export taken
from `amazon.com` will be missing it and every write will be rejected.

Sessions can die early (password change, sign-out-everywhere). There is no refresh
path; the recovery is re-import or let the integration re-establish it.

## 2. The three endpoints

Base host is your marketplace's, e.g. `https://alexa.amazon.com` (`.de`, `.co.uk`, …).

**List** — `POST /nexus/v1/graphql`

```graphql
query CustomerSmartHome {
  endpoints(endpointsQueryParams: { paginationParams: { disablePagination: true } }) {
    items {
      endpointId
      friendlyName
      legacyAppliance {
        applianceId
        applianceTypes
        friendlyName
        manufacturerName
        modelName
        entityId
        connectedVia
        mergedApplianceIds
      }
    }
  }
}
```

The useful identity lives in `data.endpoints.items[].legacyAppliance`. The
`applianceId` is what the other two endpoints take. `manufacturerName` is the field
that makes a cleanup tractable — orphans cluster by vendor (e.g. every device from a
skill you no longer use, or a local bridge whose registrations went stale).

**Reachability** — `POST /api/phoenix/state`

```json
{"stateRequests": [{"entityId": "<applianceId>", "entityType": "APPLIANCE"}]}
```

Useful to confirm a device really is dead before deleting it. Orphans typically
return an error such as `ApplianceDriverHostingServiceException`, or a
`SkillNotEnabledException` for devices from a removed skill.

**Delete** — `DELETE /api/phoenix/appliance/<url-encoded applianceId>` → `200`

URL-encode the whole id with nothing safe (`quote(id, safe="")`) — appliance ids
routinely contain `/`, `=`, `&` and base64 payloads.

## 3. Bulk deletion, done safely

Deletion is **not reversible**. The devices must be rediscovered afterwards, and
any Alexa routines, groups or names attached to them are lost.

The sequence that survives contact with reality:

1. **List to a file** and inspect it. Group by `manufacturerName` and count.
2. **Probe a few** with the state endpoint. Confirm the ones you intend to remove
   are actually unreachable, and that something you still use is not in the set.
3. **Delete a sample of two or three**, then **re-list** to confirm they are gone.
   This validates the whole chain before it touches a thousand rows.
4. **Then run the batch**, with a small sleep between calls and a hard cap on how
   many ids a single invocation will accept.
5. **Re-list at the end.** A `500` does not reliably mean the delete failed — a
   device can return `500` and still be gone. Only a fresh listing is truth.

Build the id list by filtering the listing rather than by writing a
"delete everything matching X" flag. A file of explicit ids is reviewable; a filter
evaluated at delete time is not.

## 4. After a cleanup

Deleting orphans does not restore control — it only clears the way. The account
still has to rediscover the real devices ("Alexa, discover devices", or the
appropriate *Add Device* flow for a local bridge). Expect the rediscovered set to be
slightly smaller than what you exposed; re-check and name the stragglers by hand.

## Anti-patterns

- Instantiating `AlexaLogin` / calling `finalize_login()` outside the integration —
  the single most damaging mistake here.
- Restarting Home Assistant after clobbering the cookie file, instead of reloading
  the config entry first. The restart is what makes it unrecoverable.
- Deleting straight from a filter expression with no reviewable id list.
- Trusting the HTTP status of a delete instead of re-listing.
- Hard-coding `alexa.amazon.com` in a tool used outside the US marketplace.
- Logging or printing cookie values while debugging.
