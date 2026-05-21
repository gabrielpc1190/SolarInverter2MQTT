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
class BridgeConfig:
    inverters: list[InverterCfg]
    mqtt: MqttCfg
    polling: PollingCfg = field(default_factory=PollingCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)


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

    return BridgeConfig(inverters=inverters, mqtt=mqtt, polling=polling, logging=logging_cfg)
