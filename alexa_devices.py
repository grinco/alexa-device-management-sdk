#!/usr/bin/env python3
"""List / probe / delete smart-home devices in your Alexa account.

Reuses the session cookies that Home Assistant's `alexa_media` integration
already maintains, so there is no second login and no stored password.

SAFETY CONTRACT — read before editing:

  1. The cookie file is opened READ-ONLY and is never written, moved or
     truncated. Do not "helpfully" add a refresh/save path. Constructing an
     `alexapy.AlexaLogin` outside the integration and calling
     `finalize_login()` overwrites this file with an empty jar (observed:
     4549 -> 58 bytes) and logs the account out. If that ever happens, run
     `homeassistant.reload_config_entry` on the alexa_media entry immediately,
     BEFORE any restart: unload calls `close_connections()`, which writes the
     live valid jar back.
  2. `delete` is opt-in, capped, and prints what it will do first.
  3. Cookie values are never printed, logged or included in errors.

These are undocumented, reverse-engineered endpoints. They can change without
notice; `list` failing with a GraphQL schema error is the expected symptom.

Usage
  ./alexa_devices.py list
  ./alexa_devices.py list --json > devices.json
  ./alexa_devices.py state --id "<applianceId>"
  ./alexa_devices.py delete --from-file ids.txt --yes

Building a delete list (the intended bulk workflow):
  ./alexa_devices.py list --json \
    | jq -r '.[] | select(.manufacturerName=="Royal Philips Electronics") | .applianceId' > ids.txt
  ./alexa_devices.py delete --from-file ids.txt          # dry run, prints plan
  ./alexa_devices.py delete --from-file ids.txt --yes --max 2000
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_COOKIE_GLOB = "/usr/share/hassio/homeassistant/.storage/alexa_media.*.cookies"
DEFAULT_BASE = "https://alexa.amazon.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
QUERY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customer_smart_home.graphql")


class AlexaError(RuntimeError):
    pass


def load_session(cookie_path: str) -> tuple[str, str]:
    """Return (cookie_header, csrf_token). Opens the file read-only."""
    with open(cookie_path, "r", encoding="utf-8") as fh:  # read-only, always
        blob = json.load(fh)

    cookies = blob.get("cookies")
    if not cookies:
        raise AlexaError(
            f"{cookie_path} has no cookies. If it is ~58 bytes the jar was "
            "clobbered — reload the alexa_media config entry to restore it."
        )

    pairs, csrf = [], None
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        if "amazon." not in (c.get("domain") or ""):
            continue
        pairs.append(f"{name}={value}")
        if name == "csrf":
            csrf = value

    if not pairs:
        raise AlexaError("no amazon.* cookies found in the jar")
    if not csrf:
        raise AlexaError("no 'csrf' cookie found — the Alexa API rejects writes without it")
    return "; ".join(pairs), csrf


def api(args, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    cookie_header, csrf = args._session
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(args.base + path, data=data, method=method)
    req.add_header("Cookie", cookie_header)
    req.add_header("csrf", csrf)
    req.add_header("User-Agent", args.user_agent)
    req.add_header("Accept", "application/json")
    req.add_header("Referer", args.base + "/spa/index.html")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise AlexaError(f"{method} {path} failed: {exc.reason}") from exc

    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def fetch_devices(args) -> list[dict]:
    with open(args.query_file, "r", encoding="utf-8") as fh:
        query = fh.read()
    status, payload = api(args, "POST", "/nexus/v1/graphql",
                          {"query": query, "operationName": "CustomerSmartHome", "variables": {}})
    if status != 200 or not isinstance(payload, dict):
        raise AlexaError(f"graphql HTTP {status}: {str(payload)[:400]}")
    if payload.get("errors"):
        raise AlexaError(
            "graphql returned errors (Amazon may have changed the schema; "
            f"edit {os.path.basename(args.query_file)}): "
            f"{json.dumps(payload['errors'])[:600]}"
        )
    items = ((payload.get("data") or {}).get("endpoints") or {}).get("items") or []
    out = []
    for item in items:
        legacy = item.get("legacyAppliance") or {}
        out.append({
            "applianceId": legacy.get("applianceId"),
            "friendlyName": legacy.get("friendlyName") or item.get("friendlyName"),
            "manufacturerName": legacy.get("manufacturerName"),
            "modelName": legacy.get("modelName"),
            "applianceTypes": legacy.get("applianceTypes"),
            "connectedVia": legacy.get("connectedVia"),
            "entityId": legacy.get("entityId"),
            "endpointId": item.get("endpointId"),
        })
    return out


def cmd_list(args) -> int:
    devices = fetch_devices(args)
    if args.json:
        json.dump(devices, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    print(f"{len(devices)} device(s)\n")
    for d in sorted(devices, key=lambda x: ((x.get("manufacturerName") or ""), (x.get("friendlyName") or ""))):
        print(f"  {d.get('friendlyName') or '(unnamed)':<38} "
              f"{(d.get('manufacturerName') or '-'):<30} {d.get('applianceId')}")
    by_mfr: dict[str, int] = {}
    for d in devices:
        by_mfr[d.get("manufacturerName") or "-"] = by_mfr.get(d.get("manufacturerName") or "-", 0) + 1
    print("\nby manufacturer:")
    for mfr, n in sorted(by_mfr.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {mfr}")
    return 0


def cmd_state(args) -> int:
    status, payload = api(args, "POST", "/api/phoenix/state",
                          {"stateRequests": [{"entityId": i, "entityType": "APPLIANCE"} for i in args.id]})
    print(f"HTTP {status}")
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0 if status == 200 else 1


def cmd_delete(args) -> int:
    ids = list(args.id)
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            ids += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("nothing to delete", file=sys.stderr)
        return 2

    print(f"{len(ids)} device(s) selected for deletion; cap is --max {args.max}")
    for i in ids[:10]:
        print(f"  {i}")
    if len(ids) > 10:
        print(f"  ... and {len(ids) - 10} more")

    if len(ids) > args.max:
        print(f"\nREFUSED: {len(ids)} exceeds --max {args.max}. Raise it deliberately.", file=sys.stderr)
        return 2
    if not args.yes:
        print("\nDry run. Re-run with --yes to actually delete. This cannot be undone;\n"
              "the devices must be rediscovered in the Alexa app afterwards.")
        return 0

    ok = failed = 0
    for n, appliance_id in enumerate(ids, 1):
        path = "/api/phoenix/appliance/" + urllib.parse.quote(appliance_id, safe="")
        status, payload = api(args, "DELETE", path)
        if status == 200:
            ok += 1
        else:
            failed += 1
            print(f"  [{n}] HTTP {status} for {appliance_id}: {str(payload)[:160]}", file=sys.stderr)
        if args.sleep:
            time.sleep(args.sleep)
        if n % 50 == 0:
            print(f"  ... {n}/{len(ids)} ({ok} ok, {failed} failed)")
    print(f"\ndone: {ok} deleted, {failed} failed")
    print("Verify by re-running `list` — a 500 does not always mean the device survived.")
    return 0 if failed == 0 else 1


def resolve_cookie_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    matches = sorted(glob.glob(DEFAULT_COOKIE_GLOB))
    if not matches:
        raise AlexaError(f"no cookie file matched {DEFAULT_COOKIE_GLOB}; pass --cookies")
    if len(matches) > 1:
        raise AlexaError("several cookie files matched; pass --cookies to choose:\n  " + "\n  ".join(matches))
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cookies", help=f"alexa_media cookie file (default: glob {DEFAULT_COOKIE_GLOB})")
    p.add_argument("--base", default=DEFAULT_BASE, help="Alexa host for your marketplace (default: %(default)s)")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("--query-file", default=QUERY_FILE)
    p.add_argument("--timeout", type=float, default=30.0)

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="list smart-home devices (read-only)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("state", help="probe reachability of one or more appliance ids (read-only)")
    s.add_argument("--id", action="append", required=True)
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("delete", help="delete devices (dry run unless --yes)")
    s.add_argument("--id", action="append", default=[])
    s.add_argument("--from-file", help="file of applianceIds, one per line, # comments allowed")
    s.add_argument("--yes", action="store_true", help="actually delete")
    s.add_argument("--max", type=int, default=25, help="refuse if more than this many selected (default: %(default)s)")
    s.add_argument("--sleep", type=float, default=0.1, help="seconds between deletes (default: %(default)s)")
    s.set_defaults(func=cmd_delete)

    args = p.parse_args(argv)
    try:
        args._session = load_session(resolve_cookie_path(args.cookies))
        return args.func(args)
    except AlexaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
