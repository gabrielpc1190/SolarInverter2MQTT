"""BMS BlueSun (Octopus / Seplos Modbus over BLE) module.

Port del decoder que vivía en el firmware ESPHome `panel-s3-step5-ui.yaml`,
ahora en Python para ejecución en la OPi.

Submódulos:
  - octopus_protocol: frame encoding/decoding, CRC, dataclasses tipados
  - ble_client: cliente BLE async con bleak (subscribe FFF1, write FFF2)
  - aggregator: bank-level aggregates a partir de per-pack data
"""
