#!/usr/bin/env bash
# Gera o firmware WROOM localmente e entrega os bins prontos para flash em ~/ESP32-Firmware.
# Uso: ./build-wroom.sh [--skip-build]
set -euo pipefail

REPO="$HOME/AnimatedPixelClock"
OUT="$HOME/ESP32-Firmware"
PIO_BIN="$HOME/.venvs/pio/bin"

cd "$REPO"

# O release.py reescreve arquivos versionados; nunca apagar mudanças locais não commitadas.
if [ -n "$(git status --porcelain -- docs/firmware/latest release)" ]; then
  echo "erro: há mudanças locais em docs/firmware/latest ou release/; guarde ou commite antes." >&2
  exit 1
fi

restore() { cd "$REPO" && git restore docs/firmware/latest/ release/ 2>/dev/null || true; }
# Restaura mesmo se o build falhar no meio.
trap restore EXIT

VERSION="v$(sed -n 's/.*FIRMWARE_VERSION "\([^"]*\)".*/\1/p' src/config/config.h | head -1)"
if [ -z "${VERSION#v}" ]; then
  echo "erro: não consegui ler FIRMWARE_VERSION de src/config/config.h" >&2
  exit 1
fi

PATH="$PIO_BIN:$PATH" python3 release.py --no-companion --boards wroom "$@"

mkdir -p "$OUT"
cp "release/$VERSION/firmware-$VERSION-wroom.bin" \
   "release/$VERSION/OTA_ONLY_firmware-$VERSION-wroom.bin" "$OUT"/

cd "$OUT"
sha256sum "firmware-$VERSION-wroom.bin" "OTA_ONLY_firmware-$VERSION-wroom.bin" > SHA256SUMS.txt
sha256sum -c SHA256SUMS.txt

restore
git status --short -- docs/firmware/latest release
