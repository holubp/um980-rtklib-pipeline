from __future__ import annotations

from datetime import UTC, datetime

import pytest

from um980_rtklib_pipeline.time_window import ProcessingWindow, processing_window_from_values


def test_processing_window_contract_parses_naive_as_utc_and_is_inclusive() -> None:
    window = processing_window_from_values("2026-05-30T05:00:00", "2026-05-30T05:01:00")

    assert window.start_time_utc == datetime(2026, 5, 30, 5, 0, tzinfo=UTC)
    assert window.end_time_utc == datetime(2026, 5, 30, 5, 1, tzinfo=UTC)
    assert window.inclusive_start is True
    assert window.inclusive_end is True
    assert window.contains(datetime(2026, 5, 30, 5, 0, tzinfo=UTC))
    assert window.contains(datetime(2026, 5, 30, 5, 1, tzinfo=UTC))
    assert not window.contains(datetime(2026, 5, 30, 5, 1, 1, tzinfo=UTC))
    assert window.to_cli_args() == ["--start-time", "2026-05-30T05:00:00+00:00", "--end-time", "2026-05-30T05:01:00+00:00"]
    assert window.to_json()["source"] == "cli"


def test_processing_window_overlap_and_clamp() -> None:
    window = processing_window_from_values("2026-05-30T05:00:00Z", "2026-05-30T05:01:00Z")

    assert window.overlaps(datetime(2026, 5, 30, 4, 59, 59, tzinfo=UTC), datetime(2026, 5, 30, 5, 0, 1, tzinfo=UTC))
    assert not window.overlaps(datetime(2026, 5, 30, 5, 1, 1, tzinfo=UTC), datetime(2026, 5, 30, 5, 2, tzinfo=UTC))
    assert window.clamp_interval(
        datetime(2026, 5, 30, 4, 59, 0, tzinfo=UTC),
        datetime(2026, 5, 30, 5, 2, 0, tzinfo=UTC),
    ) == (datetime(2026, 5, 30, 5, 0, tzinfo=UTC), datetime(2026, 5, 30, 5, 1, tzinfo=UTC))


def test_processing_window_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end-time"):
        processing_window_from_values("2026-05-30T05:01:00Z", "2026-05-30T05:00:00Z")


def test_processing_window_none_source() -> None:
    window = ProcessingWindow()

    assert window.source == "none"
    assert window.to_cli_args() == []
    assert window.to_json()["enabled"] is False
