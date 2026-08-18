#!/usr/bin/env bash
# Compile and flash one RTI mesh node, then optionally watch its serial output.
#
#   ./scripts/flash_node.sh                 # auto-detect port and board
#   ./scripts/flash_node.sh -p /dev/ttyACM0 # explicit port
#   ./scripts/flash_node.sh -m              # flash then monitor
#
# Flash each board one at a time: plug in, run this, unplug, label it, next.
set -euo pipefail

SKETCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/firmware/esp32_rti_node"
PORT=""; MONITOR=0; FQBN=""
export PATH="$HOME/.local/bin:$PATH"

while getopts "p:b:mh" o; do
  case "$o" in
    p) PORT="$OPTARG" ;;
    b) FQBN="$OPTARG" ;;
    m) MONITOR=1 ;;
    h) sed -n '2,10p' "$0"; exit 0 ;;
  esac
done

command -v arduino-cli >/dev/null || { echo "arduino-cli not found; see docs/GETTING_STARTED.md" >&2; exit 1; }
[ -f "$SKETCH/config.h" ]   || { echo "missing $SKETCH/config.h - run scripts/setup_firmware.py" >&2; exit 1; }
[ -f "$SKETCH/mesh_key.h" ] || { echo "missing $SKETCH/mesh_key.h - run scripts/gen_mesh_key.py" >&2; exit 1; }

if [ -z "$PORT" ]; then
  PORT=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 || true)
  [ -n "$PORT" ] || { echo "no serial device found. Plug a board in." >&2; exit 1; }
  echo "port: $PORT (auto-detected)"
fi

if [ -z "$FQBN" ]; then
  # Ask the chip what it is rather than guessing: S3 and classic ESP32 need
  # different FQBNs, and a wrong one produces a board that flashes but never
  # prints anything.
  CHIP=$(python -m esptool --port "$PORT" chip-id 2>/dev/null | grep -oE "ESP32-[A-Z0-9]+" | head -1 || true)
  case "$CHIP" in
    ESP32-S3) FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc" ;;
    ESP32-C3) FQBN="esp32:esp32:esp32c3:CDCOnBoot=cdc" ;;
    ESP32-C6) FQBN="esp32:esp32:esp32c6:CDCOnBoot=cdc" ;;
    ESP32-S2) FQBN="esp32:esp32:esp32s2:CDCOnBoot=cdc" ;;
    *)        FQBN="esp32:esp32:esp32da" ;;   # classic ESP32, external bridge
  esac
  echo "chip: ${CHIP:-unknown} -> $FQBN"
fi

echo "compiling..."
arduino-cli compile --fqbn "$FQBN" "$SKETCH" | grep -E "Sketch uses|Global variables" || true

echo "uploading to $PORT..."
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH" 2>&1 | grep -E "Hash of data verified|error" || true

echo
echo "done. The node prints its MAC on boot - that is its mesh id."
echo "Add it to config/nodes.json with its measured x/y/z position."
[ "$MONITOR" -eq 1 ] && { echo; echo "--- serial (ctrl-C to exit) ---"; arduino-cli monitor -p "$PORT" -c baudrate=115200; }
