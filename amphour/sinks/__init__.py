"""Where readings go. Prometheus is always on; InfluxDB is opt-in."""

from .base import Sink

__all__ = ["Sink"]
