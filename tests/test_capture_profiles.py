from pathlib import Path

import pytest

from um980_rtklib_pipeline.capture_profiles import (
    CaptureProfileError,
    parse_capture_profile_text,
)


def test_passive_profile_parses_to_zero_commands() -> None:
    profile = parse_capture_profile_text(
        """
        # enabled: true
        mode: passive
        expected: current-stream
        persistent: no
        """,
        path=Path("tools/um980_profiles/runtime/passive.um980"),
    )

    assert profile.enabled is True
    assert profile.mode == "passive"
    assert profile.commands == ()


def test_comments_and_blank_lines_are_ignored() -> None:
    profile = parse_capture_profile_text(
        """
        # enabled: true

        # comment
        mode: ascii
        LOG GNGGA ONTIME 1
        """,
        path=Path("profile.um980"),
    )

    assert profile.commands == ("LOG GNGGA ONTIME 1",)


def test_disabled_profile_is_marked_skipped() -> None:
    profile = parse_capture_profile_text("mode: binary\n# TODO enable after review\n")

    assert profile.enabled is False
    assert profile.commands == ()
    assert profile.warnings


@pytest.mark.parametrize(
    "token",
    ["SAVECONFIG", "RESET", "FRESET", "FACTORY", "BAUD", "USBMODE", "FLASH", "NVM", "FORMAT", "ERASE", "SAVE"],
)
def test_unsafe_active_commands_are_rejected(token: str) -> None:
    with pytest.raises(CaptureProfileError):
        parse_capture_profile_text(f"# enabled: true\nLOG GGA ONTIME 1\n{token}\n", path=Path("profile.um980"))


def test_unsafe_words_in_comments_are_ignored() -> None:
    profile = parse_capture_profile_text("# enabled: true\n# SAVECONFIG is forbidden\nLOG GGA ONTIME 1\n")

    assert profile.enabled is True
    assert profile.commands == ("LOG GGA ONTIME 1",)


def test_shell_metacharacters_are_rejected() -> None:
    with pytest.raises(CaptureProfileError):
        parse_capture_profile_text("# enabled: true\nLOG GGA ONTIME 1; SAVECONFIG\n")
