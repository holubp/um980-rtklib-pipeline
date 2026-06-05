from pathlib import Path

from um980_rtklib_pipeline.capture_validate import validate_capture_file
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.stream import unicore_binary_crc32


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _binary_frame(message_id: int, payload: bytes) -> bytes:
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    header[4:6] = message_id.to_bytes(2, "little")
    header[6:8] = len(payload).to_bytes(2, "little")
    body = bytes(header) + payload
    return body + unicore_binary_crc32(body).to_bytes(4, "little")


def test_empty_capture_fails(tmp_path: Path) -> None:
    result = validate_capture_file(_write(tmp_path / "empty.unc", b""))

    assert result.mode_expectation_passed is False
    assert result.errors == ["capture file is empty"]


def test_synthetic_nmea_unc_passes_ascii_mode(tmp_path: Path) -> None:
    sentence = make_sentence("GNRMC,052000.000,A,5000.0000,N,01400.0000,E,0.0,0.0,300526,,,A")
    result = validate_capture_file(_write(tmp_path / "sample.unc", (sentence + "\r\n").encode("ascii")), expect_mode="ascii")

    assert result.mode_expectation_passed is True
    assert result.nmea_records == 1
    assert result.message_counts["RMC"] == 1


def test_unc_and_ubx_suffixes_validate_equivalently(tmp_path: Path) -> None:
    sentence = make_sentence("GNGGA,052000.200,5000.0001,N,01400.0001,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    data = (sentence + "\r\n").encode("ascii")
    unc = validate_capture_file(_write(tmp_path / "same.unc", data), expect_mode="ascii")
    ubx = validate_capture_file(_write(tmp_path / "same.ubx", data), expect_mode="ascii")
    unc_dict = unc.as_dict()
    ubx_dict = ubx.as_dict()
    for ignored in ("path", "suffix"):
        unc_dict.pop(ignored)
        ubx_dict.pop(ignored)

    assert unc.suffix == ".unc"
    assert ubx.suffix == ".ubx"
    assert unc_dict == ubx_dict


def test_random_binary_does_not_crash_for_legacy_suffixes(tmp_path: Path) -> None:
    data = b"\x00\xff\x01not-a-known-frame"

    assert validate_capture_file(_write(tmp_path / "random.unc", data)).errors == []
    assert validate_capture_file(_write(tmp_path / "random.ubx", data)).errors == []


def test_binary_frame_passes_binary_mode(tmp_path: Path) -> None:
    result = validate_capture_file(_write(tmp_path / "binary.unc", _binary_frame(138, b"payload")), expect_mode="binary")

    assert result.mode_expectation_passed is True
    assert result.unicore_binary_frames == 1
    assert result.message_counts["OBSVMCMPB"] == 1


def test_mixed_ascii_binary_stream_passes_mixed_mode(tmp_path: Path) -> None:
    sentence = make_sentence("GNRMC,052000.000,A,5000.0000,N,01400.0000,E,0.0,0.0,300526,,,A")
    data = (sentence + "\r\n").encode("ascii") + _binary_frame(138, b"payload")
    result = validate_capture_file(_write(tmp_path / "mixed.unc", data), expect_mode="mixed")

    assert result.mode_expectation_passed is True
    assert result.nmea_records == 1
    assert result.unicore_binary_frames == 1


def test_expected_message_missing_is_reported(tmp_path: Path) -> None:
    sentence = make_sentence("GNRMC,052000.000,A,5000.0000,N,01400.0000,E,0.0,0.0,300526,,,A")
    result = validate_capture_file(
        _write(tmp_path / "sample.unc", (sentence + "\r\n").encode("ascii")),
        expect_mode="ascii",
        expected_messages=["GGA"],
    )

    assert result.expected_messages_missing == ["GGA"]
    assert result.errors
