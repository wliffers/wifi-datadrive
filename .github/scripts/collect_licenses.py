#!/usr/bin/env python3
"""Collect the licence texts that must accompany binary releases of the firmware.

    python .github/scripts/collect_licenses.py <IDF_PATH> <project dir> <output dir>

Copies the project licence, THIRD_PARTY_NOTICES.md, the licence files kept next to vendored code
in firmware/components, the LICENSE of every managed component of the project and the licence
files (or licence headers) of the ESP-IDF parts listed in THIRD_PARTY_NOTICES.md section 3.
A missing ESP-IDF file is reported and makes the script exit with 1, so a changed ESP-IDF layout
is noticed instead of shipping an incomplete bundle.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# (target name, path relative to IDF_PATH, number of header lines to keep or None for the whole file)
IDF_FILES = [
    ("esp-idf_LICENSE.txt", "LICENSE", None),
    ("esp-idf_COPYRIGHT.rst", "docs/en/COPYRIGHT.rst", None),
    ("esp_wifi_lib_LICENSE.txt", "components/esp_wifi/lib/LICENSE", None),
    ("esp_phy_lib_LICENSE.txt", "components/esp_phy/lib/LICENSE", None),
    ("esp_coex_lib_LICENSE.txt", "components/esp_coex/lib/LICENSE", None),
    ("mbedtls_LICENSE.txt", "components/mbedtls/mbedtls/LICENSE", None),
    ("freertos_LICENSE.md", "components/freertos/FreeRTOS-Kernel/LICENSE.md", None),
    ("lwip_COPYING.txt", "components/lwip/lwip/COPYING", None),
    ("wpa_supplicant_COPYING.txt", "components/wpa_supplicant/COPYING", None),
    ("newlib_COPYING.NEWLIB.txt", "components/newlib/COPYING.NEWLIB", None),
    ("http_parser_LICENSE.txt", "components/http_parser/LICENSE.txt", None),
    ("argtable3_LICENSE.txt", "components/console/argtable3/LICENSE", None),
    ("linenoise_LICENSE.txt", "components/console/linenoise/LICENSE", None),
    ("fatfs_ff.h_header.txt", "components/fatfs/src/ff.h", 19),
    ("sdmmc_sd_protocol_defs.h_header.txt", "components/sdmmc/include/sd_protocol_defs.h", 22),
    ("tlsf_tlsf.c_header.txt", "components/heap/tlsf/tlsf.c", 5),
]


def copy(src: Path, dst: Path, head: int | None) -> None:
    if head is None:
        shutil.copyfile(src, dst)
    else:
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[:head]
        dst.write_text("".join(lines), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    idf = Path(sys.argv[1])
    project = Path(sys.argv[2])
    out = Path(sys.argv[3])
    repo = Path(__file__).resolve().parents[2]
    out.mkdir(parents=True, exist_ok=True)
    missing = []

    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(repo / name, out / name)

    for lic in sorted((repo / "firmware" / "components").glob("*/LICENSE*")):
        shutil.copyfile(lic, out / f"{lic.parent.name}_{lic.name}.txt")

    managed = project / "managed_components"
    for comp in sorted(p for p in managed.glob("*") if p.is_dir()):
        lic = comp / "LICENSE"
        if lic.is_file():
            shutil.copyfile(lic, out / f"{comp.name}_LICENSE.txt")
        else:
            missing.append(str(lic))
    qr = managed / "espressif__qrcode" / "qrcodegen.c"
    if qr.is_file():
        copy(qr, out / "espressif__qrcode_qrcodegen.c_header.txt", 25)

    for name, rel, head in IDF_FILES:
        src = idf / rel
        if src.is_file():
            copy(src, out / name, head)
        else:
            missing.append(str(src))

    for m in missing:
        print(f"missing: {m}", file=sys.stderr)
    print(f"{len(list(out.iterdir()))} files in {out}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
