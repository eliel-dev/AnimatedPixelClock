#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TWIN_HOME="${TWIN_HOME:-$HOME/.local/share/animatedpixelclock-twin}"
ENGINE="${TWIN_ENGINE:-$TWIN_HOME/esp32sim}"
STATE="${TWIN_STATE:-$TWIN_HOME/state}"
ROM_DIR="${TWIN_ROM_DIR:-$TWIN_HOME/rom}"
SSID="${TWIN_WIFI_SSID:-PixelClock-TWIN}"
PASSWORD="${TWIN_WIFI_PASSWORD:-}"
WEB_PORT="${TWIN_WEB_PORT:-8790}"
PIO="${PIO:-}"

if [[ -f "$HOME/.cargo/env" ]]; then
    source "$HOME/.cargo/env"
fi

fresh=0
case "${1:-}" in
    --fresh)
        fresh=1
        shift
        ;;
    "")
        ;;
    *)
        echo "usage: $0 [--fresh]" >&2
        exit 2
        ;;
esac
if (($#)); then
    echo "usage: $0 [--fresh]" >&2
    exit 2
fi

if [[ "$SSID" == *,* || "$PASSWORD" == *,* ]]; then
    echo "TWIN Wi-Fi SSID/password cannot contain commas." >&2
    exit 2
fi
if ! command -v cargo >/dev/null 2>&1; then
    echo "Rust/Cargo is required. Install it with rustup: https://rustup.rs/" >&2
    exit 1
fi

if [[ -z "$PIO" ]]; then
    for candidate in "$HOME/.venvs/pio/bin/pio" "$HOME/.platformio/penv/bin/pio"; do
        if [[ -x "$candidate" ]]; then
            PIO="$candidate"
            break
        fi
    done
fi
if [[ -z "$PIO" ]] && command -v pio >/dev/null 2>&1; then
    PIO="$(command -v pio)"
fi
if [[ -z "$PIO" ]]; then
    echo "PlatformIO Core is required (or set PIO=/path/to/pio)." >&2
    exit 1
fi

if [[ ! -d "$ENGINE/.git" ]]; then
    mkdir -p "$(dirname "$ENGINE")"
    git clone --depth 1 --branch nickoscope/twin \
        https://github.com/NickoScope/TWIN-NickoScopeMatrix-64x128.git "$ENGINE"
fi
if [[ ! -x "$ENGINE/target/release/esp32sim" ]]; then
    (cd "$ENGINE" && cargo build --release)
fi

mkdir -p "$ROM_DIR" "$STATE"
ROM="$ROM_DIR/esp32s3_rev0_rom.elf"
if [[ ! -f "$ROM" ]]; then
    if [[ -n "${ESP32SIM_ROM_DIR:-}" && -f "$ESP32SIM_ROM_DIR/esp32s3_rev0_rom.elf" ]]; then
        cp "$ESP32SIM_ROM_DIR/esp32s3_rev0_rom.elf" "$ROM"
    else
        "$ENGINE/tools/fetch-demo-assets.sh" --no-linux
        cp "$ENGINE/web/wasm/fw/esp32s3_rev0_rom.elf" "$ROM"
    fi
fi

exec 9>"$STATE/run.lock"
if ! flock -n 9; then
    echo "Another TWIN process is already using $STATE/flash.bin." >&2
    exit 1
fi

"$PIO" run -e matrix-s3-wroom-twin -d "$ROOT"
BUILD="$ROOT/.pio/build/matrix-s3-wroom-twin"
FRAMEWORK="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}/packages/framework-arduinoespressif32"
BOOT_APP0="$FRAMEWORK/tools/partitions/boot_app0.bin"
if [[ ! -f "$BOOT_APP0" ]]; then
    echo "Missing PlatformIO boot_app0.bin: $BOOT_APP0" >&2
    exit 1
fi

FLASH="$STATE/flash.bin"
if ((fresh)); then
    if [[ -f "$FLASH" ]]; then
        printf 'Erase only the TWIN simulator state at %s? [y/N] ' "$FLASH" >&2
        read -r answer
        if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
            echo "Keeping existing TWIN state." >&2
            exit 1
        fi
        rm -- "$FLASH"
    fi
fi

python3 "$ROOT/tools/twin-wsl2/flash_update.py" \
    --flash "$FLASH" \
    --bootloader "$BUILD/bootloader.bin" \
    --partitions "$BUILD/partitions.bin" \
    --boot-app0 "$BOOT_APP0" \
    --firmware "$BUILD/firmware.bin"

IMPROV_HEX="$(python3 "$ROOT/tools/twin-wsl2/improv_packet.py" "$SSID" "$PASSWORD")"

exec "$ENGINE/target/release/esp32sim" \
    --board hub75-panel \
    --boot rom \
    --console usb \
    --rom "$ROM" \
    --flash-mb 16 \
    --psram-mb 8 \
    --bootloader "$BUILD/bootloader.bin" \
    --ptable "$BUILD/partitions.bin" \
    --app "$BUILD/firmware.bin" \
    --elf "$BUILD/firmware.elf" \
    --flash-persist "$FLASH" \
    --wifi "ssid=$SSID${PASSWORD:+,psk=$PASSWORD}" \
    --hostfwd tcp:8080-80 \
    --hostfwd udp:4210-4210 \
    --web "$WEB_PORT" \
    --serial-hex "$IMPROV_HEX" \
    "$@"
