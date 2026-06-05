#!/data/data/com.termux/files/usr/bin/sh
set -eu
cd "$(dirname "$0")/../.."
cc -Wall -Wextra -O2 \
  -o tools/termux/um980-usb-fd \
  tools/termux/um980-usb-fd.c \
  $(pkg-config --cflags --libs libusb-1.0)
