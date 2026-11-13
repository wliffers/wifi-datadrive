#!/usr/bin/env python3
"""Build the merged flash image (flash at offset 0x0) from an ESP-IDF build folder.

Reads the offsets and flash settings from <build>/flasher_args.json, which ESP-IDF writes
from the real partition table, and calls esptool's merge_bin. Nothing is hard-coded except
the chip. Run inside the ESP-IDF environment (esptool importable).

    python .github/scripts/merge_image.py firmware/printdrive/build out/merged.bin

Note: merge_bin fills the gaps between the parts with 0xFF. The image therefore also covers the
NVS partition (0x9000 in firmware/partitions.csv): flashing it at 0x0 resets the stored settings
(users, Wi-Fi, HTTPS keys). Files on the microSD card and in the internal FAT partition are
outside the image.
"""
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    build = Path(sys.argv[1])
    out = Path(sys.argv[2])
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))

    settings = args["flash_settings"]
    chip = args.get("extra_esptool_args", {}).get("chip", "esp32s3")
    files = sorted(args["flash_files"].items(), key=lambda kv: int(kv[0], 16))

    cmd = [sys.executable, "-m", "esptool", "--chip", chip, "merge_bin", "-o", str(out),
           "--flash_mode", settings["flash_mode"],
           "--flash_freq", settings["flash_freq"],
           "--flash_size", settings["flash_size"]]
    for offset, rel in files:
        part = build / rel
        if not part.is_file():
            print(f"missing part {part}", file=sys.stderr)
            return 1
        cmd += [offset, str(part)]
        print(f"  {offset:>8}  {rel}")

    out.parent.mkdir(parents=True, exist_ok=True)
    print(" ".join(cmd), flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
