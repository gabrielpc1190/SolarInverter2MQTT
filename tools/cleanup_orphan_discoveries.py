#!/usr/bin/env python3
"""Clean up zombie/orphan MQTT discovery messages on the HA broker.

The inverter-bridge daemon went through several iterations of discovery
publishes during deploy; earlier iterations left retained payloads on the
broker for unique_ids that the current canonical state no longer uses
(e.g. `gadi_inverters_*` prefix from deploy v1; HA-side collision suffixes
like `_2`, `_2_2`, `_3` created when entities were re-registered under new
unique_ids without first clearing the old ones).

This standalone tool:

  1. Subscribes to `homeassistant/sensor/+/config` and
     `homeassistant/binary_sensor/+/config` for a short window, capturing
     every retained discovery payload published by anyone (us, SA, HA-MQTT
     internals, zigbee2mqtt, ESPHome, ...).
  2. Categorises each captured discovery against the canonical spec
     (loaded from a JSON file produced alongside this tool).
  3. Reports per-category counts (always — dry-run and apply alike).
  4. When `--apply` is given, clears the orphan retained payloads by
     publishing empty retained messages to the same config topics. HA
     will then deregister the orphaned entities.

Default behaviour is `--dry-run`; `--apply` must be passed explicitly.
Solar Assistant's historical retained discoveries for STATIC config
sensors that our new daemon does not publish (`inverter_1_serial_number`,
`battery_1_capacity`, ...) are categorised as `SA_STATIC` and preserved
by default. Pass `--include-sa-static` to also clean them.

Usage:
    python tools/cleanup_orphan_discoveries.py \\
        --host 192.0.2.10 --port 1883 \\
        --username mqtt --password mqtt \\
        --canonical-spec docs/MQTT_CANONICAL.json \\
        [--dry-run | --apply] [--include-sa-static]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

log = logging.getLogger("cleanup_orphan_discoveries")


# --- Category labels ------------------------------------------------------

CAT_CANONICAL = "CANONICAL"
CAT_ORPHAN_V1 = "ORPHAN_V1"
CAT_ORPHAN_COLLISION = "ORPHAN_COLLISION"
CAT_SA_STATIC = "SA_STATIC"
CAT_UNRELATED = "UNRELATED"

# HA-side collision suffix: `_2`, `_3`, `_2_2`, `_2_3`, etc. — one or more
# `_<digit>+` groups appended.
COLLISION_SUFFIX_RE = re.compile(r"(?:_\d+)+$")

# v1 deploy prefix marker — change this to whatever wrong unique_id prefix your
# earlier deploy iteration left behind. In our reference deploy, the first
# iteration used `gadi_inverters_*` unique_ids before switching to SA-compatible
# `total_*` / `inverter_N_*`; orphans from that iteration get categorized here.
ORPHAN_V1_PREFIX = "gadi_inverters_"

# Topic prefix the current daemon publishes under (SA convention)
SA_TOPIC_PREFIX = "solar_assistant"


@dataclasses.dataclass
class Discovery:
    """A captured retained discovery payload."""
    component: str           # "sensor" or "binary_sensor"
    topic: str               # full config topic, e.g. homeassistant/sensor/x/config
    object_id: str           # path segment between component and /config
    unique_id: str           # parsed from payload (or "" if missing/empty)
    state_topic: str         # parsed from payload (or "" if missing)
    device_identifiers: list[str]  # payload["device"]["identifiers"], normalised
    raw: dict[str, Any]      # the full parsed JSON payload


@dataclasses.dataclass
class CanonicalSpec:
    aggregates: set[str]
    per_inverter_unique_ids: set[str]
    meta: set[str]
    binary_sensors: set[str]

    @property
    def all_sensor_unique_ids(self) -> set[str]:
        return self.aggregates | self.per_inverter_unique_ids | self.meta

    def __contains__(self, uid: str) -> bool:
        return uid in self.all_sensor_unique_ids or uid in self.binary_sensors


# --- Canonical spec loading ----------------------------------------------

def load_canonical_spec(path: Path, *, n_inverters: int = 2) -> CanonicalSpec:
    """Load the canonical unique_id spec from a JSON file.

    Missing keys default to empty lists. A missing file logs a warning
    and returns an EMPTY spec (everything will then look orphaned, which
    is the safe failure mode — the user will see a huge ORPHAN count and
    abort instead of cleaning canonical entities).
    """
    if not path.exists():
        log.warning("canonical spec file %s does not exist; using empty spec", path)
        return CanonicalSpec(set(), set(), set(), set())

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        log.warning("canonical spec %s is not valid JSON: %s; using empty spec", path, e)
        return CanonicalSpec(set(), set(), set(), set())

    if not isinstance(data, dict):
        log.warning("canonical spec %s root must be an object; using empty spec", path)
        return CanonicalSpec(set(), set(), set(), set())

    aggregates = set(data.get("aggregates") or [])
    suffixes = data.get("per_inverter_suffixes") or []
    meta = set(data.get("meta") or [])
    binary_sensors = set(data.get("binary_sensors") or [])

    per_inverter: set[str] = set()
    for i in range(1, n_inverters + 1):
        for suffix in suffixes:
            per_inverter.add(f"inverter_{i}_{suffix}")

    log.info(
        "canonical spec loaded: %d aggregates, %d per-inverter (n=%d), %d meta, %d binary",
        len(aggregates), len(per_inverter), n_inverters, len(meta), len(binary_sensors),
    )
    return CanonicalSpec(
        aggregates=aggregates,
        per_inverter_unique_ids=per_inverter,
        meta=meta,
        binary_sensors=binary_sensors,
    )


# --- Categorization -------------------------------------------------------

def _strip_collision_suffix(uid: str) -> tuple[str, bool]:
    """Return `(base, had_suffix)`.

    `had_suffix` is True iff `uid` had a `_<digit>+` (possibly chained) suffix
    that was stripped. We only strip a suffix that we actually find; for a
    plain canonical uid, nothing is stripped.
    """
    m = COLLISION_SUFFIX_RE.search(uid)
    if not m:
        return uid, False
    return uid[:m.start()], True


def categorize(
    d: Discovery,
    spec: CanonicalSpec,
) -> str:
    """Classify a single discovery into one of CAT_*.

    Order is significant:
      1. CANONICAL — exact match in spec (most permissive: pass through).
      2. ORPHAN_V1 — unique_id starts with the v1 wrong prefix.
      3. ORPHAN_COLLISION — base (after stripping `_<digit>+...` tails)
         is canonical AND the original is not canonical.
      4. SA_STATIC — points at SA topics (solar_assistant/...) that we
         don't publish, and uses SA-style unique_ids.
      5. UNRELATED — anything else (other domains, foreign devices, ...).
    """
    uid = d.unique_id

    # (1) Canonical, including binary_sensor.
    if d.component == "binary_sensor":
        if uid in spec.binary_sensors:
            return CAT_CANONICAL
    else:
        if uid in spec.all_sensor_unique_ids:
            return CAT_CANONICAL

    # (2) v1 prefix from the first wrong deploy.
    if uid.startswith(ORPHAN_V1_PREFIX):
        return CAT_ORPHAN_V1

    # (3) HA-side collision artifacts. Strip the `_<digits>` tail and
    # check if the base is canonical. If yes, it's a duplicate that HA
    # created when a previous deploy re-registered the same entity under
    # a new unique_id (or the same one with a different signature).
    base, had_suffix = _strip_collision_suffix(uid)
    if had_suffix and base != uid:
        if d.component == "binary_sensor":
            if base in spec.binary_sensors:
                return CAT_ORPHAN_COLLISION
        else:
            if base in spec.all_sensor_unique_ids:
                return CAT_ORPHAN_COLLISION

    # (4) SA static (legacy SA discovery for sensors we don't republish).
    # Heuristic: state_topic begins with `solar_assistant/` AND
    # unique_id looks SA-style (total_, inverter_N_, battery_N_, _meta-ish).
    if _looks_sa_static(d, spec):
        return CAT_SA_STATIC

    # (5) Everything else — never touch.
    return CAT_UNRELATED


_SA_UNIQUE_ID_RE = re.compile(
    r"^(?:total_|inverter_\d+_|battery_\d+_|bms_\d+_|meta_)",
)


def _looks_sa_static(d: Discovery, spec: CanonicalSpec) -> bool:
    """Decide whether this is an SA-era static-config discovery.

    Criteria (all must hold):
      - state_topic begins with `solar_assistant/` (SA's domain), OR
        device.identifiers contains "sa_inverter".
      - unique_id matches SA's naming convention (`total_*`,
        `inverter_N_*`, `battery_N_*`, etc.).
      - unique_id is NOT one we publish (already handled by canonical).

    The third clause is implicit because `categorize` consults this AFTER
    the canonical check.
    """
    if not _SA_UNIQUE_ID_RE.match(d.unique_id):
        return False
    if d.state_topic.startswith(SA_TOPIC_PREFIX + "/"):
        return True
    if "sa_inverter" in d.device_identifiers:
        return True
    return False


# --- Payload parsing ------------------------------------------------------

def parse_discovery(component: str, topic: str, payload_bytes: bytes) -> Discovery | None:
    """Parse a discovery payload. Return None on empty / unparseable."""
    if not payload_bytes:
        # Empty payload = cleared retained; nothing to act on.
        return None
    try:
        text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError:
        log.debug("non-utf8 payload on %s", topic)
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # HA's MQTT discovery format is always JSON; non-JSON here is a
        # leftover from some other tool. Treat as unrelated by returning a
        # placeholder that categorize() will tag as UNRELATED.
        log.debug("non-JSON payload on %s", topic)
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    parts = topic.split("/")
    # Expected shape: homeassistant/<component>/<object_id>/config
    # or homeassistant/<component>/<node_id>/<object_id>/config
    if len(parts) >= 4 and parts[-1] == "config":
        object_id = parts[-2]
    else:
        object_id = ""

    device = raw.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    ids = device.get("identifiers")
    if isinstance(ids, str):
        identifiers = [ids]
    elif isinstance(ids, list):
        identifiers = [str(x) for x in ids]
    else:
        identifiers = []

    # HA Discovery permits `uniq_id` as a short form alias.
    uid = raw.get("unique_id") or raw.get("uniq_id") or ""
    state_topic = raw.get("state_topic") or raw.get("stat_t") or ""

    return Discovery(
        component=component,
        topic=topic,
        object_id=object_id,
        unique_id=str(uid),
        state_topic=str(state_topic),
        device_identifiers=identifiers,
        raw=raw,
    )


# --- MQTT capture ---------------------------------------------------------

def capture_retained(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    capture_seconds: float,
    connect_timeout_s: float = 10.0,
) -> list[Discovery]:
    """Subscribe to the two discovery topics and return retained payloads.

    The MQTT broker fires the retained payload immediately on subscribe.
    We then sit for `capture_seconds` to absorb any straggler retained
    messages (rare, but possible on busy brokers).

    All calls are bounded — never hangs.
    """
    captured: dict[str, tuple[str, bytes]] = {}  # topic -> (component, payload)
    lock = threading.Lock()
    connected_event = threading.Event()
    rc_holder: dict[str, int] = {"connect_rc": -1}

    def on_connect(client, _userdata, _flags, rc, _properties=None):
        # paho-mqtt v2 passes a ReasonCode; .value is the numeric code (0 = success).
        try:
            rc_int = rc.value  # type: ignore[union-attr]
        except AttributeError:
            rc_int = int(rc)
        rc_holder["connect_rc"] = rc_int
        if rc_int == 0:
            client.subscribe("homeassistant/sensor/+/config", qos=0)
            client.subscribe("homeassistant/binary_sensor/+/config", qos=0)
        connected_event.set()

    def on_message(_client, _userdata, msg):
        if not msg.retain:
            # Only care about retained payloads — fresh publishes are
            # things our daemon (or others) is doing right now.
            return
        parts = msg.topic.split("/")
        if len(parts) < 4:
            return
        component = parts[1]
        if component not in ("sensor", "binary_sensor"):
            return
        with lock:
            captured[msg.topic] = (component, msg.payload)

    client = mqtt.Client(
        client_id="inverter-bridge-cleanup",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_message = on_message

    log.info("connecting to %s:%d as %r...", host, port, username)
    try:
        client.connect(host, port, keepalive=30)
    except OSError as e:
        log.error("connect to %s:%d failed: %s", host, port, e)
        raise
    client.loop_start()
    try:
        if not connected_event.wait(timeout=connect_timeout_s):
            raise TimeoutError(
                f"MQTT connect did not complete within {connect_timeout_s}s"
            )
        if rc_holder["connect_rc"] != 0:
            raise RuntimeError(
                f"MQTT CONNACK rc={rc_holder['connect_rc']} (auth/host issue)"
            )
        log.info("subscribed; capturing retained discoveries for %.1fs...", capture_seconds)
        time.sleep(capture_seconds)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 -- best-effort
            pass

    discoveries: list[Discovery] = []
    for topic, (component, payload) in captured.items():
        d = parse_discovery(component, topic, payload)
        if d is None:
            continue
        discoveries.append(d)
    log.info("captured %d retained discoveries", len(discoveries))
    return discoveries


def clear_retained(
    host: str,
    port: int,
    username: str,
    password: str,
    topics: list[str],
    *,
    connect_timeout_s: float = 10.0,
    publish_timeout_s: float = 10.0,
) -> int:
    """Publish empty retained payload to each topic in `topics`.

    Returns the number of topics for which the publish was acknowledged
    by the local client library (paho's `wait_for_publish`). Bounded by
    `publish_timeout_s` PER batch (not per topic).
    """
    if not topics:
        return 0

    connected_event = threading.Event()
    rc_holder: dict[str, int] = {"connect_rc": -1}

    def on_connect(_client, _userdata, _flags, rc, _properties=None):
        try:
            rc_holder["connect_rc"] = rc.value  # type: ignore[union-attr]
        except AttributeError:
            rc_holder["connect_rc"] = int(rc)
        connected_event.set()

    client = mqtt.Client(
        client_id="inverter-bridge-cleanup-apply",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.username_pw_set(username, password)
    client.on_connect = on_connect

    log.info("connecting to %s:%d to clear %d retained topics...", host, port, len(topics))
    client.connect(host, port, keepalive=30)
    client.loop_start()
    try:
        if not connected_event.wait(timeout=connect_timeout_s):
            raise TimeoutError(
                f"MQTT connect did not complete within {connect_timeout_s}s"
            )
        if rc_holder["connect_rc"] != 0:
            raise RuntimeError(
                f"MQTT CONNACK rc={rc_holder['connect_rc']}"
            )

        infos = []
        for t in topics:
            info = client.publish(t, payload=b"", qos=1, retain=True)
            infos.append((t, info))

        deadline = time.monotonic() + publish_timeout_s
        acked = 0
        for t, info in infos:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                info.wait_for_publish(timeout=remaining)
            except (RuntimeError, ValueError):
                log.warning("publish to %s did not complete cleanly", t)
                continue
            if info.is_published():
                acked += 1
            else:
                log.warning("publish to %s not acknowledged within deadline", t)
        return acked
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 -- best-effort
            pass


# --- Reporting ------------------------------------------------------------

def build_report(
    discoveries: list[Discovery],
    spec: CanonicalSpec,
) -> dict[str, list[Discovery]]:
    """Categorize all discoveries and return a category -> list mapping."""
    buckets: dict[str, list[Discovery]] = {
        CAT_CANONICAL: [],
        CAT_ORPHAN_V1: [],
        CAT_ORPHAN_COLLISION: [],
        CAT_SA_STATIC: [],
        CAT_UNRELATED: [],
    }
    for d in discoveries:
        buckets[categorize(d, spec)].append(d)
    return buckets


def print_report(
    buckets: dict[str, list[Discovery]],
    *,
    include_sa_static: bool,
    apply: bool,
) -> None:
    """Print per-orphan detail and the final tally."""
    total = sum(len(v) for v in buckets.values())

    def _section(cat: str, header: str, will_clean: bool) -> None:
        items = buckets[cat]
        if not items:
            return
        action = "WILL CLEAN" if (apply and will_clean) else ("WOULD CLEAN" if will_clean else "PRESERVED")
        print(f"\n--- {cat} ({len(items)}) [{action}] {header} ---")
        # Sort for stable output
        for d in sorted(items, key=lambda x: (x.component, x.unique_id, x.topic)):
            print(
                f"  {d.component:13s}  uid={d.unique_id or '<missing>':50s}  "
                f"state_topic={d.state_topic or '<missing>'}"
            )
            if d.device_identifiers:
                print(f"      device.identifiers={d.device_identifiers}")
            print(f"      topic={d.topic}")

    print(f"\n=== Discovery cleanup report ===")
    print(f"Total config topics seen: {total}")
    print(f"  CANONICAL:        {len(buckets[CAT_CANONICAL])}")
    print(
        f"  ORPHAN_V1:        {len(buckets[CAT_ORPHAN_V1])}"
        f"  ({ORPHAN_V1_PREFIX}* unique_ids — will clean)"
    )
    print(
        f"  ORPHAN_COLLISION: {len(buckets[CAT_ORPHAN_COLLISION])}"
        f"  (canonical + extra suffix — will clean)"
    )
    sa_note = "will clean" if include_sa_static else "preserved, pass --include-sa-static to clean"
    print(f"  SA_STATIC:        {len(buckets[CAT_SA_STATIC])}  (legacy SA static configs — {sa_note})")
    print(f"  UNRELATED:        {len(buckets[CAT_UNRELATED])}  (other domains, never touched)")

    _section(CAT_ORPHAN_V1, "v1 deploy prefix", will_clean=True)
    _section(CAT_ORPHAN_COLLISION, "HA-side suffix collisions", will_clean=True)
    _section(CAT_SA_STATIC, "SA legacy static configs", will_clean=include_sa_static)
    _section(CAT_CANONICAL, "current canonical entities", will_clean=False)
    _section(CAT_UNRELATED, "other domains (untouched)", will_clean=False)


# --- Main -----------------------------------------------------------------

def collect_topics_to_clean(
    buckets: dict[str, list[Discovery]],
    *,
    include_sa_static: bool,
) -> list[str]:
    """Return the list of config topics that the user wants cleared."""
    topics: list[str] = []
    for d in buckets[CAT_ORPHAN_V1]:
        topics.append(d.topic)
    for d in buckets[CAT_ORPHAN_COLLISION]:
        topics.append(d.topic)
    if include_sa_static:
        for d in buckets[CAT_SA_STATIC]:
            topics.append(d.topic)
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in topics:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear orphan HA Discovery messages left on the MQTT broker."
    )
    parser.add_argument("--host", required=True, help="MQTT broker hostname or IP")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default 1883)")
    parser.add_argument("--username", required=True, help="MQTT username")
    parser.add_argument("--password", required=True, help="MQTT password")
    parser.add_argument(
        "--canonical-spec",
        type=Path,
        default=Path("docs/MQTT_CANONICAL.json"),
        help="Path to the canonical unique_id JSON spec",
    )
    parser.add_argument(
        "--n-inverters",
        type=int,
        default=2,
        help="Number of inverters (expands per_inverter_suffixes; default 2)",
    )
    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=10.0,
        help="How long to listen for retained payloads (default 10s)",
    )
    parser.add_argument(
        "--include-sa-static",
        action="store_true",
        help="Also clear SA legacy static-config discoveries that our daemon doesn't publish",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run",
        dest="action",
        action="store_const",
        const="dry-run",
        help="Default. Categorize and print only; never publishes.",
    )
    action.add_argument(
        "--apply",
        dest="action",
        action="store_const",
        const="apply",
        help="Actually publish empty retained payloads to clear orphans.",
    )

    args = parser.parse_args(argv)
    apply = args.action == "apply"

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    spec = load_canonical_spec(args.canonical_spec, n_inverters=args.n_inverters)
    if not spec.all_sensor_unique_ids and not spec.binary_sensors:
        log.error(
            "canonical spec is empty — refusing to proceed (every discovery would look like an orphan)"
        )
        return 2

    try:
        discoveries = capture_retained(
            args.host, args.port, args.username, args.password,
            capture_seconds=args.capture_seconds,
        )
    except (OSError, TimeoutError, RuntimeError) as e:
        log.error("MQTT capture failed: %s", e)
        return 3

    buckets = build_report(discoveries, spec)
    print_report(buckets, include_sa_static=args.include_sa_static, apply=apply)

    topics_to_clean = collect_topics_to_clean(
        buckets, include_sa_static=args.include_sa_static,
    )

    if not apply:
        print(
            f"\n[DRY-RUN] would clear {len(topics_to_clean)} retained topic(s)."
            " Pass --apply to actually publish empty retained payloads."
        )
        return 0

    if not topics_to_clean:
        print("\n[APPLY] nothing to clean. Exiting.")
        return 0

    print(f"\n[APPLY] clearing {len(topics_to_clean)} retained topic(s)...")
    try:
        acked = clear_retained(
            args.host, args.port, args.username, args.password, topics_to_clean,
        )
    except (OSError, TimeoutError, RuntimeError) as e:
        log.error("clear failed: %s", e)
        return 4
    print(f"[APPLY] {acked}/{len(topics_to_clean)} cleanup publishes acknowledged.")
    return 0 if acked == len(topics_to_clean) else 5


if __name__ == "__main__":
    sys.exit(main())
