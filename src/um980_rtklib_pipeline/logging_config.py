"""CLI logging setup."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(verbose: bool = False, log_file: str | Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
    )

