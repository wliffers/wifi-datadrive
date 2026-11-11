#!/usr/bin/env python3
"""Mock server for the "WiFi DataDrive" web UI (MOCK ONLY - no real device involved).

Serves the single-file web UI and implements the HTTP API of docs/07_api.md with an
in-memory file store, so every state of the UI can be exercised on the PC without the
dongle. Python 3.10+, standard library only.

Usage
-----
    python tools/mock_server.py [--port 8080] [--bind 127.0.0.1]
                                [--html firmware/printdrive/main/web/index.html]
                                [--scenario NAME] [--upload-kbps 400]

Then open http://127.0.0.1:8080/ . The HTML file is re-read from disk on every request
(edit, save, reload). Relative paths are resolved against the repository root, the
current directory does not matter. `--bind 0.0.0.0` makes the mock reachable from a
phone in the same network. Stop with Ctrl+C.

Examples
--------
    python tools/mock_server.py
    python tools/mock_server.py --scenario files --upload-kbps 150
    python tools/mock_server.py --port 8099 --scenario ap_setup --bind 0.0.0.0
    curl "http://127.0.0.1:8080/mock/set?host_writing=hold&delay_ms=300"
    curl "http://127.0.0.1:8080/mock/scenario?name=full"

Behaviour mirrored from the firmware
------------------------------------
* One request at a time: a running upload blocks every other request (also status polls).
* Every POST needs the header `X-PrintDrive: 1`, otherwise 403 FORBIDDEN.
* Owner state machine app/usb with HOST_ACTIVE / HOST_WRITING / BUSY, file operations
  only while the owner is `app`, name/type/space/content checks of uploads, German
  error texts, restart after format/reboot (the mock is unreachable for about 4 s; in
  download mode until `/mock/set?online=1`).
* Uploads are read at a limited rate (default 400 KB/s) so progress bars are visible.
* Uploaded content is kept in memory (files above 64 MiB: only the first 64 KiB are
  kept, the reported size stays). Scenario files report a fake `size`; a download
  returns the few bytes that are really stored.
* Not mirrored: the reported IP is 192.0.2.152 and the hostname `datadrive-mock`, so
  nobody ends up on a real dongle by following a link shown in the UI.

Scenarios (`--scenario NAME` or `GET /mock/scenario?name=NAME`)
---------------------------------------------------------------
    usb_active    default: drive provided to a connected USB host, a few files
    usb_no_host   drive provided to USB, but no host connected (take-over needs no force)
    files         owner app, 14 varied files (bytes to hundreds of MB), 2 hidden folders
    empty         owner app, no files
    full          owner app, almost no free space (180 KiB)
    no_fs         no readable file system (needs_format), microSD
    ap_setup      Wi-Fi not configured, setup access point active
    flash_medium  internal flash (10.8 MB) instead of the microSD card
    many          owner app, 300 files, list not truncated
    truncated     owner app, 1200 files, list truncated (32 KiB list buffer like the firmware)
A scenario switch resets all mock settings below (uptime keeps running).

Control endpoints (GET, no CSRF header, exist only in the mock)
---------------------------------------------------------------
    /mock                 short help (JSON)
    /mock/state           internal state incl. the current /api/status answer
    /mock/scenario?name=  switch scenario (without name: list of scenarios)
    /mock/set?key=value&...   change state; several keys per call are allowed:

    owner=app|usb|none      set the owner directly (no checks)
    usb_connected=0|1       host plugged in. Like the device: unplugging while owner=usb
                            switches to app, plugging in while owner=app provides the
                            drive - unless `owner=` is given in the same call
    usb_suspended=0|1       status flag only
    host_ejected=0|1        host ejected the drive (owner=usb switches to app, see above)
    host_writable=0|1       host may write (takes effect at once, unlike the API setting)
    host_writing=1|hold|0   1: host wrote "now" (HOST_WRITING for write_guard_ms);
                            hold: keeps writing until host_writing=0
    host_reading=1          host read "now"
    fs_ok=0|1, needs_format=0|1
    sd=0|1                  medium: microSD (62.5 GB) or internal flash (10.8 MB)
    total=<bytes>           size of the medium (suffix K/M/G allowed)
    fill=<bytes>            extra used space that belongs to no file (suffix K/M/G)
    types=all|.3mf|.3mf,.gcode   allowed upload types (BAD_TYPE for everything else)
    dirs=<n>                number of hidden folders reported by /api/files
    wifi=sta|ap|connecting
    upload_kbps=<n>         upload rate limit in KiB/s (0 = unlimited)
    delay_ms=<n>            artificial latency of every /api request
    online=0|1              0: connections are accepted and reset without an answer
    fail_next=<CODE>        the next POST fails with that error code (proper HTTP status
                            and German text). `CODE@/api/upload` limits it to one path,
                            e.g. to let /api/upload/check pass first. `none` clears it.
    upload_fail_at=<pct>    the next upload connection is reset at that percentage
    other_upload=0|1        status shows an upload by another client; mode, upload,
                            rename, delete, format and reboot answer BUSY
    reset=1                 back to the start of the current scenario
"""
from __future__ import annotations

import argparse
import calendar
import collections
import io
import json
import math
import random
import re
import select
import signal
import socket
import socketserver
import struct
import sys
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote_to_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = "firmware/printdrive/main/web/index.html"

VERSION = "0.2.0"
CSRF_HEADER = "X-PrintDrive"
NAME_MAX = 120                      # DRIVE_NAME_MAX: bytes (UTF-8) of an uploaded/renamed name
FILE_NAME_MAX = 255                 # DRIVE_FILE_NAME_MAX: existing (host-created) names
FREE_RESERVE = 64 * 1024            # FREE_RESERVE_BYTES
TAIL_SIZE = 1024                    # bytes checked for the ZIP end-of-central-directory record
MTIME_MIN = 347155200               # 1981-01-01, older stamps mean "clock was not set"
LIST_BUF_SIZE = 32 * 1024           # packed file list of the firmware: 8 bytes + name + NUL each
WRITE_GUARD_MS = 3000               # CONFIG_PD_HOST_WRITE_GUARD_MS
LOG_BYTES = 8192                    # /api/log returns the last 8 KB
RESTART_S = 4.0                     # simulated restart time after format/reboot
STORE_MAX = 64 * 1024 * 1024        # larger uploads are not kept in memory completely
STORE_HEAD = 64 * 1024              # ... only this many bytes
SD_TOTAL = 62_525_505_536
FLASH_TOTAL = 11_288_576            # shown as 10.8 MB
FAKE_IP = "192.0.2.152"             # TEST-NET-1: never a real device
SOCKET_TIMEOUT_S = 20

# code -> (HTTP status, German text). Texts are those of drive_result_str() in drive.c.
ERRORS = {
    "BAD_NAME": (400, "Ungültiger Dateiname."),
    "BAD_REQUEST": (400, "Parameter fehlt oder ist ungültig"),
    "INCOMPLETE": (400, "Die Übertragung war unvollständig."),
    "FORBIDDEN": (403, "Anfrage nicht erlaubt."),
    "NOT_FOUND": (404, "Die Datei wurde nicht gefunden."),
    "BUSY": (409, "Es läuft gerade eine Dateiübertragung. Bitte warten, bis sie abgeschlossen ist."),
    "HOST_ACTIVE": (409, "Das USB-Gerät verwendet das Laufwerk noch. Bitte dort zuerst auswerfen oder das Trennen bestätigen."
                         "Umschalten bestätigen."),
    "HOST_WRITING": (409, "Das USB-Gerät schreibt gerade auf das Laufwerk. Bitte warten, bis es fertig ist."),
    "WRONG_OWNER": (409, "Das geht nur, solange die Dateiverwaltung aktiv ist."),
    "NO_FS": (409, "Der Speicher ist nicht lesbar und muss formatiert werden."),
    "EXISTS": (409, "Eine Datei mit diesem Namen gibt es bereits."),
    "TOO_LARGE": (413, "Die Datei ist zu groß – möglich sind höchstens 4 GB pro Datei."),
    "BAD_TYPE": (415, "Dieser Dateityp ist nicht erlaubt."),
    "INVALID_FILE": (422, "Die Datei ist unvollständig oder beschädigt (keine gültige 3MF-Datei)."),
    "IO": (500, "Lese- oder Schreibfehler auf dem Speicher."),
    "NO_SPACE": (507, "Nicht genug freier Speicher."),
}
# With fail_next on /api/upload these are reported after the body was received (like a
# failed final check on the device), all others right away.
LATE_UPLOAD_ERRORS = {"INVALID_FILE", "INCOMPLETE", "IO"}

SCENARIO_HELP = {
    "usb_active": "default: drive provided to a connected USB host, a few files",
    "usb_no_host": "drive provided to USB, but no host connected",
    "files": "owner app, 14 varied files, 2 hidden folders",
    "empty": "owner app, no files",
    "full": "owner app, almost no free space",
    "no_fs": "no readable file system (needs_format)",
    "ap_setup": "Wi-Fi not configured, setup access point active",
    "flash_medium": "internal flash (10.8 MB) instead of microSD",
    "many": "owner app, 300 files, not truncated",
    "truncated": "owner app, 1200 files, list truncated",
}

SET_HELP = {
    "owner": "app|usb|none",
    "usb_connected": "0|1",
    "usb_suspended": "0|1",
    "host_ejected": "0|1",
    "host_writable": "0|1",
    "host_writing": "1|hold|0",
    "host_reading": "1",
    "fs_ok": "0|1",
    "needs_format": "0|1",
    "sd": "0|1 (medium: microSD or internal flash)",
    "total": "bytes (suffix K/M/G allowed)",
    "fill": "bytes of extra used space (suffix K/M/G allowed)",
    "types": "all | .3mf | .3mf,.gcode",
    "dirs": "number of hidden folders",
    "wifi": "sta|ap|connecting",
    "upload_kbps": "KiB/s, 0 = unlimited",
    "delay_ms": "latency per /api request",
    "online": "0|1",
    "fail_next": "error CODE for the next POST, optional CODE@/api/path, 'none' clears",
    "upload_fail_at": "percent at which the next upload connection is reset, 'none' clears",
    "other_upload": "0|1",
    "reset": "1",
}


class ClientGone(Exception):
    """The client closed the connection before the request body was complete."""


# ============================================================== helpers

def local_now() -> int:
    """Seconds since 1970 in local time (the FAT convention of the contract)."""
    return calendar.timegm(time.localtime())


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types/>')
        z.writestr("3D/3dmodel.model", '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter"/>')
    return buf.getvalue()


ZIP_BYTES = make_zip()
SAMPLE_BYTES = {
    ".3mf": ZIP_BYTES,
    ".zip": ZIP_BYTES,
    ".pdf": b"%PDF-1.4\n% WiFi DataDrive mock file\n%%EOF\n",
    ".jpg": bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9"),
    ".gcode": b"; WiFi DataDrive mock file\nG28\nG1 X10 Y10 F3000\nM84\n",
    ".gco": b"; WiFi DataDrive mock file\nG28\nM84\n",
    ".txt": "Notizen zum Druck:\n- Düse 0,4 mm\n- PETG, 240 °C\n".encode("utf-8"),
}


def sample_bytes(name: str) -> bytes:
    dot = name.rfind(".")
    return SAMPLE_BYTES.get(name[dot:].lower() if dot >= 0 else "", b"WiFi DataDrive mock file\n")


_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def parse_query(qs: str) -> dict:
    """key -> decoded value. The value is None if it cannot be decoded (bad %XX or UTF-8).
    Like the firmware: '+' is a space, the first occurrence of a key wins."""
    out = {}
    for part in qs.split("&"):
        if not part:
            continue
        key, _, val = part.partition("=")
        if key in out:
            continue
        if _BAD_PERCENT.search(val):
            out[key] = None
            continue
        try:
            out[key] = unquote_to_bytes(val.replace("+", " ").encode("latin-1", "replace")).decode("utf-8")
        except UnicodeDecodeError:
            out[key] = None
    return out


def flag(q: dict, key: str) -> bool:
    return q.get(key) in ("1", "true")


def leading_uint(s) -> int:
    """strtoul-like: leading decimal digits, otherwise 0."""
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0


def has_suffix_ci(name: str, suffix: str) -> bool:
    return len(name) > len(suffix) and name.lower().endswith(suffix.lower())


def check_name(name, types: str = "all"):
    """drive_check_name(): returns None or an error code."""
    if name is None:
        return "BAD_NAME"
    nbytes = len(name.encode("utf-8", "surrogatepass"))
    if nbytes == 0 or nbytes > NAME_MAX or name[0] in ". " or name[-1] in ". ":
        return "BAD_NAME"
    for ch in name:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch in '/\\:*?"<>|':
            return "BAD_NAME"
    if types != "all" and not any(has_suffix_ci(name, ext) for ext in types.split(",")):
        return "BAD_TYPE"
    return None


def check_existing_name(name):
    """Name rule for delete/download/rename source (also host-created names)."""
    if name is None or name == "" or name[0] == "." or "/" in name or "\\" in name or "\x00" in name:
        return "BAD_NAME"
    if len(name.encode("utf-8", "surrogatepass")) > FILE_NAME_MAX:
        return "BAD_NAME"
    return None


def parse_bool(v: str) -> bool:
    lv = (v or "").strip().lower()
    if lv in ("1", "true", "on", "yes"):
        return True
    if lv in ("0", "false", "off", "no"):
        return False
    raise ValueError(f"'{v}' is not 0 or 1")


def parse_size(v: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", (v or "").lower())
    if not m:
        raise ValueError(f"'{v}' is not a size (bytes, optional suffix K/M/G)")
    return int(float(m.group(1)) * 1024 ** " kmgt".index(m.group(2) or " "))


def parse_nonneg(v: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"'{v}' is not a number") from None
    if not (0 <= x < 1e12):
        raise ValueError(f"'{v}' is out of range")
    return x


def is_cleared(v: str) -> bool:
    return (v or "").strip().lower() in ("", "none", "off", "-1")


# ============================================================== simulated device

class MockFile:
    __slots__ = ("name", "data", "size", "mtime")

    def __init__(self, name: str, data: bytes, size: int, mtime: int):
        self.name = name
        self.data = data      # bytes really kept in memory
        self.size = size      # size reported by the API (may be larger than len(data))
        self.mtime = mtime


class Device:
    def __init__(self, upload_kbps: int, scenario: str = "usb_active"):
        self.default_upload_kbps = upload_kbps
        self.boot_t = time.monotonic()
        self.scenario = ""
        self._log = collections.deque(maxlen=600)
        self._log_ms = 0
        self.apply_scenario(scenario)

    # ---------------------------------------------------------- scenarios

    def _base(self):
        """Defaults of every scenario: drive provided to a connected host, empty microSD."""
        now = time.monotonic()
        self.owner = "usb"
        self.fs_ok = True
        self.needs_format = False
        self.usb_connected = True
        self.usb_suspended = False
        self.host_ejected = False
        self.host_writable = True         # the setting (NVS)
        self.host_write_applied = True    # as applied the last time the drive was provided
        self.last_read = now - 5.2        # monotonic time of the last host read / write
        self.last_write = None
        self.write_hold = False
        self.sd = True
        self.medium_name = "SDABC"
        self.total = SD_TOTAL
        self.sector = 512
        self.fill = 0
        self.files = {}                   # name.lower() -> MockFile (FAT ignores case)
        self.dirs = 0
        self.types = "all"
        self.wifi_mode = "sta"
        self.ssid = "MeinWLAN"
        self.wifi_connect_at = None
        self.clock_offset = None          # set by the firmware's undocumented upload param `now`
        # mock controls
        self.online = True
        self.pending_boot = False
        self.offline_until = None
        self.delay_ms = 0
        self.fail_next = None             # (code, path or None)
        self.upload_fail_at = None        # percent
        self.other_upload = None          # monotonic start time
        self.upload_kbps = self.default_upload_kbps

    def add_file(self, name: str, size=None, age_s: float = 3600, mtime=None, data=None):
        data = sample_bytes(name) if data is None else data
        if mtime is None:
            mtime = local_now() - int(age_s)
        self.files[name.lower()] = MockFile(name, data, len(data) if size is None else size, mtime)

    def _few_files(self):
        self.add_file("Benchy.3mf", 4_312_876, 2 * 3600)
        self.add_file("Würfel.3mf", 524_288, 26 * 3600)
        self.add_file("Halterung Kabelkanal v2.3mf", 812_300, 9 * 86400)

    def _varied_files(self):
        self._few_files()
        self.add_file("Getriebe_Gehäuse.gcode", 38_417_920, 40 * 60)
        self.add_file("Anleitung Drucker.pdf", 2_483_201, 200 * 86400)
        self.add_file("Foto Druckbett.jpg", 3_145_728, 3 * 86400 + 4000)
        self.add_file("Projektarchiv 2026.zip", 412_316_860, 31 * 86400)
        self.add_file("notizen.txt", None, 5 * 60)
        self.add_file("Überlanger Dateiname für die Größenprüfung der Oberfläche, "
                      "Lüfterhaube mit Düsenkühlung, Maße geändert v7.3mf", 15_728_640, 86400 + 1234)
        self.add_file("LEER.TXT", None, 12 * 86400, data=b"")
        self.add_file("firmware_backup.bin", 16_777_216, 60 * 86400)
        self.add_file("Video Zeitraffer.mp4", 817_889_280, 2 * 86400)
        self.add_file("README", 1_234, mtime=0)
        self.add_file("KURZNAME.GCO", 1_953_125, 400 * 86400)

    def _numbered_files(self, count: int, prefix: str):
        rnd = random.Random(count)
        exts = [".3mf", ".3mf", ".3mf", ".gcode", ".stl", ".txt"]
        for i in range(1, count + 1):
            self.add_file(f"{prefix}{i:04d}{exts[i % len(exts)]}", rnd.randint(200, 30_000_000),
                          rnd.randint(60, 300 * 86400))

    def _as_app(self):
        self.owner = "app"
        self.last_read = None

    def _sc_usb_active(self):
        self._few_files()

    def _sc_usb_no_host(self):
        self._few_files()
        self.usb_connected = False
        self.last_read = None

    def _sc_files(self):
        self._as_app()
        self._varied_files()
        self.dirs = 2

    def _sc_empty(self):
        self._as_app()

    def _sc_full(self):
        self._as_app()
        self._varied_files()
        self.fill = self.total - self.used_by_files() - 180 * 1024

    def _sc_no_fs(self):
        self._as_app()
        self.fs_ok = False
        self.needs_format = True

    def _sc_ap_setup(self):
        self._few_files()
        self.wifi_mode = "ap"
        self.ssid = ""

    def _sc_flash_medium(self):
        self._as_app()
        self._set_medium(False)
        self.add_file("Würfel.3mf", 524_288, 26 * 3600)
        self.add_file("Kabelclip 4x.3mf", 1_253_376, 3 * 86400)

    def _sc_many(self):
        self._as_app()
        self._numbered_files(300, "Teil_")

    def _sc_truncated(self):
        self._as_app()
        self._numbered_files(1200, "Serie_A_Bauteil_")
        self.dirs = 3

    def apply_scenario(self, name: str):
        fn = getattr(self, "_sc_" + name, None)
        if name not in SCENARIO_HELP or fn is None:
            raise ValueError(f"unknown scenario '{name}'")
        self._base()
        fn()
        self.scenario = name
        self._boot_log()

    def _set_medium(self, sd: bool):
        self.sd = sd
        self.medium_name = "SDABC" if sd else "Flash"
        self.total = SD_TOTAL if sd else FLASH_TOTAL
        self.sector = 512

    # ---------------------------------------------------------- device log

    def log(self, level: str, tag: str, msg: str):
        # Never earlier than the (synthetic) boot messages before it.
        self._log_ms = max(int((time.monotonic() - self.boot_t) * 1000), self._log_ms + 1)
        self._log.append(f"{level} ({self._log_ms}) {tag}: {msg}")

    def _boot_log(self):
        self._log.clear()
        boot = [
            ("I", "main", f"WiFi DataDrive {VERSION}"),
            ("I", "board", "T-Dongle-S3, LCD 80x160, LED ok"),
        ]
        if self.sd:
            boot.append(("I", "drive", f"microSD card '{self.medium_name}': {self.total // (1024 * 1024)} MB, "
                                       "40000 kHz, 4-bit"))
        else:
            boot.append(("I", "drive", "no usable microSD card (ESP_ERR_TIMEOUT) - using internal flash"))
        if self.needs_format:
            boot.append(("W", "drive", "no valid FAT found (13)"))
        else:
            boot.append(("I", "drive", f"FAT ok (type {3 if self.sd else 1}: 1=FAT12 2=FAT16 3=FAT32, "
                                       "label 'DATADRIVE')"))
        if self.wifi_mode == "ap":
            boot.append(("I", "wifi", "started, station (not configured)"))
            boot.append(("W", "wifi", "setup access point on: SSID 'DataDrive-54D5' (password on the LCD), "
                                      "http://192.168.4.1"))
        else:
            boot.append(("I", "wifi", f"started, station {self.ssid}"))
            boot.append(("I", "wifi", f"connecting to '{self.ssid}' ..."))
            if self.wifi_mode == "sta":
                boot.append(("I", "wifi", f"connected to '{self.ssid}', IP {FAKE_IP}"))
        boot.append(("I", "wifi", "mDNS: http://datadrive-mock.local"))
        boot.append(("I", "web", "web server started on port 80"))
        if self.owner == "usb":
            if self.usb_connected:
                boot.append(("I", "drive", "host present at boot -> providing drive"))
            boot.append(("I", "drive", f"owner: USB host ({'read/write' if self.host_writable else 'read-only'})"))
        else:
            boot.append(("I", "drive", "no USB host at boot -> staying in upload mode"))
        boot.append(("I", "main", "start-up complete"))
        boot.append(("I", "mock", f"MOCK SERVER - scenario '{self.scenario}'"))
        for i, (level, tag, msg) in enumerate(boot):
            self._log_ms = 312 + i * 187
            self._log.append(f"{level} ({self._log_ms}) {tag}: {msg}")

    def log_text(self) -> bytes:
        raw = ("\n".join(self._log) + "\n").encode("utf-8")[-LOG_BYTES:]
        while raw and (raw[0] & 0xC0) == 0x80:     # do not start in the middle of a character
            raw = raw[1:]
        return raw

    # ---------------------------------------------------------- time dependent state

    def tick(self):
        now = time.monotonic()
        if self.pending_boot and self.offline_until is not None and now >= self.offline_until:
            self._boot(self.offline_until)
        if self.wifi_connect_at is not None and now >= self.wifi_connect_at:
            self.wifi_connect_at = None
            self.wifi_mode = "sta"
            self.log("I", "wifi", f"connected to '{self.ssid}', IP {FAKE_IP}")

    def is_offline(self) -> bool:
        return not self.online or self.pending_boot

    def restart(self, download: bool = False):
        """The answer has been sent; the device is now gone for a while."""
        self.pending_boot = True
        self.offline_until = math.inf if download else time.monotonic() + RESTART_S

    def _boot(self, at: float):
        self.pending_boot = False
        self.offline_until = None
        self.boot_t = at
        self.other_upload = None
        self.clock_offset = None
        self.host_ejected = False
        self.last_read = self.last_write = None
        if self.needs_format:
            self.owner, self.fs_ok = "app", False
        elif self.fs_ok and self.usb_connected:
            self._provide()                     # boot policy: host present -> provide the drive
        else:
            self.owner = "app"
        self._boot_log()

    # ---------------------------------------------------------- drive state

    def used_by_files(self) -> int:
        return sum(f.size for f in self.files.values())

    def free(self) -> int:
        return max(0, self.total - self.fill - self.used_by_files())

    def visible_files(self):
        return [f for f in self.files.values() if not f.name.startswith(".")]

    def write_idle_ms(self) -> int:
        if self.write_hold:
            return 0
        if self.last_write is None:
            return -1
        return max(0, int((time.monotonic() - self.last_write) * 1000))

    def read_idle_ms(self) -> int:
        if self.last_read is None:
            return -1
        return max(0, int((time.monotonic() - self.last_read) * 1000))

    def host_write_enabled(self) -> bool:
        return self.owner == "usb" and self.host_write_applied

    def host_wrote_recently(self) -> bool:
        return self.host_write_enabled() and 0 <= self.write_idle_ms() < WRITE_GUARD_MS

    def _provide(self):
        """do_mount_usb()"""
        self.owner = "usb"
        self.host_ejected = False
        self.host_write_applied = self.host_writable
        self.last_write = None
        # A connected host looks at the "new" medium right away.
        self.last_read = time.monotonic() if self.usb_connected else None
        self.log("I", "drive", f"owner: USB host ({'read/write' if self.host_writable else 'read-only'})")

    def _mount_app(self):
        """do_mount_app(): returns None or an error code."""
        self.owner = "app"
        if self.needs_format:
            self.fs_ok = False
        if not self.fs_ok:
            self.log("E", "drive", "file system could not be mounted for the app")
            return "NO_FS"
        self.log("I", "drive", f"owner: APP (web upload possible), {self.free()} of {self.total} bytes free")
        return None

    def set_owner(self, target: str, force: bool):
        """drive_set_owner()"""
        if target == self.owner:
            return None
        if self.other_upload is not None:
            return "BUSY"
        if target == "usb":
            self._provide()
            return None
        if self.usb_connected and not self.host_ejected:
            if self.host_wrote_recently():
                return "HOST_WRITING"           # never taken away during a copy
            if not force:
                return "HOST_ACTIVE"
            self.log("W", "drive", "taking the drive from the host (confirmed by user)")
        return self._mount_app()

    def require_app_fs(self):
        if self.owner != "app":
            return "WRONG_OWNER"
        return None if self.fs_ok else "NO_FS"

    def upload_precheck(self, name, size: int, overwrite: bool):
        """drive_check_name() + size limit + drive_upload_begin()"""
        err = check_name(name, self.types)
        if err:
            return err
        if size > 0xFFFFFFFF:
            return "TOO_LARGE"
        if size == 0:
            return "INVALID_FILE"
        err = self.require_app_fs()
        if err:
            return err
        if self.other_upload is not None:
            return "BUSY"
        if name.lower() in self.files and not overwrite:
            return "EXISTS"
        # The old file is only removed after success, so the new one needs its full size.
        if size + FREE_RESERVE > self.free():
            return "NO_SPACE"
        return None

    def store_upload(self, name: str, data: bytes, size: int, mtime: int):
        if mtime < MTIME_MIN:                    # not given: the device clock, if it was ever set
            mtime = local_now() + self.clock_offset if self.clock_offset is not None else 0
        self.files.pop(name.lower(), None)       # overwrite replaces; new directory entry at the end
        self.files[name.lower()] = MockFile(name, data, size, mtime)
        self.log("I", "drive", f"upload ok '{name}'")

    def delete(self, name):
        err = check_existing_name(name) or self.require_app_fs()
        if err:
            return err
        if self.other_upload is not None:
            return "BUSY"
        if name.lower() not in self.files:
            return "NOT_FOUND"
        del self.files[name.lower()]
        self.log("I", "drive", f"deleted '{name}'")
        return None

    def rename(self, name, to):
        """drive_rename()"""
        err = check_existing_name(name) or check_name(to, self.types) or self.require_app_fs()
        if err:
            return err
        if self.other_upload is not None:
            return "BUSY"
        if name == to:
            return None
        src = self.files.get(name.lower())
        if src is None:
            return "NOT_FOUND"
        dst = self.files.get(to.lower())
        if dst is not None and dst is not src:
            return "EXISTS"
        src.name = to                            # same object: only the case changes
        if dst is None:                          # keep the position in the directory
            self.files = {(to.lower() if f is src else key): f for key, f in self.files.items()}
        self.log("I", "drive", f"renamed '{name}' -> '{to}'")
        return None

    def list_files(self) -> dict:
        """drive_list(): the firmware packs the list into a 32 KiB buffer."""
        out, used, truncated = [], 0, False
        for f in self.visible_files():
            need = 8 + len(f.name.encode("utf-8", "surrogatepass")) + 1
            if used + need > LIST_BUF_SIZE:
                truncated = True
                continue
            used += need
            out.append({"name": f.name, "size": f.size, "mtime": f.mtime})
        return {"ok": True, "files": out, "dirs": self.dirs, "truncated": truncated}

    def format(self):
        self.files.clear()
        self.dirs = 0
        self.fill = 0
        self.needs_format = False
        self.fs_ok = True
        self.log("W", "drive", "format requested - restarting")

    # ---------------------------------------------------------- JSON views

    def status(self) -> dict:
        read_idle, write_idle = self.read_idle_ms(), self.write_idle_ms()
        idles = [x for x in (read_idle, write_idle) if x >= 0]
        known = not self.needs_format            # without a file system the device never learnt the sizes
        upload = {"active": False, "name": "", "received": 0, "total": 0}
        if self.other_upload is not None:
            total = 24 * 1024 * 1024
            rate = (self.upload_kbps or 400) * 1024
            received = int((time.monotonic() - self.other_upload) * rate) % total
            upload = {"active": True, "name": "Upload von anderem Gerät.3mf", "received": received, "total": total}
        sta = self.wifi_mode == "sta"
        ap = self.wifi_mode in ("ap", "connecting")
        return {
            "version": VERSION,
            "uptime_s": max(0, int(time.monotonic() - self.boot_t)),
            "heap": 140000 + random.randrange(-6, 7) * 512,
            "types": self.types,
            "drive": {
                "owner": self.owner, "fs_ok": self.fs_ok, "needs_format": self.needs_format,
                "usb_connected": self.usb_connected, "usb_suspended": self.usb_suspended,
                "host_ejected": self.host_ejected, "host_writable": self.host_writable,
                "host_idle_ms": min(idles) if idles else -1, "host_write_idle_ms": write_idle,
                "write_guard_ms": WRITE_GUARD_MS,
                "total": self.total if known else 0, "free": self.free() if known else 0,
                "files": len(self.visible_files()) if known else 0, "sector": self.sector,
            },
            "medium": {"sd": self.sd, "name": self.medium_name},
            "upload": upload,
            "wifi": {
                "sta_configured": self.wifi_mode != "ap", "sta_connected": sta,
                "ssid": "" if self.wifi_mode == "ap" else self.ssid,
                "ip": FAKE_IP if sta else "", "rssi": -61 + random.randrange(-2, 3) if sta else 0,
                "ap_active": ap, "ap_ssid": "DataDrive-54D5", "ap_ip": "192.168.4.1" if ap else "",
                "hostname": "datadrive-mock",
            },
        }

    def dump(self) -> dict:
        offline_for = None
        if self.pending_boot:
            offline_for = "until online=1" if math.isinf(self.offline_until) else \
                round(max(0.0, self.offline_until - time.monotonic()), 1)
        return {
            "mock": True,
            "scenario": self.scenario,
            "online": not self.is_offline(),
            "restarting": self.pending_boot,
            "restart_remaining_s": offline_for,
            "owner": self.owner, "fs_ok": self.fs_ok, "needs_format": self.needs_format,
            "usb_connected": self.usb_connected, "usb_suspended": self.usb_suspended,
            "host_ejected": self.host_ejected, "host_writable": self.host_writable,
            "host_write_applied": self.host_write_applied,
            "host_writing": "hold" if self.write_hold else self.host_wrote_recently(),
            "sd": self.sd, "total": self.total, "fill": self.fill, "free": self.free(),
            "types": self.types, "dirs": self.dirs, "wifi": self.wifi_mode,
            "upload_kbps": self.upload_kbps, "delay_ms": self.delay_ms,
            "fail_next": None if self.fail_next is None else
            self.fail_next[0] + ("@" + self.fail_next[1] if self.fail_next[1] else ""),
            "upload_fail_at": self.upload_fail_at,
            "other_upload": self.other_upload is not None,
            "file_count": len(self.files),
            "files": [{"name": f.name, "size": f.size, "stored": len(f.data), "mtime": f.mtime}
                      for f in self.files.values()],
            "status": self.status(),
        }

    # ---------------------------------------------------------- /mock/set

    def apply_set(self, q: dict) -> list:
        """Validates everything first, then applies in a fixed order. Returns notes.
        Raises ValueError for unknown keys or bad values."""
        unknown = [k for k in q if k not in SET_HELP]
        if unknown:
            raise ValueError("unknown key(s): " + ", ".join(unknown))
        if any(v is None for v in q.values()):
            raise ValueError("a value could not be URL-decoded")
        v = {}
        for key in ("usb_connected", "usb_suspended", "host_ejected", "host_writable", "fs_ok", "needs_format",
                    "online", "other_upload", "reset", "host_reading"):
            if key in q:
                v[key] = parse_bool(q[key])
        if "sd" in q:
            medium = q["sd"].strip().lower()
            v["sd"] = True if medium == "sd" else False if medium == "flash" else parse_bool(medium)
        for key in ("total", "fill"):
            if key in q:
                v[key] = parse_size(q[key])
        for key in ("dirs", "upload_kbps", "delay_ms"):
            if key in q:
                v[key] = int(parse_nonneg(q[key]))
        if "owner" in q:
            if q["owner"] not in ("app", "usb", "none"):
                raise ValueError("owner must be app, usb or none")
            v["owner"] = q["owner"]
        if "host_writing" in q:
            v["host_writing"] = "hold" if q["host_writing"].lower() == "hold" else parse_bool(q["host_writing"])
        if "wifi" in q:
            if q["wifi"] not in ("sta", "ap", "connecting"):
                raise ValueError("wifi must be sta, ap or connecting")
            v["wifi"] = q["wifi"]
        if "types" in q:
            t = q["types"].strip().lower().replace(" ", "")
            if t != "all" and not re.fullmatch(r"\.[a-z0-9]+(,\.[a-z0-9]+)*", t):
                raise ValueError("types must be 'all' or a list like .3mf,.gcode")
            v["types"] = t
        if "fail_next" in q:
            if is_cleared(q["fail_next"]) or q["fail_next"] == "0":
                v["fail_next"] = None
            else:
                code, _, path = q["fail_next"].partition("@")
                code = code.strip().upper()
                if code not in ERRORS:
                    raise ValueError(f"fail_next: unknown code '{code}', known: " + ", ".join(ERRORS))
                if path and not path.startswith("/api/"):
                    raise ValueError("fail_next: the path after @ must start with /api/")
                v["fail_next"] = (code, path or None)
        if "upload_fail_at" in q:
            if is_cleared(q["upload_fail_at"]):
                v["upload_fail_at"] = None
            else:
                pct = parse_nonneg(q["upload_fail_at"])
                if pct > 100:
                    raise ValueError("upload_fail_at must be 0..100")
                v["upload_fail_at"] = pct

        notes = []
        if v.get("reset"):
            self.apply_scenario(self.scenario)
            notes.append(f"scenario '{self.scenario}' restored")
        if "sd" in v:
            self._set_medium(v["sd"])
        if "total" in v:
            self.total = v["total"]
        if "fill" in v:
            self.fill = v["fill"]
        if "types" in v:
            self.types = v["types"]
        if "dirs" in v:
            self.dirs = v["dirs"]
        if "needs_format" in v:
            self.needs_format = v["needs_format"]
            self.fs_ok = not v["needs_format"]
        if "fs_ok" in v:
            self.fs_ok = v["fs_ok"]
        auto = "owner" not in v                  # device-like automatic owner changes
        if "usb_connected" in v and v["usb_connected"] != self.usb_connected:
            self.usb_connected = v["usb_connected"]
            if self.usb_connected:
                self.log("I", "drive", "USB host attached")
                if auto and self.owner == "app" and self.fs_ok and self.other_upload is None:
                    self._provide()
                    notes.append("host attached: drive provided automatically (owner=usb), like the device; "
                                 "add owner=app to the same call to prevent this")
            else:
                self.log("W", "drive", "USB host detached")
                if auto and self.owner == "usb":
                    self._mount_app()
                    notes.append("host detached: owner switched to app automatically, like the device; "
                                 "add owner=usb to the same call to prevent this")
        if "usb_suspended" in v:
            self.usb_suspended = v["usb_suspended"]
        if "host_ejected" in v:
            if v["host_ejected"] and not self.host_ejected:
                self.log("I", "drive", "host ejected the drive")
                if auto and self.owner == "usb":
                    self._mount_app()
                    notes.append("host ejected: owner switched to app automatically, like the device; "
                                 "add owner=usb to the same call to prevent this")
            self.host_ejected = v["host_ejected"]
        if "owner" in v:
            if v["owner"] == "usb" and self.owner != "usb":
                self.host_write_applied = self.host_writable
                if "host_ejected" not in v:
                    self.host_ejected = False
            self.owner = v["owner"]
        if "host_writable" in v:
            self.host_writable = self.host_write_applied = v["host_writable"]
        if "host_writing" in v:
            now = time.monotonic()
            if v["host_writing"] == "hold":
                self.write_hold = True
                self.last_write = self.last_read = now
            elif v["host_writing"]:
                self.write_hold = False
                self.last_write = self.last_read = now
            else:
                self.write_hold = False
                if self.last_write is not None:   # push the last write out of the guard window
                    self.last_write = min(self.last_write, now - WRITE_GUARD_MS / 1000 - 0.001)
            if v["host_writing"] and not self.host_write_enabled():
                notes.append("HOST_WRITING will not be reported: it needs owner=usb and host_writable=1")
            elif v["host_writing"] and not self.usb_connected:
                notes.append("HOST_WRITING is only reported by /api/mode while usb_connected=1")
        if v.get("host_reading"):
            self.last_read = time.monotonic()
        if "wifi" in v:
            self.wifi_mode = v["wifi"]
            self.wifi_connect_at = None
            if v["wifi"] != "ap" and not self.ssid:
                self.ssid = "MeinWLAN"
        if "other_upload" in v:
            self.other_upload = time.monotonic() if v["other_upload"] else None
        if "upload_kbps" in v:
            self.upload_kbps = v["upload_kbps"]
        if "delay_ms" in v:
            self.delay_ms = v["delay_ms"]
        if "fail_next" in v:
            self.fail_next = v["fail_next"]
        if "upload_fail_at" in v:
            self.upload_fail_at = v["upload_fail_at"]
        if "online" in v:
            self.online = v["online"]
            if self.online and self.pending_boot:
                self._boot(time.monotonic())
                notes.append("restart finished")
        return notes

    def take_fail_next(self, path: str):
        if self.fail_next is None or (self.fail_next[1] and self.fail_next[1] != path):
            return None
        code = self.fail_next[0]
        self.fail_next = None
        return code


# ============================================================== HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WiFiDataDriveMock"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_S

    def log_message(self, fmt, *args):       # replaced by one compact line per request
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _other_method(self):
        self._route(self.command)

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other_method

    # ---------------------------------------------------------- plumbing

    @property
    def dev(self) -> Device:
        return self.server.dev

    def _route(self, method: str):
        self._status = None
        self._info = ""
        self.close_connection = True
        t0 = time.monotonic()
        path, _, query = self.path.partition("#")[0].partition("?")
        q = parse_query(query)
        dev = self.dev
        dev.tick()
        try:
            self._dispatch(method, path, q)
        except ClientGone:
            self._abort("client closed the connection")
        except (ConnectionError, TimeoutError, socket.timeout) as e:
            self._abort(f"connection lost ({type(e).__name__})")
        except Exception:                    # a bug in the mock must not kill the server
            traceback.print_exc()
            self._abort("internal error in mock")
        finally:
            ms = (time.monotonic() - t0) * 1000
            status = "---" if self._status is None else str(self._status)
            line = f"{time.strftime('%H:%M:%S')} {method:<4} {self.path} -> {status}"
            if self._info:
                line += " " + self._info
            print(f"{line} ({ms:.0f} ms)", flush=True)

    def _dispatch(self, method: str, path: str, q: dict):
        dev = self.dev
        is_mock = path == "/mock" or path.startswith("/mock/")
        if dev.is_offline() and not is_mock:
            self._abort("offline: connection reset")
            return
        if path.startswith("/api/") and dev.delay_ms > 0:
            time.sleep(dev.delay_ms / 1000)

        get = {
            "/": self._index, "/index.html": self._index,
            "/api/status": self._api_status, "/api/files": self._api_files,
            "/api/download": self._api_download, "/api/log": self._api_log,
            "/mock": self._mock_help, "/mock/": self._mock_help, "/mock/state": self._mock_state,
            "/mock/set": self._mock_set, "/mock/scenario": self._mock_scenario,
        }
        post = {
            "/api/mode": self._api_mode, "/api/upload/check": self._api_upload_check,
            "/api/upload": self._api_upload, "/api/delete": self._api_delete,
            "/api/rename": self._api_rename, "/api/settings/host_writable": self._api_host_writable,
            "/api/wifi": self._api_wifi, "/api/format": self._api_format, "/api/reboot": self._api_reboot,
        }
        clen = self._content_length()
        if method == "GET" and path in get:
            get[path](q)
        elif method == "POST" and path in post:
            # CSRF protection first, like check_csrf() in every POST handler.
            if self.headers.get(CSRF_HEADER) != "1":
                self._drain(clen)
                self._error("FORBIDDEN")
                return
            code = dev.take_fail_next(path)
            if code:
                late = path == "/api/upload" and code in LATE_UPLOAD_ERRORS
                self._read_body(clen or 0, throttled=late)
                self._error(code, info="(fail_next)")
                return
            post[path](q, clen)
        elif path in get or path in post:
            self._drain(clen)
            self._send(405, b"Specified method is invalid for this resource", "text/plain")
        else:
            self._drain(clen)
            self._send(404, b"Nothing matches the given URI", "text/plain")

    def _content_length(self):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return n if n >= 0 else None

    def _abort(self, info: str):
        """Close the connection hard (RST) without (further) answer."""
        self.server.abort_current = True
        self.close_connection = True
        self._info = (self._info + " " + info).strip()

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store", headers=()):
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Connection", "close")
        for key, val in headers:
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, info: str = ""):
        if info:
            self._info = (self._info + " " + info).strip()
        self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   "application/json")

    def _error(self, code: str, text: str = "", info: str = ""):
        status, default_text = ERRORS[code]
        self._json(status, {"ok": False, "code": code, "error": text or default_text},
                   (code + " " + info).strip())

    def _result(self, code, info: str = ""):
        """send_result(): None = success."""
        if code:
            self._error(code, info=info)
        else:
            self._json(200, {"ok": True, "code": "OK", "error": ""}, info)

    def _read_body(self, total: int, throttled: bool = False, on_data=None, stop_at=None) -> int:
        """Read `total` body bytes (optionally at the simulated upload rate). Returns the
        number of bytes read; that is less than `total` only if `stop_at` was reached.
        Raises ClientGone if the client stops sending."""
        dev = self.dev
        received = 0
        t0 = time.monotonic()
        while received < total:
            if stop_at is not None and received >= stop_at:
                break
            rate = dev.upload_kbps * 1024 if throttled else 0
            want = min(total - received, 65536 if not rate else max(512, min(16384, rate // 20)))
            if stop_at is not None:
                want = min(want, stop_at - received)
            data = self.rfile.read1(want)
            if not data:
                raise ClientGone()
            received += len(data)
            if on_data:
                on_data(data)
            if rate:
                ahead = received / rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(min(ahead, 1.0))
        return received

    def _drain(self, clen):
        """Read and discard the body so that the (error) answer reaches the client."""
        if clen:
            self._read_body(clen)

    # ---------------------------------------------------------- GET

    def _index(self, q):
        path = self.server.html_path
        try:
            body = path.read_bytes()
        except OSError as e:
            msg = f"mock_server: cannot read {path}: {e.strerror}\n"
            self._send(500, msg.encode("utf-8"), "text/plain; charset=utf-8")
            self._info = "HTML file missing"
            return
        self._send(200, body, "text/html; charset=utf-8", "no-cache")

    def _api_status(self, q):
        self._json(200, self.dev.status())

    def _api_files(self, q):
        err = self.dev.require_app_fs()
        if err:
            self._error(err)
            return
        listing = self.dev.list_files()
        self._json(200, listing, f"{len(listing['files'])} files" + (" truncated" if listing["truncated"] else ""))

    def _api_download(self, q):
        dev = self.dev
        if "name" not in q:
            self._error("BAD_REQUEST", "name fehlt")
            return
        name = q["name"]
        err = check_existing_name(name) or dev.require_app_fs()
        if not err and name.lower() not in dev.files:
            err = "NOT_FOUND"
        if err:
            self._error(err)
            return
        f = dev.files[name.lower()]
        self._info = f"{len(f.data)} B" + (f" (reported size {f.size})" if f.size != len(f.data) else "")
        self._send(200, f.data, "application/octet-stream", headers=[("Content-Disposition", "attachment")])

    def _api_log(self, q):
        self._send(200, self.dev.log_text(), "text/plain; charset=utf-8")

    # ---------------------------------------------------------- POST

    def _api_mode(self, q, clen):
        self._drain(clen)
        target = q.get("target")
        if "target" not in q:
            self._error("BAD_REQUEST", "target fehlt")
        elif target not in ("usb", "app"):
            self._error("BAD_REQUEST", "target muss usb oder app sein")
        else:
            self._result(self.dev.set_owner(target, flag(q, "force")), f"owner={self.dev.owner}")

    def _api_upload_check(self, q, clen):
        self._drain(clen)
        if "name" not in q:
            self._error("BAD_REQUEST", "name fehlt")
            return
        if not re.fullmatch(r"\d{1,20}", q.get("size") or ""):
            self._error("BAD_REQUEST", "size fehlt oder ist ungültig")
            return
        self._result(self.dev.upload_precheck(q["name"], int(q["size"]), flag(q, "overwrite")))

    def _api_upload(self, q, clen):
        dev = self.dev
        if clen is None:
            self._error("BAD_REQUEST", "Content-Length fehlt")
            return
        if "name" not in q:
            self._drain(clen)
            self._error("BAD_REQUEST", "name fehlt")
            return
        name = q["name"]
        overwrite = flag(q, "overwrite")
        err = dev.upload_precheck(name, clen, overwrite)
        if err:
            self._drain(clen)
            self._error(err)
            return

        dev.log("I", "drive", f"upload start '{name}' ({clen} bytes)")
        stop_at = None
        if dev.upload_fail_at is not None:
            stop_at = int(clen * dev.upload_fail_at / 100)
            dev.upload_fail_at = None
        keep_limit = clen if clen <= STORE_MAX else STORE_HEAD
        head, tail, keep = bytearray(), bytearray(), bytearray()

        def on_data(data: bytes):
            if len(head) < 4:
                head.extend(data[:4 - len(head)])
            if len(keep) < keep_limit:
                keep.extend(data[:keep_limit - len(keep)])
            tail.extend(data)
            del tail[:-TAIL_SIZE]

        t0 = time.monotonic()
        try:
            received = self._read_body(clen, throttled=True, on_data=on_data, stop_at=stop_at)
        except (ClientGone, ConnectionError, TimeoutError, socket.timeout):
            dev.log("W", "web", "upload connection lost (-1)")
            dev.log("W", "drive", f"upload aborted '{name}'")
            self._info = "upload aborted, nothing stored:"
            raise
        if received < clen:
            dev.log("W", "web", "upload connection lost (-1)")
            dev.log("W", "drive", f"upload aborted '{name}' after {received} bytes")
            self._abort(f"upload_fail_at: connection reset after {received} of {clen} B")
            return

        dt = max(time.monotonic() - t0, 1e-6)
        err = None
        if has_suffix_ci(name, ".3mf"):
            # "PK\5\6" must start at least 22 bytes (size of the record) before the end.
            zip_ok = bytes(head) == b"PK\x03\x04" and tail.find(b"PK\x05\x06", 0, max(0, len(tail) - 18)) >= 0
            if not zip_ok:
                err = "INVALID_FILE"
        if not err:
            mtime = leading_uint(q.get("mtime"))
            client_now = leading_uint(q.get("now"))      # firmware extra: sets the device clock once
            if client_now > 1_700_000_000 and dev.clock_offset is None:
                dev.clock_offset = client_now - local_now()
            dev.store_upload(name, bytes(keep), clen, mtime)
        else:
            dev.log("W", "drive", f"upload failed '{name}': {ERRORS[err][1]}")
        dev.log("I", "web", f"upload '{name}' {clen} bytes in {dt * 1000:.0f} ms ({clen / 1024 / dt:.1f} KB/s): "
                            f"{ERRORS[err][1] if err else 'OK'}")
        self._result(err, f"{clen} B in {dt:.2f} s" + (f" = {clen / 1024 / dt:.0f} KB/s" if dt >= 0.05 else ""))

    def _api_delete(self, q, clen):
        self._drain(clen)
        if "name" not in q:
            self._error("BAD_REQUEST", "name fehlt")
            return
        self._result(self.dev.delete(q["name"]))

    def _api_rename(self, q, clen):
        self._drain(clen)
        if "name" not in q or "to" not in q:
            self._error("BAD_REQUEST", "name oder to fehlt")
            return
        self._result(self.dev.rename(q["name"], q["to"]))

    def _api_host_writable(self, q, clen):
        self._drain(clen)
        value = q.get("value")
        if value not in ("0", "1", "true", "false"):
            self._error("BAD_REQUEST", "value muss 0 oder 1 sein")
            return
        # Stored only; takes effect the next time the drive is provided.
        self.dev.host_writable = value in ("1", "true")
        self._result(None, f"host_writable={int(self.dev.host_writable)}")

    def _api_wifi(self, q, clen):
        dev = self.dev
        if clen is None or clen >= 256:
            self._drain(clen)
            self._error("BAD_REQUEST", "Anfrage zu groß" if clen else "SSID fehlt")
            return
        body = bytearray()
        self._read_body(clen, on_data=body.extend)
        form = parse_query(body.decode("latin-1"))
        ssid = form.get("ssid")
        if ssid is None:
            self._error("BAD_REQUEST", "SSID fehlt")
            return
        password = form.get("pass") or ""
        ssid_len, pass_len = len(ssid.encode("utf-8")), len(password.encode("utf-8"))
        if not (1 <= ssid_len <= 32) or not (pass_len == 0 or 8 <= pass_len <= 63):
            self._error("BAD_REQUEST", "Ungültig: SSID 1-32 Zeichen, Passwort leer oder 8-63 Zeichen")
            return
        dev.ssid = ssid
        dev.wifi_mode = "connecting"
        dev.wifi_connect_at = time.monotonic() + 3.0
        dev.log("I", "wifi", f"new credentials stored for '{ssid}'")
        dev.log("I", "wifi", f"connecting to '{ssid}' ...")
        self._json(200, {"ok": True}, f"ssid='{ssid}' (connected in 3 s)")

    def _api_format(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if q.get("confirm") != "FORMAT":
            self._error("BAD_REQUEST", "Bestätigung fehlt")
        elif dev.owner != "app":
            self._error("WRONG_OWNER")
        elif dev.other_upload is not None:
            self._error("BUSY")
        else:
            self._json(200, {"ok": True, "restart": True}, f"formatted, restarting ({RESTART_S:.0f} s offline)")
            dev.format()
            dev.restart()

    def _api_reboot(self, q, clen):
        self._drain(clen)
        dev = self.dev
        mode = q.get("mode", "normal")
        force = flag(q, "force")
        if mode not in ("normal", "download"):
            self._error("BAD_REQUEST", "mode muss normal oder download sein")
        elif not force and dev.other_upload is not None:
            self._error("BUSY")
        elif not force and dev.host_wrote_recently():
            self._error("HOST_WRITING")
        else:
            download = mode == "download"
            self._json(200, {"ok": True}, "download mode: offline until /mock/set?online=1" if download
                       else f"restarting ({RESTART_S:.0f} s offline)")
            dev.restart(download)

    # ---------------------------------------------------------- /mock

    def _mock_help(self, q):
        self._json(200, {
            "mock": True,
            "info": "WiFi DataDrive mock server - control endpoints, not part of the device API",
            "endpoints": ["/mock/state", "/mock/set?key=value&...", "/mock/scenario?name=NAME"],
            "scenario": self.dev.scenario,
            "scenarios": SCENARIO_HELP,
            "set_keys": SET_HELP,
            "fail_next_codes": list(ERRORS),
        })

    def _mock_state(self, q):
        self._json(200, self.dev.dump())

    def _mock_set(self, q):
        try:
            notes = self.dev.apply_set(q)
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e), "set_keys": SET_HELP}, str(e))
            return
        state = self.dev.dump()
        del state["files"]
        self._json(200, {"ok": True, "notes": notes, "state": state}, "; ".join(notes))

    def _mock_scenario(self, q):
        name = q.get("name")
        if "name" not in q:
            self._json(200, {"ok": True, "scenario": self.dev.scenario, "scenarios": SCENARIO_HELP})
            return
        try:
            self.dev.apply_scenario(name or "")
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e), "scenarios": SCENARIO_HELP}, str(e))
            return
        state = self.dev.dump()
        del state["files"]
        self._json(200, {"ok": True, "scenario": name, "state": state})


class MockServer(HTTPServer):
    """Accepts many connections but works on one request at a time, like the ESP-IDF HTTP
    server: connections wait (without blocking each other while idle) until they have sent
    something, then exactly one request is handled to the end."""
    allow_reuse_address = False       # on Windows this would allow two servers on one port
    request_queue_size = 64

    def __init__(self, addr, dev: Device, html_path: Path):
        self.dev = dev
        self.html_path = html_path
        self.abort_current = False
        super().__init__(addr, Handler)

    def server_bind(self):
        # A small receive buffer keeps the client's upload progress close to what the
        # throttled reader has really consumed.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
        socketserver.TCPServer.server_bind(self)     # skips the slow getfqdn() of HTTPServer
        self.server_name, self.server_port = self.server_address[:2]

    def run(self):
        pending = {}                  # socket -> time accepted
        try:
            while True:
                self.dev.tick()
                try:
                    readable, _, _ = select.select([self.socket, *pending], [], [], 0.2)
                except (OSError, ValueError):
                    readable = []
                    for s in list(pending):          # drop the socket that went bad
                        try:
                            select.select([s], [], [], 0)
                        except (OSError, ValueError):
                            del pending[s]
                            self.close_connection(s, hard=True)
                    time.sleep(0.05)
                now = time.monotonic()
                if self.socket in readable:
                    try:
                        conn, _ = self.socket.accept()
                        pending[conn] = now
                    except OSError:
                        pass
                for conn in [s for s in pending if s in readable][:1]:   # one request, then look again
                    del pending[conn]
                    self.handle_connection(conn)
                for conn, t in list(pending.items()):
                    if now - t > 60 or len(pending) > 200:
                        del pending[conn]
                        self.close_connection(conn, hard=False)
        finally:
            for conn in pending:
                self.close_connection(conn, hard=False)

    def handle_connection(self, conn: socket.socket):
        self.abort_current = False
        try:
            conn.setblocking(True)
            Handler(conn, conn.getpeername() if conn.fileno() >= 0 else ("?", 0), self)
        except (OSError, ClientGone):
            pass                       # client went away in the middle of something
        except Exception:
            traceback.print_exc()
        self.close_connection(conn, hard=self.abort_current)

    @staticmethod
    def close_connection(conn: socket.socket, hard: bool):
        try:
            if hard:                   # SO_LINGER 0: the client sees a connection reset
                fmt = "HH" if sys.platform == "win32" else "ii"
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(fmt, 1, 0))
            else:
                conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


# ============================================================== main

def resolve_path(arg: str) -> Path:
    p = Path(arg)
    if p.is_absolute():
        return p
    in_repo = REPO_ROOT / p
    if not in_repo.exists() and (Path.cwd() / p).exists():
        return (Path.cwd() / p).resolve()
    return in_repo


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mock server for the WiFi DataDrive web UI (see module docstring).")
    ap.add_argument("--port", type=int, default=8080, help="TCP port (default 8080)")
    ap.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1; "
                                                        "0.0.0.0 = reachable from the network)")
    ap.add_argument("--html", default=DEFAULT_HTML, help=f"UI file, relative to the repo root (default {DEFAULT_HTML})")
    ap.add_argument("--scenario", default="usb_active", choices=list(SCENARIO_HELP), help="start scenario")
    ap.add_argument("--upload-kbps", type=int, default=400, help="upload rate limit in KiB/s, 0 = unlimited "
                                                                 "(default 400)")
    a = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    for sig in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _raise_interrupt)

    dev = Device(max(0, a.upload_kbps), a.scenario)
    html_path = resolve_path(a.html)
    try:
        server = MockServer((a.bind, a.port), dev, html_path)
    except OSError as e:
        print(f"mock_server: cannot listen on {a.bind}:{a.port}: {e}", file=sys.stderr)
        return 1
    host = "127.0.0.1" if a.bind in ("0.0.0.0", "") else a.bind
    base = f"http://{host}:{server.server_port}"
    print(f"WiFi DataDrive MOCK server (no real device)\n"
          f"  UI:        {base}/\n"
          f"  HTML file: {html_path}{'' if html_path.is_file() else '   <-- NOT FOUND'}\n"
          f"  scenario:  {a.scenario}   upload limit: {dev.upload_kbps or 'none'} KiB/s\n"
          f"  control:   {base}/mock   {base}/mock/state\n"
          f"Ctrl+C stops the server.", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\nmock_server: stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
