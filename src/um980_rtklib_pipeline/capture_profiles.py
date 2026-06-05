"""Safe runtime UM980 capture profile parsing.

The capture helper intentionally accepts only reviewed, line-oriented receiver
commands.  This module is shared by the Python CLI wrapper and tests; the native
Termux helper contains a second conservative safety check so direct helper calls
remain protected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


UNSAFE_COMMAND_TOKENS = (
    "SAVECONFIG",
    "SAVE",
    "FRESET",
    "FACTORY",
    "DEFAULT",
    "ERASE",
    "FORMAT",
    "UPDATE",
    "UPGRADE",
    "BOOT",
    "BAUD",
    "COM",
    "USBMODE",
    "PERMANENT",
    "NVM",
    "FLASH",
    "RESET",
)
SHELL_METACHAR_RE = re.compile(r"[;&|`$<>]")
PROFILE_METADATA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\s*:\s*(.*)$")


@dataclass(frozen=True)
class CaptureProfile:
    """Parsed UM980 runtime capture profile."""

    path: Path
    enabled: bool
    metadata: dict[str, str] = field(default_factory=dict)
    commands: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        """Return declared profile mode, defaulting to ``unknown``."""

        return self.metadata.get("mode", "unknown")


class CaptureProfileError(ValueError):
    """Raised when a runtime profile is unsafe or malformed."""


def parse_capture_profile(path: Path) -> CaptureProfile:
    """Read, parse, and validate a UM980 runtime profile.

    Args:
        path: Profile file.

    Returns:
        Parsed profile.

    Raises:
        CaptureProfileError: If an active command is unsafe.
    """

    text = path.read_text(encoding="utf-8")
    return parse_capture_profile_text(text, path=path)


def parse_capture_profile_text(text: str, *, path: Path | None = None) -> CaptureProfile:
    """Parse profile text for tests or CLI use."""

    profile_path = path or Path("<memory>")
    enabled = False
    metadata: dict[str, str] = {}
    commands: list[str] = []
    warnings: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            comment = stripped[1:].strip()
            if comment.lower() == "enabled: true":
                enabled = True
            continue
        meta_match = PROFILE_METADATA_RE.match(stripped)
        if meta_match:
            key = stripped.split(":", 1)[0].strip().lower()
            value = meta_match.group(1).strip()
            metadata[key] = value
            if key == "enabled" and value.lower() == "true":
                enabled = True
            continue
        validate_runtime_command(stripped, line_number=line_number, path=profile_path)
        commands.append(stripped)
    if not enabled:
        warnings.append("profile is disabled; add '# enabled: true' only after commands are reviewed safe")
    return CaptureProfile(
        path=profile_path,
        enabled=enabled,
        metadata=metadata,
        commands=tuple(commands),
        warnings=tuple(warnings),
    )


def validate_runtime_command(command: str, *, line_number: int = 0, path: Path | None = None) -> None:
    """Reject commands that could alter persistent receiver state or shell out."""

    if not command.strip():
        raise CaptureProfileError(_where(path, line_number) + "empty active command is not allowed")
    if SHELL_METACHAR_RE.search(command):
        raise CaptureProfileError(_where(path, line_number) + f"unsafe shell metacharacter in command: {command!r}")
    upper = command.upper()
    for token in UNSAFE_COMMAND_TOKENS:
        if token in upper:
            raise CaptureProfileError(_where(path, line_number) + f"unsafe receiver token {token!r} in command: {command!r}")


def _where(path: Path | None, line_number: int) -> str:
    location = f"{path}: " if path is not None else ""
    if line_number:
        location += f"line {line_number}: "
    return location
