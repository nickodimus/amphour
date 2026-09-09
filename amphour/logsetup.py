"""Logging configuration. Stdlib only - the predecessor vendored a third-party
`duallog` module to do this, which is a dependency and a maintenance burden for
about twenty lines of handler wiring.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure(level: str = "INFO", file: str | None = None, keep_days: int = 14) -> None:
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        raise ValueError(
            f"unknown log level {level!r}; "
            f"expected one of {', '.join(logging.getLevelNamesMapping())}"
        )

    root = logging.getLogger()
    root.setLevel(numeric)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    formatter = logging.Formatter(FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.TimedRotatingFileHandler(
            path, when="midnight", backupCount=keep_days, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # bleak is chatty at DEBUG and most of it is D-Bus noise
    logging.getLogger("bleak").setLevel(max(numeric, logging.INFO))
