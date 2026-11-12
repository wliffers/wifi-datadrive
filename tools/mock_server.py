#!/usr/bin/env python3
"""Mock server for the "WiFi DataDrive" web UI (MOCK ONLY - no real device involved).

Serves the single-file web UI and implements the HTTP API version 3 of docs/09_api_v3.md
(login with roles, folders, Wi-Fi list, update; everything it does not mention follows
docs/07_api.md) plus the additions of version 4 (docs/12_api_v4.md: Wi-Fi scan, drive encryption) with an in-memory file tree, so every state of the UI can be exercised on
the PC without the dongle. Python 3.10+, standard library only.

Usage
-----
    python tools/mock_server.py [--port 8080] [--bind 127.0.0.1]
                                [--html firmware/printdrive/main/web/index.html]
                                [--scenario NAME] [--upload-kbps 400]
                                [--next-version 0.3.1] [--no-auth]

Then open http://127.0.0.1:8080/ . The HTML file is re-read from disk on every request
(edit, save, reload). Relative paths are resolved against the repository root, the
current directory does not matter. `--bind 0.0.0.0` makes the mock reachable from a
phone in the same network. Stop with Ctrl+C.

Start state: scenario usb_active, one account admin / admin123 (the delivery password
was "already changed", default_password: false). Only the scenario `default_login` starts
with admin / admin: then every request except GET /api/session, POST /api/login,
POST /api/logout and POST /api/users/password (own account) answers 403
MUST_CHANGE_PASSWORD, for sessions and Basic alike; the new password may not be "admin"
(400 BAD_PASSWORD); the session keeps working after the change.
`/mock/set?default_password=1|0` switches the admin password between admin and admin123.

Examples
--------
    python tools/mock_server.py
    python tools/mock_server.py --scenario files --upload-kbps 150
    python tools/mock_server.py --port 8099 --scenario ap_setup --bind 0.0.0.0
    python tools/mock_server.py --scenario two_users --no-auth     # old tests without login

    # scripts: HTTP Basic on every request (POST needs the X-PrintDrive header)
    curl -u admin:admin123 "http://127.0.0.1:8080/api/status"
    curl -u admin:admin123 "http://127.0.0.1:8080/api/files?dir=Projekte/Drachen"
    curl -u admin:admin123 -H "X-PrintDrive: 1" -X POST "http://127.0.0.1:8080/api/mkdir?path=Neu"
    curl -u admin:admin123 -H "X-PrintDrive: 1" -X POST --data-binary @Teil.3mf \\
         "http://127.0.0.1:8080/api/push?path=Projekte/Teil.3mf"       # one step, takes over the drive
    curl -u admin:admin123 -H "X-PrintDrive: 1" -X POST --data-binary @printdrive.bin \\
         "http://127.0.0.1:8080/api/ota"                                # first byte must be 0xE9

    # browser-like: session cookie
    curl -c jar -H "X-PrintDrive: 1" -X POST -d "user=admin&pass=admin" http://127.0.0.1:8080/api/login
    curl -b jar http://127.0.0.1:8080/api/session

    # control endpoints (no login, no CSRF header)
    curl "http://127.0.0.1:8080/mock/set?host_writing=hold&delay_ms=300"
    curl "http://127.0.0.1:8080/mock/scenario?name=two_users"
    curl "http://127.0.0.1:8080/mock/set?fail_next=NO_SPACE@/api/copy&copy_kbps=2000"

Behaviour mirrored from the firmware
------------------------------------
* One request at a time: a running upload (or copy) blocks every other request (also
  status polls). `copy_kbps` makes a copy take as long as the data would on the device.
* Every POST needs the header `X-PrintDrive: 1`, otherwise 403 FORBIDDEN.
* Login is mandatory except for GET / /index.html /manifest.webmanifest /icon-180.png
  /icon-192.png /icon-512.png /api/session and POST /api/login (otherwise 401 UNAUTHORIZED).
  Session cookie `dd_session` (64 hex characters, HttpOnly, SameSite=Strict) or HTTP Basic
  on any request. Max 8 sessions (the oldest is displaced), 12 h idle expiry, all gone
  after a restart (format/reboot/update). Lock-out per peer address like auth.c: 5 wrong passwords
  in a row lock that peer for 30 s, every further lock-out doubles it (60, 120 ... max 900 s), a
  verified password resets it; more than 30 failures in 60 s restrict password checks of peers
  without a verified password in the last 12 h for 30 s (429 LOCKED with a Retry-After header,
  also for Basic and for a wrong `old` password; the mock sees one peer: 127.0.0.1). Roles: `user` and
  `admin` (403 FORBIDDEN_ROLE) exactly as in the table of section 2.3; max 8 users,
  LAST_ADMIN, USER_LIMIT, BAD_USER, BAD_PASSWORD, EXISTS; changing a password ends the
  user's other sessions. /mock/* is never authenticated.
* Trusted devices (2.2a): POST /api/login with remember=1 and device=<label> sets a cookie
  with Max-Age=31536000 and stores the device (max 8 in total, max 4 per user; the same user and
  label replaces the old entry, else the user's oldest goes; when the total is full and the user
  has none to replace the login gets a volatile session without Max-Age). Unlike normal
  sessions, devices survive a simulated restart and never expire. GET /api/devices (user: own,
  admin: all), POST /api/devices/delete?id=. They end on password/role change, user deletion
  and logout (a device that changes its own password stays). The forced password change
  applies to them too.
* Files and folders: tree with folders (depth <= 8, path <= 240 bytes, segment rules of
  the contract, BAD_NAME), case-insensitive names like FAT. mkdir, delete (recursive=1),
  move (also rename; case-only rename allowed), copy (recursive, NO_SPACE pre-check with
  64 KiB reserve), upload/check, upload and the one-step push (take-over rules of
  /api/mode?target=app, drive is provided to the USB host again afterwards).
* Owner state machine app/usb with HOST_ACTIVE / HOST_WRITING / BUSY, file operations
  only while the owner is `app`, name/type/space/content checks of uploads, German
  error texts, restart after format/reboot/update (the mock is unreachable for about 4 s;
  in download mode until `/mock/set?online=1`).
* Uploads and updates are read at a limited rate (default 400 KB/s) so progress bars are
  visible. Uploaded content is kept in memory (files above 64 MiB: only the first 64 KiB
  are kept, the reported size stays). Scenario files report a fake `size`; a download
  returns the few bytes that are really stored.
* Update: POST /api/ota accepts any body whose first byte is 0xE9 (INVALID_FILE otherwise),
  then restarts and reports the version of `--next-version` (default 0.3.1), the other
  partition and `pending_verify: true` for the first 15 s.
* Static files: manifest and icons are served from firmware/printdrive/main/web/
  (manifest.webmanifest, icons/icon-180.png ...) if they exist, else generated placeholders.
* Not mirrored: the reported IP is 192.0.2.152 and the hostname `datadrive-mock`, so
  nobody ends up on a real dongle by following a link shown in the UI. Passwords are
  kept in plain text in memory (no PBKDF2).

* API v4 (docs/12_api_v4.md): GET /api/wifi/scan (2.5 s, 409 BUSY while `other_upload`), drive
  encryption GET /api/crypt, POST /api/crypt/setup|unlock|lock|password|disable|recovery|erase, `crypt` object in
  /api/status. Drive password of the scenarios: Geheimes-Passwort-12, recovery key
  7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S (setup issues a new random one). While locked, every
  file/folder/upload/download/push/mode/format request answers 423 DRIVE_LOCKED and the status
  shows owner "none" without sizes. A restart (reboot/update) locks an encrypted drive again. State
  "damaged" (key area unreadable, scenario crypt_damaged): locked, and every such request, unlock,
  password and recovery answer 423 CRYPT_DAMAGED; only crypt/erase (confirm=ERASE) helps. Wrong drive
  passwords: ONE global counter, 2 free tries, then 5 s doubling up to 300 s (429 LOCKED + Retry-After);
  RAM only (restart resets). POST /api/format on an unlocked encrypted drive: no restart, stays unlocked.
  503 AUTH_UNAVAILABLE via /mock/set?auth_unavailable=1.
  Optional HTTPS (experimental): `https`, `https_active`, `https_restart`, `cert_sha256`, `ca_sha256`,
  `cert_expires` in /api/status, POST /api/settings/https?value=0|1[&renew=1][&regenerate=1] (admin),
  GET /api/https/ca.cer (user; 404 NOT_FOUND until the first enable). The mock keeps serving plain HTTP:
  the setting only becomes `https_active` after a restart (reboot/update) or /mock/set?https=0|1.

Scenarios (`--scenario NAME` or `GET /mock/scenario?name=NAME`)
---------------------------------------------------------------
    usb_active    default: drive provided to a connected USB host, a few files, 2 folders
    default_login same content as usb_active, but admin / admin: forced password change
    two_users     owner app, files + folders; accounts admin/admin123 and role user: gast/gast123
    usb_no_host   drive provided to USB, but no host connected (take-over needs no force)
    files         owner app, 14 varied files, nested folders (3-4 levels, umlauts and spaces,
                  an empty folder "Leerer Ordner"), 2 hidden folders
    empty         owner app, no files
    full          owner app, almost no free space (180 KiB)
    no_fs         no readable file system (needs_format), microSD
    ap_setup      Wi-Fi not configured, setup access point active, no stored network
    flash_medium  internal flash (10.8 MB) instead of the microSD card
    many          owner app, 300 files + folders (also an empty one), list not truncated
    truncated     owner app, 1200 files, list truncated (32 KiB list buffer like the firmware)
    crypt_enabled owner app, files + folders, encrypted drive, unlocked (also gast/gast123)
    crypt_locked  same, but locked (423 DRIVE_LOCKED, no medium for USB)
    crypt_damaged encrypted, key area unreadable: locked + damaged (423 CRYPT_DAMAGED; only crypt/erase)
A scenario switch resets all mock settings below (uptime keeps running), including users
(back to admin/admin123), sessions (everybody is logged out) and the stored Wi-Fi networks.

Control endpoints (GET, no CSRF header, no login, exist only in the mock)
-------------------------------------------------------------------------
    /mock                 short help (JSON)
    /mock/state           internal state incl. users (no passwords), session count, tree
                          summary, Wi-Fi list, file list and the current /api/status answer
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
    dirs=<n>                number of hidden folders in the main directory
    wifi=sta|ap|connecting
    wifi_count=<0..5>       number of stored Wi-Fi networks (first = the current one)
    upload_kbps=<n>         upload/update rate limit in KiB/s (0 = unlimited)
    copy_kbps=<n>           /api/copy takes size/rate seconds (0 = instant; blocks the server)
    delay_ms=<n>            artificial latency of every /api request
    online=0|1              0: connections are accepted and reset without an answer
    auth=0|1                0: no authentication at all, everybody is admin (old tests);
                            reset to 1 by a scenario switch unless started with --no-auth
    default_password=1|0    1: admin password is admin (forced change, MUST_CHANGE_PASSWORD);
                            0: admin123 (sessions stay valid)
    devices=<0..8>          replaces all remembered devices by n new ones for admin
                            (labels iPhone, Windows-PC, iPad, ...); their cookies are unknown
    login_locked=1|0       1: every login is refused with LOCKED for 30 s; 0 clears every lock-out
    auth_unavailable=1|0   1: session, login and protected requests answer 503 AUTH_UNAVAILABLE
    crypt=off|locked|unlocked|damaged   drive encryption state (files stay); crypt_password=<pw>
    crypt_kdf_ms=<n>        duration of unlock/password/recovery (default 800)
    crypt_setup_ms=<n>      extra duration of setup / erase (the whole-medium erase; default 300)
    https=0|1               HTTPS setting AND active, as if the device had restarted (fingerprints are made
                            on the first enable); the API route only sets the setting (https_restart:true)
    crypt_setup_io=1        the next setup fails with 500 IO after the key area was written: drive is
                            encrypted + unlocked and the answer still carries recovery_key
    scan_ms=<n>             duration of GET /api/wifi/scan (default 2500)
    session_expire=1        all sessions end, the next request answers 401 UNAUTHORIZED
    fail_next=<CODE>        the next POST fails with that error code (proper HTTP status
                            and German text; works for every POST of API v3). `CODE@/api/path`
                            limits it to one path, e.g. NO_SPACE@/api/copy, and then also
                            applies to a GET of that path (e.g. IO@/api/files). `none` clears.
    upload_fail_at=<pct>    the next upload/push connection is reset at that percentage
    other_upload=0|1        status shows an upload by another client; mode, upload,
                            push, mkdir, delete, move, copy, format and reboot answer BUSY
    ota_fail=<CODE>|none    every /api/ota fails with that code (after the body, except
                            BUSY/HOST_WRITING/TOO_LARGE) until cleared
    next_version=<x.y.z>    version reported after the next successful /api/ota
    reset=1                 back to the start of the current scenario
"""
from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import collections
import hashlib
import hmac
import io
import json
import math
import random
import re
import secrets
import select
import signal
import socket
import socketserver
import struct
import sys
import time
import traceback
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote_to_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = "firmware/printdrive/main/web/index.html"

VERSION = "0.3.0"
NEXT_VERSION = "0.3.1"           # reported after a successful POST /api/ota
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
PATH_DEPTH_MAX = 8                  # levels of a path (contract 3.1)
PATH_BYTES_MAX = 240                # total length of a path in bytes
USERS_MAX = 8
SESSIONS_MAX = 8
SESSION_IDLE_S = 12 * 3600          # a session expires after 12 h without a request
LOCK_FAILS = 5                      # wrong passwords in a row (per peer address) ...
LOCK_S = 30                         # ... lock that peer for this long (PEER_LOCK_BASE_US), doubled each time
LOCK_MAX_S = 15 * 60                # ... up to this (PEER_LOCK_MAX_US)
LOCK_PEERS_MAX = 16                 # PEERS_MAX: tracked peer addresses (least recently used is replaced)
LOCK_RECENT_S = 12 * 3600           # a peer with a verified password this recently is exempt from the global cap
GLOBAL_FAILS = 30                   # more than this many failures within ...
GLOBAL_WINDOW_S = 60                # ... this window restrict password checks of all other peers for ...
GLOBAL_LOCK_S = 30                  # ... this long
DEVICES_PER_USER = 4                # trusted devices per user (DEVICES_PER_USER)
CRYPT_PASS_MIN = 12                 # drive password: characters
CRYPT_PASS_MAX = 64                 # drive password: bytes
CRYPT_KDF_MS = 800                  # simulated PBKDF2 time of unlock / password change / setup
CRYPT_SETUP_MS = 300                # simulated erase of the whole medium in setup / erase (firmware: 10 s .. 2 min)
CRYPT_FREE_FAILS = 2                # wrong drive passwords without any wait
CRYPT_WAIT_S = 5                    # then this wait, doubling ...
CRYPT_WAIT_MAX_S = 300              # ... up to this
SCAN_MS = 2500                      # simulated duration of GET /api/wifi/scan
SCAN_MAX = 20                       # networks in the answer
CRYPT_DEFAULT_PASSWORD = "Geheimes-Passwort-12"
CRYPT_DEFAULT_RECOVERY = "7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S"
WIFI_MAX = 5
OTA_MAX = 0x280000                  # size of one firmware partition
OTA_VERIFY_S = 15                   # a new firmware counts as good after this uptime
SESSION_COOKIE = "dd_session"
DEVICES_MAX = 8                     # remembered ("trusted") devices
DEVICE_MAX_AGE = 31536000           # cookie lifetime of a trusted device (1 year)
DEVICE_LABEL_MAX = 32               # bytes
DEVICE_POOL = ["iPhone", "Windows-PC", "iPad", "Android-Handy", "MacBook", "Linux-Laptop", "Pixel", "Surface"]
HIDDEN_DIR_NAMES = ["System Volume Information", ".Trash-1000", ".Spotlight-V100", ".fseventsd",
                    ".TemporaryItems", ".Trashes", ".DS_Store_dir", ".git_tmp"]
WIFI_POOL = ["Werkstatt", "Büro Gast", "Handy-Hotspot", "Keller 2.4G"]

# code -> (HTTP status, German text). Texts are those of drive_result_str() in drive.c.
ERRORS = {
    "BAD_NAME": (400, "Ungültiger Dateiname."),
    "BAD_REQUEST": (400, "Parameter fehlt oder ist ungültig"),
    "INCOMPLETE": (400, "Die Übertragung war unvollständig."),
    "BAD_USER": (400, "Ungültiger Benutzername. Erlaubt sind 1 bis 24 Zeichen: Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich."),
    "BAD_PASSWORD": (400, "Das Passwort muss zwischen 5 und 64 Zeichen lang sein."),
    "IS_DIR": (400, "Das ist ein Ordner, keine Datei."),
    "UNAUTHORIZED": (401, "Bitte zuerst anmelden."),
    "BAD_LOGIN": (401, "Benutzername oder Passwort ist falsch."),
    "FORBIDDEN": (403, "Anfrage nicht erlaubt."),
    "FORBIDDEN_ROLE": (403, "Dafür sind Administratorrechte nötig."),
    "MUST_CHANGE_PASSWORD": (403, "Bitte zuerst das Passwort ändern."),
    "NOT_FOUND": (404, "Die Datei oder der Ordner wurde nicht gefunden."),
    "BUSY": (409, "Es läuft gerade eine Dateiübertragung. Bitte warten, bis sie abgeschlossen ist."),
    "HOST_ACTIVE": (409, "Das USB-Gerät verwendet das Laufwerk noch. Bitte dort zuerst auswerfen oder das Trennen bestätigen."),
    "HOST_WRITING": (409, "Das USB-Gerät schreibt gerade auf das Laufwerk. Bitte warten, bis es fertig ist."),
    "WRONG_OWNER": (409, "Das geht nur, solange die Dateiverwaltung aktiv ist."),
    "NO_FS": (409, "Der Speicher ist nicht lesbar und muss formatiert werden."),
    "EXISTS": (409, "Eine Datei oder ein Ordner mit diesem Namen gibt es bereits."),
    "DIR_NOT_EMPTY": (409, "Der Ordner ist nicht leer."),
    "LAST_ADMIN": (409, "Der letzte Administrator kann nicht entfernt werden."),
    "USER_LIMIT": (409, "Es sind bereits 8 Benutzer angelegt. Bitte zuerst einen löschen."),
    "WIFI_LIMIT": (409, "Es sind bereits 5 WLANs gespeichert. Bitte zuerst eines entfernen."),
    "TOO_LARGE": (413, "Die Datei ist zu groß – möglich sind höchstens 4 GB pro Datei."),
    "BAD_TYPE": (415, "Dieser Dateityp ist nicht erlaubt."),
    "INVALID_FILE": (422, "Die Datei ist unvollständig oder beschädigt (keine gültige 3MF-Datei)."),
    "IO": (500, "Lese- oder Schreibfehler auf dem Speicher."),
    "LOCKED": (429, "Zu viele Fehlversuche. Bitte etwas warten und dann erneut versuchen."),
    "NO_SPACE": (507, "Nicht genug freier Speicher."),
    # API v3 addition (auth_result_str of auth.c)
    "AUTH_UNAVAILABLE": (503, "Die Benutzerverwaltung ist nicht lesbar. Bitte die Taste am Gerät 10 Sekunden halten, "
                              "um die Anmeldung zurückzusetzen."),
    # API v4 (docs/12_api_v4.md)
    "CRYPT_WEAK_PASSWORD": (400, "Das Laufwerks-Passwort muss mindestens 12 Zeichen lang sein."),
    "CRYPT_WRONG_PASSWORD": (403, "Das Laufwerks-Passwort oder der Wiederherstellungsschlüssel ist falsch."),
    "CRYPT_NOT_ENABLED": (409, "Die Verschlüsselung ist nicht eingeschaltet."),
    "CRYPT_ALREADY_ENABLED": (409, "Die Verschlüsselung ist bereits eingeschaltet."),
    "DRIVE_LOCKED": (423, "Das Laufwerk ist gesperrt. Bitte zuerst entsperren."),
    "CRYPT_DAMAGED": (423, "Der Schlüsselbereich des Laufwerks ist beschädigt. Nur Löschen und neu einrichten hilft."),
    "CRYPT_UNAVAILABLE": (503, "Die Verschlüsselung ist auf diesem Speicher nicht möglich."),
}
TEXT_DEFAULT_PASSWORD = "Bitte ein anderes Passwort als das Auslieferungspasswort wählen."
TEXT_NO_USER = "Diesen Benutzer gibt es nicht."
TEXT_USER_EXISTS = "Diesen Benutzer gibt es schon."
TEXT_NO_WIFI = "Dieses WLAN ist nicht gespeichert."
TEXT_BAD_FIRMWARE = "Das ist keine gültige Firmware-Datei für diesen Chip."
# With fail_next on /api/upload, /api/push and /api/ota these are reported after the body was received (like a
# failed final check on the device), all others right away.
LATE_UPLOAD_ERRORS = {"INVALID_FILE", "INCOMPLETE", "IO"}
UPLOAD_PATHS = ("/api/upload", "/api/push", "/api/ota")
# Everything that touches the content or the owner of the drive: 423 DRIVE_LOCKED while the drive is locked (API v4).
LOCKED_ROUTES = {("GET", "/api/files"), ("GET", "/api/download"), ("POST", "/api/mode"), ("POST", "/api/mkdir"),
                 ("POST", "/api/delete"), ("POST", "/api/move"), ("POST", "/api/copy"),
                 ("POST", "/api/upload/check"), ("POST", "/api/upload"), ("POST", "/api/push"),
                 ("POST", "/api/format")}

SCENARIO_HELP = {
    "usb_active": "default: drive provided to a connected USB host, a few files and folders",
    "default_login": "like usb_active, but admin/admin: delivery password, change is forced (MUST_CHANGE_PASSWORD)",
    "two_users": "owner app, files and folders; accounts admin/admin123 and user 'gast'/'gast123'",
    "usb_no_host": "drive provided to USB, but no host connected",
    "files": "owner app, 14 varied files + nested folders (umlauts, empty folder), 2 hidden folders",
    "empty": "owner app, no files",
    "full": "owner app, almost no free space",
    "no_fs": "no readable file system (needs_format)",
    "ap_setup": "Wi-Fi not configured, setup access point active",
    "flash_medium": "internal flash (10.8 MB) instead of microSD",
    "many": "owner app, 300 files + a few folders, not truncated",
    "truncated": "owner app, 1200 files, list truncated",
    "crypt_enabled": "encrypted drive, unlocked, owner app, files + folders; drive password Geheimes-Passwort-12",
    "crypt_damaged": "encrypted drive, key area unreadable: locked + damaged (423 CRYPT_DAMAGED; only crypt/erase helps)",
    "crypt_locked": "encrypted drive, locked (423 DRIVE_LOCKED, USB shows no medium); password Geheimes-Passwort-12, "
                    "recovery key 7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S; accounts admin/admin123 and gast/gast123",
}

# Fake Wi-Fi networks in range for GET /api/wifi/scan: (ssid, rssi, secure)
SCAN_POOL = [
    ("MeinWLAN", -52, True), ("FRITZ!Box 7590 KL", -61, True), ("Büro Gast", -66, True),
    ("Café \"Zur Linde\"", -69, False), ("Werkstatt", -72, True), ("Müller's Größe-WLAN", -75, True),
    ("Handy-Hotspot", -78, True), ("Keller 2.4G", -81, True), ("Telekom_FON", -84, False),
    ("Gäste-Netz \"Zimmer 4\"", -86, True), ("DIRECT-roku-3F9A", -88, True), ("UPC8472913", -89, True),
] + [(f"Nachbar-WLAN-{i:02d}", -86 - i, True) for i in range(1, 13)]      # more than SCAN_MAX in range

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
    "dirs": "number of hidden folders in the main directory",
    "wifi": "sta|ap|connecting",
    "upload_kbps": "KiB/s, 0 = unlimited",
    "delay_ms": "latency per /api request",
    "online": "0|1",
    "fail_next": "error CODE for the next POST, optional CODE@/api/path (then GET of that path too), 'none' clears",
    "upload_fail_at": "percent at which the next upload connection is reset, 'none' clears",
    "other_upload": "0|1",
    "default_password": "1|0 (1: admin password is admin, every request except the password change gets MUST_CHANGE_PASSWORD; 0: admin123)",
    "devices": "0..8 (creates that many remembered devices for admin: iPhone, Windows-PC, ...)",
    "auth": "0|1 (0: no authentication at all, everybody is admin; reset by a scenario switch)",
    "login_locked": "0|1 (1: logins are refused with LOCKED for 30 s, Retry-After header; 0 clears every lock-out)",
    "auth_unavailable": "0|1 (1: user database unreadable: session, login and every protected request answer 503 AUTH_UNAVAILABLE)",
    "crypt": "off|locked|unlocked|damaged (drive encryption state; the files stay)",
    "crypt_password": "drive password the mock expects (default Geheimes-Passwort-12)",
    "crypt_kdf_ms": "ms the crypt endpoints take (simulated PBKDF2), default 800",
    "crypt_setup_ms": "extra ms of crypt/setup and crypt/erase (erasing the whole medium), default 300",
    "https": "0|1 (HTTPS setting and https_active at once, like after a restart; the mock itself stays plain HTTP)",
    "crypt_setup_io": "0|1 (1: the next crypt/setup fails with 500 IO after the key area was written; recovery_key is still in the answer)",
    "scan_ms": "ms GET /api/wifi/scan takes, default 2500",
    "session_expire": "1 (all sessions end, the next request gets 401)",
    "wifi_count": "0..5 (number of stored networks)",
    "copy_kbps": "KiB/s for /api/copy, 0 = unlimited (makes the blocking visible)",
    "ota_fail": "error CODE for every /api/ota until 'none' (e.g. INVALID_FILE, IO)",
    "next_version": "version reported after the next successful /api/ota (default 0.3.1)",
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


def parse_path(raw, existing: bool = False):
    """Contract 3.1. Returns (segments, None) or (None, "BAD_NAME").
    existing=False: path of something that is created (strict segment rules, 240 bytes).
    existing=True: path of something that is read/deleted/used as source (host-created names up
    to 255 bytes per segment; the 240-byte total only applies to new names)."""
    if raw is None:
        return None, "BAD_NAME"
    if raw == "":
        return [], None
    if raw.startswith("/") or raw.endswith("/"):
        return None, "BAD_NAME"
    segs = raw.split("/")
    if len(segs) > PATH_DEPTH_MAX:
        return None, "BAD_NAME"
    if not existing and len(raw.encode("utf-8", "surrogatepass")) > PATH_BYTES_MAX:
        return None, "BAD_NAME"
    for seg in segs:
        if (check_existing_name(seg) if existing else check_name(seg)):
            return None, "BAD_NAME"
    return segs, None


def make_png(size: int) -> bytes:
    """Small placeholder icon: blue square with a light 'USB stick' shape."""
    bg, fg, ink = bytes((0x1F, 0x6F, 0xEB)), bytes((0xF4, 0xF7, 0xFB)), bytes((0x1F, 0x6F, 0xEB))
    x0, x1 = size * 36 // 100, size * 64 // 100
    w = (x1 - x0) // 5
    rows = []
    for y in range(size):
        if size * 14 // 100 <= y < size * 30 // 100:                     # plug
            row = bg * (x0 + w) + fg * (x1 - x0 - 2 * w) + bg * (size - x1 + w)
        elif size * 30 // 100 <= y < size * 86 // 100:                   # body
            if size * 52 // 100 <= y < size * 58 // 100:                 # label strip
                row = bg * x0 + fg * w + ink * (x1 - x0 - 2 * w) + fg * w + bg * (size - x1)
            else:
                row = bg * x0 + fg * (x1 - x0) + bg * (size - x1)
        else:
            row = bg * size
        rows.append(bytes(1) + row)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (bytes((137, 80, 78, 71, 13, 10, 26, 10)) + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


MANIFEST_FALLBACK = json.dumps({
    "name": "WiFi DataDrive", "short_name": "DataDrive", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#1f6feb", "theme_color": "#1f6feb",
    "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"} for n in (180, 192, 512)],
}, ensure_ascii=False).encode("utf-8")
_PNG_CACHE = {}


def icon_png(size: int) -> bytes:
    if size not in _PNG_CACHE:
        _PNG_CACHE[size] = make_png(size)
    return _PNG_CACHE[size]


def valid_username(name) -> bool:
    return bool(name) and re.fullmatch(r"[A-Za-z0-9._-]{1,24}", name) is not None


def valid_password(pw) -> bool:
    return pw is not None and 5 <= len(pw.encode("utf-8", "surrogatepass")) <= 64


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

class Node:
    """A file or a folder of the in-memory tree. Children are keyed by name.lower() (FAT)."""
    __slots__ = ("name", "is_dir", "data", "size", "mtime", "children")

    def __init__(self, name: str, is_dir: bool = False, data: bytes = b"", size: int = 0, mtime: int = 0):
        self.name = name
        self.is_dir = is_dir
        self.data = data          # bytes really kept in memory
        self.size = size          # size reported by the API (may be larger than len(data))
        self.mtime = mtime
        self.children = {} if is_dir else None


def walk(node: Node, prefix: str = ""):
    """Yields (path, node) for everything below `node` (depth first, directory order)."""
    for child in list(node.children.values()):
        path = prefix + child.name
        yield path, child
        if child.is_dir:
            yield from walk(child, path + "/")


def subtree_size(node: Node) -> int:
    if not node.is_dir:
        return node.size
    return sum(subtree_size(c) for c in node.children.values())


def subtree_depth(node: Node) -> int:
    """Levels the node itself occupies (1 for a file or an empty folder)."""
    if not node.is_dir or not node.children:
        return 1
    return 1 + max(subtree_depth(c) for c in node.children.values())


def clone(node: Node, name: str) -> Node:
    c = Node(name, node.is_dir, node.data, node.size, node.mtime)
    if node.is_dir:
        for child in node.children.values():
            c.children[child.name.lower()] = clone(child, child.name)
    return c


def is_hidden(name: str) -> bool:
    return name.startswith(".") or name.lower() == "system volume information"


class Lockout:
    """Failure bookkeeping per peer address like auth.c: 5 failures in a row lock that peer for 30 s, every
    further lock-out doubles the time (up to 15 min), a verified password resets the peer. Optional global cap
    (more than 30 failures in 60 s: peers without a verified password in the last 12 h wait 30 s). `forced_until`
    is the mock's own switch (/mock/set?login_locked=1), valid for every peer."""

    def __init__(self, global_cap: bool = True):
        self.global_cap = global_cap
        self.clear()

    def clear(self):
        self.peers = collections.OrderedDict()    # addr -> dict(fails, lockouts, until, success)
        self.glob = collections.deque(maxlen=GLOBAL_FAILS)
        self.glob_until = 0.0
        self.forced_until = 0.0

    def _peer(self, addr: str) -> dict:
        p = self.peers.get(addr)
        if p is None:
            while len(self.peers) >= LOCK_PEERS_MAX:
                self.peers.popitem(last=False)
            p = self.peers[addr] = {"fails": 0, "lockouts": 0, "until": 0.0, "success": None}
        self.peers.move_to_end(addr)
        return p

    @staticmethod
    def _recent(p: dict, now: float) -> bool:
        return p["success"] is not None and now - p["success"] < LOCK_RECENT_S

    def blocked_s(self, addr: str) -> int:
        """0, or the seconds (>= 1) until a password may be checked for this peer."""
        now = time.monotonic()
        p = self._peer(addr)
        until = max(p["until"], self.forced_until)
        if self.global_cap and now < self.glob_until and not self._recent(p, now):
            until = max(until, self.glob_until)
        return math.ceil(until - now) if until > now else 0

    def success(self, addr: str):
        p = self._peer(addr)
        p.update(fails=0, lockouts=0, until=0.0, success=time.monotonic())

    def failure(self, addr: str) -> int:
        """Counts a wrong password. Returns the new lock-out time in seconds, else 0."""
        now = time.monotonic()
        p = self._peer(addr)
        p["fails"] += 1
        dur = 0
        if p["fails"] >= LOCK_FAILS:
            dur = min(LOCK_S * 2 ** p["lockouts"], LOCK_MAX_S)
            p["fails"] = 0
            p["lockouts"] += 1
            p["until"] = now + dur
        if self.global_cap:
            full = len(self.glob) == GLOBAL_FAILS
            oldest = self.glob[0] if full else None
            self.glob.append(now)
            if full and now - oldest < GLOBAL_WINDOW_S:
                self.glob_until = now + GLOBAL_LOCK_S
        return dur

    def fails_total(self) -> int:
        return sum(p["fails"] for p in self.peers.values())

    def remaining(self) -> float:
        now = time.monotonic()
        return max([self.forced_until - now] + [p["until"] - now for p in self.peers.values()] + [0.0])


class CryptThrottle:
    """Wrong drive passwords: ONE global counter (not per address). The first CRYPT_FREE_FAILS are free, then a wait
    of CRYPT_WAIT_S doubling up to CRYPT_WAIT_MAX_S. RAM only. A right password resets it."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.fails = 0
        self.until = 0.0

    def blocked_s(self, addr=None) -> int:
        now = time.monotonic()
        return math.ceil(self.until - now) if self.until > now else 0

    def success(self, addr=None):
        self.clear()

    def failure(self, addr=None) -> int:
        self.fails += 1
        dur = 0
        if self.fails > CRYPT_FREE_FAILS:
            dur = min(CRYPT_WAIT_S * 2 ** (self.fails - CRYPT_FREE_FAILS - 1), CRYPT_WAIT_MAX_S)
            self.until = time.monotonic() + dur
        return dur

    def fails_total(self) -> int:
        return self.fails


class Device:
    def __init__(self, upload_kbps: int, scenario: str = "usb_active", next_version: str = NEXT_VERSION,
                 default_auth: bool = True):
        self.default_upload_kbps = upload_kbps
        self.default_auth = default_auth
        self.next_version = next_version
        self.boot_t = time.monotonic()
        self.scenario = ""
        self.version = VERSION
        self.pending_version = None
        self.ota_partition = "ota_0"
        self.ota_verify_until = None      # monotonic time until which pending_verify is true
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
        self.root = Node("", True)        # main directory
        self.types = "all"
        self.wifi_mode = "sta"
        self.ssid = "MeinWLAN"
        self.networks = ["MeinWLAN"]      # stored networks (passwords are never kept/reported)
        self.wifi_connect_at = None
        self.clock_offset = None          # set by the firmware's undocumented upload param `now`
        # authentication
        self.auth_enabled = self.default_auth
        # The mock starts with the delivery password already changed; only `default_login` keeps admin/admin.
        self.users = [{"name": "admin", "role": "admin", "pw": "admin123"}]
        self.sessions = collections.OrderedDict()    # token -> {"user": name, "last": monotonic}
        self.devices = collections.OrderedDict()     # sha256(token) hex -> {"user", "label"}; survive restarts
        self.lockout = Lockout(True)
        self.cur_peer = "127.0.0.1"                  # address of the request being handled (set by the handler)
        self.auth_unavailable = False
        # drive encryption (API v4): enabled + unlocked; the files stay in self.root while locked
        self.crypt_enabled = False
        self.crypt_unlocked = True
        self.crypt_password = CRYPT_DEFAULT_PASSWORD
        self.crypt_recovery = CRYPT_DEFAULT_RECOVERY
        self.crypt_lockout = CryptThrottle()
        self.crypt_damaged = False
        self.https_on = False             # stored setting
        self.https_active = False         # in this boot
        self.https_dirty = False          # setting/certificate changed since boot
        self.cert_sha256 = self.ca_sha256 = self.cert_expires = ""
        self.crypt_setup_ms = CRYPT_SETUP_MS
        self.crypt_setup_io = False
        self.crypt_kdf_ms = CRYPT_KDF_MS
        self.scan_ms = SCAN_MS
        # mock controls
        self.online = True
        self.pending_boot = False
        self.offline_until = None
        self.delay_ms = 0
        self.fail_next = None             # (code, path or None)
        self.upload_fail_at = None        # percent
        self.other_upload = None          # monotonic start time
        self.upload_kbps = self.default_upload_kbps
        self.copy_kbps = 0
        self.ota_fail = None

    # ---------------------------------------------------------- tree helpers

    def resolve(self, segs):
        """Node at the path (list of segments) or None."""
        node = self.root
        for seg in segs:
            if not node.is_dir:
                return None
            node = node.children.get(seg.lower())
            if node is None:
                return None
        return node

    def mkdirs(self, path: str, age_s: float = 86400):
        """Scenario helper: creates the folder and its parents."""
        node = self.root
        for seg in path.split("/"):
            nxt = node.children.get(seg.lower())
            if nxt is None:
                nxt = Node(seg, True, mtime=local_now() - int(age_s))
                node.children[seg.lower()] = nxt
            node = nxt
        return node

    def add_file(self, path: str, size=None, age_s: float = 3600, mtime=None, data=None):
        data = sample_bytes(path) if data is None else data
        if mtime is None:
            mtime = local_now() - int(age_s)
        parent_path, _, name = path.rpartition("/")
        parent = self.mkdirs(parent_path) if parent_path else self.root
        parent.children[name.lower()] = Node(name, False, data, len(data) if size is None else size, mtime)

    def set_hidden_dirs(self, n: int):
        for key in [k for k, c in self.root.children.items() if c.is_dir and is_hidden(c.name)]:
            del self.root.children[key]
        for name in HIDDEN_DIR_NAMES[:n]:
            self.root.children[name.lower()] = Node(name, True, mtime=local_now() - 90 * 86400)

    def hidden_dir_count(self) -> int:
        return sum(1 for c in self.root.children.values() if c.is_dir and is_hidden(c.name))

    def _few_files(self):
        self.add_file("Benchy.3mf", 4_312_876, 2 * 3600)
        self.add_file("Würfel.3mf", 524_288, 26 * 3600)
        self.add_file("Halterung Kabelkanal v2.3mf", 812_300, 9 * 86400)

    def _few_folders(self):
        self.add_file("Projekte/Drachen/Drachenkopf.3mf", 6_291_456, 3 * 86400)
        self.add_file("Projekte/Drachen/Teile/Flügel links.3mf", 2_097_152, 3 * 86400)
        self.add_file("Projekte/Drachen/Teile/Flügel rechts.3mf", 2_097_152, 3 * 86400 + 60)
        self.add_file("Projekte/Drachen/Teile/Schwanz/Segment 01.gcode", 3_145_728, 4 * 86400)
        self.add_file("Projekte/Halterungen/Wandhalter.3mf", 733_184, 12 * 86400)
        self.add_file("Größen Prüfung äöü/Ölwanne für Küche.3mf", 1_048_576, 20 * 86400)
        self.add_file("Größen Prüfung äöü/Zweiter Ordner/Notiz zum Drucken.txt", None, 6 * 86400)
        self.mkdirs("Leerer Ordner")

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
        self.add_file("Würfel Varianten/Würfel 20mm.3mf", 311_296, 30 * 3600)
        self.add_file("Würfel Varianten/Klein/Würfel 10mm.3mf", 201_728, 30 * 3600)
        self.mkdirs("Neu")

    def _sc_default_login(self):
        self._sc_usb_active()
        self.users[0]["pw"] = "admin"          # delivery state: password change is forced

    def _sc_two_users(self):
        self._as_app()
        self._varied_files()
        self._few_folders()
        self.set_hidden_dirs(2)
        self.users.append({"name": "gast", "role": "user", "pw": "gast123"})

    def _sc_usb_no_host(self):
        self._few_files()
        self.usb_connected = False
        self.last_read = None

    def _sc_files(self):
        self._as_app()
        self._varied_files()
        self._few_folders()
        self.set_hidden_dirs(2)

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
        self.networks = []

    def _sc_flash_medium(self):
        self._as_app()
        self._set_medium(False)
        self.add_file("Würfel.3mf", 524_288, 26 * 3600)
        self.add_file("Kabelclip 4x.3mf", 1_253_376, 3 * 86400)

    def _sc_many(self):
        self._as_app()
        self._numbered_files(300, "Teil_")
        self._few_folders()
        self.add_file("Archiv 2025/Q1/Teil_alt_0001.3mf", 1_200_000, 300 * 86400)
        self.add_file("Archiv 2025/Q2/Teil_alt_0002.3mf", 1_300_000, 250 * 86400)
        self.mkdirs("Archiv 2025/Leer")

    def _sc_truncated(self):
        self._as_app()
        self._numbered_files(1200, "Serie_A_Bauteil_")
        self.set_hidden_dirs(3)

    def _sc_crypt_enabled(self):
        self._as_app()
        self._few_files()
        self._few_folders()
        self.users.append({"name": "gast", "role": "user", "pw": "gast123"})
        self.crypt_enabled, self.crypt_unlocked = True, True

    def _sc_crypt_locked(self):
        self._sc_crypt_enabled()
        self._crypt_lock()

    def _sc_crypt_damaged(self):
        self._sc_crypt_locked()
        self.crypt_damaged = True

    # ---------------------------------------------------------- drive encryption

    def locked_code(self) -> str:
        return "CRYPT_DAMAGED" if self.crypt_damaged else "DRIVE_LOCKED"

    def drive_locked(self) -> bool:
        return self.crypt_enabled and not self.crypt_unlocked

    def _crypt_lock(self):
        self.crypt_unlocked = False
        self.owner = "none"
        self.host_ejected = False
        self.last_read = self.last_write = None
        self.write_hold = False
        self.log("I", "crypt", "drive locked")

    def _crypt_unlock(self):
        """After unlocking the drive reports itself like after a change of mode."""
        self.crypt_unlocked = True
        self.log("I", "crypt", "drive unlocked")
        if self.fs_ok and self.usb_connected:
            self._provide()
        else:
            self._mount_app()

    @staticmethod
    def _norm_recovery(s: str) -> str:
        return re.sub(r"[\s-]", "", s or "").upper().replace("O", "0").replace("I", "1").replace("L", "1")

    def crypt_check(self, password, recovery) -> bool:
        if password is not None:
            return hmac.compare_digest(password.encode("utf-8", "surrogatepass"),
                                       self.crypt_password.encode("utf-8", "surrogatepass"))
        return hmac.compare_digest(self._norm_recovery(recovery).encode(), self._norm_recovery(self.crypt_recovery).encode())

    @staticmethod
    def _fp() -> str:
        return ":".join(f"{b:02X}" for b in secrets.token_bytes(32))

    def https_make(self, ca: bool, cert: bool):
        if ca or not self.ca_sha256:
            self.ca_sha256 = self._fp()
        if cert or ca or not self.cert_sha256:
            self.cert_sha256 = self._fp()
            self.cert_expires = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 820 * 86400))

    def crypt_check_any(self, secret) -> bool:
        """Drive password or (current) recovery key."""
        return self.crypt_check(secret, None) or self.crypt_check(None, secret)

    def scan(self) -> list:
        """Networks in range: strongest first, at most SCAN_MAX, with small random signal jitter."""
        found = [{"ssid": s, "rssi": max(-95, min(-30, r + random.randint(-3, 3))), "secure": sec,
                  "saved": s in self.networks} for s, r, sec in SCAN_POOL]
        found.sort(key=lambda n: -n["rssi"])
        return found[:SCAN_MAX]

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
            ("I", "main", f"WiFi DataDrive {self.version}"),
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
        self.sessions.clear()                   # sessions live in RAM only
        self.lockout.clear()
        self.crypt_lockout.clear()
        self.https_active, self.https_dirty = self.https_on, False
        if self.https_on:
            self.https_make(False, False)       # missing/expiring certificates are made at boot
        if self.crypt_enabled:                  # the key lives in RAM only: locked after every restart
            self.crypt_unlocked = False
        if self.pending_version:
            self.version, self.pending_version = self.pending_version, None
            self.ota_verify_until = at + OTA_VERIFY_S
        if self.crypt_enabled:
            self.owner = "none"                 # locked: no medium for the USB host, nothing for the app
        elif self.needs_format:
            self.owner, self.fs_ok = "app", False
        elif self.fs_ok and self.usb_connected:
            self._provide()                     # boot policy: host present -> provide the drive
        else:
            self.owner = "app"
        self._boot_log()

    # ---------------------------------------------------------- drive state

    def used_by_files(self) -> int:
        return sum(subtree_size(c) for c in self.root.children.values())

    def free(self) -> int:
        return max(0, self.total - self.fill - self.used_by_files())

    def visible_file_count(self) -> int:
        def count(node):
            n = 0
            for c in node.children.values():
                if is_hidden(c.name):
                    continue
                n += count(c) if c.is_dir else 1
            return n
        return count(self.root)

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

    def upload_static_check(self, path, size: int):
        """drive_check_name() + size limit. Returns (segments, error code)."""
        segs, err = parse_path(path)
        if err:
            return None, err
        if not segs:
            return None, "BAD_NAME"
        err = check_name(segs[-1], self.types)       # BAD_TYPE
        if err:
            return None, err
        if size > 0xFFFFFFFF:
            return None, "TOO_LARGE"
        if size == 0:
            return None, "INVALID_FILE"
        return segs, None

    def upload_app_check(self, segs, size: int, overwrite: bool):
        """drive_upload_begin(): everything that depends on the drive state."""
        err = self.require_app_fs()
        if err:
            return err
        if self.other_upload is not None:
            return "BUSY"
        parent = self.resolve(segs[:-1])
        if parent is None or not parent.is_dir:
            return "NOT_FOUND"
        old = parent.children.get(segs[-1].lower())
        if old is not None and old.is_dir:
            return "IS_DIR"
        if old is not None and not overwrite:
            return "EXISTS"
        # The old file is only removed after success, so the new one needs its full size.
        if size + FREE_RESERVE > self.free():
            return "NO_SPACE"
        return None

    def upload_precheck(self, path, size: int, overwrite: bool):
        segs, err = self.upload_static_check(path, size)
        return err or self.upload_app_check(segs, size, overwrite)

    def store_upload(self, segs, data: bytes, size: int, mtime: int):
        if mtime < MTIME_MIN:                    # not given: the device clock, if it was ever set
            mtime = local_now() + self.clock_offset if self.clock_offset is not None else 0
        parent = self.resolve(segs[:-1])
        name = segs[-1]
        if parent is None or not parent.is_dir:  # folder vanished meanwhile (cannot happen: one request at a time)
            return
        parent.children.pop(name.lower(), None)  # overwrite replaces; new directory entry at the end
        parent.children[name.lower()] = Node(name, False, data, size, mtime)
        self.log("I", "drive", f"upload ok '{'/'.join(segs)}'")

    def _fs_ready(self):
        err = self.require_app_fs()
        if err:
            return err
        return "BUSY" if self.other_upload is not None else None

    def list_dir(self, dir_path):
        """Returns (listing dict, error code)."""
        segs, err = parse_path(dir_path, existing=True)
        err = err or self.require_app_fs()
        if err:
            return None, err
        node = self.resolve(segs)
        if node is None or not node.is_dir:
            return None, "NOT_FOUND"
        out, used, truncated = [], 0, False
        for c in node.children.values():
            if is_hidden(c.name):
                continue
            need = 8 + len(c.name.encode("utf-8", "surrogatepass")) + 1   # packed list buffer of the firmware
            if used + need > LIST_BUF_SIZE:
                truncated = True
                continue
            used += need
            out.append({"name": c.name, "type": "dir" if c.is_dir else "file",
                        "size": 0 if c.is_dir else c.size, "mtime": 0 if c.is_dir else c.mtime})
        return {"ok": True, "dir": "/".join(segs), "entries": out, "truncated": truncated}, None

    def get_file(self, path):
        segs, err = parse_path(path, existing=True)
        err = err or self.require_app_fs()
        if not err and not segs:
            err = "IS_DIR"
        if err:
            return None, err
        node = self.resolve(segs)
        if node is None:
            return None, "NOT_FOUND"
        if node.is_dir:
            return None, "IS_DIR"
        return node, None

    def mkdir(self, path):
        segs, err = parse_path(path)
        if not err and not segs:
            err = "BAD_NAME"
        err = err or self._fs_ready()
        if err:
            return err
        parent = self.resolve(segs[:-1])
        if parent is None or not parent.is_dir:
            return "NOT_FOUND"
        if segs[-1].lower() in parent.children:
            return "EXISTS"
        parent.children[segs[-1].lower()] = Node(segs[-1], True, mtime=local_now() + (self.clock_offset or 0))
        self.log("I", "drive", f"mkdir '{'/'.join(segs)}'")
        return None

    def delete(self, path, recursive: bool):
        segs, err = parse_path(path, existing=True)
        if not err and not segs:
            err = "BAD_NAME"
        err = err or self._fs_ready()
        if err:
            return err
        node = self.resolve(segs)
        if node is None:
            return "NOT_FOUND"
        if node.is_dir and node.children and not recursive:
            return "DIR_NOT_EMPTY"
        del self.resolve(segs[:-1]).children[segs[-1].lower()]
        self.log("I", "drive", f"deleted '{'/'.join(segs)}'")
        return None

    def _move_copy_prepare(self, src_path, dst_path, copy: bool):
        """Common checks of move and copy. Returns (error, src_segs, dst_segs, src_node, dst_parent, dst_old)."""
        src, err = parse_path(src_path, existing=True)
        if not err and not src:
            err = "BAD_NAME"
        dst = None
        if not err:
            dst, err = parse_path(dst_path)
            if not err and not dst:
                err = "BAD_NAME"
        err = err or self._fs_ready()
        if err:
            return err, None, None, None, None, None
        node = self.resolve(src)
        if node is None:
            return "NOT_FOUND", None, None, None, None, None
        dst_parent = self.resolve(dst[:-1])
        if dst_parent is None or not dst_parent.is_dir:
            return "NOT_FOUND", None, None, None, None, None
        lsrc, ldst = [s.lower() for s in src], [s.lower() for s in dst]
        if node.is_dir and len(ldst) > len(lsrc) and ldst[:len(lsrc)] == lsrc:
            return "BAD_NAME", None, None, None, None, None       # folder into itself
        if len(dst) - 1 + subtree_depth(node) > PATH_DEPTH_MAX:
            return "BAD_NAME", None, None, None, None, None       # would exceed 8 levels
        old = dst_parent.children.get(dst[-1].lower())
        return None, src, dst, node, dst_parent, old

    def move(self, src_path, dst_path):
        """Rename and/or move (drive_move())."""
        err, src, dst, node, dst_parent, old = self._move_copy_prepare(src_path, dst_path, False)
        if err:
            return err
        if self.types != "all" and not node.is_dir and check_name(dst[-1], self.types):
            return "BAD_TYPE"
        if old is not None and old is not node:
            return "EXISTS"
        src_parent = self.resolve(src[:-1])
        if src_parent is dst_parent:
            # same folder: keep the position in the directory (only the name changes)
            old_key, new_key = src[-1].lower(), dst[-1].lower()
            node.name = dst[-1]
            dst_parent.children = {(new_key if k == old_key else k): c for k, c in dst_parent.children.items()}
        else:
            del src_parent.children[src[-1].lower()]
            node.name = dst[-1]
            dst_parent.children[dst[-1].lower()] = node
        self.log("I", "drive", f"moved '{'/'.join(src)}' -> '{'/'.join(dst)}'")
        return None

    def copy(self, src_path, dst_path):
        """Recursive copy (drive_copy()). Blocks like the real device while it works."""
        err, src, dst, node, dst_parent, old = self._move_copy_prepare(src_path, dst_path, True)
        if err:
            return err
        if old is not None:
            return "EXISTS"
        total = subtree_size(node)
        if total + FREE_RESERVE > self.free():
            return "NO_SPACE"
        if self.copy_kbps > 0:
            end = time.monotonic() + total / (self.copy_kbps * 1024)
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.25))
        dst_parent.children[dst[-1].lower()] = clone(node, dst[-1])
        self.log("I", "drive", f"copied '{'/'.join(src)}' -> '{'/'.join(dst)}' ({total} bytes)")
        return None

    def format(self):
        self.root = Node("", True)
        self.fill = 0
        self.needs_format = False
        self.fs_ok = True
        self.log("W", "drive", "format requested - restarting")

    # ---------------------------------------------------------- authentication

    def find_user(self, name):
        lname = (name or "").lower()
        for u in self.users:
            if u["name"].lower() == lname:
                return u
        return None

    def is_default_password(self, u) -> bool:
        return u["name"].lower() == "admin" and u["pw"] == "admin"

    def blocked_s(self) -> int:
        """Seconds (Retry-After) until the current peer may try a password again, 0 if it may now."""
        return self.lockout.blocked_s(self.cur_peer)

    def locked(self) -> bool:
        return self.blocked_s() > 0

    def note_failure(self):
        dur = self.lockout.failure(self.cur_peer)
        if dur:
            self.log("W", "auth", f"{LOCK_FAILS} failed password checks from {self.cur_peer} - locked for {dur} s")

    def check_credentials(self, name, pw):
        """Returns the user, or None (and counts the failure)."""
        u = self.find_user(name)
        ok = u is not None and hmac.compare_digest(u["pw"].encode("utf-8", "surrogatepass"),
                                                   (pw or "").encode("utf-8", "surrogatepass"))
        if ok:
            self.lockout.success(self.cur_peer)
            return u
        self.note_failure()
        return None

    def new_session(self, u) -> str:
        while len(self.sessions) >= SESSIONS_MAX:
            self.sessions.popitem(last=False)         # the oldest one is displaced
        token = secrets.token_hex(32)
        self.sessions[token] = {"user": u["name"], "last": time.monotonic()}
        return token

    @staticmethod
    def token_hash(token) -> str:
        return hashlib.sha256((token or "").encode("utf-8", "surrogatepass")).hexdigest()

    def new_device(self, u, label: str, token=None, per_user_limit: bool = True):
        """Stores a trusted device and returns its token, or None if there is no slot (then the login only
        gets a volatile session). Like auth.c: the same user with the same label replaces the old entry; at
        DEVICES_PER_USER the user's oldest is replaced; other users' devices are never displaced."""
        label = label or "Unbekanntes Gerät"
        if per_user_limit:
            own = [h for h, d in self.devices.items() if d["user"].lower() == u["name"].lower()]
            same = [h for h in own if self.devices[h]["label"] == label]
            if same:
                del self.devices[same[0]]
            elif len(own) >= DEVICES_PER_USER:
                del self.devices[own[0]]              # insertion order: the first one is the oldest
            if len(self.devices) >= DEVICES_MAX:
                return None
        else:                                         # /mock/set?devices=n: test helper, no per-user limit
            while len(self.devices) >= DEVICES_MAX:
                self.devices.popitem(last=False)
        token = token or secrets.token_hex(32)
        self.devices[self.token_hash(token)] = {"user": u["name"], "label": label}
        return token

    def end_devices(self, name, keep=None):
        for h in [h for h, d in self.devices.items() if d["user"].lower() == name.lower() and h != keep]:
            del self.devices[h]

    def session_user(self, token):
        if token and token not in self.sessions:
            d = self.devices.get(self.token_hash(token))
            return self.find_user(d["user"]) if d else None
        s = self.sessions.get(token) if token else None
        if s is None:
            return None
        now = time.monotonic()
        u = self.find_user(s["user"])
        if u is None or now - s["last"] > SESSION_IDLE_S:
            del self.sessions[token]
            return None
        s["last"] = now
        return u

    def end_sessions(self, name, keep=None):
        for tok in [t for t, s in self.sessions.items() if s["user"].lower() == name.lower() and t != keep]:
            del self.sessions[tok]

    def admin_count(self) -> int:
        return sum(1 for u in self.users if u["role"] == "admin")

    # ---------------------------------------------------------- JSON views

    def status(self) -> dict:
        read_idle, write_idle = self.read_idle_ms(), self.write_idle_ms()
        idles = [x for x in (read_idle, write_idle) if x >= 0]
        locked = self.drive_locked()
        # without a file system (or while the drive is locked) the device does not know the sizes
        known = not self.needs_format and not locked
        upload = {"active": False, "name": "", "received": 0, "total": 0}
        if self.other_upload is not None:
            total = 24 * 1024 * 1024
            rate = (self.upload_kbps or 400) * 1024
            received = int((time.monotonic() - self.other_upload) * rate) % total
            upload = {"active": True, "name": "Upload von anderem Gerät.3mf", "received": received, "total": total}
        sta = self.wifi_mode == "sta"
        ap = self.wifi_mode in ("ap", "connecting")
        return {
            "version": self.version,
            "uptime_s": max(0, int(time.monotonic() - self.boot_t)),
            "heap": 140000 + random.randrange(-6, 7) * 512,
            "types": self.types,
            "drive": {
                "owner": "none" if locked else self.owner, "fs_ok": self.fs_ok and not locked,
                "needs_format": self.needs_format and not locked,
                "usb_connected": self.usb_connected, "usb_suspended": self.usb_suspended,
                "host_ejected": self.host_ejected, "host_writable": self.host_writable,
                "host_idle_ms": min(idles) if idles else -1, "host_write_idle_ms": write_idle,
                "write_guard_ms": WRITE_GUARD_MS,
                "total": self.total if known else 0, "free": self.free() if known else 0,
                "files": self.visible_file_count() if known else 0, "sector": self.sector,
            },
            "crypt": {"enabled": self.crypt_enabled, "unlocked": self.crypt_unlocked or not self.crypt_enabled,
                      "damaged": self.crypt_damaged},
            "https": self.https_on, "https_active": self.https_active, "https_restart": self.https_dirty,
            "cert_sha256": self.cert_sha256, "ca_sha256": self.ca_sha256, "cert_expires": self.cert_expires,
            "medium": {"sd": self.sd, "name": self.medium_name},
            "upload": upload,
            "wifi": {
                "sta_configured": self.wifi_mode != "ap", "sta_connected": sta,
                "ssid": "" if self.wifi_mode == "ap" else self.ssid,
                "ip": FAKE_IP if sta else "", "rssi": -61 + random.randrange(-2, 3) if sta else 0,
                "ap_active": ap, "ap_ssid": "DataDrive-54D5", "ap_ip": "192.168.4.1" if ap else "",
                "hostname": "datadrive-mock",
            },
            "ota": {"partition": self.ota_partition,
                    "pending_verify": self.ota_verify_until is not None and time.monotonic() < self.ota_verify_until},
        }

    def tree_summary(self) -> dict:
        dirs = files = 0
        depth = 0
        for path, n in walk(self.root):
            depth = max(depth, path.count("/") + 1)
            if n.is_dir:
                dirs += 1
            else:
                files += 1
        return {"dirs": dirs, "files": files, "max_depth": depth, "used": self.used_by_files(),
                "root": [c.name + ("/" if c.is_dir else "") for c in list(self.root.children.values())[:40]]}

    def flat_files(self) -> list:
        return [{"path": p, "type": "dir"} if n.is_dir else
                {"path": p, "type": "file", "size": n.size, "stored": len(n.data), "mtime": n.mtime}
                for p, n in walk(self.root)]

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
            "types": self.types, "dirs": self.hidden_dir_count(), "wifi": self.wifi_mode,
            "upload_kbps": self.upload_kbps, "delay_ms": self.delay_ms,
            "fail_next": None if self.fail_next is None else
            self.fail_next[0] + ("@" + self.fail_next[1] if self.fail_next[1] else ""),
            "upload_fail_at": self.upload_fail_at,
            "other_upload": self.other_upload is not None,
            "auth": self.auth_enabled,
            "users": [{"name": u["name"], "role": u["role"], "default_password": self.is_default_password(u)}
                      for u in self.users],
            "sessions": len(self.sessions),
            "devices": [{"id": h[:8], "user": d["user"], "label": d["label"]} for h, d in self.devices.items()],
            "login_locked": round(self.lockout.remaining(), 1) if self.lockout.remaining() > 0 else False,
            "failed_logins": self.lockout.fails_total(),
            "auth_unavailable": self.auth_unavailable,
            "crypt": {"enabled": self.crypt_enabled, "unlocked": self.crypt_unlocked or not self.crypt_enabled,
                      "damaged": self.crypt_damaged, "retry_after": self.crypt_lockout.blocked_s(),
                      "kdf_ms": self.crypt_kdf_ms, "failed_unlocks": self.crypt_lockout.fails_total()},
            "scan_ms": self.scan_ms,
            "wifi_networks": [{"ssid": n, "active": self.wifi_mode == "sta" and n == self.ssid}
                              for n in self.networks],
            "copy_kbps": self.copy_kbps, "ota_fail": self.ota_fail,
            "next_version": self.next_version, "version": self.version,
            "tree": self.tree_summary(),
            "files": self.flat_files(),
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
                    "online", "other_upload", "reset", "host_reading", "auth", "default_password", "login_locked", "session_expire",
                    "auth_unavailable", "crypt_setup_io", "https"):
            if key in q:
                v[key] = parse_bool(q[key])
        if "crypt" in q:
            if q["crypt"].strip().lower() not in ("off", "locked", "unlocked", "damaged"):
                raise ValueError("crypt must be off, locked, unlocked or damaged")
            v["crypt"] = q["crypt"].strip().lower()
        if "crypt_password" in q:
            if not q["crypt_password"]:
                raise ValueError("crypt_password must not be empty")
            v["crypt_password"] = q["crypt_password"]
        for key in ("crypt_kdf_ms", "crypt_setup_ms", "scan_ms"):
            if key in q:
                v[key] = int(parse_nonneg(q[key]))
        if "sd" in q:
            medium = q["sd"].strip().lower()
            v["sd"] = True if medium == "sd" else False if medium == "flash" else parse_bool(medium)
        for key in ("total", "fill"):
            if key in q:
                v[key] = parse_size(q[key])
        for key in ("dirs", "upload_kbps", "delay_ms", "copy_kbps"):
            if key in q:
                v[key] = int(parse_nonneg(q[key]))
        if "devices" in q:
            n = int(parse_nonneg(q["devices"]))
            if n > DEVICES_MAX:
                raise ValueError(f"devices must be 0..{DEVICES_MAX}")
            v["devices"] = n
        if "wifi_count" in q:
            n = int(parse_nonneg(q["wifi_count"]))
            if n > WIFI_MAX:
                raise ValueError(f"wifi_count must be 0..{WIFI_MAX}")
            v["wifi_count"] = n
        if "ota_fail" in q:
            if is_cleared(q["ota_fail"]) or q["ota_fail"] == "0":
                v["ota_fail"] = None
            else:
                code = q["ota_fail"].strip().upper()
                if code not in ERRORS:
                    raise ValueError(f"ota_fail: unknown code '{code}', known: " + ", ".join(ERRORS))
                v["ota_fail"] = code
        if "next_version" in q:
            if not re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", q["next_version"]):
                raise ValueError("next_version must look like 0.3.1")
            v["next_version"] = q["next_version"]
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
            self.set_hidden_dirs(min(v["dirs"], len(HIDDEN_DIR_NAMES)))
        if "needs_format" in v:
            self.needs_format = v["needs_format"]
            self.fs_ok = not v["needs_format"]
        if "fs_ok" in v:
            self.fs_ok = v["fs_ok"]
        if "crypt_password" in v:
            self.crypt_password = v["crypt_password"]
        if "crypt_kdf_ms" in v:
            self.crypt_kdf_ms = v["crypt_kdf_ms"]
        if "crypt_setup_ms" in v:
            self.crypt_setup_ms = v["crypt_setup_ms"]
        if "https" in v:
            self.https_on = self.https_active = v["https"]
            self.https_dirty = False
            if self.https_on:
                self.https_make(False, False)
            notes.append(f"https: {int(self.https_on)}")
        if "crypt_setup_io" in v:
            self.crypt_setup_io = v["crypt_setup_io"]
        if "scan_ms" in v:
            self.scan_ms = v["scan_ms"]
        if "auth_unavailable" in v:
            self.auth_unavailable = v["auth_unavailable"]
        if "crypt" in v:
            mode = v["crypt"]
            self.crypt_lockout.clear()
            self.crypt_damaged = False
            if mode == "off":
                self.crypt_enabled, self.crypt_unlocked = False, True
                if self.owner == "none":
                    self.owner = "app"
            elif mode in ("locked", "damaged"):
                self.crypt_enabled = True
                self._crypt_lock()
                self.crypt_damaged = mode == "damaged"
            else:
                self.crypt_enabled = True
                if not self.crypt_unlocked or self.owner == "none":
                    self.crypt_unlocked = True
                    self.owner = "app"
            notes.append(f"crypt: {mode}")
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
            if v["wifi"] != "ap" and self.ssid not in self.networks and len(self.networks) < WIFI_MAX:
                self.networks.insert(0, self.ssid)
        if "wifi_count" in v:
            n = v["wifi_count"]
            first = self.ssid or "MeinWLAN"
            self.networks = ([first] + [x for x in WIFI_POOL if x != first])[:n]
            if n and self.wifi_mode != "ap":
                self.ssid = self.networks[0]
        if "devices" in v:
            self.devices.clear()
            admin = next((u for u in self.users if u["role"] == "admin"), self.users[0])
            for i in range(v["devices"]):
                self.new_device(admin, DEVICE_POOL[i % len(DEVICE_POOL)], per_user_limit=False)
        if "copy_kbps" in v:
            self.copy_kbps = v["copy_kbps"]
        if "ota_fail" in v:
            self.ota_fail = v["ota_fail"]
        if "next_version" in v:
            self.next_version = v["next_version"]
        if "auth" in v:
            self.auth_enabled = v["auth"]
        if "default_password" in v:
            self.users[0]["pw"] = "admin" if v["default_password"] else "admin123"
            self.users[0]["name"] = "admin"
            notes.append("admin password is now " + ("admin (change forced)" if v["default_password"] else "admin123"))
        if "login_locked" in v:
            self.lockout.clear()
            self.lockout.forced_until = time.monotonic() + LOCK_S if v["login_locked"] else 0.0
            if v["login_locked"]:
                notes.append(f"logins are refused with LOCKED for {LOCK_S} s")
        if v.get("session_expire"):
            self.sessions.clear()
            notes.append("all sessions ended: the next request gets 401")
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

    def take_fail_next(self, path: str, is_post: bool = True):
        """POST: any pending fail_next, or one limited to this path. GET: only one limited to this path."""
        if self.fail_next is None or (self.fail_next[1] and self.fail_next[1] != path):
            return None
        if not is_post and not self.fail_next[1]:
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
        self._user, self._token, self._locked = None, None, False
        self.close_connection = True
        t0 = time.monotonic()
        path, _, query = self.path.partition("#")[0].partition("?")
        q = parse_query(query)
        dev = self.dev
        dev.tick()
        dev.cur_peer = self.client_address[0]
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
            if self._user is not None:
                line += f" [{self._user['name']}]"
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

        clen = self._content_length()
        if is_mock:                                   # control endpoints: never authenticated
            mock = {"/mock": self._mock_help, "/mock/": self._mock_help, "/mock/state": self._mock_state,
                    "/mock/set": self._mock_set, "/mock/scenario": self._mock_scenario}
            if method == "GET" and path in mock:
                mock[path](q)
            elif path in mock:
                self._drain(clen)
                self._send(405, b"Specified method is invalid for this resource", "text/plain")
            else:
                self._drain(clen)
                self._send(404, b"Nothing matches the given URI", "text/plain")
            return

        # (method, path) -> (handler, minimum role: None = no login, "user", "admin")
        routes = {
            ("GET", "/"): (self._index, None), ("GET", "/index.html"): (self._index, None),
            ("GET", "/manifest.webmanifest"): (self._manifest, None),
            ("GET", "/icon-180.png"): (lambda q: self._icon(180), None),
            ("GET", "/icon-192.png"): (lambda q: self._icon(192), None),
            ("GET", "/icon-512.png"): (lambda q: self._icon(512), None),
            ("GET", "/api/session"): (self._api_session, None),
            ("POST", "/api/login"): (self._api_login, None),
            ("POST", "/api/logout"): (self._api_logout, "user"),
            ("GET", "/api/users"): (self._api_users, "admin"),
            ("GET", "/api/devices"): (self._api_devices, "user"),
            ("POST", "/api/devices/delete"): (self._api_devices_delete, "user"),
            ("POST", "/api/users/add"): (self._api_users_add, "admin"),
            ("POST", "/api/users/delete"): (self._api_users_delete, "admin"),
            ("POST", "/api/users/role"): (self._api_users_role, "admin"),
            ("POST", "/api/users/password"): (self._api_users_password, "user"),
            ("GET", "/api/status"): (self._api_status, "user"),
            ("GET", "/api/files"): (self._api_files, "user"),
            ("GET", "/api/download"): (self._api_download, "user"),
            ("GET", "/api/log"): (self._api_log, "admin"),
            ("GET", "/api/wifi/list"): (self._api_wifi_list, "admin"),
            ("GET", "/api/wifi/scan"): (self._api_wifi_scan, "admin"),
            ("GET", "/api/crypt"): (self._api_crypt, "user"),
            ("POST", "/api/crypt/setup"): (self._api_crypt_setup, "admin"),
            ("POST", "/api/crypt/unlock"): (self._api_crypt_unlock, "user"),
            ("POST", "/api/crypt/lock"): (self._api_crypt_lock, "user"),
            ("POST", "/api/crypt/password"): (self._api_crypt_password, "admin"),
            ("POST", "/api/crypt/disable"): (self._api_crypt_disable, "admin"),
            ("POST", "/api/crypt/recovery"): (self._api_crypt_recovery, "admin"),
            ("POST", "/api/crypt/erase"): (self._api_crypt_erase, "admin"),
            ("POST", "/api/mode"): (self._api_mode, "user"),
            ("POST", "/api/mkdir"): (self._api_mkdir, "user"),
            ("POST", "/api/delete"): (self._api_delete, "user"),
            ("POST", "/api/move"): (self._api_move, "user"),
            ("POST", "/api/copy"): (self._api_copy, "user"),
            ("POST", "/api/upload/check"): (self._api_upload_check, "user"),
            ("POST", "/api/upload"): (self._api_upload, "user"),
            ("POST", "/api/push"): (self._api_push, "user"),
            ("POST", "/api/settings/host_writable"): (self._api_host_writable, "admin"),
            ("POST", "/api/settings/https"): (self._api_settings_https, "admin"),
            ("GET", "/api/https/ca.cer"): (self._api_https_ca, "user"),
            ("POST", "/api/wifi"): (self._api_wifi_add, "admin"),
            ("POST", "/api/wifi/add"): (self._api_wifi_add, "admin"),
            ("POST", "/api/wifi/delete"): (self._api_wifi_delete, "admin"),
            ("POST", "/api/format"): (self._api_format, "admin"),
            ("POST", "/api/reboot"): (self._api_reboot, "admin"),
            ("POST", "/api/ota"): (self._api_ota, "admin"),
        }
        entry = routes.get((method, path))
        if entry is None:
            self._drain(clen)
            if any(p == path for _, p in routes):
                self._send(405, b"Specified method is invalid for this resource", "text/plain")
            else:
                self._send(404, b"Nothing matches the given URI", "text/plain")
            return
        handler, role = entry
        if dev.auth_unavailable and (role is not None or path in ("/api/session", "/api/login")):
            self._drain(clen)                          # auth.c: stored user database unreadable
            self._error("AUTH_UNAVAILABLE")
            return
        if role is not None or path == "/api/session":
            self._authenticate()
        if role is not None:
            if self._user is None:
                self._drain(clen)
                self._error("LOCKED" if self._locked else "UNAUTHORIZED")
                return
            if (dev.auth_enabled and dev.is_default_password(self._user)
                    and path not in ("/api/logout", "/api/users/password")):
                self._drain(clen)
                self._error("MUST_CHANGE_PASSWORD")
                return
            if role == "admin" and self._user["role"] != "admin":
                self._drain(clen)
                self._error("FORBIDDEN_ROLE")
                return
        if method == "POST":
            # CSRF protection, like check_csrf() in every POST handler.
            if self.headers.get(CSRF_HEADER) != "1":
                self._drain(clen)
                self._error("FORBIDDEN")
                return
            if dev.drive_locked() and (method, path) in LOCKED_ROUTES:
                self._drain(clen)
                self._error(dev.locked_code())
                return
            code = dev.take_fail_next(path)
        else:
            if dev.drive_locked() and (method, path) in LOCKED_ROUTES:
                self._error(dev.locked_code())
                return
            code = dev.take_fail_next(path, is_post=False) if path.startswith("/api/") else None
        if code:
            late = path in UPLOAD_PATHS and code in LATE_UPLOAD_ERRORS
            self._read_body(clen or 0, throttled=late)
            text = TEXT_BAD_FIRMWARE if (path == "/api/ota" and code == "INVALID_FILE") else ""
            self._error(code, text, info="(fail_next)")
            return
        if method == "POST":
            handler(q, clen)
        else:
            handler(q)

    def _authenticate(self):
        """Sets self._user (dict or None), self._token (session in use) and self._locked."""
        dev = self.dev
        user, self._token = None, None
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            key, _, val = part.strip().partition("=")
            if key == SESSION_COOKIE:
                user = dev.session_user(val)
                if user is not None:
                    self._token = val
                break
        if user is None:
            auth = self.headers.get("Authorization") or ""
            if auth[:6].lower() == "basic ":
                if dev.locked():
                    self._locked = True
                else:
                    try:
                        raw = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
                    except (binascii.Error, UnicodeDecodeError, ValueError):
                        raw = ""
                    name, _, pw = raw.partition(":")
                    user = dev.check_credentials(name, pw) if raw else None
        if user is None and not dev.auth_enabled:
            user = next((u for u in dev.users if u["role"] == "admin"), dev.users[0] if dev.users else None)
        self._user = user

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

    def _json(self, status: int, obj, info: str = "", headers=()):
        if info:
            self._info = (self._info + " " + info).strip()
        self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   "application/json", headers=headers)

    def _error(self, code: str, text: str = "", info: str = "", extra=None, headers=None):
        status, default_text = ERRORS[code]
        body = {"ok": False, "code": code, "error": text or default_text}
        body.update(extra or {})
        if code == "LOCKED" and headers is None:        # lock-out of the login: seconds until the next try
            headers = [("Retry-After", str(max(1, self.dev.blocked_s())))]
        self._json(status, body, (code + " " + info).strip(), headers=headers or ())

    def _result(self, code, info: str = "", extra=None):
        """send_result(): None = success."""
        if code:
            self._error(code, info=info, extra=extra)
        else:
            self._json(200, {"ok": True, "code": "OK", "error": "", **(extra or {})}, info)

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

    # ---------------------------------------------------------- helpers for handlers

    def _form(self, clen, limit: int = 512):
        """Reads a small URL-encoded body. Returns a dict, or None after an error was sent."""
        if clen is None:
            self._error("BAD_REQUEST", "Content-Length fehlt")
            return None
        if clen > limit:
            self._drain(clen)
            self._error("BAD_REQUEST", "Anfrage zu groß")
            return None
        body = bytearray()
        self._read_body(clen, on_data=body.extend)
        return parse_query(body.decode("latin-1"))

    def _need(self, q: dict, *keys) -> bool:
        """True if all keys are present; otherwise sends BAD_REQUEST."""
        for key in keys:
            if key not in q or q[key] is None:
                self._error("BAD_REQUEST", f"{key} fehlt oder ist ungültig")
                return False
        return True

    def _session_json(self, u) -> dict:
        return {"ok": True, "user": u["name"], "role": u["role"],
                "default_password": self.dev.is_default_password(u)}

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

    def _manifest(self, q):
        real = self.server.html_path.parent / "manifest.webmanifest"
        try:
            body, info = real.read_bytes(), ""
        except OSError:
            body, info = MANIFEST_FALLBACK, "(generated placeholder)"
        self._info = info
        self._send(200, body, "application/manifest+json", "no-cache")

    def _icon(self, size: int):
        real = self.server.html_path.parent / "icons" / f"icon-{size}.png"
        try:
            body = real.read_bytes()
        except OSError:
            body = icon_png(size)
            self._info = "(generated placeholder)"
        self._send(200, body, "image/png", "max-age=86400")

    def _api_session(self, q):
        if self._user is None:
            self._error("LOCKED" if self._locked else "UNAUTHORIZED")
        else:
            self._json(200, self._session_json(self._user))

    def _api_status(self, q):
        self._json(200, self.dev.status())

    def _api_users(self, q):
        self._json(200, {"ok": True, "users": [{"name": u["name"], "role": u["role"]} for u in self.dev.users],
                         "max": USERS_MAX})

    def _api_files(self, q):
        listing, err = self.dev.list_dir(q.get("dir", ""))
        if err:
            self._error(err)
            return
        self._json(200, listing, f"{len(listing['entries'])} entries" + (" truncated" if listing["truncated"] else ""))

    def _api_download(self, q):
        if not self._need(q, "path"):
            return
        f, err = self.dev.get_file(q["path"])
        if err:
            self._error(err)
            return
        self._info = f"{len(f.data)} B" + (f" (reported size {f.size})" if f.size != len(f.data) else "")
        self._send(200, f.data, "application/octet-stream", headers=[("Content-Disposition", "attachment")])

    def _api_log(self, q):
        self._send(200, self.dev.log_text(), "text/plain; charset=utf-8")

    def _api_wifi_list(self, q):
        dev = self.dev
        self._json(200, {"ok": True, "max": WIFI_MAX,
                         "networks": [{"ssid": n, "active": dev.wifi_mode == "sta" and n == dev.ssid}
                                      for n in dev.networks]})

    def _api_wifi_scan(self, q):
        dev = self.dev
        if dev.other_upload is not None:
            self._error("BUSY")
            return
        time.sleep(dev.scan_ms / 1000)               # the device is busy (and answers nothing else) meanwhile
        found = dev.scan()
        dev.log("I", "wifi", f"scan: {len(found)} networks")
        self._json(200, {"ok": True, "networks": found}, f"{len(found)} networks")

    # ---------------------------------------------------------- drive encryption (API v4)

    def _api_crypt(self, q):
        dev = self.dev
        self._json(200, {"ok": True, "enabled": dev.crypt_enabled,
                         "unlocked": dev.crypt_unlocked or not dev.crypt_enabled,
                         "damaged": dev.crypt_damaged, "retry_after": dev.crypt_lockout.blocked_s()})

    def _crypt_gate(self, code=None) -> bool:
        """Sends the error and returns False if the drive is busy: BUSY, HOST_WRITING (a running format or
        take-away must not hit a host that is writing)."""
        dev = self.dev
        err = code or ("BUSY" if dev.other_upload is not None else "HOST_WRITING" if dev.host_wrote_recently() else None)
        if err:
            self._error(err)
        return err is None

    def _crypt_try(self, secret, recovery=None, either=False) -> bool:
        """Checks the drive password / recovery key with the global throttle. Sends the error and returns False if wrong.
        either: `secret` may be the drive password or the recovery key."""
        dev = self.dev
        wait = dev.crypt_lockout.blocked_s()
        if wait:
            self._error("LOCKED", headers=[("Retry-After", str(wait))])
            return False
        time.sleep(dev.crypt_kdf_ms / 1000)
        ok = dev.crypt_check_any(secret) if either else dev.crypt_check(secret, recovery)
        if ok:
            dev.crypt_lockout.success()
            return True
        dur = dev.crypt_lockout.failure()
        if dur:
            dev.log("W", "crypt", f"wrong drive password #{dev.crypt_lockout.fails} - next try in {dur} s")
        self._error("CRYPT_WRONG_PASSWORD")
        return False

    @staticmethod
    def _new_recovery() -> str:
        return "-".join("".join(secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(4)) for _ in range(8))

    def _api_crypt_setup(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "password"):
            return
        pw = form["password"]
        if len(pw.encode("utf-8", "surrogatepass")) > CRYPT_PASS_MAX:
            self._error("BAD_REQUEST", "Das Laufwerks-Passwort darf höchstens 64 Bytes lang sein.")
        elif len(pw) < CRYPT_PASS_MIN:
            self._error("CRYPT_WEAK_PASSWORD")
        elif dev.crypt_enabled:
            self._error("CRYPT_ALREADY_ENABLED")
        elif self._crypt_gate() and self._crypt_gate("WRONG_OWNER" if dev.owner != "app" else None):
            # the whole medium is erased first; the device answers nothing else meanwhile (this thread blocks)
            time.sleep((dev.crypt_kdf_ms + dev.crypt_setup_ms) / 1000)
            dev.format()                              # formats the drive (no restart: the key is in RAM already)
            dev.crypt_enabled = dev.crypt_unlocked = True
            dev.crypt_damaged = False
            dev.crypt_password = pw
            dev.crypt_recovery = self._new_recovery()
            dev.crypt_lockout.clear()
            dev.log("W", "crypt", "encryption set up, drive formatted")
            if dev.crypt_setup_io:                    # key area written, then the file system failed
                dev.crypt_setup_io = False
                self._error("IO", extra={"recovery_key": dev.crypt_recovery}, info="after the key area was written")
                return
            self._json(200, {"ok": True, "code": "OK", "error": "", "recovery_key": dev.crypt_recovery}, "recovery key issued")

    def _api_crypt_unlock(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None:
            return
        password, recovery = form.get("password"), form.get("recovery_key")
        if password is None and recovery is None:
            self._error("BAD_REQUEST", "password oder recovery_key fehlt")
        elif not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif dev.crypt_damaged:
            self._error("CRYPT_DAMAGED")
        elif dev.crypt_unlocked:
            self._result(None, "already unlocked")
        elif dev.other_upload is not None:
            self._error("BUSY")
        elif self._crypt_try(password, recovery if password is None else None):
            dev._crypt_unlock()
            self._result(None, "owner=" + dev.owner)

    def _api_crypt_lock(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif not dev.crypt_unlocked:
            self._result(None, "already locked")
        elif self._crypt_gate():
            dev._crypt_lock()
            self._result(None, "locked")

    def _api_crypt_password(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "old", "new"):
            return
        new = form["new"]
        if len(new.encode("utf-8", "surrogatepass")) > CRYPT_PASS_MAX:
            self._error("BAD_REQUEST", "Das Laufwerks-Passwort darf höchstens 64 Bytes lang sein.")
        elif len(new) < CRYPT_PASS_MIN:
            self._error("CRYPT_WEAK_PASSWORD")
        elif not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif dev.drive_locked():
            self._error(dev.locked_code())            # only with an unlocked drive; forgotten: unlock with the key first
        elif self._crypt_try(form["old"], either=True):   # old = drive password or recovery key
            dev.crypt_password = new                  # only the master key is wrapped anew; recovery key stays valid
            dev.log("I", "crypt", "drive password changed")
            self._result(None, "password changed")

    def _api_crypt_recovery(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "password"):
            return
        if not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif dev.drive_locked():
            self._error(dev.locked_code())
        elif self._crypt_try(form["password"], either=True):
            dev.crypt_recovery = self._new_recovery()  # the old key stops working
            dev.log("W", "crypt", "new recovery key issued")
            self._json(200, {"ok": True, "recovery_key": dev.crypt_recovery}, "recovery key issued")

    def _api_crypt_erase(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None:
            return
        if form.get("confirm") != "ERASE":
            self._error("BAD_REQUEST", "Bestätigung fehlt")
        elif not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif dev.crypt_unlocked:
            self._error("CRYPT_ALREADY_ENABLED")      # only allowed locked or damaged
        elif dev.other_upload is not None:
            self._error("BUSY")
        else:
            time.sleep(dev.crypt_setup_ms / 1000)     # erases the key area and the medium
            dev.format()
            dev.crypt_enabled, dev.crypt_unlocked, dev.crypt_damaged = False, True, False
            dev.owner = "app"
            dev.crypt_lockout.clear()
            dev.log("W", "crypt", "key area erased, drive formatted plain")
            self._result(None, "erased, formatted plain")

    def _api_crypt_disable(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "password"):
            return
        if not dev.crypt_enabled:
            self._error("CRYPT_NOT_ENABLED")
        elif dev.crypt_damaged:
            self._error("CRYPT_DAMAGED")
        elif not self._crypt_gate():
            return
        elif dev.crypt_unlocked and dev.owner != "app":
            self._error("WRONG_OWNER")
        elif self._crypt_try(form["password"], None):
            dev.format()                              # formats the drive without encryption
            dev.crypt_enabled, dev.crypt_unlocked = False, True
            dev.owner = "app"
            dev.crypt_lockout.clear()
            dev.log("W", "crypt", "encryption removed, drive formatted")
            self._result(None, "formatted plain")

    # ---------------------------------------------------------- POST: authentication

    def _api_login(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None:
            return
        if not self._need(form, "user", "pass"):
            return
        if dev.locked():
            self._error("LOCKED")
            return
        u = dev.check_credentials(form["user"], form["pass"])
        if u is None:
            self._error("BAD_LOGIN", info=f"({dev.lockout.fails_total()} failed)" if dev.locked() is False else "(now locked)")
            return
        self._user = u
        cookie = "Path=/; HttpOnly; SameSite=Strict"
        token = None
        if flag(form, "remember"):
            label = (form.get("device") or "").strip().encode("utf-8", "ignore")[:DEVICE_LABEL_MAX].decode("utf-8", "ignore")
            token = dev.new_device(u, label)
            if token is not None:
                cookie += f"; Max-Age={DEVICE_MAX_AGE}"
                info = f"trusted device '{label}' ({len(dev.devices)} of {DEVICES_MAX})"
        if token is None:            # no remember, or no free device slot: volatile session
            token = dev.new_session(u)
            info = f"session {token[:6]}... ({len(dev.sessions)} of {SESSIONS_MAX})"
        self._json(200, self._session_json(u), info,
                   headers=[("Set-Cookie", f"{SESSION_COOKIE}={token}; {cookie}")])

    def _api_logout(self, q, clen):
        self._drain(clen)
        if self._token:
            self.dev.sessions.pop(self._token, None)
            self.dev.devices.pop(self.dev.token_hash(self._token), None)
        self._json(200, {"ok": True}, headers=[
            ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")])

    def _api_devices(self, q):
        me, cur = self._user, self.dev.token_hash(self._token) if self._token else None
        self._json(200, {"ok": True, "max": DEVICES_MAX, "devices": [
            {"id": h[:8], "user": d["user"], "label": d["label"], "current": h == cur}
            for h, d in self.dev.devices.items() if me["role"] == "admin" or d["user"].lower() == me["name"].lower()]})

    def _api_devices_delete(self, q, clen):
        self._drain(clen)
        if not self._need(q, "id"):
            return
        dev, me = self.dev, self._user
        match = [h for h in dev.devices if h[:8] == q["id"].lower()]
        if not match:
            self._error("NOT_FOUND", "Dieses Gerät gibt es nicht.")
        elif me["role"] != "admin" and dev.devices[match[0]]["user"].lower() != me["name"].lower():
            self._error("FORBIDDEN_ROLE")
        else:
            del dev.devices[match[0]]
            self._json(200, {"ok": True})

    def _api_users_add(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "user", "pass", "role"):
            return
        if form["role"] not in ("admin", "user"):
            self._error("BAD_REQUEST", "role muss admin oder user sein")
        elif not valid_username(form["user"]):
            self._error("BAD_USER")
        elif not valid_password(form["pass"]):
            self._error("BAD_PASSWORD")
        elif dev.find_user(form["user"]) is not None:
            self._error("EXISTS", TEXT_USER_EXISTS)
        elif len(dev.users) >= USERS_MAX:
            self._error("USER_LIMIT")
        else:
            dev.users.append({"name": form["user"], "role": form["role"], "pw": form["pass"]})
            self._json(200, {"ok": True}, f"user '{form['user']}' ({form['role']})")

    def _api_users_delete(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if not self._need(q, "user"):
            return
        u = dev.find_user(q["user"])
        if u is None:
            self._error("NOT_FOUND", TEXT_NO_USER)
        elif u["role"] == "admin" and dev.admin_count() <= 1:
            self._error("LAST_ADMIN")
        else:
            dev.users.remove(u)
            dev.end_sessions(u["name"])
            dev.end_devices(u["name"])
            self._json(200, {"ok": True})

    def _api_users_role(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if not self._need(q, "user", "role"):
            return
        if q["role"] not in ("admin", "user"):
            self._error("BAD_REQUEST", "role muss admin oder user sein")
            return
        u = dev.find_user(q["user"])
        if u is None:
            self._error("NOT_FOUND", TEXT_NO_USER)
        elif u["role"] == "admin" and q["role"] == "user" and dev.admin_count() <= 1:
            self._error("LAST_ADMIN")
        else:
            if u["role"] != q["role"]:
                dev.end_devices(u["name"])
            u["role"] = q["role"]
            self._json(200, {"ok": True})

    def _api_users_password(self, q, clen):
        dev = self.dev
        form = self._form(clen)
        if form is None or not self._need(form, "user", "pass"):
            return
        me = self._user
        own = form["user"].lower() == me["name"].lower()
        if own:
            if dev.locked():
                self._error("LOCKED")
                return
            if dev.check_credentials(me["name"], form.get("old") or "") is None:
                self._error("BAD_LOGIN")
                return
            target = me
        else:
            if me["role"] != "admin":
                self._error("FORBIDDEN_ROLE")
                return
            target = dev.find_user(form["user"])
            if target is None:
                self._error("NOT_FOUND", TEXT_NO_USER)
                return
        if not valid_password(form["pass"]):
            self._error("BAD_PASSWORD")
            return
        if form["pass"] == "admin":
            self._error("BAD_PASSWORD", TEXT_DEFAULT_PASSWORD)
            return
        target["pw"] = form["pass"]
        dev.end_sessions(target["name"], keep=self._token if own else None)
        # trusted devices of the user end too; the device that changed its own password stays
        dev.end_devices(target["name"], keep=dev.token_hash(self._token) if own and self._token else None)
        self._json(200, {"ok": True}, f"password of '{target['name']}' changed")

    # ---------------------------------------------------------- POST: drive

    def _api_mode(self, q, clen):
        self._drain(clen)
        target = q.get("target")
        if "target" not in q:
            self._error("BAD_REQUEST", "target fehlt")
        elif target not in ("usb", "app"):
            self._error("BAD_REQUEST", "target muss usb oder app sein")
        else:
            self._result(self.dev.set_owner(target, flag(q, "force")), f"owner={self.dev.owner}")

    def _api_mkdir(self, q, clen):
        self._drain(clen)
        if self._need(q, "path"):
            self._result(self.dev.mkdir(q["path"]))

    def _api_delete(self, q, clen):
        self._drain(clen)
        if self._need(q, "path"):
            self._result(self.dev.delete(q["path"], flag(q, "recursive")))

    def _api_move(self, q, clen):
        self._drain(clen)
        if self._need(q, "from", "to"):
            self._result(self.dev.move(q["from"], q["to"]))

    def _api_copy(self, q, clen):
        self._drain(clen)
        if self._need(q, "from", "to"):
            t0 = time.monotonic()
            err = self.dev.copy(q["from"], q["to"])
            self._result(err, f"{time.monotonic() - t0:.2f} s")

    def _api_upload_check(self, q, clen):
        self._drain(clen)
        if not self._need(q, "path"):
            return
        if not re.fullmatch(r"\d{1,20}", q.get("size") or ""):
            self._error("BAD_REQUEST", "size fehlt oder ist ungültig")
            return
        self._result(self.dev.upload_precheck(q["path"], int(q["size"]), flag(q, "overwrite")))

    def _api_upload(self, q, clen):
        self._receive_file(q, clen, push=False)

    def _api_push(self, q, clen):
        self._receive_file(q, clen, push=True)

    def _receive_file(self, q, clen, push: bool):
        """POST /api/upload and the one-step POST /api/push."""
        dev = self.dev
        if clen is None:
            self._error("BAD_REQUEST", "Content-Length fehlt")
            return
        if "path" not in q or q["path"] is None:
            self._drain(clen)
            self._error("BAD_REQUEST", "path fehlt")
            return
        path = q["path"]
        overwrite = (q.get("overwrite") not in ("0", "false")) if push else flag(q, "overwrite")
        force = push and flag(q, "force")
        segs, err = dev.upload_static_check(path, clen)
        took = False
        if not err and push and dev.owner == "usb":
            err = dev.set_owner("app", force)           # same rules as /api/mode?target=app
            took = err is None
        if not err:
            err = dev.upload_app_check(segs, clen, overwrite)

        def extra() -> dict:
            return {"took_over": took, "provided": took} if push else {}

        def give_back():
            if took:
                dev._provide()
                dev.log("I", "drive", "push: drive provided to the USB host again")

        if err:
            self._drain(clen)
            give_back()
            self._error(err, extra=extra())
            return

        dev.log("I", "drive", f"{'push' if push else 'upload'} start '{path}' ({clen} bytes)")
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
            dev.log("W", "drive", f"upload aborted '{path}'")
            self._info = "upload aborted, nothing stored:"
            give_back()
            raise
        if received < clen:
            dev.log("W", "web", "upload connection lost (-1)")
            dev.log("W", "drive", f"upload aborted '{path}' after {received} bytes")
            give_back()
            self._abort(f"upload_fail_at: connection reset after {received} of {clen} B")
            return

        dt = max(time.monotonic() - t0, 1e-6)
        err = None
        name = segs[-1]
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
            dev.store_upload(segs, bytes(keep), clen, mtime)
        else:
            dev.log("W", "drive", f"upload failed '{path}': {ERRORS[err][1]}")
        dev.log("I", "web", f"upload '{path}' {clen} bytes in {dt * 1000:.0f} ms ({clen / 1024 / dt:.1f} KB/s): "
                            f"{ERRORS[err][1] if err else 'OK'}")
        give_back()
        info = f"{clen} B in {dt:.2f} s" + (f" = {clen / 1024 / dt:.0f} KB/s" if dt >= 0.05 else "")
        if push and not err:
            info += f" took_over={took}"
        self._result(err, info, extra=extra() if push else None)

    def _https_state(self):
        dev = self.dev
        self._json(200, {"ok": True, "https": dev.https_on, "https_active": dev.https_active,
                         "https_restart": dev.https_dirty, "cert_sha256": dev.cert_sha256,
                         "ca_sha256": dev.ca_sha256, "cert_expires": dev.cert_expires})

    def _api_settings_https(self, q, clen):
        self._drain(clen)
        dev = self.dev
        value = q.get("value")
        regen, renew = flag(q, "regenerate"), flag(q, "renew")
        if value is None and not regen and not renew:
            self._error("BAD_REQUEST", "value, renew oder regenerate fehlt")
            return
        if regen:
            dev.https_make(True, True)
            dev.https_dirty = True
        elif renew:
            dev.https_make(False, True)
            dev.https_dirty = True
        if value is not None:
            on = value in ("1", "true")
            if on != dev.https_on:
                dev.https_dirty = True
            dev.https_on = on
            if on:
                dev.https_make(False, False)      # enabling creates the key material now
        dev.log("W", "https", f"setting={int(dev.https_on)} (restart needed)")
        self._https_state()

    def _api_https_ca(self, q):
        if not self.dev.ca_sha256:
            self._error("NOT_FOUND", "Noch kein Zertifikat vorhanden")
            return
        der = b"\x30\x82\x01\x00" + hashlib.sha256(self.dev.ca_sha256.encode()).digest() * 8   # not a real certificate
        self._send(200, der, "application/x-x509-ca-cert",
                   headers=[("Content-Disposition", 'attachment; filename="DataDrive-CA.cer"')])

    def _api_host_writable(self, q, clen):
        self._drain(clen)
        value = q.get("value")
        if value not in ("0", "1", "true", "false"):
            self._error("BAD_REQUEST", "value muss 0 oder 1 sein")
            return
        # Stored only; takes effect the next time the drive is provided.
        self.dev.host_writable = value in ("1", "true")
        self._result(None, f"host_writable={int(self.dev.host_writable)}")

    def _api_wifi_add(self, q, clen):
        dev = self.dev
        form = self._form(clen, 255)
        if form is None:
            return
        ssid = form.get("ssid")
        if ssid is None:
            self._error("BAD_REQUEST", "SSID fehlt")
            return
        password = form.get("pass") or ""
        ssid_len, pass_len = len(ssid.encode("utf-8")), len(password.encode("utf-8"))
        if not (1 <= ssid_len <= 32) or not (pass_len == 0 or 8 <= pass_len <= 63):
            self._error("BAD_REQUEST", "Ungültig: SSID 1-32 Zeichen, Passwort leer oder 8-63 Zeichen")
            return
        if ssid not in dev.networks:
            if len(dev.networks) >= WIFI_MAX:
                self._error("WIFI_LIMIT")
                return
            dev.networks.append(ssid)
        dev.ssid = ssid
        dev.wifi_mode = "connecting"
        dev.wifi_connect_at = time.monotonic() + 3.0
        dev.log("I", "wifi", f"credentials stored for '{ssid}'")
        dev.log("I", "wifi", f"connecting to '{ssid}' ...")
        self._json(200, {"ok": True}, f"ssid='{ssid}' (connected in 3 s)")

    def _api_wifi_delete(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if not self._need(q, "ssid"):
            return
        ssid = q["ssid"]
        if ssid not in dev.networks:
            self._error("NOT_FOUND", TEXT_NO_WIFI)
            return
        dev.networks.remove(ssid)
        if ssid == dev.ssid and dev.wifi_mode != "ap":
            if dev.networks:
                dev.ssid, dev.wifi_mode = dev.networks[0], "connecting"
                dev.wifi_connect_at = time.monotonic() + 3.0
            else:
                dev.ssid, dev.wifi_mode, dev.wifi_connect_at = "", "ap", None
        self._json(200, {"ok": True})

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
            if dev.crypt_enabled:                 # encrypted + unlocked: new file system, no restart, stays unlocked
                dev.format()
                self._json(200, {"ok": True, "restart": False}, "formatted, stays encrypted and unlocked")
                return
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

    def _api_ota(self, q, clen):
        dev = self.dev
        if clen is None:
            self._error("BAD_REQUEST", "Content-Length fehlt")
            return
        err = None
        if dev.other_upload is not None:
            err = "BUSY"
        elif dev.host_wrote_recently():
            err = "HOST_WRITING"
        elif clen > OTA_MAX:
            err = "TOO_LARGE"
        elif dev.ota_fail in ("BUSY", "HOST_WRITING", "TOO_LARGE"):
            err = dev.ota_fail
        if err:
            self._drain(clen)
            self._error(err)
            return
        first = bytearray()

        def on_data(data: bytes):
            if not first:
                first.append(data[0])

        t0 = time.monotonic()
        self._read_body(clen, throttled=True, on_data=on_data)      # throttled like an upload
        if dev.ota_fail:
            err = dev.ota_fail
        elif clen == 0 or first[0] != 0xE9:
            err = "INVALID_FILE"
        if err:
            dev.log("W", "ota", f"update rejected: {err}")
            self._error(err, TEXT_BAD_FIRMWARE if err == "INVALID_FILE" else "", f"{clen} B")
            return
        dev.ota_partition = "ota_1" if dev.ota_partition == "ota_0" else "ota_0"
        dev.pending_version = dev.next_version
        dev.log("I", "ota", f"image ok ({clen} bytes), next boot: {dev.ota_partition} ({dev.next_version})")
        self._json(200, {"ok": True, "restart": True},
                   f"{clen} B in {time.monotonic() - t0:.2f} s, restarting as {dev.next_version}")
        dev.restart()

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
    ap.add_argument("--next-version", default=NEXT_VERSION, help="version reported after a successful "
                                                                 f"POST /api/ota (default {NEXT_VERSION})")
    ap.add_argument("--no-auth", action="store_true", help="start with authentication disabled "
                                                           "(same as /mock/set?auth=0; kept by scenario switches)")
    a = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    for sig in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _raise_interrupt)

    dev = Device(max(0, a.upload_kbps), a.scenario, a.next_version, not a.no_auth)
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
          f"  login:     {'DISABLED (--no-auth)' if a.no_auth else 'admin / admin123'}   next OTA version: {a.next_version}\n"
          f"  control:  {base}/mock   {base}/mock/state\n"
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
