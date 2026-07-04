"""CLI entry: `python -m inverter_bridge --config /etc/inverter-bridge.yaml`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .config import load_config
from .daemon import Daemon


def _sigterm_handler(signum, frame):
    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    """Translate SIGTERM (what systemd sends on stop/restart) into
    KeyboardInterrupt so Daemon.start()'s finally block runs: persist energy,
    stop the BMS service, publish availability=offline, drain the executor.

    Without this, every `systemctl restart` killed the process on the spot
    and lost the energy integrated since the last periodic save (audit A1).
    """
    signal.signal(signal.SIGTERM, _sigterm_handler)


def main() -> int:
    p = argparse.ArgumentParser(prog="inverter-bridge")
    p.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/inverter-bridge.yaml"),
        help="Path to YAML config (default: /etc/inverter-bridge.yaml)",
    )
    p.add_argument(
        "--log-level",
        default=None,
        help="Override config log level (DEBUG / INFO / WARNING / ERROR)",
    )
    args = p.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    level = args.log_level or cfg.logging.level
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    install_signal_handlers()
    try:
        Daemon(cfg).start()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception("fatal error in daemon")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
