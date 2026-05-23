"""YAML config loader with dataclass validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class InverterCfg:
    name: str
    port: str
    slave: int


@dataclass(frozen=True, slots=True)
class PollingCfg:
    hot_interval_s: float = 3.0
    cold_interval_s: float = 60.0
    serial_timeout_s: float = 1.5
    inter_query_delay_s: float = 0.05
    retry_attempts: int = 3
    retry_backoff_s: float = 0.1


@dataclass(frozen=True, slots=True)
class MqttCfg:
    host: str
    username: str
    password: str
    port: int = 1883
    client_id: str = "inverter_bridge"
    topic_prefix: str = "solar_assistant"
    discovery_prefix: str = "homeassistant"
    retain_discovery: bool = True
    qos: int = 0


@dataclass(frozen=True, slots=True)
class LoggingCfg:
    level: str = "INFO"
    format: str = "text"


@dataclass(frozen=True, slots=True)
class BmsCfg:
    """Configuración del módulo BMS BlueSun (Octopus / Seplos sobre BLE).

    `enabled=False` por defecto: el daemon NO conecta a BLE ni publica BMS
    discovery a menos que esté habilitado explícitamente. Esto permite hacer
    deploy del código antes del cutover sin afectar producción.
    """
    enabled: bool = False
    master_mac: str = ""                  # MAC del Pack01 master (ej. C0:D6:3C:52:0F:0D)
    pack_count: int = 4                    # 1..4 packs detrás del master via RS485 interno
    poll_fast_interval_s: float = 5.0      # cmd 0x10 PIA per-pack
    poll_slow_interval_s: float = 300.0    # cmd 0x11 PIB per-pack
    inter_pack_delay_s: float = 0.5        # rate-limit al chain RS485 interno
    connect_timeout_s: float = 15.0
    reconnect_initial_backoff_s: float = 2.0
    reconnect_max_backoff_s: float = 60.0
    mqtt_topic_prefix: str = "gadi_bms"    # topics raíz para MQTT publishing
    mqtt_device_name: str = "BlueSun"      # device name (slug → entity_id prefix)
    mqtt_device_id: str = "bluesun_bms"    # discovery device.identifiers
    energy_persist_path: str = "/var/lib/inverter-bridge/bms-energy.json"
    # Pack serials (no son legibles por BLE Octopus para packs 2-4; solo pack 1 master
    # los expone via cmd 0x17/VIA). Hardcoded basado en etiqueta + app Octopus.
    # Si se reemplaza un pack, editar y reiniciar daemon.
    pack_serials: tuple[str, ...] = (
        "BN012502180020",   # Pack 1
        "BN012502180443",   # Pack 2
        "BN012502180269",   # Pack 3
        "BN012502180456",   # Pack 4
    )


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    inverters: list[InverterCfg]
    mqtt: MqttCfg
    polling: PollingCfg = field(default_factory=PollingCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    bms: BmsCfg = field(default_factory=BmsCfg)


def _parse_int_or_hex(v: Any) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return int(v, 0)
    raise ValueError(f"expected int or hex string, got {type(v).__name__}: {v!r}")


def load_config(path: Path) -> BridgeConfig:
    """Load and validate a BridgeConfig from a YAML file."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping, got {type(data).__name__}")

    if "inverters" not in data or not data["inverters"]:
        raise ValueError("config missing 'inverters' or it is empty")
    if "mqtt" not in data:
        raise ValueError("config missing 'mqtt' section")

    inverters: list[InverterCfg] = []
    seen_slaves: set[int] = set()
    for entry in data["inverters"]:
        slave = _parse_int_or_hex(entry["slave"])
        if not (1 <= slave <= 247):
            raise ValueError(f"slave {slave} out of valid range 1..247")
        if slave in seen_slaves:
            raise ValueError(f"duplicate slave address {slave}")
        seen_slaves.add(slave)
        inverters.append(InverterCfg(name=entry["name"], port=entry["port"], slave=slave))

    mq = data["mqtt"]
    pw_file = Path(mq["password_file"])
    password = pw_file.read_text().strip() if pw_file.exists() else ""
    mqtt = MqttCfg(
        host=mq["host"],
        username=mq["username"],
        password=password,
        port=int(mq.get("port", 1883)),
        client_id=mq.get("client_id", "inverter_bridge"),
        topic_prefix=mq.get("topic_prefix", "solar_assistant"),
        discovery_prefix=mq.get("discovery_prefix", "homeassistant"),
        retain_discovery=bool(mq.get("retain_discovery", True)),
        qos=int(mq.get("qos", 0)),
    )

    poll_data = data.get("polling", {})
    polling = PollingCfg(
        hot_interval_s=float(poll_data.get("hot_interval_s", 3.0)),
        cold_interval_s=float(poll_data.get("cold_interval_s", 60.0)),
        serial_timeout_s=float(poll_data.get("serial_timeout_s", 1.5)),
        inter_query_delay_s=float(poll_data.get("inter_query_delay_s", 0.05)),
        retry_attempts=int(poll_data.get("retry_attempts", 3)),
        retry_backoff_s=float(poll_data.get("retry_backoff_s", 0.1)),
    )

    log_data = data.get("logging", {})
    logging_cfg = LoggingCfg(
        level=log_data.get("level", "INFO"),
        format=log_data.get("format", "text"),
    )

    bms_data = data.get("bms", {})
    # Pack serials override desde YAML (lista, opcional)
    serials_yaml = bms_data.get("pack_serials")
    if serials_yaml is None:
        serials = BmsCfg.__dataclass_fields__["pack_serials"].default
    else:
        if not isinstance(serials_yaml, list) or not all(isinstance(x, str) for x in serials_yaml):
            raise ValueError("bms.pack_serials debe ser lista de strings")
        serials = tuple(serials_yaml)

    bms = BmsCfg(
        enabled=bool(bms_data.get("enabled", False)),
        master_mac=str(bms_data.get("master_mac", "")),
        pack_count=int(bms_data.get("pack_count", 4)),
        poll_fast_interval_s=float(bms_data.get("poll_fast_interval_s", 5.0)),
        poll_slow_interval_s=float(bms_data.get("poll_slow_interval_s", 300.0)),
        inter_pack_delay_s=float(bms_data.get("inter_pack_delay_s", 0.5)),
        connect_timeout_s=float(bms_data.get("connect_timeout_s", 15.0)),
        reconnect_initial_backoff_s=float(bms_data.get("reconnect_initial_backoff_s", 2.0)),
        reconnect_max_backoff_s=float(bms_data.get("reconnect_max_backoff_s", 60.0)),
        mqtt_topic_prefix=str(bms_data.get("mqtt_topic_prefix", "gadi_bms")),
        mqtt_device_name=str(bms_data.get("mqtt_device_name", "BlueSun")),
        mqtt_device_id=str(bms_data.get("mqtt_device_id", "bluesun_bms")),
        energy_persist_path=str(bms_data.get("energy_persist_path", "/var/lib/inverter-bridge/bms-energy.json")),
        pack_serials=serials,
    )
    if bms.enabled:
        if not bms.master_mac:
            raise ValueError("bms.enabled=true requires bms.master_mac to be set")
        if not (1 <= bms.pack_count <= 16):
            raise ValueError(f"bms.pack_count {bms.pack_count} out of range 1..16")

    return BridgeConfig(inverters=inverters, mqtt=mqtt, polling=polling, logging=logging_cfg, bms=bms)
