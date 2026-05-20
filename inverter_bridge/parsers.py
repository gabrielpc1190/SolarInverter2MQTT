"""Apply the SRNE register map to ModbusFrame responses, producing typed sensor values."""

from __future__ import annotations

from dataclasses import dataclass

from .modbus import ModbusFrame
from .srne_map import BLOCKS, fields_for


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    block_addr: int
    block_name: str
    slave: int
    regs_raw: tuple[int, ...]
    fields: dict[str, float]


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def parse_block(*, block_addr: int, frame: ModbusFrame) -> ParsedBlock:
    """Apply the field table for `block_addr` to the registers in `frame`."""
    block = next((b for b in BLOCKS if b.addr == block_addr), None)
    if block is None:
        raise ValueError(f"unknown block 0x{block_addr:04x}")
    if len(frame.regs) != block.count:
        raise ValueError(
            f"block 0x{block_addr:04x} expects {block.count} regs, got {len(frame.regs)}"
        )
    fields_out: dict[str, float] = {}
    for f in fields_for(block_addr):
        raw = frame.regs[f.offset]
        val = _s16(raw) if f.signed else raw
        fields_out[f.key] = round(val * f.scale, 4)
    return ParsedBlock(
        block_addr=block_addr,
        block_name=block.name,
        slave=frame.slave,
        regs_raw=tuple(frame.regs),
        fields=fields_out,
    )
