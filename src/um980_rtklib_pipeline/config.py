"""Configuration loading helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Load JSON or YAML configuration.

    YAML support uses PyYAML when available. The core package has no mandatory
    dependency, so users who need YAML can install the `config` extra.
    """

    if path is None:
        return {}
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("YAML configuration requires PyYAML; install the config extra") from exc
    loaded = yaml.safe_load(text)
    return loaded or {}


def deep_get(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current

