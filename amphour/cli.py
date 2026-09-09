"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__, drivers, logsetup
from .config import Config, ConfigError, load
from .device import Device
from .reading import Reading
from .sinks.base import Sink
from .supervisor import Supervisor

log = logging.getLogger("amphour")

DEFAULT_CONFIG = "/etc/amphour/config.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amphour",
        description="Monitor DC power-system gear and export metrics.",
    )
    parser.add_argument("--version", action="version", version=f"amphour {__version__}")
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument("--log-level", help="override the configured log level")
    parser.add_argument(
        "--once",
        action="store_true",
        help="take one reading from each device, print it, and exit - for commissioning",
    )
    parser.add_argument(
        "--list-drivers", action="store_true", help="print available drivers and exit"
    )
    parser.add_argument(
        "--list-fields",
        metavar="DRIVER",
        help="print a driver's field map with confidence levels and exit",
    )
    parser.add_argument(
        "--decode",
        metavar="HEX",
        help="decode one captured Renogy frame offline and exit; needs no hardware",
    )
    return parser


def _list_drivers() -> int:
    for name in drivers.available():
        print(name)
    return 0


def _list_fields(driver: str) -> int:
    modules = {
        "renogy_bt1": "amphour.drivers.renogy.registers",
        "victron_vedirect": "amphour.drivers.victron_vedirect.fields",
    }
    if driver not in modules:
        print(
            f"unknown driver {driver!r}; available: {', '.join(drivers.available())}",
            file=sys.stderr,
        )
        return 2
    import importlib

    fields = importlib.import_module(modules[driver]).FIELDS
    width = max(len(f.name) for f in fields)
    print(f"{'field'.ljust(width)}  {'unit'.ljust(14)}  confidence")
    print(f"{'-' * width}  {'-' * 14}  ----------")
    for f in fields:
        print(f"{f.name.ljust(width)}  {f.unit.ljust(14)}  {f.confidence}")
    return 0


def _decode_hex(raw: str) -> int:
    from .drivers.renogy import decode, protocol

    cleaned = "".join(raw.split()).replace(":", "")
    try:
        frame = bytes.fromhex(cleaned)
    except ValueError as exc:
        print(f"not valid hex: {exc}", file=sys.stderr)
        return 2
    try:
        reading = decode(frame, source="offline")
    except protocol.ProtocolError as exc:
        print(f"frame rejected: {exc}", file=sys.stderr)
        return 1
    width = max(len(k) for k in reading.values)
    for name, number in reading.values.items():
        print(f"{name.ljust(width)}  {number}")
    for name, phrase in reading.text.items():
        print(f"{name.ljust(width)}  {phrase}")
    print(f"\n{reading}")
    return 0


def _build_devices(config: Config) -> list[Device]:
    built: list[Device] = []
    for entry in config.devices:
        try:
            built.append(drivers.build(entry.driver, name=entry.name, **entry.options))
        except KeyError as exc:
            raise ConfigError(str(exc)) from exc
        except TypeError as exc:
            raise ConfigError(f"device {entry.name!r} (driver {entry.driver!r}): {exc}") from exc
    return built


def _build_sinks(config: Config, devices: list[Device]) -> tuple[list[Sink], object | None]:
    sinks: list[Sink] = []
    prometheus: object | None = None
    if config.prometheus.enabled:
        from .sinks.prometheus import PrometheusSink

        prometheus = PrometheusSink(
            devices,
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
                devices,
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


async def _once(devices: list[Device], timeout: float = 90.0) -> int:
    """Collect one reading from every device, print them, and stop.

    Works for polling and streaming drivers alike: each device runs normally
    and is cancelled as soon as it has produced something.
    """
    seen: dict[str, Reading] = {}
    done = asyncio.Event()

    async def emit_for(device: Device) -> None:
        async def emit(reading: Reading) -> None:
            if device.name not in seen:
                seen[device.name] = reading
                if len(seen) == len(devices):
                    done.set()

        await device.run(emit)

    tasks = [asyncio.create_task(emit_for(d), name=f"once:{d.name}") for d in devices]
    try:
        async with asyncio.timeout(timeout):
            await done.wait()
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for device in devices:
        reading = seen.get(device.name)
        print(f"\n=== {device.name} ===")
        if reading is None:
            print("  no reading within the timeout")
            continue
        width = max(len(k) for k in (*reading.values, *reading.text)) if reading.values else 1
        for name, value in reading.values.items():
            print(f"  {name.ljust(width)}  {value}")
        for name, text in reading.text.items():
            print(f"  {name.ljust(width)}  {text}")
    missing = [d.name for d in devices if d.name not in seen]
    if missing:
        print(f"\nno reading from: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


async def _run(config: Config, once: bool) -> int:
    devices = _build_devices(config)
    if once:
        return await _once(devices)

    sinks, prometheus = _build_sinks(config, devices)
    if prometheus is not None:
        hook = getattr(prometheus, "record_failure", None)
        for device in devices:
            setter = getattr(device, "set_failure_hook", None)
            if callable(setter) and callable(hook):
                setter(hook)

    supervisor = Supervisor(devices, sinks)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, supervisor.stop)
    await supervisor.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_drivers:
        return _list_drivers()
    if args.list_fields:
        return _list_fields(args.list_fields)
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
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.error("fatal: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
