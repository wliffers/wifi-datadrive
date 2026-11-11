"""Read the complete flash of the T-Dongle-S3 in chunks, with retries.

Large single read_flash calls over the ESP32-S3 USB-Serial/JTAG are unreliable
("Corrupt data, expected 0x1000 bytes"), so this script reads 256 KiB chunks,
retries failed chunks (re-connecting if needed) and verifies every chunk with
the stub's MD5 command before accepting it.

Run with the ESP-IDF Python environment (tools/backup_flash.ps1 does that):
    python tools/backup_flash.py COM4 backup/flash_backup.bin
"""
import hashlib
import struct
import sys
import time

import esptool

CHUNK = 256 * 1024
TOTAL = 16 * 1024 * 1024
RETRIES = 6


def read_flash_small(esp, offset, length, packet=1024, in_flight=2):
    """Like esptool's ESPLoader.read_flash, but with small packets and few
    unacknowledged packets so the USB-Serial/JTAG FIFO cannot overflow when
    the data contains many SLIP escape bytes (0xC0/0xDB double in size)."""
    esp.check_command(
        "read flash",
        esp.ESP_READ_FLASH,
        struct.pack("<IIII", offset, length, packet, in_flight),
    )
    data = b""
    while len(data) < length:
        p = esp.read()
        data += p
        if len(data) < length and len(p) < packet:
            raise RuntimeError(f"short packet: expected 0x{packet:x}, got 0x{len(p):x}")
        esp.write(struct.pack("<I", len(data)))
    digest = esp.read()
    if len(data) != length or digest.hex() != hashlib.md5(data).hexdigest():
        raise RuntimeError("digest mismatch")
    return data


def connect(port):
    esp = esptool.cmds.detect_chip(port, 115200, "default_reset", False, 3)
    esp = esp.run_stub()
    esp.flash_set_parameters(TOTAL)
    return esp


def main():
    port, out_path = sys.argv[1], sys.argv[2]
    esp = connect(port)
    image = bytearray()
    for offset in range(0, TOTAL, CHUNK):
        for attempt in range(1, RETRIES + 1):
            try:
                data = read_flash_small(esp, offset, CHUNK)
                if hashlib.md5(data).hexdigest() != esp.flash_md5sum(offset, CHUNK):
                    raise RuntimeError("MD5 mismatch")
                image += data
                break
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                print(f"  chunk 0x{offset:06x} attempt {attempt} failed: {exc}")
                if attempt == RETRIES:
                    raise
                time.sleep(1)
                try:
                    esp._port.close()
                except Exception:  # noqa: BLE001
                    pass
                esp = connect(port)
        print(f"0x{offset + CHUNK:07x} / 0x{TOTAL:07x}", end="\r", flush=True)
    with open(out_path, "wb") as f:
        f.write(image)
    print(f"\nwrote {len(image)} bytes to {out_path}")
    esp.hard_reset()


if __name__ == "__main__":
    main()
