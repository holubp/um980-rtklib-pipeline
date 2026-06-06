#!/data/data/com.termux/files/usr/bin/sh
set -eu

if [ "${UM980_HW_TEST:-}" != "1" ]; then
  echo "Set UM980_HW_TEST=1 to run real UM980 hardware capture tests." >&2
  exit 2
fi
if [ -z "${UM980_TERMUX_DEVICE:-}" ]; then
  echo "Set UM980_TERMUX_DEVICE, for example /dev/bus/usb/002/002." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DURATION="${UM980_CAPTURE_DURATION:-20}"
PROFILE_DISCARD_MS="${UM980_PROFILE_DISCARD_MS:-2000}"
OUTDIR="captures/termux-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTDIR"
CASES_JSONL="$OUTDIR/hw_matrix_cases.jsonl"
: > "$CASES_JSONL"

echo "output directory: $OUTDIR"
echo "building native helper"
/data/data/com.termux/files/usr/bin/sh tools/termux/build-um980-usb-fd.sh > "$OUTDIR/00_build.log" 2>&1

HELPER="$(PYTHONPATH=src python - <<'PY'
from pathlib import Path
from um980_rtklib_pipeline.capture_termux import _helper_for_subprocess
print(_helper_for_subprocess(Path("tools/termux/um980-usb-fd")))
PY
)"

termux-usb -r "$UM980_TERMUX_DEVICE" > "$OUTDIR/00_permission.log" 2>&1 || true

echo "probing USB descriptors"
if termux-usb -e "$HELPER --probe --verbose" "$UM980_TERMUX_DEVICE" > "$OUTDIR/00_probe.log" 2>&1; then
  PROBE_STATUS=passed
else
  PROBE_STATUS=failed
fi

json_case() {
  profile="$1"
  name="$2"
  mode="$3"
  status="$4"
  capture="$5"
  log="$6"
  size="$7"
  validation="$8"
  extract="$9"
  python - "$profile" "$name" "$mode" "$status" "$capture" "$log" "$size" "$validation" "$extract" <<'PY' >> "$CASES_JSONL"
import json, sys
profile, name, mode, status, capture, log, size, validation, extract = sys.argv[1:]
print(json.dumps({
    "profile": profile,
    "case": name,
    "mode": mode,
    "status": status,
    "capture": capture,
    "suffix": ".unc" if capture.endswith(".unc") else "",
    "capture_size": int(size),
    "validation": validation,
    "extract_check": extract,
    "log": log,
}, sort_keys=True))
PY
}

run_case() {
  name="$1"
  mode="$2"
  profile="$3"
  capture="$OUTDIR/${name}.unc"
  analysis="$OUTDIR/${name}.analysis.json"
  log="$OUTDIR/${name}.log"
  if [ -n "$profile" ] && ! grep -Eq '^(# )?enabled:[[:space:]]*true' "$profile"; then
    echo "skipping disabled profile: $profile"
    json_case "$profile" "$name" "$mode" "skipped" "$capture" "$log" 0 "skipped" "skipped"
    return 0
  fi
  cmd="PYTHONPATH=src python -m um980_rtklib_pipeline.cli capture-usb --termux-device '$UM980_TERMUX_DEVICE' --duration '$DURATION' --out '$capture' --analysis-json '$analysis' --validate --extract-check --expect-mode '$mode' -v"
  if [ -n "$profile" ]; then
    cmd="$cmd --profile '$profile'"
    if [ "$name" != "01_passive_current_stream" ] && [ "$PROFILE_DISCARD_MS" -gt 0 ]; then
      cmd="$cmd --discard-after-profile-ms '$PROFILE_DISCARD_MS'"
    fi
  fi
  echo "running $name"
  if sh -c "$cmd" > "$log" 2>&1; then
    size="$(wc -c < "$capture" 2>/dev/null || echo 0)"
    json_case "$profile" "$name" "$mode" "passed" "$capture" "$log" "$size" "passed" "passed"
    return 0
  fi
  size="$(wc -c < "$capture" 2>/dev/null || echo 0)"
  json_case "$profile" "$name" "$mode" "failed" "$capture" "$log" "$size" "failed" "failed"
  return 1
}

PASSIVE_OK=0
run_case "01_passive_current_stream" "passive" "tools/um980_profiles/runtime/passive.um980" || PASSIVE_OK=1
if [ "$PASSIVE_OK" -ne 0 ]; then
  echo "passive capture failed; not sending runtime profiles" >&2
else
  run_case "02_ascii_nmea_minimal" "ascii" "tools/um980_profiles/runtime/ascii_nmea_minimal.um980" || true
  run_case "03_ascii_unicore_solution" "ascii" "tools/um980_profiles/runtime/ascii_unicore_solution.um980" || true
  run_case "04_binary_solution" "binary" "tools/um980_profiles/runtime/binary_solution.um980" || true
  run_case "05_binary_rawobs_solution" "binary" "tools/um980_profiles/runtime/binary_rawobs_solution.um980" || true
  run_case "06_mixed_nmea_binary_rawobs" "mixed" "tools/um980_profiles/runtime/mixed_nmea_binary_rawobs.um980" || true
fi

FINAL_STATUS=passed
if grep -q '"status": "failed"' "$CASES_JSONL"; then
  FINAL_STATUS=failed
fi
if [ "$PASSIVE_OK" -ne 0 ]; then
  FINAL_STATUS=failed
fi

jq -s \
  --arg device "$UM980_TERMUX_DEVICE" \
  --arg probe "$PROBE_STATUS" \
  --arg helper "$HELPER" \
  --arg final "$FINAL_STATUS" \
  '{device_path:$device, probe_result:$probe, native_helper:$helper, final_status:$final, no_persistent_config_sent:true, captures_are_git_ignored:true, cases:.}' \
  "$CASES_JSONL" > "$OUTDIR/hw_matrix_summary.json"

{
  echo "# UM980 Termux Capture Matrix"
  echo
  echo "- Device path: \`$UM980_TERMUX_DEVICE\`"
  echo "- Probe result: $PROBE_STATUS"
  echo "- Native helper: \`$HELPER\`"
  echo "- Final status: $FINAL_STATUS"
  echo "- Captures are ignored by git: yes"
  echo "- Persistent receiver configuration sent: no"
  echo
  echo "| Case | Profile | Mode | Status | Bytes | Log |"
  echo "|---|---|---|---:|---:|---|"
  jq -rs '.[] | "| \(.case) | \(.profile // "") | \(.mode) | \(.status) | \(.capture_size) | `\(.log)` |"' "$CASES_JSONL"
  echo
  echo "No SAVECONFIG or persistent receiver commands were sent. Power-cycle UM980 to restore saved output configuration if active runtime profiles were enabled."
} > "$OUTDIR/hw_matrix_summary.md"

echo "summary: $OUTDIR/hw_matrix_summary.md"
[ "$FINAL_STATUS" = "passed" ]
