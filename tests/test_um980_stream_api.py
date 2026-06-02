from __future__ import annotations

from io import BytesIO

from um980_rtklib_pipeline.um980_stream import (
    UNICORE_MESSAGE_REGISTRY,
    iter_records,
    parse_bytes,
    summarize_records,
)


def test_command_response_line_is_not_invalid_nmea() -> None:
    result = parse_bytes(b"$command,CONFIG,response: OK\n")

    assert result.records[0].kind == "command_response"
    assert result.diagnostics.command_response_records == 1
    assert result.diagnostics.invalid_nmea_records == 0


def test_iter_records_and_summarize_records_public_api() -> None:
    records = list(iter_records(BytesIO(b"$command,CONFIG,response: OK\nnoise")))
    summary = summarize_records(records)

    assert records[0].kind == "command_response"
    assert summary.command_response_records == 1
    assert summary.noise_bytes == 5


def test_unicore_message_registry_uses_a_b_suffixes() -> None:
    by_root = {entry.root_name: entry for entry in UNICORE_MESSAGE_REGISTRY}

    assert by_root["OBSVMCMP"].ascii_name == "OBSVMCMPA"
    assert by_root["OBSVMCMP"].binary_name == "OBSVMCMPB"
    assert by_root["GPSEPH"].ascii_name == "GPSEPHA"
    assert by_root["GPSEPH"].binary_name == "GPSEPHB"
    assert by_root["BD3EPH"].decoder_status == "known_unsupported"
