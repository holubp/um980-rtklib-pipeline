from pathlib import Path

from um980_rtklib_pipeline.rinex_nav import extract_rover_nav
from um980_rtklib_pipeline.stream import parse_stream


def test_gpsepha_does_not_write_empty_nav_file(tmp_path: Path):
    nav_path = tmp_path / "rover.nav"
    records, _ = parse_stream(b"#GPSEPHA,COM1,0,0;dummy*00000000\r\n")
    report = extract_rover_nav(records, nav_path)
    assert report.found["GPSEPHA"] == 1
    assert report.converted["GPSEPHA"] == 0
    assert not nav_path.exists()
    assert any("no rover NAV file was written" in warning for warning in report.warnings)

