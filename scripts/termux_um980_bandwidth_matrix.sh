#!/data/data/com.termux/files/usr/bin/sh
set -eu

if [ "${UM980_HW_TEST:-}" != "1" ]; then
  echo "Set UM980_HW_TEST=1 to run real UM980 bandwidth hardware tests." >&2
  exit 2
fi
if [ -z "${UM980_TERMUX_DEVICE:-}" ]; then
  echo "Set UM980_TERMUX_DEVICE, for example /dev/bus/usb/002/002." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

/data/data/com.termux/files/usr/bin/sh tools/termux/build-um980-usb-fd.sh

PYTHONPATH=src python -m um980_rtklib_pipeline.capture_bandwidth \
  --termux-device "$UM980_TERMUX_DEVICE" \
  --stage "${UM980_BANDWIDTH_STAGE:-smoke}" \
  "$@"
