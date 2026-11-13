#!/usr/bin/env python3
"""Initialize or update TWIN's flash while preserving NVS and LittleFS."""
import argparse
import os
from pathlib import Path
import struct
import tempfile

FLASH_SIZE = 16 * 1024 * 1024
PARTITIONS_OFFSET = 0x8000
OTA_DATA_OFFSET = 0xE000
APP0_OFFSET = 0x10000


def read_image(path: Path) -> bytes:
    data = path.read_bytes()
    if not data:
        raise ValueError(f"{path} is empty")
    return data


def write_region(flash: bytearray, offset: int, path: Path) -> None:
    data = read_image(path)
    if offset + len(data) > len(flash):
        raise ValueError(f"{path} does not fit in the 16 MB flash image")
    flash[offset:offset + len(data)] = data


def app0_bounds(partitions: bytes) -> tuple[int, int]:
    for offset in range(0, min(len(partitions), 0xC00), 32):
        entry = partitions[offset:offset + 32]
        if entry[:2] != b"\xaa\x50":
            break
        if entry[2] == 0 and entry[3] == 0x10:
            return struct.unpack_from("<II", entry, 4)
    raise ValueError("the partition table has no app0 partition")


def update_flash(args: argparse.Namespace) -> None:
    flash_path = args.flash
    if flash_path.exists():
        flash = bytearray(flash_path.read_bytes())
        if len(flash) != FLASH_SIZE:
            raise ValueError(f"{flash_path} must be exactly {FLASH_SIZE} bytes")
    else:
        flash = bytearray(b"\xff") * FLASH_SIZE

    partition_data = read_image(args.partitions)
    app0_offset, app0_size = app0_bounds(partition_data)
    if app0_offset != APP0_OFFSET:
        raise ValueError("app0 must start at 0x10000")
    if args.firmware.stat().st_size > app0_size:
        raise ValueError(f"{args.firmware} exceeds the app0 partition size of {app0_size} bytes")

    write_region(flash, 0, args.bootloader)
    write_region(flash, PARTITIONS_OFFSET, args.partitions)
    write_region(flash, OTA_DATA_OFFSET, args.boot_app0)
    write_region(flash, APP0_OFFSET, args.firmware)

    flash_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{flash_path.name}.", dir=flash_path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(flash)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, flash_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flash", type=Path, required=True)
    parser.add_argument("--bootloader", type=Path, required=True)
    parser.add_argument("--partitions", type=Path, required=True)
    parser.add_argument("--boot-app0", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    args = parser.parse_args()
    try:
        update_flash(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
