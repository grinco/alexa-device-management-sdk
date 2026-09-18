# alexa-devices

A small, dependency-free CLI for the **smart-home device registry of your own Amazon
Alexa account** — list, probe reachability, and bulk-delete.

The Alexa app has no bulk operations. When a skill gets disabled and re-enabled, a
bridge is re-paired, or you move house, the account fills with orphaned devices that
can only be removed one tap at a time. This removes them in one pass.

It reuses the session that Home Assistant's [`alexa_media`](https://github.com/alandtse/alexa_media_player)
integration already maintains, so there is **no second login and no stored password**.

> These are undocumented, reverse-engineered endpoints. They can change without
> notice. A GraphQL schema error on `list` is expected maintenance — edit
> `customer_smart_home.graphql` and carry on.

## Requirements

- Python 3.9+ (standard library only)
- A working `alexa_media` config entry in Home Assistant, and read access to its
  cookie file: `<config>/.storage/alexa_media.<email>.cookies`

## Usage

```bash
./alexa_devices.py list                       # human-readable, with a count by manufacturer
./alexa_devices.py list --json > devices.json
./alexa_devices.py state --id "<applianceId>" # is it actually reachable?
```

Bulk cleanup — build an explicit, reviewable id list, verify on a sample, then run:

```bash
# 1. pick the orphans (adjust the filter to your own case)
./alexa_devices.py list --json \
  | jq -r '.[] | select(.manufacturerName=="Some Vendor") | .applianceId' > ids.txt

# 2. sanity-check a couple
./alexa_devices.py state --id "$(head -1 ids.txt)"

# 3. dry run — prints the plan, deletes nothing
./alexa_devices.py delete --from-file ids.txt

# 4. do it
./alexa_devices.py delete --from-file ids.txt --yes --max 2000

# 5. confirm
./alexa_devices.py list
```

Options worth knowing:

| Flag | Why |
|---|---|
| `--base https://alexa.amazon.de` | Non-US marketplaces (`.de`, `.co.uk`, `.co.jp`, …) |
| `--cookies PATH` | If the glob finds zero or several cookie files |
| `--max N` | `delete` refuses more than N ids (default **25**) so a bad filter can't run away |
| `--sleep S` | Pause between deletes (default 0.1 s) |
| `--query-file` | Point at your own GraphQL query if Amazon changes the schema |

## Safety

Deletion is **irreversible**. Deleted devices must be rediscovered, and any routines,
groups or custom names attached to them are gone.

- `delete` is a **dry run unless `--yes`**, prints its plan first, and is capped by `--max`.
- The cookie file is opened **read-only** and is never written, moved or truncated.
- Cookie values are never printed or logged.
- A `500` on a delete does not reliably mean the device survived — **re-list to confirm**.

### The one mistake that logs you out

Do **not** construct an `alexapy.AlexaLogin` outside the integration, and do not call
`finalize_login()` / `save_cookiefile()`. That writes an empty jar over the
integration's cookie file (observed: ~4.5 KB → 58 bytes) and logs the account out.

If it happens, run `homeassistant.reload_config_entry` on the `alexa_media` entry
**before any restart** — unload calls `close_connections()`, which writes the live
jar back. Restarting first loads the empty jar and the session is gone.

This is why the tool never opens that file for writing, and why you should not add a
"refresh the session" feature to it.

## Files

| | |
|---|---|
| `alexa_devices.py` | The CLI. Standard library only. |
| `customer_smart_home.graphql` | The listing query, kept separate so it can be fixed without touching code. |
| `SKILL.md` | The same knowledge as an agent-readable skill — endpoints, the cookie-clobbering trap, and the safe bulk-delete sequence. |

## Scope

This talks to **your own account, with your own session**, to manage **your own
registered devices**. It is an administrative convenience for data you already own,
not a way to reach anything you do not.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by Amazon. "Alexa" and "Amazon" are
trademarks of Amazon.com, Inc. or its affiliates.
