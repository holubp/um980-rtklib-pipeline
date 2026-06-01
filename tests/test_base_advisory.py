from __future__ import annotations

import binascii
import json
from datetime import UTC, datetime
from pathlib import Path

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.archive_probe import probe_station_archives
from um980_rtklib_pipeline.stations import StationCatalog, ecef_to_geodetic, parse_ssc, write_station_catalog_cache


def _ascii_record(body: str) -> str:
    crc = binascii.crc32(body.encode("ascii")) & 0xFFFFFFFF
    return f"#{body}*{crc:08X}\n"


def test_base_candidates_outputs_ranked_json_from_bestnav(tmp_path, capsys):
    rover = tmp_path / "rover.unc"
    rover.write_text(
        _ascii_record(
            "BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
            "SOL_COMPUTED,NARROW_FLOAT,49.8,14.2,250.5,45.1,WGS84,0.1,0.1,0.2,"
            "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2"
        ),
        encoding="ascii",
    )
    args = cli.build_parser().parse_args(
        [
            "base-candidates",
            str(rover),
            "--track-source",
            "bestnav",
            "--stations",
            "CPAR,TUBO",
            "--format",
            "json",
            "--station-catalog-source",
            "curated",
            "--radius-km",
            "500",
            "--max-candidates",
            "2",
        ]
    )

    assert cli.cmd_base_candidates(args) == 0
    out = capsys.readouterr().out
    assert '"candidates"' in out
    assert "CPAR00CZE" in out
    assert "TUBO00CZE" in out


def test_base_candidates_table_accepts_time_window(tmp_path, capsys):
    rover = tmp_path / "rover.unc"
    rover.write_text(
        _ascii_record(
            "BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
            "SOL_COMPUTED,NARROW_FLOAT,49.8,14.2,250.5,45.1,WGS84,0.1,0.1,0.2,"
            "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2"
        ),
        encoding="ascii",
    )
    args = cli.build_parser().parse_args(
        [
            "base-candidates",
            str(rover),
            "--track-source",
            "bestnav",
            "--stations",
            "CPAR",
            "--start-time",
            "2026-05-17T12:00:00Z",
            "--format",
            "table",
            "--station-catalog-source",
            "curated",
        ]
    )

    assert cli.cmd_base_candidates(args) == 0
    assert "station" in capsys.readouterr().out


def test_parse_epn_ssc_fixture_with_long_station_ids():
    text = """
+SOLUTION/ESTIMATE
*INDEX TYPE__ CODE PT SOLN _REF_EPOCH__ UNIT S __ESTIMATED VALUE__ _STD_DEV_
     1 STAX   CPAR00CZE A    1 24:001:00000 m    2  3949919.0811 0.001
     2 STAY   CPAR00CZE A    1 24:001:00000 m    2  1116467.0408 0.001
     3 STAZ   CPAR00CZE A    1 24:001:00000 m    2  4865832.5323 0.001
     4 STAX   TUBO00CZE A    1 24:001:00000 m    2  4001470.5995 0.001
     5 STAY   TUBO00CZE A    1 24:001:00000 m    2  1192345.3042 0.001
     6 STAZ   TUBO00CZE A    1 24:001:00000 m    2  4805795.3148 0.001
-SOLUTION/ESTIMATE
"""
    records = parse_ssc(text, source_file="fixture.SSC", downloaded_at="2026-06-01T00:00:00+00:00")
    assert {record.station_id_long for record in records} == {"CPAR00CZE", "TUBO00CZE"}
    assert records[0].lat is not None


def test_station_catalog_ambiguous_short_id_is_reported(tmp_path: Path, capsys):
    lat, lon, height = ecef_to_geodetic(3949919.0811, 1116467.0408, 4865832.5323)
    catalog = StationCatalog.from_json(
        {
            "loaded_from": "fixture",
            "generated_at": "2026-06-01T00:00:00+00:00",
            "frame": "ETRF2000",
            "records": [
                {
                    "station_id_long": "ABCD00AAA",
                    "station_id_short": "ABCD",
                    "network": "EPN",
                    "x": 3949919.0811,
                    "y": 1116467.0408,
                    "z": 4865832.5323,
                    "lat": lat,
                    "lon": lon,
                    "height": height,
                    "source": "fixture",
                },
                {
                    "station_id_long": "ABCD00BBB",
                    "station_id_short": "ABCD",
                    "network": "EPN",
                    "x": 3949919.0811,
                    "y": 1116467.0408,
                    "z": 4865832.5323,
                    "lat": lat,
                    "lon": lon,
                    "height": height,
                    "source": "fixture",
                },
            ],
        }
    )
    cache = tmp_path / "stations.json"
    write_station_catalog_cache(cache, catalog)
    rover = tmp_path / "rover.unc"
    rover.write_text(
        _ascii_record(
            "BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
            "SOL_COMPUTED,NARROW_FLOAT,49.8,14.2,250.5,45.1,WGS84,0.1,0.1,0.2,"
            "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2"
        ),
        encoding="ascii",
    )
    rc = cli.main(
        [
            "base-candidates",
            str(rover),
            "--track-source",
            "bestnav",
            "--stations",
            "ABCD",
            "--station-catalog-source",
            "cache",
            "--station-catalog-cache",
            str(cache),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "ambiguous" in data["warnings"][0]


def test_archive_probe_uses_head_and_reports_available(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("um980_rtklib_pipeline.archive_probe._url_exists", lambda _url: (True, None))
    result = probe_station_archives(
        station="TUBO00CZE",
        start=datetime(2026, 5, 23, 5, 0, tzinfo=UTC),
        end=datetime(2026, 5, 23, 5, 15, tzinfo=UTC),
        resolution="high",
        cache_dir=tmp_path,
        refresh=True,
    )
    assert result.status == "available"
    assert result.available_files == result.expected_files


def test_archive_probe_missing_high_rate_can_be_forbidden(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(
        "um980_rtklib_pipeline.archive_probe._url_exists",
        lambda url: (("_30S_" in url), None if "_30S_" in url else "HTTP 404"),
    )
    rover = tmp_path / "rover.unc"
    rover.write_text(
        _ascii_record(
            "BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
            "SOL_COMPUTED,NARROW_FLOAT,49.8,14.2,250.5,45.1,WGS84,0.1,0.1,0.2,"
            "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2"
        ),
        encoding="ascii",
    )
    rc = cli.main(
        [
            "base-candidates",
            str(rover),
            "--track-source",
            "bestnav",
            "--stations",
            "TUBO",
            "--station-catalog-source",
            "curated",
            "--radius-km",
            "500",
            "--base-resolution",
            "high",
            "--allow-resolution-fallback",
            "no",
            "--probe-archives",
            "--probe-cache-dir",
            str(tmp_path / "probes"),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    candidate = json.loads(capsys.readouterr().out)["candidates"][0]
    assert candidate["high_rate_status"] == "missing"
    assert candidate["fallback_needed"] is True
    assert "fallback is forbidden" in candidate["warnings"][0]
