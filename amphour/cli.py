"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import TYPE_CHECKING

from . import __version__, logsetup, protocol
from .config import Config, ConfigError, load
from .poller import Poller
from .reading import decode
from .registers import FIELDS

if TYPE_CHECKING:
    from .sinks.base import Sink
    from .sinks.prometheus import PrometheusSink

log = logging.getLogger("amphour")

DEFAULT_CONFIG = "/etc/amphour/config.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amphour", description="Monitor DC power-system gear and export metrics."
    )
    parser.add_argument("--version", action="version", version=f"amphour {__version__}")
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument("--log-level", help="override the configured log level")
    parser.add_argument(
        "--once",
        action="store_true",
        help="take one reading, print it, and exit - for commissioning",
    )
    parser.add_argument(
        "--decode",
        metavar="HEX",
        help="decode one captured frame offline and exit; needs no hardware",
    )
    parser.add_argument(
        "--list-fields",
        action="store_true",
        help="print the register map with confidence levels and exit",
    )
    return parser


def _list_fields() -> int:
    width = max(len(f.name) for f in FIELDS)
    print(f"{'field'.ljust(width)}  register  unit         confidence")
    print(f"{'-' * width}  --------  -----------  ----------")
    for f in FIELDS:
        print(f"{f.name.ljust(width)}  0x{f.register:04x}    {f.unit.ljust(11)}  {f.confidence}")
    return 0


def _decode_hex(raw: str) -> int:
    cleaned = "".join(raw.split()).replace(":", "")
    try:
        frame = bytes.fromhex(cleaned)
    except ValueError as exc:
        print(f"not valid hex: {exc}", file=sys.stderr)
        return 2
    try:
        reading = decode(frame)
    except protocol.ProtocolError as exc:
        print(f"frame rejected: {exc}", file=sys.stderr)
        return 1
    width = max(len(k) for k in reading.values)
    for name, value in reading.values.items():
        print(f"{name.ljust(width)}  {value}")
    print(f"\n{reading}")
    return 0


def _build_sinks(config: Config) -> tuple[list[Sink], PrometheusSink | None]:
    sinks: list[Sink] = []
    prometheus: PrometheusSink | None = None
    if config.prometheus.enabled:
        from .sinks.prometheus import PrometheusSink

        prometheus = PrometheusSink(
            port=config.prometheus.port,
            addr=config.prometheus.addr,
            prefix=config.prometheus.metric_prefix,
            include_unverified=config.prometheus.include_unverified,
        )
        sinks.append(prometheus)
    if config.influxdb.enabled:
        from .sinks.influx import InfluxSink

        sinks.append(
            InfluxSink(
                url=config.influxdb.url,
                database=config.influxdb.database,
                measurement=config.influxdb.measurement,
                username=config.influxdb.username,
                password=config.influxdb.password,
                tags=config.influxdb.tags,
                include_unverified=config.influxdb.include_unverified,
                batch_size=config.influxdb.batch_size,
                flush_interval=config.influxdb.flush_interval,
                timeout=config.influxdb.timeout,
                retention_policy=config.influxdb.retention_policy,
            )
        )
    return sinks, prometheus


async def _run(config: Config, once: bool) -> int:
    from .devices.renogy_bt1 import RenogyBT1Transport

    transport = RenogyBT1Transport(
        config.device.address,
        adapter=config.device.adapter,
        alias=config.device.alias,
        connect_timeout=config.poll.connect_timeout,
        response_timeout=config.poll.response_timeout,
    )

    if once:
        await transport.connect()
        try:
            frame = await transport.request(
                protocol.build_read_request(), protocol.EXPECTED_FRAME_LEN
            )
            reading = decode(frame)
            print(f"frame  {frame.hex()}")
            width = max(len(k) for k in reading.values)
            for name, value in reading.values.items():
                print(f"{name.ljust(width)}  {value}")
            print(f"\n{reading}")
        finally:
            await transport.disconnect()
        return 0

    sinks, prometheus = _build_sinks(config)
    poller = Poller(transport, sinks, config, on_failure=prometheus)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, poller.stop)

    await poller.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_fields:
        return _list_fields()
    if args.decode:
        return _decode_hex(args.decode)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logsetup.configure(
        level=args.log_level or config.logging.level,
        file=config.logging.file,
        keep_days=config.logging.keep_days,
    )

    try:
        return asyncio.run(_run(config, args.once))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.error("fatal: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
