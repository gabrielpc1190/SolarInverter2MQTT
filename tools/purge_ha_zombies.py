#!/usr/bin/env python3
"""Purge zombie entities from the Home Assistant entity_registry via WS.

After clearing retained MQTT discovery payloads on the broker (see
`cleanup_orphan_discoveries.py`), Home Assistant may still keep records of
those entities in its persisted entity_registry. They show up in the UI as
`unavailable` and never go away on their own — the registry is what
"defines" an entity, not the discovery message.

This tool talks to HA's WebSocket API at `wss://<host>/api/websocket`,
authenticates with a long-lived access token, lists the entity_registry,
cross-references each entry with `/api/states` (REST) to learn the current
state and `last_updated`, and removes entries that:

  * match one of the `--domain` entity_id prefixes (default: the
    inverter-bridge / solar_assistant entity families — override with
    `--domain` to match whatever your device slug became in HA), AND
  * have a current state of `unavailable` or `unknown`, AND
  * have a `last_updated` older than `--unavailable-min-age-s` seconds.

Default behaviour is `--dry-run`. `--apply` actually removes; an
interactive `Are you sure? [y/N]` confirmation guards the apply step
unless `--yes` is also passed.

Sample invocations:

    python tools/purge_ha_zombies.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --dry-run

    python tools/purge_ha_zombies.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --apply --yes
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger("purge_ha_zombies")


# --- Defaults ------------------------------------------------------------

# NOTE: these defaults assume your inverter device in HA generated entities with
# `gadi_inverters_` (the slug taken from the device name). Override via `--domain`
# if your installation used a different device name (e.g. `solar_assistant_`).
DEFAULT_DOMAINS: tuple[str, ...] = (
    "sensor.solar_assistant_",
    "binary_sensor.solar_assistant_",
    "binary_sensor.inverter_bridge_",
)

DEFAULT_MIN_AGE_S = 3600
DEFAULT_WS_TIMEOUT_S = 10.0
DEFAULT_REST_TIMEOUT_S = 15.0

STALE_STATES: frozenset[str] = frozenset({"unavailable", "unknown"})

# Cloudflare gates the HA host on the public domain; default browser-style
# UA strings get challenged. `curl/8.0` is allow-listed for us.
USER_AGENT = "curl/8.0"


# --- Data classes --------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class StateRecord:
    """Subset of `/api/states` entry that we care about."""
    entity_id: str
    state: str
    last_updated: datetime | None  # None if HA returned no/invalid timestamp


@dataclasses.dataclass(frozen=True)
class RegistryEntry:
    """Subset of `config/entity_registry/list` entry that we care about."""
    entity_id: str
    unique_id: str
    platform: str
    # We keep the raw dict around so the caller can dump it if useful.
    raw: dict[str, Any]


# --- Time parsing --------------------------------------------------------

def _parse_iso8601(s: str | None) -> datetime | None:
    """Parse HA's `last_updated` ISO 8601 string into an aware datetime.

    HA emits e.g. `2025-05-19T19:30:45.123456+00:00`. `datetime.fromisoformat`
    handles that on Python 3.11+. Returns None if `s` is empty or unparseable.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # HA always sends timezone-aware, but guard against drift.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Candidate selection (pure, tested) ----------------------------------

def select_candidates(
    registry: Sequence[RegistryEntry],
    states: Sequence[StateRecord],
    *,
    domain_prefixes: Sequence[str],
    min_age_s: float,
    include_stale_values: bool = False,
    canonical_unique_ids: frozenset[str] | None = None,
    now: datetime | None = None,
) -> list[RegistryEntry]:
    """Return registry entries that should be removed.

    Selection rule (verbatim, applied per-entry):

      An entity is selected iff ALL of:
        (a) its `entity_id` starts with at least one of `domain_prefixes`;
        (b) Either:
              (b.1) `/api/states` reports a current state of `unavailable`
                    or `unknown`, OR the entity is MISSING from states
                    entirely (HA's default for fully-dropped entities);
              (b.2) If `include_stale_values=True`: the entity has ANY
                    state value but `(now - last_updated) >= min_age_s`
                    AND its `unique_id` is NOT in `canonical_unique_ids`.
                    This catches orphan duplicates with stuck values from
                    earlier discovery iterations.
        (c) For (b.1): if `last_updated` is available, the age check
            must pass. If missing, assume stale.

    `canonical_unique_ids` is the safety net: entries with unique_ids
    matching the spec are NEVER deleted regardless of staleness.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    state_by_id: dict[str, StateRecord] = {s.entity_id: s for s in states}
    prefixes = tuple(domain_prefixes)
    canonical = canonical_unique_ids or frozenset()
    out: list[RegistryEntry] = []
    for entry in registry:
        if not entry.entity_id.startswith(prefixes):
            continue
        # Safety: never delete an entity whose unique_id is canonical
        if entry.unique_id and entry.unique_id in canonical:
            continue
        st = state_by_id.get(entry.entity_id)
        if st is None:
            # Entity in registry but not in /api/states: zombie.
            out.append(entry)
            continue
        if st.state in STALE_STATES:
            # Original rule (b.1): unavailable/unknown + age check
            if st.last_updated is None:
                out.append(entry)
                continue
            age_s = (now - st.last_updated).total_seconds()
            if age_s >= min_age_s:
                out.append(entry)
            continue
        if include_stale_values and st.last_updated is not None:
            # Rule (b.2): valid value but stale last_updated AND
            # unique_id not canonical AND not in protected list above.
            age_s = (now - st.last_updated).total_seconds()
            if age_s >= min_age_s:
                out.append(entry)
    return out


# --- REST: /api/states ---------------------------------------------------

def fetch_states(
    ha_base: str,
    ha_token: str,
    *,
    timeout_s: float = DEFAULT_REST_TIMEOUT_S,
) -> list[StateRecord]:
    """GET <ha_base>/api/states and parse into `StateRecord` list.

    Adds the `curl/8.0` UA to placate the Cloudflare host-gate. Uses only
    stdlib `urllib.request` — no `requests`/`httpx` dependency.
    """
    url = ha_base.rstrip("/") + "/api/states"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    log.info("fetching %s ...", url)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {url} failed: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GET {url} failed: {e.reason}") from e

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"GET {url} returned non-JSON body") from e
    if not isinstance(data, list):
        raise RuntimeError(f"GET {url} expected list, got {type(data).__name__}")

    out: list[StateRecord] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        state = item.get("state")
        if not isinstance(state, str):
            state = ""
        last_updated = _parse_iso8601(item.get("last_updated"))
        out.append(StateRecord(
            entity_id=entity_id,
            state=state,
            last_updated=last_updated,
        ))
    log.info("/api/states returned %d entities", len(out))
    return out


# --- WebSocket -----------------------------------------------------------

def ha_base_to_ws_url(ha_base: str) -> str:
    """Translate `https://host[:port][/path]` to `wss://host[:port]/api/websocket`."""
    parsed = urllib.parse.urlparse(ha_base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"--ha-base must use http(s); got {parsed.scheme!r}")
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path  # cope with bare `host`
    base_path = (parsed.path if parsed.netloc else "").rstrip("/")
    return f"{ws_scheme}://{netloc}{base_path}/api/websocket"


async def _recv_json(ws: ClientConnection, timeout_s: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise RuntimeError(f"unexpected non-object WS frame: {msg!r}")
    return msg


async def _send_json(ws: ClientConnection, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload))


async def _auth(ws: ClientConnection, ha_token: str, *, timeout_s: float) -> None:
    """Drive the `auth_required` -> `auth_ok` handshake."""
    first = await _recv_json(ws, timeout_s)
    if first.get("type") != "auth_required":
        raise RuntimeError(f"expected auth_required, got {first!r}")
    await _send_json(ws, {"type": "auth", "access_token": ha_token})
    reply = await _recv_json(ws, timeout_s)
    rtype = reply.get("type")
    if rtype == "auth_ok":
        log.info("WS auth OK (HA version %s)", reply.get("ha_version"))
        return
    if rtype == "auth_invalid":
        raise RuntimeError(f"WS auth invalid: {reply.get('message')!r}")
    raise RuntimeError(f"unexpected auth reply: {reply!r}")


async def _request(
    ws: ClientConnection,
    msg_id: int,
    payload: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Send a command with `id=msg_id`, wait for the matching `result` frame."""
    out = {"id": msg_id, **payload}
    await _send_json(ws, out)
    # HA may interleave events on this socket once we subscribe to them.
    # We are not subscribing, so the next frame with our id should be the
    # result; but be defensive.
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = max(0.0, deadline - asyncio.get_event_loop().time())
        if remaining <= 0:
            raise TimeoutError(f"WS command id={msg_id} timed out after {timeout_s}s")
        msg = await _recv_json(ws, remaining)
        if msg.get("id") == msg_id and msg.get("type") == "result":
            return msg
        log.debug("ignoring out-of-band WS frame: %r", msg)


def _make_ssl_context() -> ssl.SSLContext:
    """Default SSL context for the WS connection."""
    ctx = ssl.create_default_context()
    return ctx


class WSClient:
    """Async context-managed wrapper around an authenticated HA WS."""

    def __init__(
        self,
        ha_base: str,
        ha_token: str,
        *,
        timeout_s: float = DEFAULT_WS_TIMEOUT_S,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._ws_url = ha_base_to_ws_url(ha_base)
        self._token = ha_token
        self._timeout_s = timeout_s
        self._ssl = ssl_context
        self._ws: ClientConnection | None = None
        self._next_id = 1

    async def __aenter__(self) -> "WSClient":
        log.info("connecting to %s ...", self._ws_url)
        # Some HA installs sit behind Cloudflare; pass our curl-style UA in
        # the upgrade headers just in case the host gate requires it.
        is_wss = self._ws_url.startswith("wss://")
        ssl_kwargs: dict[str, Any] = {}
        if is_wss:
            ssl_kwargs["ssl"] = self._ssl or _make_ssl_context()
        self._ws = await asyncio.wait_for(
            websockets.connect(  # type: ignore[arg-type]
                self._ws_url,
                additional_headers=[("User-Agent", USER_AGENT)],
                max_size=64 * 1024 * 1024,  # registry list can be big
                **ssl_kwargs,
            ),
            timeout=self._timeout_s,
        )
        try:
            await _auth(self._ws, self._token, timeout_s=self._timeout_s)
        except Exception:
            await self._ws.close()
            self._ws = None
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    def _next(self) -> int:
        n = self._next_id
        self._next_id += 1
        return n

    async def list_entity_registry(self) -> list[RegistryEntry]:
        """Send `config/entity_registry/list`, parse the result."""
        assert self._ws is not None, "WSClient not connected"
        msg_id = self._next()
        log.info("requesting entity_registry/list (id=%d) ...", msg_id)
        reply = await _request(
            self._ws,
            msg_id,
            {"type": "config/entity_registry/list"},
            timeout_s=self._timeout_s,
        )
        if not reply.get("success"):
            raise RuntimeError(
                f"entity_registry/list failed: {reply.get('error')!r}"
            )
        result = reply.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError(
                f"entity_registry/list returned non-list: {type(result).__name__}"
            )
        entries: list[RegistryEntry] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            entries.append(RegistryEntry(
                entity_id=entity_id,
                unique_id=str(item.get("unique_id") or ""),
                platform=str(item.get("platform") or ""),
                raw=item,
            ))
        log.info("entity_registry has %d entries", len(entries))
        return entries

    async def remove_entity(self, entity_id: str) -> tuple[bool, str | None]:
        """Send `config/entity_registry/remove`. Return (ok, error_msg)."""
        assert self._ws is not None, "WSClient not connected"
        msg_id = self._next()
        try:
            reply = await _request(
                self._ws,
                msg_id,
                {
                    "type": "config/entity_registry/remove",
                    "entity_id": entity_id,
                },
                timeout_s=self._timeout_s,
            )
        except TimeoutError as e:
            return False, str(e)
        if reply.get("success"):
            return True, None
        err = reply.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, str(msg) if msg else "unknown error"


# --- Reporting -----------------------------------------------------------

def _domain_of(entity_id: str) -> str:
    """Return the HA domain (before the first dot) of an entity_id."""
    head, _, _ = entity_id.partition(".")
    return head or "<no-domain>"


def print_report(
    registry: Iterable[RegistryEntry],
    candidates: Sequence[RegistryEntry],
    *,
    domain_prefixes: Sequence[str],
) -> None:
    """Print summary + per-domain breakdown of candidates."""
    registry_list = list(registry)
    matching_prefix = [
        e for e in registry_list
        if e.entity_id.startswith(tuple(domain_prefixes))
    ]
    by_domain: Counter[str] = Counter(_domain_of(e.entity_id) for e in candidates)
    by_platform: Counter[str] = Counter(e.platform for e in candidates)

    print("=== HA entity_registry zombie purge report ===")
    print(f"Registry entries (total):     {len(registry_list)}")
    print(f"Matching --domain prefixes:   {len(matching_prefix)}")
    print(f"Candidates for removal:       {len(candidates)}")
    if by_domain:
        print("Candidate breakdown by domain:")
        for dom, n in sorted(by_domain.items()):
            print(f"  {dom:30s}  {n}")
    if by_platform:
        print("Candidate breakdown by platform:")
        for plat, n in sorted(by_platform.items()):
            print(f"  {plat:30s}  {n}")
    print()


def print_candidates(candidates: Sequence[RegistryEntry]) -> None:
    if not candidates:
        print("(no candidates)")
        return
    print("Candidates (entity_id  |  unique_id  |  platform):")
    for c in sorted(candidates, key=lambda x: x.entity_id):
        print(f"  {c.entity_id:55s}  {c.unique_id:55s}  {c.platform}")


# --- Main ----------------------------------------------------------------

def _prompt_confirm(n: int) -> bool:
    """Interactive Y/N. Defaults to N on empty input or non-TTY."""
    try:
        answer = input(f"Remove {n} entit{'y' if n == 1 else 'ies'}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


async def _apply(
    client: WSClient,
    candidates: Sequence[RegistryEntry],
) -> tuple[int, list[tuple[str, str]]]:
    """Remove each candidate; return (n_ok, failures)."""
    n_ok = 0
    failures: list[tuple[str, str]] = []
    for c in candidates:
        log.info("removing %s (unique_id=%s, platform=%s) ...",
                 c.entity_id, c.unique_id, c.platform)
        ok, err = await client.remove_entity(c.entity_id)
        if ok:
            n_ok += 1
            log.info("  -> ok")
        else:
            log.warning("  -> FAILED: %s", err)
            failures.append((c.entity_id, err or "?"))
    return n_ok, failures


async def _run_async(args: argparse.Namespace) -> int:
    # Step 1: fetch states (sync REST, run in default executor to keep
    # the event loop responsive — but in this one-shot tool it's fine
    # to call it synchronously before opening the WS).
    try:
        states = fetch_states(
            args.ha_base, args.ha_token, timeout_s=args.rest_timeout_s,
        )
    except RuntimeError as e:
        log.error("REST /api/states failed: %s", e)
        return 3

    # Step 2-4: connect, auth, list registry.
    try:
        async with WSClient(
            args.ha_base, args.ha_token, timeout_s=args.ws_timeout_s,
        ) as client:
            try:
                registry = await client.list_entity_registry()
            except (RuntimeError, TimeoutError) as e:
                log.error("entity_registry/list failed: %s", e)
                return 4

            # Optional: load canonical spec for safety net
            canonical_uids: frozenset[str] | None = None
            if args.canonical_spec:
                try:
                    with open(args.canonical_spec) as fh:
                        spec = json.load(fh)
                    uids: set[str] = set()
                    uids.update(spec.get("aggregates", []))
                    uids.update(spec.get("meta", []))
                    uids.update(spec.get("binary_sensors", []))
                    for n in (1, 2):  # canonical inverter count
                        for suf in spec.get("per_inverter_suffixes", []):
                            uids.add(f"inverter_{n}_{suf}")
                    canonical_uids = frozenset(uids)
                    log.info("loaded %d canonical unique_ids (safety net)", len(canonical_uids))
                except (OSError, json.JSONDecodeError) as e:
                    log.warning("canonical spec load failed: %s — proceeding WITHOUT safety net", e)

            # Step 5: cross-reference.
            candidates = select_candidates(
                registry, states,
                domain_prefixes=args.domain,
                min_age_s=args.unavailable_min_age_s,
                include_stale_values=args.include_stale_values,
                canonical_unique_ids=canonical_uids,
            )

            # Step 6: report.
            print_report(registry, candidates, domain_prefixes=args.domain)

            if args.action != "apply":
                # Dry-run: list and exit.
                print_candidates(candidates)
                print()
                print(
                    f"[DRY-RUN] would remove {len(candidates)} registry entries. "
                    "Pass --apply to actually remove."
                )
                return 0

            # Step 8: apply path.
            if not candidates:
                print("[APPLY] nothing to remove.")
                return 0

            print_candidates(candidates)
            print()
            if not args.yes:
                if not _prompt_confirm(len(candidates)):
                    print("Aborted by user.")
                    return 0

            n_ok, failures = await _apply(client, candidates)
            print()
            print(
                f"[APPLY] removed {n_ok}/{len(candidates)} registry entries."
            )
            if failures:
                print(f"Failures ({len(failures)}):")
                for eid, err in failures:
                    print(f"  {eid}: {err}")
                return 6
            return 0
    except OSError as e:
        log.error("WS connect failed: %s", e)
        return 2
    except websockets.exceptions.WebSocketException as e:  # type: ignore[attr-defined]
        log.error("WebSocket error: %s", e)
        return 2
    except RuntimeError as e:
        log.error("%s", e)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove zombie unavailable/unknown entities from the HA entity_registry."
    )
    parser.add_argument("--ha-base", required=True,
                        help="HA base URL, e.g. https://ha.example.com")
    parser.add_argument("--ha-token", required=True,
                        help="HA long-lived access token")
    parser.add_argument(
        "--unavailable-min-age-s",
        type=float, default=DEFAULT_MIN_AGE_S,
        help=f"Minimum age in seconds for an unavailable/unknown entity "
             f"to be considered a zombie (default {DEFAULT_MIN_AGE_S})",
    )
    parser.add_argument(
        "--domain",
        nargs="+",
        default=list(DEFAULT_DOMAINS),
        help="Entity_id prefixes to consider. "
             f"Default: {' '.join(DEFAULT_DOMAINS)}",
    )
    parser.add_argument(
        "--ws-timeout-s", type=float, default=DEFAULT_WS_TIMEOUT_S,
        help=f"Per-command WS timeout in seconds (default {DEFAULT_WS_TIMEOUT_S})",
    )
    parser.add_argument(
        "--rest-timeout-s", type=float, default=DEFAULT_REST_TIMEOUT_S,
        help=f"REST /api/states timeout in seconds (default {DEFAULT_REST_TIMEOUT_S})",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt in --apply mode",
    )
    parser.add_argument(
        "--include-stale-values",
        action="store_true",
        help="Also select entities with VALID state values whose last_updated "
             "exceeds --unavailable-min-age-s. Use with --canonical-spec for safety: "
             "canonical unique_ids are NEVER deleted regardless.",
    )
    parser.add_argument(
        "--canonical-spec",
        type=str, default=None,
        help="Path to MQTT_CANONICAL.json. Entities with unique_id in this spec "
             "are protected from deletion. STRONGLY recommended when using "
             "--include-stale-values.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run", dest="action", action="store_const", const="dry-run",
        help="Default. List candidates only; never removes.",
    )
    action.add_argument(
        "--apply", dest="action", action="store_const", const="apply",
        help="Actually remove the candidate entities.",
    )
    parser.set_defaults(action="dry-run")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    if not args.domain:
        log.error("--domain must list at least one entity_id prefix")
        return 1

    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
