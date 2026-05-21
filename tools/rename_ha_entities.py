#!/usr/bin/env python3
"""Rename Home Assistant entities via the WebSocket API.

A one-off tool for audit finding F-7: inv2 has entities whose entity_id
got a `_3` suffix appended by HA (because the clean name was already
taken at registration time), but whose `unique_id` is the canonical
one. The clean entity_id is now free, so we can rename the orphans to
match inv1's naming.

The tool is general — you can also pass arbitrary `old=new` pairs or a
JSON mapping file. Either way the validation rules are the same:

  * the OLD entity_id must exist in the registry,
  * the NEW entity_id must NOT be taken by another entity,
  * OLD and NEW must share the same HA domain (the part before the
    first `.`),
  * OLD != NEW.

The `--audit-f7` flag auto-builds the mapping for the F-7 fix:
scan the registry for `sensor.gadi_inverters_inverter_<n>_*_3`
where the unique_id is the canonical `inverter_<n>_<base>`, and
propose a rename that strips the trailing `_3` — but only if the
target entity_id is not already owned by some OTHER entity.

Default behaviour is `--dry-run`. `--apply` actually renames; an
interactive `Are you sure? [y/N]` confirmation guards the apply step
unless `--yes` is also passed.

Sample invocations:

    # Dry-run an explicit pair
    python tools/rename_ha_entities.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --pairs "sensor.foo_x_3=sensor.foo_x"

    # Dry-run a mapping file
    python tools/rename_ha_entities.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --mapping renames.json

    # Audit F-7 dry-run
    python tools/rename_ha_entities.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --audit-f7

    # Audit F-7 apply
    python tools/rename_ha_entities.py \\
        --ha-base https://homeassistant.example.com \\
        --ha-token "$HA_TOKEN" \\
        --audit-f7 --apply --yes
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import re
import ssl
import sys
import urllib.parse
from collections.abc import Iterable, Sequence
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger("rename_ha_entities")


# --- Defaults ------------------------------------------------------------

DEFAULT_WS_TIMEOUT_S = 10.0

# Cloudflare gates the HA host on the public domain; default browser-style
# UA strings get challenged. `curl/8.0` is allow-listed for us.
USER_AGENT = "curl/8.0"

# The audit-F7 rename targets inv1 and inv2 entities under the
# inverter-bridge family.
AUDIT_F7_PREFIXES: tuple[str, ...] = (
    "sensor.gadi_inverters_inverter_1_",
    "sensor.gadi_inverters_inverter_2_",
)

# Matches a `sensor.gadi_inverters_inverter_<n>_<base>_3` entity_id.
# The trailing `_3` is the HA-appended suffix we want to strip.
_AUDIT_F7_ENTITY_RE = re.compile(
    r"^sensor\.gadi_inverters_inverter_(?P<inv>[12])_(?P<base>.+)_3$"
)


# --- Data classes --------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RegistryEntry:
    """Subset of `config/entity_registry/list` entry that we care about."""
    entity_id: str
    unique_id: str
    platform: str
    raw: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class Rename:
    """A single old -> new rename request."""
    old: str
    new: str


@dataclasses.dataclass(frozen=True)
class PlanItem:
    """A validated rename plan entry."""
    old: str
    new: str
    status: str  # "ok" | "not-found" | "would-collide" | "domain-mismatch" | "noop"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --- Mapping construction ------------------------------------------------

def parse_pairs(pairs: Iterable[str]) -> list[Rename]:
    """Parse a list of `old=new` strings into Rename records.

    Raises ValueError if any pair is malformed (no `=`, empty side, etc.).
    """
    out: list[Rename] = []
    for raw in pairs:
        if "=" not in raw:
            raise ValueError(f"--pairs entry has no '=': {raw!r}")
        old, _, new = raw.partition("=")
        old = old.strip()
        new = new.strip()
        if not old or not new:
            raise ValueError(f"--pairs entry has empty side: {raw!r}")
        out.append(Rename(old=old, new=new))
    return out


def load_mapping_file(path: str) -> list[Rename]:
    """Load a JSON `{old: new, ...}` mapping file into Rename records."""
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(
            f"mapping file {path!r} must contain a JSON object, got {type(data).__name__}"
        )
    out: list[Rename] = []
    for old, new in data.items():
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(
                f"mapping file {path!r}: keys and values must be strings"
            )
        if not old or not new:
            raise ValueError(
                f"mapping file {path!r}: empty entity_id encountered"
            )
        out.append(Rename(old=old, new=new))
    return out


def build_audit_f7_renames(
    registry: Sequence[RegistryEntry],
    *,
    prefixes: Sequence[str] = AUDIT_F7_PREFIXES,
) -> list[Rename]:
    """Build the rename list for the F-7 audit fix.

    Scan the registry for entities matching the inv1/inv2 sensor prefixes
    with a trailing `_3` whose `unique_id` is the canonical
    `inverter_<n>_<base>`. Propose stripping the `_3` from the entity_id.

    Skip silently if:
      * the unique_id does not look canonical (unique_id mismatch),
      * the proposed new entity_id is already owned by some OTHER entry
        in the registry,
      * the entry's entity_id doesn't match the regex.
    """
    by_id: dict[str, RegistryEntry] = {e.entity_id: e for e in registry}
    out: list[Rename] = []
    prefix_tuple = tuple(prefixes)
    for entry in registry:
        if not entry.entity_id.startswith(prefix_tuple):
            continue
        m = _AUDIT_F7_ENTITY_RE.match(entry.entity_id)
        if not m:
            continue
        inv = m.group("inv")
        base = m.group("base")
        expected_uid = f"inverter_{inv}_{base}"
        if entry.unique_id != expected_uid:
            log.debug(
                "audit-f7 skip %s: unique_id=%r does not match canonical %r",
                entry.entity_id, entry.unique_id, expected_uid,
            )
            continue
        new_eid = f"sensor.gadi_inverters_inverter_{inv}_{base}"
        # Skip if the target name is already owned by some OTHER entry.
        other = by_id.get(new_eid)
        if other is not None and other.entity_id != entry.entity_id:
            log.debug(
                "audit-f7 skip %s -> %s: target already owned by %s (unique_id=%r)",
                entry.entity_id, new_eid, other.entity_id, other.unique_id,
            )
            continue
        out.append(Rename(old=entry.entity_id, new=new_eid))
    # Stable order: sort by old entity_id for reproducible report
    out.sort(key=lambda r: r.old)
    return out


# --- Validation (pure, tested) -------------------------------------------

def _domain_of(entity_id: str) -> str:
    """Return the HA domain (before the first dot) of an entity_id."""
    head, _, _ = entity_id.partition(".")
    return head or ""


def validate_renames(
    renames: Sequence[Rename],
    registry: Sequence[RegistryEntry],
) -> list[PlanItem]:
    """Validate each rename against the current registry.

    Rules (per rename):
      * old.entity_id must exist in the registry  -> "not-found" if not
      * new.entity_id must NOT be taken by some OTHER entry -> "would-collide"
      * domain prefix of old == domain prefix of new -> "domain-mismatch"
      * old == new -> "noop"

    Returns a PlanItem per input rename, with `status="ok"` for the
    valid ones.
    """
    by_id: dict[str, RegistryEntry] = {e.entity_id: e for e in registry}
    out: list[PlanItem] = []
    for rn in renames:
        if rn.old == rn.new:
            out.append(PlanItem(
                old=rn.old, new=rn.new, status="noop",
                detail="old == new",
            ))
            continue
        dom_old = _domain_of(rn.old)
        dom_new = _domain_of(rn.new)
        if not dom_old or not dom_new or dom_old != dom_new:
            out.append(PlanItem(
                old=rn.old, new=rn.new, status="domain-mismatch",
                detail=f"{dom_old!r} != {dom_new!r}",
            ))
            continue
        old_entry = by_id.get(rn.old)
        if old_entry is None:
            out.append(PlanItem(
                old=rn.old, new=rn.new, status="not-found",
                detail="old entity_id not in registry",
            ))
            continue
        other = by_id.get(rn.new)
        if other is not None and other.entity_id != rn.old:
            out.append(PlanItem(
                old=rn.old, new=rn.new, status="would-collide",
                detail=f"target owned by another entity (unique_id={other.unique_id!r})",
            ))
            continue
        out.append(PlanItem(old=rn.old, new=rn.new, status="ok"))
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
    return ssl.create_default_context()


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
        is_wss = self._ws_url.startswith("wss://")
        ssl_kwargs: dict[str, Any] = {}
        if is_wss:
            ssl_kwargs["ssl"] = self._ssl or _make_ssl_context()
        self._ws = await asyncio.wait_for(
            websockets.connect(  # type: ignore[arg-type]
                self._ws_url,
                additional_headers=[("User-Agent", USER_AGENT)],
                max_size=64 * 1024 * 1024,
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

    async def update_entity_id(
        self, old: str, new: str,
    ) -> tuple[bool, str | None]:
        """Send `config/entity_registry/update` to rename old -> new.

        Returns (ok, error_msg). On failure error_msg is set.
        """
        assert self._ws is not None, "WSClient not connected"
        msg_id = self._next()
        try:
            reply = await _request(
                self._ws,
                msg_id,
                {
                    "type": "config/entity_registry/update",
                    "entity_id": old,
                    "new_entity_id": new,
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

def print_plan(plan: Sequence[PlanItem]) -> None:
    """Print the planned renames + validation outcomes."""
    if not plan:
        print("(no renames to plan)")
        return
    ok = [p for p in plan if p.status == "ok"]
    bad = [p for p in plan if p.status != "ok"]
    print(f"Plan: {len(ok)} OK, {len(bad)} blocked (out of {len(plan)} requested)")
    print()
    print(f"{'status':16s}  {'old':55s}  ->  {'new':55s}  detail")
    for p in plan:
        print(
            f"{p.status:16s}  {p.old:55s}  ->  {p.new:55s}  {p.detail}"
        )
    print()


def _prompt_confirm(n: int) -> bool:
    """Interactive Y/N. Defaults to N on empty input or non-TTY."""
    try:
        answer = input(
            f"Rename {n} entit{'y' if n == 1 else 'ies'}? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# --- Apply loop ----------------------------------------------------------

async def _apply(
    client: WSClient,
    plan_ok: Sequence[PlanItem],
) -> tuple[int, list[tuple[str, str, str]]]:
    """Apply each ok plan item. Returns (n_ok, failures)."""
    n_ok = 0
    failures: list[tuple[str, str, str]] = []
    for p in plan_ok:
        log.info("renaming %s -> %s ...", p.old, p.new)
        ok, err = await client.update_entity_id(p.old, p.new)
        if ok:
            n_ok += 1
            log.info("  -> ok")
        else:
            log.warning("  -> FAILED: %s", err)
            failures.append((p.old, p.new, err or "?"))
    return n_ok, failures


# --- Main ----------------------------------------------------------------

def _build_renames_from_args(
    args: argparse.Namespace,
    registry: Sequence[RegistryEntry] | None = None,
) -> list[Rename]:
    """Build the rename list according to which CLI source the user picked.

    `registry` is required only when --audit-f7 is selected.
    """
    if args.audit_f7:
        if registry is None:
            raise RuntimeError("--audit-f7 requires the registry to be loaded")
        return build_audit_f7_renames(registry)
    if args.mapping:
        return load_mapping_file(args.mapping)
    if args.pairs:
        return parse_pairs(args.pairs)
    return []


async def _run_async(args: argparse.Namespace) -> int:
    try:
        async with WSClient(
            args.ha_base, args.ha_token, timeout_s=args.ws_timeout_s,
        ) as client:
            try:
                registry = await client.list_entity_registry()
            except (RuntimeError, TimeoutError) as e:
                log.error("entity_registry/list failed: %s", e)
                return 4

            try:
                renames = _build_renames_from_args(args, registry=registry)
            except (ValueError, OSError, json.JSONDecodeError) as e:
                log.error("rename mapping load failed: %s", e)
                return 1

            if not renames:
                print("(no renames requested)")
                return 0

            plan = validate_renames(renames, registry)
            print_plan(plan)

            plan_ok = [p for p in plan if p.status == "ok"]
            plan_bad = [p for p in plan if p.status != "ok"]

            if args.action != "apply":
                print(
                    f"[DRY-RUN] would rename {len(plan_ok)} entit"
                    f"{'y' if len(plan_ok) == 1 else 'ies'}. "
                    f"({len(plan_bad)} blocked) — pass --apply to actually rename."
                )
                return 5 if plan_bad and args.fail_on_blocked else 0

            if not plan_ok:
                print("[APPLY] nothing to rename.")
                return 5 if plan_bad and args.fail_on_blocked else 0

            if not args.yes:
                if not _prompt_confirm(len(plan_ok)):
                    print("Aborted by user.")
                    return 0

            n_ok, failures = await _apply(client, plan_ok)
            print()
            print(
                f"[APPLY] renamed {n_ok}/{len(plan_ok)} entities."
            )
            if failures:
                print(f"Failures ({len(failures)}):")
                for old, new, err in failures:
                    print(f"  {old} -> {new}: {err}")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rename HA entities via config/entity_registry/update over WS.",
    )
    parser.add_argument("--ha-base", required=True,
                        help="HA base URL, e.g. https://ha.example.com")
    parser.add_argument("--ha-token", required=True,
                        help="HA long-lived access token")

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--mapping", type=str, default=None,
        help="Path to a JSON file containing {old_entity_id: new_entity_id, ...}.",
    )
    src.add_argument(
        "--pairs", nargs="+", default=None,
        help="One or more 'old=new' rename pairs.",
    )
    src.add_argument(
        "--audit-f7", action="store_true",
        help="Auto-build the rename list for the F-7 audit fix.",
    )

    parser.add_argument(
        "--ws-timeout-s", type=float, default=DEFAULT_WS_TIMEOUT_S,
        help=f"Per-command WS timeout in seconds (default {DEFAULT_WS_TIMEOUT_S})",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt in --apply mode",
    )
    parser.add_argument(
        "--fail-on-blocked", action="store_true",
        help="Exit non-zero if any planned rename was blocked by validation.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run", dest="action", action="store_const", const="dry-run",
        help="Default. Show the plan only; never renames.",
    )
    action.add_argument(
        "--apply", dest="action", action="store_const", const="apply",
        help="Actually rename the entities.",
    )
    parser.set_defaults(action="dry-run")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    # Must specify exactly one source of renames
    sources = sum(bool(x) for x in (args.mapping, args.pairs, args.audit_f7))
    if sources == 0:
        log.error("must specify one of --mapping, --pairs, or --audit-f7")
        return 1
    if sources > 1:
        log.error("--mapping, --pairs and --audit-f7 are mutually exclusive")
        return 1

    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        return 130


# Public alias so tests can import the validation helpers easily.
__all__ = [
    "RegistryEntry",
    "Rename",
    "PlanItem",
    "AUDIT_F7_PREFIXES",
    "parse_pairs",
    "load_mapping_file",
    "build_audit_f7_renames",
    "validate_renames",
    "ha_base_to_ws_url",
    "WSClient",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
