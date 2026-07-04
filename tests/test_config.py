"""Tests for YAML config loader."""

from pathlib import Path

import pytest

from inverter_bridge.config import BridgeConfig, load_config


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


def test_load_minimal_valid_config(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("pwd")
    p = write_yaml(
        tmp_path,
        f"""
inverters:
  - name: inv1
    port: /dev/ttyUSB0
    slave: 1
  - name: inv2
    port: /dev/ttyUSB1
    slave: 2
mqtt:
  host: localhost
  username: u
  password_file: {secret}
        """,
    )
    cfg = load_config(p)
    assert isinstance(cfg, BridgeConfig)
    assert len(cfg.inverters) == 2
    assert cfg.inverters[0].slave == 1
    assert cfg.inverters[1].slave == 2
    assert cfg.mqtt.host == "localhost"
    # Defaults applied
    assert cfg.polling.hot_interval_s == 3.0
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.password == "pwd"


def test_invalid_slave_addr_raises(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("p")
    p = write_yaml(
        tmp_path,
        f"""
inverters:
  - name: inv1
    port: /dev/ttyUSB0
    slave: 300
mqtt:
  host: localhost
  username: u
  password_file: {secret}
        """,
    )
    with pytest.raises(ValueError, match="slave"):
        load_config(p)


def test_duplicate_slave_raises(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("p")
    p = write_yaml(
        tmp_path,
        f"""
inverters:
  - name: a
    port: /dev/ttyUSB0
    slave: 1
  - name: b
    port: /dev/ttyUSB1
    slave: 1
mqtt:
  host: localhost
  username: u
  password_file: {secret}
        """,
    )
    with pytest.raises(ValueError, match="duplicate slave"):
        load_config(p)


def test_missing_required_field_raises(tmp_path):
    p = write_yaml(tmp_path, "inverters: []\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_load_password_from_file(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("super-secret-value\n")
    p = write_yaml(
        tmp_path,
        f"""
inverters:
  - name: inv1
    port: /dev/ttyUSB0
    slave: 1
mqtt:
  host: localhost
  username: u
  password_file: {secret}
        """,
    )
    cfg = load_config(p)
    assert cfg.mqtt.password == "super-secret-value"


def test_load_hex_slave_string(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("p")
    p = write_yaml(
        tmp_path,
        f"""
inverters:
  - name: inv1
    port: /dev/ttyUSB0
    slave: "0x01"
mqtt:
  host: localhost
  username: u
  password_file: {secret}
        """,
    )
    cfg = load_config(p)
    assert cfg.inverters[0].slave == 1


def test_load_example_config_in_repo(tmp_path):
    """Verify that the shipped example YAML loads.

    The example references /etc/inverter-bridge.secrets, which doesn't exist
    on dev machines — and since audit fix M8 a missing password_file is a hard
    error (it used to silently degrade to an empty password). So point it at a
    real temp file before loading.
    """
    example = Path(__file__).parent.parent / "config.example.yaml"
    pw = tmp_path / "secrets"
    pw.write_text("dummy")
    patched = tmp_path / "config.yaml"
    patched.write_text(
        example.read_text().replace("/etc/inverter-bridge.secrets", str(pw))
    )
    cfg = load_config(patched)
    assert cfg.inverters[0].slave == 1
    assert cfg.inverters[1].slave == 2
    assert cfg.mqtt.topic_prefix == "gadi_inverters"
    assert cfg.mqtt.password == "dummy"
