"""CLI logging setup."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(verbose: bool = False, log_file: str | Path | None = None) -> None:
    """Configure process-wide CLI logging.

    Args:
        verbose: Enable progress and informational logging when true.
        log_file: Optional file path that receives a copy of log messages.
    """

    level = logging.INFO if verbose else logging.WARNING
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
    )
