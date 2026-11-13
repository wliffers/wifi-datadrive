#!/usr/bin/env python3
"""Mock server for the "WiFi DataDrive" web UI (MOCK ONLY - no real device involved).

Serves the single-file web UI and implements the HTTP API version 3 of docs/09_api_v3.md
(login with roles, folders, Wi-Fi list, update; everything it does not mention follows
docs/07_api.md) plus the additions of version 4 (docs/12_api_v4.md: Wi-Fi scan, drive encryption), version 5
(docs/15_api_v5.md) and version 6 (docs/16_api_v6.md: auto-lock countdown, network settings, signed updates)
with an in-memory file tree, so every state of the UI can be exercised on
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

* API v5 (docs/15_api_v5.md):
  - Clock: `time` {epoch, synced, tz} in /api/status, POST /api/settings/time?tz= (admin), POST /api/time?epoch=
    (user; ignored with set:false while NTP-synced). Every time stamp (mtime of listings, upload parameters `mtime`
    and `now`) is real Unix time (UTC) from v5 on. Files uploaded without `mtime` and new folders get the device
    clock, 0 while it is unknown (scenario ap_setup, /mock/set?clock=unsynced).
  - Automatic lock: `autolock` {minutes, on_detach} in /api/status, POST /api/settings/autolock (admin). File
    requests count as activity, status polls do not; never locks while an upload by another client runs. Since
    0.6.0 (API v6) the timer only runs in file manager mode (see below). Simulate idle time with /mock/set?idle_s=N.
    on_detach (also in USB mode; not while the host writes): locks 10 s after usb_connected=0 or after 60 s of
    usb_suspended=1.
  - Display language: `lang` in /api/status, POST /api/settings/lang?value=de|en (user).
  - Resumable upload: GET /api/upload/resume?path=&size=, POST /api/upload?path=&size=&offset=N (409
    RESUME_MISMATCH with the server's `offset`, also for offset=0 while a part of that file is held and for
    offset > size; Content-Length != size - offset: 400), `upload.resumable` in /api/status. A broken resumable
    upload keeps whole 4 KiB blocks; one at a time; discarded by any fresh upload (also push and WebDAV PUT), format,
    hand-over to USB, lock and restart. The resume query always answers (0 while locked or owned by USB).
  - Folder ZIP: GET /api/zip?path= streams a stored ZIP (chunked, data descriptors, UTF-8 names, folder entries,
    hidden names left out; refused like a download; 413 TOO_MANY_FILES above 4096 files, 404 NOT_FOUND for a file;
    the firmware switches to ZIP64 where needed, the mock's archives are always small).
  - WebDAV at /dav/ (HTTP Basic only, cookie ignored): OPTIONS, PROPFIND (Depth 0/1), GET, HEAD, PUT, DELETE, MKCOL,
    MOVE, COPY, LOCK/UNLOCK (answered, not enforced), PROPPATCH (accepted, ignored). Same roles and drive rules as
    the file API. `webdav` switch in /api/status, POST /api/settings/webdav?value=0|1 (admin; off: /dav/ answers
    404). Names with a leading dot and desktop.ini / Thumbs.db / ehthumbs.db: reads 404, writes 403. PROPFIND
    without Depth means infinity: 403. LOCK on a new name: 201 (nothing is created).
        curl -u admin:admin123 -X PROPFIND -H "Depth: 1" http://127.0.0.1:8080/dav/
        curl -u admin:admin123 -T Teil.3mf "http://127.0.0.1:8080/dav/Projekte/Teil.3mf"

* API v6 (docs/16_api_v6.md):
  - Auto-lock countdown: `autolock` in /api/status also carries `remaining_s` (seconds until the timer locks, -1 when no
    countdown runs: timer off, drive not encrypted / locked / damaged, upload of another client running, or the drive
    provided to the USB device) and `waiting` (true with remaining_s 0: the time is up, but the lock attempt was
    refused). Like the firmware, the timer runs only while the file manager owns the drive (owner app): with
    owner usb there is no countdown and the timer never locks; every switch to the file manager starts the period
    anew. File requests restart the period, status polls do not; host reads/writes play no part (they cannot happen
    in file manager mode). POST /api/autolock/touch (user) restarts it too: {"ok":true,"remaining_s":S}. on_detach is
    not affected. Mock controls: autolock_remaining=<s>, autolock_waiting=0|1 (see below).
  - Network settings: GET /api/network (admin), POST /api/settings/network?dhcp=0|1[&ip=&mask=&gateway=&dns=]
    [&hostname=] (admin; parameters as form body or, without a body, in the query; dhcp also true/false). Rules and
    German texts of the firmware: 400 BAD_REQUEST naming the field in the text (no extra JSON key); mask /8../30;
    ip and gateway unicast, not the network/broadcast address; gateway in the subnet and not = ip; ip not 192.168.4.1
    (setup access point); dns empty = gateway; host name lower-cased, missing or empty keeps the stored one; with
    dhcp=1 given addresses are ignored and the stored static values stay.
    The answer {"ok":true,"restart":false,"ip":..,"hostname":..} is sent before the change is applied.
    The mock keeps listening on its own address whatever is configured; `current` and status.wifi.ip show the static
    address after a switch to static, 192.0.2.152 with DHCP. /mock/set?net_fail=1 makes it unreachable for 3 s after
    the next change (imitates the address change). Default hostname of the mock: datadrive-mock (device: datadrive).
    status.wifi gains `dhcp`, status.wifi.hostname follows the setting.
  - Signed updates: status.ota gains `signed_required` and `key_id` (mock key 4d6f636b while required, else "").
    With /mock/set?ota_signed=1 POST /api/ota checks a signature after the 0xE9 check, like the firmware explains a
    refusal: size no multiple of 4096, or no signature block (magic 0xE7 and valid CRC-32) at the start of the last
    4096-byte sector: 422 UNSIGNED; a block whose public key has another key id (first 8 hex digits of its SHA-256)
    than the mock's 4d6f636b: 422 BAD_SIGNATURE (the mock cannot verify RSA; images signed with a real key therefore
    get BAD_SIGNATURE). /mock/set?ota_sig=ok|bad|none overrides the result for the next upload that reaches the
    check (ok: accepted and "flashed"). A refused image leaves the running firmware untouched (no restart).

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
    usb_suspended=0|1       status flag (and: autolock on_detach locks after 60 s of suspend)
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
    clock=synced|unsynced|manual|<unix s>   device clock (API v5): NTP-synced, unknown (epoch 0), set by a
                            browser (now, or the given time; not synced)
    resumable=PATH|SIZE|OFFSET   pretend a broken resumable upload exists (content 'PK\\3\\4' + zero bytes);
                            none clears
    upload_drop_after=<bytes>    the next upload/push connection is reset after that many body bytes (a
                            resumable upload keeps the whole 4 KiB blocks received); none clears
    idle_s=<n>              pretend the last file activity, host access, unplugging, suspend and (owner app) the switch to
                            the file manager were n s ago (the automatic lock strikes at once if due)
    autolock_remaining=<s>  (API v6) shift the last activity so that the countdown shows s seconds (needs a timer,
                            0 <= s <= minutes*60; 0 locks at once unless the lock attempt is refused). Same mechanism as
                            idle_s, only expressed from the other end. With owner usb there is no countdown
    autolock_waiting=0|1    (API v6) 1: every timer lock attempt is refused (as if the drive were busy): when the time
                            is up the status shows remaining_s 0 / waiting true; 0: locks when due
    net_dhcp=0|1            (API v6) stored network mode; 0 without stored static values fills 192.0.2.152 /
                            255.255.255.0 / gateway and DNS 192.0.2.1
    net_fail=0|1|<s>        (API v6) the next successful POST /api/settings/network makes the mock unreachable (connections
                            reset) for 3 s (1) or s seconds (2 or more), after the answer; 0 clears
    ota_signed=0|1          (API v6) the device requires signed updates (status.ota.signed_required, key_id)
    ota_sig=ok|bad|none     (API v6) result of the signature check for the next /api/ota that reaches it (ok: accepted,
                            bad: 422 BAD_SIGNATURE, none: 422 UNSIGNED); clear|- clears
    reset=1                 back to the start of the current scenario
"""
from __future__ import annotations

import argparse
import base64
import binascii
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
from email.utils import formatdate
from urllib.parse import quote, unquote_to_bytes, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape
import mimetypes
import uuid

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = "firmware/printdrive/main/web/index.html"

VERSION = "0.6.0"
NEXT_VERSION = "0.6.1"           # reported after a successful POST /api/ota
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
# API v5 (docs/15_api_v5.md)
TZ_DEFAULT = "CET-1CEST,M3.5.0,M10.5.0/3"   # POSIX TZ of Germany (default of /api/settings/time)
TZ_MAX = 63                         # bytes of a TZ string
EPOCH_MIN = 1_700_000_000           # plausible clock for POST /api/time: after the "build date" (firmware: its real one)
EPOCH_MAX = 4_102_444_800           # ... and before 2100-01-01
AUTOLOCK_MAX = 1440                 # minutes
DETACH_LOCK_S = 10                  # on_detach: lock this long after the host disappeared ...
SUSPEND_LOCK_S = 300                # ... or after this long in USB suspend (5 min, like the firmware)
RESUME_BLOCK = 4096                 # a broken resumable upload keeps whole blocks of this size
ZIP_FILES_MAX = 4096                # more files in the tree: 413 TOO_MANY_FILES (firmware limit)
DAV_PREFIX = "/dav"
DAV_REALM = "DataDrive"
# API v6 (docs/16_api_v6.md)
HOSTNAME_MOCK = "datadrive-mock"    # default host name of the mock (the device: "datadrive")
HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?")      # 1..31, a-z 0-9 '-', not at either end
NET_DHCP_CURRENT = {"ip": FAKE_IP, "mask": "255.255.255.0",
                    "gateway": "192.0.2.1", "dns": "192.0.2.1"}           # what "DHCP" hands out in the mock
NET_FAIL_S = 3                      # net_fail=1: unreachable this long after a network change
MOCK_KEY_ID = "4d6f636b"            # key id of the mock's update signing key ("Mock" in hex)
SIG_SECTOR = 4096                   # signature sector at the end of a signed image
SIG_BLOCK_CRC_AT = 1196             # CRC-32 (little endian) of the first 1196 bytes of a signature block
SIG_KEY_AT, SIG_KEY_LEN = 36, 776   # RSA-3072 public key inside the block (its SHA-256 gives the key id)
OTA_MAX = 0x280000                 # size of one firmware partition
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
    # API v5 (docs/15_api_v5.md)
    "RESUME_MISMATCH": (409, "Der Upload kann an dieser Stelle nicht fortgesetzt werden."),
    "TOO_MANY_FILES": (413, "Der Ordner enthält zu viele Dateien für ein ZIP (höchstens 4096)."),
    # API v6 (docs/16_api_v6.md)
    "BAD_SIGNATURE": (422, "Die Update-Datei ist nicht mit dem Schlüssel dieses Geräts signiert."),
    "UNSIGNED": (422, "Die Update-Datei ist nicht signiert. Dieses Gerät nimmt nur signierte Updates an."),
}
TEXT_NOT_A_DIR = "Das ist eine Datei, kein Ordner."
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
                 ("POST", "/api/format"), ("GET", "/api/zip")}
# GET /api/upload/resume always answers (offset 0 while locked or owned by USB: nothing can be held then).
# Requests that count as activity for the automatic lock (API v5); status polls do not.
ACTIVITY_PATHS = {"/api/files", "/api/download", "/api/mode", "/api/mkdir", "/api/delete", "/api/move", "/api/copy",
                  "/api/upload/check", "/api/upload", "/api/push", "/api/zip", "/api/upload/resume",
                  "/api/autolock/touch"}        # API v6: the explicit touch

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
    "next_version": "version reported after the next successful /api/ota (default 0.6.1)",
    "clock": "synced|unsynced|manual|<unix s> (device clock: NTP-synced, unknown (epoch 0), set from a browser; a "
             "number sets the clock to that time, not synced)",
    "resumable": "PATH|SIZE|OFFSET or none (pretend a broken resumable upload of PATH with SIZE bytes exists, "
                 "OFFSET bytes received; '|' separates)",
    "upload_drop_after": "bytes or none (the next upload/push connection is reset after that many body bytes; a "
                         "resumable upload keeps the received whole 4 KiB blocks)",
    "idle_s": "seconds (pretend the last file activity, host access, unplugging, suspend and - in file manager mode - the "
              "switch to it were that long ago; triggers the automatic lock if due)",
    "autolock_remaining": "seconds (API v6: shift the last activity so that the countdown shows that many seconds; needs a "
                          "timer, 0..minutes*60; 0 locks at once unless refused; no countdown while owner=usb)",
    "autolock_waiting": "0|1 (API v6: 1 makes every timer lock attempt fail as if the drive were busy; when the time is up "
                        "the status shows remaining_s 0, waiting true)",
    "net_dhcp": "0|1 (API v6: stored network mode; 0 without stored static values fills 192.0.2.152/24, gateway 192.0.2.1)",
    "net_fail": "0|1|<s> (API v6: the next network change makes the mock unreachable for 3 s (1) or s seconds)",
    "ota_signed": "0|1 (API v6: updates must be signed; status.ota.signed_required / key_id)",
    "ota_sig": "ok|bad|none|clear (API v6: result of the signature check of the next /api/ota that reaches it)",
    "reset": "1",
}


class ClientGone(Exception):
    """The client closed the connection before the request body was complete."""


# ============================================================== helpers

def local_now() -> int:
    """Unix seconds (UTC). API v5 reports every time stamp as real Unix time; until v4 the contract used local
    wall-clock seconds. Scenario files carry stamps written by someone with a clock, independent of the device
    clock (see Device.clock())."""
    return int(time.time())


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


_IPV4_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4_RE = re.compile(rf"{_IPV4_OCTET}(?:\.{_IPV4_OCTET}){{3}}")


def ipv4(s):
    """Dotted decimal IPv4 address (exactly four parts 0..255, no leading zeros) as int, or None."""
    if s is None or not _IPV4_RE.fullmatch(s):
        return None
    a, b, c, d = (int(x) for x in s.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def mask_prefix(m: int):
    """Prefix length of a contiguous netmask, or None (also for 0.0.0.0)."""
    inv = ~m & 0xFFFFFFFF
    if m == 0 or (inv & (inv + 1)) != 0:
        return None
    return 32 - inv.bit_length()


NET_TEXT = {        # texts of the firmware (web_wifi.c); the field is named in the text, there is no extra JSON key
    "dhcp": "Bitte „dhcp“ angeben: 1 = Adresse per DHCP, 0 = feste Adresse.",
    "ip": "Ungültige IP-Adresse: bitte im Format a.b.c.d angeben (nicht die Netz- oder Broadcast-Adresse und "
          "nicht die Adresse des Einrichtungs-WLANs).",
    "mask": "Ungültige Subnetzmaske: zum Beispiel 255.255.255.0 (von 255.0.0.0 bis 255.255.255.252).",
    "gateway": "Ungültiges Gateway: Es muss im selben Netz wie die IP-Adresse liegen und darf nicht die IP-Adresse "
               "selbst sein.",
    "dns": "Ungültiger DNS-Server: bitte im Format a.b.c.d angeben oder leer lassen (dann gilt das Gateway).",
    "hostname": "Ungültiger Gerätename: 1 bis 31 Zeichen, nur a–z, 0–9 und Bindestrich, nicht am Anfang "
                "oder Ende.",
}
AP_IP = "192.168.4.1"               # the setup access point keeps this address; a static ip must differ


def ip_unicast(v: int) -> bool:
    first = v >> 24
    return first not in (0, 127) and first < 224


def check_network(q: dict):
    """POST /api/settings/network like the firmware: returns (None, settings) or (field, None).
    Order: dhcp; with dhcp=0 ip, mask, gateway present (dns may be missing or empty); hostname; then the syntax of
    ip, mask, gateway, dns; then mask (contiguous, /8../30), ip (unicast, not network/broadcast, not the setup
    access point's address), gateway (unicast, same subnet, not ip, not network/broadcast), dns (unicast)."""
    dhcp = q.get("dhcp")
    if dhcp not in ("0", "1", "true", "false"):
        return "dhcp", None
    out = {"dhcp": dhcp in ("1", "true")}
    if not out["dhcp"]:
        for key in ("ip", "mask", "gateway"):
            if q.get(key) is None:
                return key, None
        if "dns" in q and q["dns"] is None:
            return "dns", None
    h = (q.get("hostname") or "").lower()          # host names are case-insensitive: stored in lower case
    if "hostname" in q and q["hostname"] is None:
        return "hostname", None
    if h:                                           # missing or empty: the stored host name stays
        if not HOSTNAME_RE.fullmatch(h):
            return "hostname", None
        out["hostname"] = h
    if out["dhcp"]:
        return None, out                # addresses are ignored with DHCP (the stored static values stay)
    vals = {}
    for key in ("ip", "mask", "gateway", "dns"):
        raw = q.get(key) or ""
        if key == "dns" and raw == "":
            continue
        v = ipv4(raw)
        if v is None:
            return key, None
        vals[key] = v
    ip, mask, gw = vals["ip"], vals["mask"], vals["gateway"]
    dns = vals.get("dns", gw)                      # no DNS server given: the gateway
    p = mask_prefix(mask)
    if p is None or not 8 <= p <= 30:
        return "mask", None
    host = ~mask & 0xFFFFFFFF
    if not ip_unicast(ip) or ip & host in (0, host) or ip == ipv4(AP_IP):
        return "ip", None
    if not ip_unicast(gw) or gw == ip or gw & mask != ip & mask or gw & host in (0, host):
        return "gateway", None
    if not ip_unicast(dns):
        return "dns", None
    out.update({"ip": q["ip"], "mask": q["mask"], "gateway": q["gateway"],
                "dns": q["dns"] if vals.get("dns") is not None else q["gateway"]})
    return None, out


def signature_state(length: int, tail: bytes) -> str:
    """Signature of an update image as the mock sees it (like ota_upload_has_signature() of the firmware, the mock
    cannot verify RSA): 'none' (size no multiple of 4096, or the last 4096-byte sector holds no signature block with
    magic 0xE7 and a valid CRC-32), 'ok' (a block whose public key has the mock's key id: first 4 bytes of the
    SHA-256 of the key) or 'bad' (a block with any other key)."""
    if length < 2 * SIG_SECTOR or length % SIG_SECTOR or len(tail) < SIG_SECTOR:
        return "none"
    block = tail[-SIG_SECTOR:]
    crc = struct.unpack_from("<I", block, SIG_BLOCK_CRC_AT)[0]
    if block[0] != 0xE7 or zlib.crc32(block[:SIG_BLOCK_CRC_AT]) & 0xFFFFFFFF != crc:
        return "none"
    key_id = hashlib.sha256(block[SIG_KEY_AT:SIG_KEY_AT + SIG_KEY_LEN]).hexdigest()[:8]
    return "ok" if key_id == MOCK_KEY_ID else "bad"


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


def visible_walk(node: Node, prefix: str = ""):
    """Like walk(), but skips hidden names (and everything below them), like the listing."""
    for child in list(node.children.values()):
        if is_hidden(child.name):
            continue
        path = prefix + child.name
        yield path, child
        if child.is_dir:
            yield from visible_walk(child, path + "/")


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
        # device clock (API v5): synced (NTP), unsynced (unknown: epoch 0) or manual (set by POST /api/time or the
        # upload parameter `now`; then clock_offset = device time - PC time)
        self.clock_mode = "synced"
        self.clock_offset = 0.0
        self.tz = TZ_DEFAULT
        self.lang = "de"                  # display language (API v5)
        self.autolock_min = 0             # automatic lock of the encrypted drive: idle minutes, 0 = off
        self.autolock_detach = False      # ... and/or when the USB host is unplugged
        self.detach_at = None             # on_detach: monotonic time the host disappeared (lock DETACH_LOCK_S later)
        self.suspend_since = None         # monotonic time the host went into USB suspend
        self.last_activity = now          # last file request (status polls do not count)
        self.partial = None               # kept broken resumable upload (dict) or None
        self.upload_drop_after = None     # bytes
        self.webdav = True                # WebDAV switch (optional in the contract)
        self.dav_locks = {}               # token -> path (LOCK is answered, never enforced)
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
        # API v6
        self.autolock_hold = False        # /mock/set?autolock_waiting=1: every timer lock attempt is refused (busy)
        self.net_dhcp = True              # network settings (one for all stored Wi-Fi networks)
        self.net_static = {"ip": "", "mask": "", "gateway": "", "dns": ""}     # stored, empty until set
        self.hostname = HOSTNAME_MOCK
        self.net_fail_s = 0               # next network change: unreachable this long
        self.net_down_until = None        # monotonic time until which connections are reset
        self.ota_signed = False           # updates must be signed
        self.ota_sig = None               # forced result of the next signature check: ok|bad|none

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
        self.clock_mode = "unsynced"           # no internet, no NTP: the clock is unknown until a browser sets it
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

    def _crypt_lock(self, why: str = ""):
        self.crypt_unlocked = False
        self.owner = "none"
        self.host_ejected = False
        self.last_read = self.last_write = None
        self.write_hold = False
        self.detach_at = None
        self.discard_partial("drive locked")
        self.log("I", "crypt", "drive locked" + (f" ({why})" if why else ""))

    def _crypt_unlock(self):
        """After unlocking the drive reports itself like after a change of mode."""
        self.crypt_unlocked = True
        self.last_activity = time.monotonic()
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

    # ---------------------------------------------------------- clock, auto-lock, resumable upload (API v5)

    def clock(self) -> int:
        """Device time as Unix seconds, 0 = unknown."""
        if self.clock_mode == "unsynced":
            return 0
        return int(time.time() + (self.clock_offset if self.clock_mode == "manual" else 0))

    def set_clock(self, epoch: int):
        self.clock_mode = "manual"
        self.clock_offset = epoch - time.time()
        self.log("I", "time", f"clock set by a client: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(epoch))} UTC")

    def time_json(self) -> dict:
        return {"epoch": self.clock(), "synced": self.clock_mode == "synced", "tz": self.tz}

    def discard_partial(self, why: str):
        if self.partial is not None:
            self.log("I", "drive", f"resumable upload '{self.partial['path']}' discarded ({why})")
            self.partial = None

    def partial_json(self):
        p = self.partial
        return None if p is None else {"path": p["path"], "size": p["size"], "offset": p["offset"]}

    def partial_offset(self, path, size: int) -> int:
        p = self.partial
        return p["offset"] if p is not None and p["path"] == path and p["size"] == size else 0

    def busy_for_lock(self) -> bool:
        """The automatic lock never strikes while an operation runs or the host writes."""
        return self.other_upload is not None or self.host_wrote_recently() or self.write_hold

    def autolock_tick(self):
        if not (self.crypt_enabled and self.crypt_unlocked and not self.crypt_damaged) or self.pending_boot:
            self.detach_at = None
            return
        now = time.monotonic()
        # on_detach (works in USB mode too; never while an operation runs or the host writes)
        if not self.busy_for_lock():
            if self.autolock_detach and self.detach_at is not None and now - self.detach_at >= DETACH_LOCK_S:
                self._crypt_lock("automatic: USB host unplugged")
                return
            if (self.autolock_detach and self.usb_suspended and self.suspend_since is not None
                    and now - self.suspend_since >= SUSPEND_LOCK_S):
                self._crypt_lock("automatic: USB host suspended")
                return
        # timer (API v6, 0.6.0): runs only while the file manager owns the drive, never in USB mode
        if self.autolock_due() and not self.autolock_hold:
            self._crypt_lock(f"automatic: {self.autolock_min} min without activity")

    @property
    def owner(self) -> str:
        return self._owner

    @owner.setter
    def owner(self, value: str):
        """Every owner change is noted: the timer period starts afresh when the file manager gets the drive."""
        if value != getattr(self, "_owner", None):
            self.owner_since = time.monotonic()
        self._owner = value

    def autolock_last(self) -> float:
        """Start of the current timer period: the last file request / touch, at the earliest the switch to the file
        manager. Host reads/writes do not count (they cannot happen in file manager mode)."""
        return max(self.last_activity, self.owner_since)

    def autolock_due(self) -> bool:
        """The timer's time is up: timer on, file manager mode, no upload of another client, period over."""
        return (self.autolock_min > 0 and self.owner == "app" and self.other_upload is None
                and time.monotonic() - self.autolock_last() >= self.autolock_min * 60)

    def autolock_countdown(self):
        """(remaining_s, waiting) of status.autolock (API v6). -1: no countdown (timer off, not unlocked, transfer
        running, or the drive is provided to the USB device). waiting: the time is up but the lock attempt was refused
        (/mock/set?autolock_waiting=1)."""
        if (not (self.crypt_enabled and self.crypt_unlocked and not self.crypt_damaged) or self.autolock_min == 0
                or self.other_upload is not None or self.pending_boot or self.owner != "app"):
            return -1, False
        left = self.autolock_min * 60 - (time.monotonic() - self.autolock_last())
        if left > 0:
            return int(math.ceil(left)), False
        return 0, True                   # due, but the lock attempt was refused

    def autolock_json(self) -> dict:
        remaining, waiting = self.autolock_countdown()
        return {"minutes": self.autolock_min, "on_detach": self.autolock_detach, "remaining_s": remaining,
                "waiting": waiting}

    # ---------------------------------------------------------- network settings (API v6)

    def net_connected(self) -> bool:
        return self.wifi_mode == "sta"

    def net_current(self) -> dict:
        """What the station interface has now (empty strings while not connected)."""
        if not self.net_connected():
            return {"ip": "", "mask": "", "gateway": "", "dns": ""}
        return dict(NET_DHCP_CURRENT) if self.net_dhcp else dict(self.net_static)

    def network_json(self) -> dict:
        return {"ok": True, "dhcp": self.net_dhcp, **self.net_static, "hostname": self.hostname,
                "current": self.net_current(), "connected": self.net_connected()}

    def net_apply(self, s: dict):
        """Applies validated settings (after the answer was sent, like the firmware)."""
        self.net_dhcp = s["dhcp"]
        if not s["dhcp"]:
            self.net_static = {k: s[k] for k in ("ip", "mask", "gateway", "dns")}
        if "hostname" in s:
            self.hostname = s["hostname"]
        cur = self.net_current()
        self.log("I", "wifi", "network: " + ("DHCP" if self.net_dhcp else
                                            f"static {self.net_static['ip']}/{self.net_static['mask']} "
                                            f"gw {self.net_static['gateway']} dns {self.net_static['dns']}")
                 + f", hostname {self.hostname}" + (f", IP {cur['ip']}" if cur["ip"] else ""))
        if self.net_fail_s:
            self.net_down_until = time.monotonic() + self.net_fail_s
            self.log("I", "mock", f"unreachable for {self.net_fail_s} s (net_fail)")
            self.net_fail_s = 0

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
                boot.append(("I", "wifi", f"connected to '{self.ssid}', IP {self.net_current()['ip']}"
                                          + ("" if self.net_dhcp else " (static)")))
        boot.append(("I", "wifi", f"mDNS: http://{self.hostname}.local"))
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
            self.log("I", "wifi", f"connected to '{self.ssid}', IP {self.net_current()['ip']}")
        self.autolock_tick()

    def is_offline(self) -> bool:
        if self.net_down_until is not None and time.monotonic() >= self.net_down_until:
            self.net_down_until = None
        return not self.online or self.pending_boot or self.net_down_until is not None

    def restart(self, download: bool = False):
        """The answer has been sent; the device is now gone for a while."""
        self.pending_boot = True
        self.offline_until = math.inf if download else time.monotonic() + RESTART_S

    def _boot(self, at: float):
        self.pending_boot = False
        self.offline_until = None
        self.boot_t = at
        self.other_upload = None
        # The RTC keeps the time across a software restart (power loss would clear it: /mock/set?clock=unsynced).
        self.partial = None                     # resumable uploads live in RAM only
        self.detach_at = None
        self.suspend_since = None
        self.dav_locks.clear()
        self.last_activity = at
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
        self.discard_partial("drive handed to USB")
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
        if mtime < MTIME_MIN:                    # not given: the device clock (0 while it is unknown)
            mtime = self.clock()
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
        parent.children[segs[-1].lower()] = Node(segs[-1], True, mtime=self.clock())
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
        self.discard_partial("format")
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
        upload["resumable"] = self.partial_json()
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
                "ip": self.net_current()["ip"], "rssi": -61 + random.randrange(-2, 3) if sta else 0,
                "ap_active": ap, "ap_ssid": "DataDrive-54D5", "ap_ip": "192.168.4.1" if ap else "",
                "hostname": self.hostname, "dhcp": self.net_dhcp,
            },
            "ota": {"partition": self.ota_partition,
                    "pending_verify": self.ota_verify_until is not None and time.monotonic() < self.ota_verify_until,
                    "signed_required": self.ota_signed, "key_id": MOCK_KEY_ID if self.ota_signed else ""},
            "time": self.time_json(),
            "autolock": self.autolock_json(),
            "lang": self.lang,
            "webdav": self.webdav,
        }

    # ---------------------------------------------------------- folder ZIP and WebDAV helpers (API v5)

    def zip_source(self, raw):
        """GET /api/zip: returns (node, top-level prefix, error code, error text)."""
        segs, err = parse_path(raw, existing=True)
        err = err or self._fs_ready()
        if err:
            return None, "", err, ""
        if any(is_hidden(s) for s in segs):
            return None, "", "NOT_FOUND", ""
        node = self.resolve(segs)
        if node is None:
            return None, "", "NOT_FOUND", ""
        if not node.is_dir:
            return None, "", "NOT_FOUND", TEXT_NOT_A_DIR      # like the firmware: a file is "no such folder"
        files = sum(0 if n.is_dir else 1 for _, n in visible_walk(node))
        if files > ZIP_FILES_MAX:
            return None, "", "TOO_MANY_FILES", ""
        return node, (segs[-1] + "/") if segs else "", None, ""

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
            "clock": self.clock_mode, "time": self.time_json(), "lang": self.lang,
            "autolock": {"minutes": self.autolock_min, "on_detach": self.autolock_detach,
                         "idle_s": round(time.monotonic() - max(x for x in (self.last_activity, self.last_read,
                                                                            self.last_write) if x is not None), 1),
                         "period_start_ago_s": round(time.monotonic() - self.autolock_last(), 1),
                         "lock_after_detach_s": None if self.detach_at is None else
                         round(max(0.0, self.detach_at + DETACH_LOCK_S - time.monotonic()), 1),
                         "remaining_s": self.autolock_countdown()[0], "waiting": self.autolock_countdown()[1],
                         "timer_runs": self.owner == "app", "hold": self.autolock_hold},
            "network": {**self.network_json(), "net_fail_s": self.net_fail_s,
                        "down_remaining_s": None if self.net_down_until is None else
                        round(max(0.0, self.net_down_until - time.monotonic()), 1)},
            "ota_signed": self.ota_signed, "ota_sig": self.ota_sig,
            "resumable": self.partial_json(), "upload_drop_after": self.upload_drop_after,
            "webdav": self.webdav, "dav_locks": len(self.dav_locks),
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
        if "clock" in q:
            c = q["clock"].strip().lower()
            if c in ("synced", "unsynced", "manual"):
                v["clock"] = c
            elif re.fullmatch(r"\d{1,11}", c) and EPOCH_MIN < int(c) < EPOCH_MAX:
                v["clock"] = int(c)
            else:
                raise ValueError(f"clock must be synced, unsynced, manual or Unix seconds {EPOCH_MIN}..{EPOCH_MAX}")
        if "resumable" in q:
            if is_cleared(q["resumable"]):
                v["resumable"] = None
            else:
                parts = q["resumable"].split("|")
                if len(parts) != 3 or not parts[1].strip().isdigit() or not parts[2].strip().isdigit():
                    raise ValueError("resumable must be PATH|SIZE|OFFSET (e.g. Projekte/Teil.3mf|1048576|262144) or none")
                rpath, rsize, roff = parts[0], int(parts[1]), int(parts[2])
                segs, err = parse_path(rpath)
                if err or not segs:
                    raise ValueError("resumable: invalid path")
                if not 0 < roff < rsize or rsize > 0xFFFFFFFF:
                    raise ValueError("resumable: needs 0 < OFFSET < SIZE <= 4 GiB")
                v["resumable"] = (rpath, rsize, roff)
        if "upload_drop_after" in q:
            v["upload_drop_after"] = None if is_cleared(q["upload_drop_after"]) else parse_size(q["upload_drop_after"])
        if "idle_s" in q:
            v["idle_s"] = parse_nonneg(q["idle_s"])
        for key in ("autolock_waiting", "net_dhcp", "ota_signed"):
            if key in q:
                v[key] = parse_bool(q[key])
        if "autolock_remaining" in q:
            s = parse_nonneg(q["autolock_remaining"])
            if self.autolock_min == 0:
                raise ValueError("autolock_remaining: the timer is off (POST /api/settings/autolock?minutes=N first)")
            if s > self.autolock_min * 60:
                raise ValueError(f"autolock_remaining must be 0..{self.autolock_min * 60} (minutes * 60)")
            v["autolock_remaining"] = s
        if "net_fail" in q:
            n = int(parse_nonneg(q["net_fail"]))
            v["net_fail"] = NET_FAIL_S if n == 1 else n
        if "ota_sig" in q:
            sig = q["ota_sig"].strip().lower()
            if sig in ("clear", "-", ""):
                v["ota_sig"] = None
            elif sig in ("ok", "bad", "none"):
                v["ota_sig"] = sig
            else:
                raise ValueError("ota_sig must be ok, bad, none or clear")

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
                self.detach_at = None                   # back within the grace time: no automatic lock
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
                if self.autolock_detach and self.crypt_enabled and self.crypt_unlocked and not self.crypt_damaged:
                    self.detach_at = time.monotonic()   # applied by autolock_tick() (never during an operation)
                    notes.append(f"autolock on_detach: the drive is locked in {DETACH_LOCK_S} s")
        if "usb_suspended" in v:
            if v["usb_suspended"] and not self.usb_suspended:
                self.suspend_since = time.monotonic()
                if self.autolock_detach and self.crypt_enabled and self.crypt_unlocked:
                    notes.append(f"autolock on_detach: the drive is locked after {SUSPEND_LOCK_S} s of suspend")
            elif not v["usb_suspended"]:
                self.suspend_since = None
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
        if "clock" in v:
            c = v["clock"]
            if isinstance(c, int):
                self.set_clock(c)
            elif c == "manual":
                self.set_clock(int(time.time()))
            else:
                self.clock_mode, self.clock_offset = c, 0.0
            notes.append(f"clock: {self.clock_mode}, epoch {self.clock()}")
        if "resumable" in v:
            if v["resumable"] is None:
                self.discard_partial("mock")
            else:
                rpath, rsize, roff = v["resumable"]
                filler = b"PK\x03\x04" + bytes(max(0, min(roff, STORE_MAX if rsize <= STORE_MAX else STORE_HEAD) - 4))
                self.partial = {"path": rpath, "size": rsize, "offset": roff, "data": bytearray(filler[:roff]),
                                "tail": bytearray(filler[-TAIL_SIZE:]) if roff >= 4 else bytearray(filler[:roff])}
                notes.append(f"resumable upload '{rpath}': {roff} of {rsize} bytes (content: 'PK\\3\\4' + zero bytes)")
        if "upload_drop_after" in v:
            self.upload_drop_after = v["upload_drop_after"]
        if "idle_s" in v:
            back = v["idle_s"]
            self.last_activity -= back
            if self.last_read is not None:
                self.last_read -= back
            if self.last_write is not None:
                self.last_write -= back
            if self.detach_at is not None:
                self.detach_at -= back
            if self.suspend_since is not None:
                self.suspend_since -= back
            if self.owner == "app":             # the period cannot have started before the switch to the file manager
                self.owner_since -= back
        if "autolock_waiting" in v:
            self.autolock_hold = v["autolock_waiting"]
            notes.append("timer lock " + ("refused while due (waiting)" if self.autolock_hold else "locks when due"))
        if "autolock_remaining" in v:
            start = time.monotonic() - (self.autolock_min * 60 - v["autolock_remaining"])
            self.last_activity = start
            if self.last_read is not None:
                self.last_read = min(self.last_read, start)
            if self.last_write is not None:
                self.last_write = min(self.last_write, start)
            if self.owner == "app":
                self.owner_since = min(self.owner_since, start)
            else:
                notes.append("the drive is provided to the USB device: no countdown (the timer only runs in file manager "
                             "mode and starts afresh when the file manager gets the drive)")
        if "net_dhcp" in v:
            self.net_dhcp = v["net_dhcp"]
            if not self.net_dhcp and not self.net_static["ip"]:
                self.net_static = {"ip": FAKE_IP, "mask": "255.255.255.0", "gateway": "192.0.2.1", "dns": "192.0.2.1"}
            notes.append("network: " + ("DHCP" if self.net_dhcp else f"static {self.net_static['ip']}"))
        if "net_fail" in v:
            self.net_fail_s = v["net_fail"]
            if self.net_fail_s:
                notes.append(f"the next network change makes the mock unreachable for {self.net_fail_s} s")
        if "ota_signed" in v:
            self.ota_signed = v["ota_signed"]
            notes.append("updates " + (f"must be signed (key id {MOCK_KEY_ID})" if self.ota_signed else "need no signature"))
        if "ota_sig" in v:
            self.ota_sig = v["ota_sig"]
        self.autolock_tick()
        if "autolock_remaining" in v or "autolock_waiting" in v:
            r, w = self.autolock_countdown()
            notes.append(f"countdown: remaining_s {r}, waiting {str(w).lower()}")
        if "idle_s" in v or "usb_connected" in v or "autolock_remaining" in v or "autolock_waiting" in v:
            notes.append("drive is " + ("locked" if self.drive_locked() else "unlocked" if self.crypt_enabled
                                        else "not encrypted"))
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
    do_PROPFIND = do_PROPPATCH = do_MKCOL = do_MOVE = do_COPY = do_LOCK = do_UNLOCK = _other_method   # WebDAV

    # ---------------------------------------------------------- plumbing

    @property
    def dev(self) -> Device:
        return self.server.dev

    def _route(self, method: str):
        self._status = None
        self._info = ""
        self._user, self._token, self._locked = None, None, False
        self._activity = False
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
            if self._activity:                   # the idle time of the automatic lock starts when the request ends
                dev.last_activity = time.monotonic()
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
        if path == DAV_PREFIX or path.startswith(DAV_PREFIX + "/"):     # WebDAV (API v5): HTTP Basic only
            self._dav(method, path, clen)
            return
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
            # API v5
            ("POST", "/api/settings/time"): (self._api_settings_time, "admin"),
            ("POST", "/api/time"): (self._api_time, "user"),
            ("POST", "/api/settings/autolock"): (self._api_settings_autolock, "admin"),
            ("POST", "/api/settings/lang"): (self._api_settings_lang, "user"),
            ("POST", "/api/settings/webdav"): (self._api_settings_webdav, "admin"),
            ("GET", "/api/upload/resume"): (self._api_upload_resume, "user"),
            ("GET", "/api/zip"): (self._api_zip, "user"),
            # API v6
            ("POST", "/api/autolock/touch"): (self._api_autolock_touch, "user"),
            ("GET", "/api/network"): (self._api_network, "admin"),
            ("POST", "/api/settings/network"): (self._api_settings_network, "admin"),
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
        if path in ACTIVITY_PATHS:
            self._activity = True                     # automatic lock: file requests count, status polls do not
            dev.last_activity = time.monotonic()
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
        # API v5: resumable upload (/api/upload only): size = whole file, offset = where this body starts
        resume = not push and "offset" in q
        offset, size = 0, clen
        if resume:
            if not re.fullmatch(r"\d{1,20}", q.get("offset") or "") or not re.fullmatch(r"\d{1,20}", q.get("size") or ""):
                self._drain(clen)
                self._error("BAD_REQUEST", "offset und size müssen Zahlen sein")
                return
            offset, size = int(q["offset"]), int(q["size"])
            server_off = dev.partial_offset(path, size)
            if offset <= size and offset + clen != size:
                self._drain(clen)
                self._error("BAD_REQUEST", "Content-Length muss size - offset sein")
                return
            if offset != server_off:                   # also offset=0 while a part of this file is held
                self._drain(clen)
                self._error("RESUME_MISMATCH", extra={"offset": server_off}, info=f"(server offset {server_off})")
                return
        segs, err = dev.upload_static_check(path, size)
        took = False
        if not err and push and dev.owner == "usb":
            err = dev.set_owner("app", force)           # same rules as /api/mode?target=app
            took = err is None
        if not err:
            err = dev.upload_app_check(segs, size, overwrite)

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

        prev = dev.partial if resume and offset > 0 else None
        dev.discard_partial("continued" if prev else "another upload")   # one partial upload at a time
        dev.log("I", "drive", f"{'push' if push else 'upload'} {'resume' if prev else 'start'} '{path}' "
                              f"({clen} bytes" + (f" from offset {offset} of {size})" if resume else ")"))
        stop_at = None
        if dev.upload_fail_at is not None:
            stop_at = int(clen * dev.upload_fail_at / 100)
            dev.upload_fail_at = None
        if dev.upload_drop_after is not None:
            stop_at = min(clen, dev.upload_drop_after) if stop_at is None else min(stop_at, dev.upload_drop_after)
            dev.upload_drop_after = None
        keep_limit = size if size <= STORE_MAX else STORE_HEAD
        keep = bytearray(prev["data"]) if prev else bytearray()
        # rolling end of the stream: enough to cut back to a block boundary and still have TAIL_SIZE bytes
        tail = bytearray(prev["tail"]) if prev else bytearray()
        got = [0]

        def on_data(data: bytes):
            got[0] += len(data)
            if len(keep) < keep_limit:
                keep.extend(data[:keep_limit - len(keep)])
            tail.extend(data)
            del tail[:-(TAIL_SIZE + 2 * RESUME_BLOCK)]

        def keep_partial() -> str:
            """A broken resumable upload keeps the received whole blocks (RAM bookkeeping, data in the temp file)."""
            if not resume:
                return "nothing kept"
            new_off = offset + got[0] // RESUME_BLOCK * RESUME_BLOCK
            if new_off <= offset and not prev:
                return "nothing kept (less than one block)"
            cut = offset + got[0] - new_off            # bytes beyond the last whole block
            t = tail[:len(tail) - cut] if cut else tail
            dev.partial = {"path": path, "size": size, "offset": new_off, "data": keep[:min(new_off, keep_limit)],
                           "tail": bytearray(t[-TAIL_SIZE:])}
            return f"resumable at offset {new_off} of {size}"

        t0 = time.monotonic()
        try:
            received = self._read_body(clen, throttled=True, on_data=on_data, stop_at=stop_at)
        except (ClientGone, ConnectionError, TimeoutError, socket.timeout):
            dev.log("W", "web", "upload connection lost (-1)")
            kept = keep_partial()
            dev.log("W", "drive", f"upload aborted '{path}' after {got[0]} bytes, {kept}")
            self._info = f"upload aborted, {kept}:"
            give_back()
            raise
        if received < clen:
            dev.log("W", "web", "upload connection lost (-1)")
            kept = keep_partial()
            dev.log("W", "drive", f"upload aborted '{path}' after {received} bytes, {kept}")
            give_back()
            self._abort(f"connection reset after {received} of {clen} B ({kept})")
            return

        dt = max(time.monotonic() - t0, 1e-6)
        err = None
        name = segs[-1]
        if has_suffix_ci(name, ".3mf"):
            # "PK\5\6" must start at least 22 bytes (size of the record) before the end.
            end = bytes(tail[-TAIL_SIZE:])
            zip_ok = bytes(keep[:4]) == b"PK\x03\x04" and end.find(b"PK\x05\x06", 0, max(0, len(end) - 18)) >= 0
            if not zip_ok:
                err = "INVALID_FILE"
        if not err and resume:                           # the final checks run again when the file is complete
            err = dev.upload_app_check(segs, size, overwrite)
            if err == "NO_SPACE":
                err = None                               # the space was reserved when the upload began
        if not err:
            mtime = leading_uint(q.get("mtime"))
            client_now = leading_uint(q.get("now"))      # as before: sets the device clock once, if it is unknown
            if EPOCH_MIN < client_now < EPOCH_MAX and dev.clock_mode == "unsynced":
                dev.set_clock(client_now)
            dev.store_upload(segs, bytes(keep), size, mtime)
        else:
            dev.log("W", "drive", f"upload failed '{path}': {ERRORS[err][1]}")
        dev.log("I", "web", f"upload '{path}' {clen} bytes in {dt * 1000:.0f} ms ({clen / 1024 / dt:.1f} KB/s): "
                            f"{ERRORS[err][1] if err else 'OK'}")
        give_back()
        info = f"{clen} B in {dt:.2f} s" + (f" = {clen / 1024 / dt:.0f} KB/s" if dt >= 0.05 else "")
        if resume:
            info += f" (resumable, offset {offset} of {size})"
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

    # ---------------------------------------------------------- API v5: clock, auto-lock, language, WebDAV switch

    def _api_settings_time(self, q, clen):
        self._drain(clen)
        tz = q.get("tz")
        if not tz or len(tz.encode("utf-8")) > TZ_MAX or not re.fullmatch(r"[\x21-\x7e]+", tz):
            self._error("BAD_REQUEST", "tz fehlt oder ist ungültig (1 bis 63 druckbare ASCII-Zeichen ohne Leerzeichen)")
            return
        self.dev.tz = tz
        self.dev.log("I", "time", f"time zone '{tz}'")
        self._json(200, {"ok": True, "time": self.dev.time_json()}, f"tz={tz}")

    def _api_time(self, q, clen):
        self._drain(clen)
        dev = self.dev
        raw = q.get("epoch") or ""
        if not re.fullmatch(r"\d{1,11}", raw) or not EPOCH_MIN < int(raw) < EPOCH_MAX:
            self._error("BAD_REQUEST", "epoch fehlt oder ist nicht plausibel")
            return
        done = dev.clock_mode != "synced"          # an NTP-synced clock is never overridden
        if done:
            dev.set_clock(int(raw))
        self._json(200, {"ok": True, "set": done}, "clock set" if done else "NTP-synced, ignored")

    def _api_settings_autolock(self, q, clen):
        self._drain(clen)
        dev = self.dev
        if "minutes" not in q and "on_detach" not in q:
            self._error("BAD_REQUEST", "minutes oder on_detach fehlt")
            return
        minutes, detach = dev.autolock_min, dev.autolock_detach
        if "minutes" in q:
            if not re.fullmatch(r"\d{1,4}", q["minutes"] or "") or int(q["minutes"]) > AUTOLOCK_MAX:
                self._error("BAD_REQUEST", f"minutes muss 0 bis {AUTOLOCK_MAX} sein")
                return
            minutes = int(q["minutes"])
        if "on_detach" in q:
            if q["on_detach"] not in ("0", "1", "true", "false"):
                self._error("BAD_REQUEST", "on_detach muss 0 oder 1 sein")
                return
            detach = q["on_detach"] in ("1", "true")
        if minutes != dev.autolock_min:
            dev.last_activity = time.monotonic()    # a changed time counts from now
        dev.autolock_min, dev.autolock_detach = minutes, detach
        dev.log("I", "crypt", f"autolock: {minutes} min, on detach {int(detach)}")
        self._json(200, {"ok": True, "autolock": {"minutes": minutes, "on_detach": detach}},
                   f"autolock {minutes} min, on_detach={int(detach)}")

    # ---------------------------------------------------------- API v6: countdown, network settings

    def _api_autolock_touch(self, q, clen):
        self._drain(clen)
        dev = self.dev
        dev.last_activity = time.monotonic()        # (also set by ACTIVITY_PATHS; here before the answer is built)
        remaining = dev.autolock_countdown()[0]
        self._json(200, {"ok": True, "remaining_s": remaining}, f"touch: remaining_s {remaining}")

    def _api_network(self, q):
        self._json(200, self.dev.network_json())

    def _api_settings_network(self, q, clen):
        dev = self.dev
        if clen:                                    # like the firmware: a form body, else the query string
            q = self._form(clen, 384)
            if q is None:
                return
        field, s = check_network(q)
        if field:
            self._error("BAD_REQUEST", NET_TEXT[field], f"({field})")
            return
        hostname = s.get("hostname", dev.hostname)
        ip = "" if s["dhcp"] else s["ip"]
        down = dev.net_fail_s
        self._json(200, {"ok": True, "restart": False, "ip": ip, "hostname": hostname},
                   ("DHCP" if s["dhcp"] else f"static {ip}") + f", hostname {hostname}"
                   + (f" (unreachable for {down} s)" if down else ""))
        self.wfile.flush()
        dev.net_apply(s)                            # applied after the answer, like the firmware

    def _api_settings_lang(self, q, clen):
        self._drain(clen)
        value = q.get("value")
        if value not in ("de", "en"):
            self._error("BAD_REQUEST", "value muss de oder en sein")
            return
        self.dev.lang = value
        self._json(200, {"ok": True, "lang": value}, f"display language {value}")

    def _api_settings_webdav(self, q, clen):
        self._drain(clen)
        value = q.get("value")
        if value not in ("0", "1"):
            self._error("BAD_REQUEST", "value muss 0 oder 1 sein")
            return
        self.dev.webdav = value == "1"
        self.dev.log("I", "dav", f"WebDAV {'on' if self.dev.webdav else 'off'}")
        self._json(200, {"ok": True, "webdav": self.dev.webdav}, f"webdav={value}")

    # ---------------------------------------------------------- API v5: resumable upload, folder ZIP

    def _api_upload_resume(self, q):
        if not self._need(q, "path"):
            return
        if not re.fullmatch(r"\d{1,20}", q.get("size") or ""):
            self._error("BAD_REQUEST", "size fehlt oder ist ungültig")
            return
        off = self.dev.partial_offset(q["path"], int(q["size"]))
        self._json(200, {"ok": True, "offset": off}, f"offset {off}")

    def _api_zip(self, q):
        dev = self.dev
        node, prefix, err, text = dev.zip_source(q.get("path", ""))
        if err:
            self._error(err, text)
            return
        fname = (prefix[:-1] or "DataDrive") + ".zip"
        ascii_name = fname.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname, safe='')}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        out = self.wfile

        class Chunked:
            """Write-only stream without tell/seek: zipfile then writes data descriptors (bit 3), like the device."""
            sent = 0

            def write(self, b):
                if b:
                    out.write(f"{len(b):X}\r\n".encode() + bytes(b) + b"\r\n")
                    Chunked.sent += len(b)
                return len(b)

            def flush(self):
                out.flush()

        count = 0
        with zipfile.ZipFile(Chunked(), "w", zipfile.ZIP_STORED) as z:
            entries = ([(prefix, node)] if prefix else []) + list(visible_walk(node, prefix))
            for path, n in entries:
                name = path if not n.is_dir or path.endswith("/") else path + "/"
                t = time.localtime(n.mtime) if n.mtime >= MTIME_MIN else None
                zi = zipfile.ZipInfo(name, date_time=t[:6] if t and t.tm_year >= 1980 else (1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_STORED
                zi.external_attr = (0o40775 << 16) | 0x10 if n.is_dir else 0o644 << 16
                z.writestr(zi, b"" if n.is_dir else n.data)
                count += 1
        out.write(b"0\r\n\r\n")
        self._info = f"{count} entries, {Chunked.sent} B (stored, chunked)"

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
        tail = bytearray()                       # last bytes: the signature sector of a signed image (API v6)

        def on_data(data: bytes):
            if not first:
                first.append(data[0])
            tail.extend(data)
            if len(tail) > 2 * SIG_SECTOR:
                del tail[:-SIG_SECTOR]

        t0 = time.monotonic()
        self._read_body(clen, throttled=True, on_data=on_data)      # throttled like an upload
        if dev.ota_fail:
            err = dev.ota_fail
        elif clen == 0 or first[0] != 0xE9:
            err = "INVALID_FILE"
        elif dev.ota_signed:                     # API v6: signature check after the image check
            sig = dev.ota_sig or signature_state(clen, bytes(tail))
            dev.ota_sig = None
            err = {"bad": "BAD_SIGNATURE", "none": "UNSIGNED"}.get(sig)
            if not err:
                dev.log("I", "ota", f"signature ok (key {MOCK_KEY_ID})")
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

    # ---------------------------------------------------------- WebDAV (API v5): /dav/, HTTP Basic only

    DAV_ALLOW = "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK"
    DAV_STATUS = {      # (code, method group) -> HTTP status that WebDAV clients expect (RFC 4918)
        ("NOT_FOUND", "parent"): 409, ("EXISTS", "mkcol"): 405, ("EXISTS", "target"): 412,
        ("BAD_NAME", "target"): 403, ("IS_DIR", "put"): 405,
    }

    def _dav_error(self, code: str, status=None, text: str = ""):
        """Same JSON body as the API (clients ignore it, it helps when debugging), WebDAV-like HTTP status."""
        st, default = ERRORS.get(code, (status or 400, ""))
        hdrs = []
        if code in ("UNAUTHORIZED", "BAD_LOGIN"):
            hdrs.append(("WWW-Authenticate", f'Basic realm="{DAV_REALM}", charset="UTF-8"'))
        if code == "LOCKED":
            hdrs.append(("Retry-After", str(max(1, self.dev.blocked_s()))))
        if status == 405:
            hdrs.append(("Allow", self.DAV_ALLOW))
        self._info = (self._info + " " + code).strip()
        body = json.dumps({"ok": False, "code": code, "error": text or default}, ensure_ascii=False).encode("utf-8")
        self._send(status or st, body, "application/json", headers=hdrs)

    def _dav_auth(self):
        """HTTP Basic only: a session cookie is ignored here (no CSRF header needed, none checked)."""
        dev = self.dev
        user = None
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

    def _dav(self, method: str, path: str, clen):
        dev = self.dev
        if not dev.webdav:
            self._drain(clen)
            self._send(404, b"Nothing matches the given URI", "text/plain")
            return
        if dev.auth_unavailable:
            self._drain(clen)
            self._dav_error("AUTH_UNAVAILABLE")
            return
        self._dav_auth()
        if self._user is None:
            self._drain(clen)
            self._dav_error("LOCKED" if self._locked else "UNAUTHORIZED")
            return
        if dev.auth_enabled and dev.is_default_password(self._user):
            self._drain(clen)
            self._dav_error("MUST_CHANGE_PASSWORD")
            return
        try:
            rel = unquote_to_bytes(path[len(DAV_PREFIX):]).decode("utf-8").strip("/")
        except UnicodeDecodeError:
            self._drain(clen)
            self._dav_error("BAD_NAME")
            return
        if method == "OPTIONS":
            self._drain(clen)
            self._send(200, b"", "text/plain", headers=[("DAV", "1, 2"), ("Allow", self.DAV_ALLOW),
                                                        ("MS-Author-Via", "DAV")])
            return
        handlers = {"PROPFIND": self._dav_propfind, "GET": self._dav_get, "HEAD": self._dav_get,
                    "PUT": self._dav_put, "DELETE": self._dav_delete, "MKCOL": self._dav_mkcol,
                    "MOVE": self._dav_move, "COPY": self._dav_move, "LOCK": self._dav_lock,
                    "UNLOCK": self._dav_unlock, "PROPPATCH": self._dav_proppatch}
        handler = handlers.get(method)
        if handler is None:
            self._drain(clen)
            self._dav_error("BAD_REQUEST", 405, "Methode nicht unterstützt")
            return
        # same drive rules as the file API
        if dev.drive_locked():
            self._drain(clen)
            self._dav_error(dev.locked_code())
            return
        err = dev._fs_ready()
        if err:
            self._drain(clen)
            self._dav_error(err)
            return
        self._activity = True
        dev.last_activity = time.monotonic()
        handler(method, rel, clen)

    @staticmethod
    def _dav_junk(rel) -> bool:
        """Probe and metadata files of Windows and macOS (like web_dav.c): any segment with a leading dot, and
        desktop.ini / Thumbs.db / ehthumbs.db as the last segment. Reads answer 404, writes 403."""
        segs = rel.split("/") if rel else []
        return any(s.startswith(".") for s in segs) or bool(segs) and segs[-1].lower() in (
            "desktop.ini", "thumbs.db", "ehthumbs.db")

    def _dav_node(self, rel):
        """Existing object (hidden and junk names do not exist): (node, segments) or (None, None)."""
        segs, err = parse_path(rel, existing=True)
        if err or any(is_hidden(s) for s in segs) or self._dav_junk(rel):
            return None, None
        node = self.dev.resolve(segs)
        return (node, segs) if node is not None else (None, None)

    @staticmethod
    def _dav_href(segs, is_dir: bool) -> str:
        return DAV_PREFIX + "/" + quote("/".join(segs), safe="/") + ("/" if is_dir and segs else "")

    @staticmethod
    def _dav_meta(n: Node):
        mtime = n.mtime if n.mtime >= MTIME_MIN else MTIME_MIN
        etag = f'"{n.size:x}-{mtime:x}"'
        ctype = mimetypes.guess_type(n.name)[0] or "application/octet-stream"
        return mtime, etag, ctype

    def _dav_response(self, segs, n: Node) -> str:
        mtime, etag, ctype = self._dav_meta(n)
        props = [f"<D:displayname>{xml_escape(segs[-1] if segs else '')}</D:displayname>",
                 f"<D:getlastmodified>{formatdate(mtime, usegmt=True)}</D:getlastmodified>",
                 f"<D:creationdate>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(mtime))}</D:creationdate>"]
        if n.is_dir:
            props.append("<D:resourcetype><D:collection/></D:resourcetype>")
        else:
            props += ["<D:resourcetype/>", f"<D:getcontentlength>{n.size}</D:getcontentlength>",
                      f"<D:getcontenttype>{ctype}</D:getcontenttype>", f"<D:getetag>{xml_escape(etag)}</D:getetag>"]
        if not segs:
            props += [f"<D:quota-available-bytes>{self.dev.free()}</D:quota-available-bytes>",
                      f"<D:quota-used-bytes>{self.dev.used_by_files()}</D:quota-used-bytes>"]
        props.append("<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>"
                     "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>")
        return (f"<D:response><D:href>{xml_escape(self._dav_href(segs, n.is_dir))}</D:href><D:propstat><D:prop>"
                f"{''.join(props)}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")

    def _dav_propfind(self, method, rel, clen):
        self._drain(clen)                              # the requested property list is ignored: all are sent
        depth = (self.headers.get("Depth") or "infinity").strip().lower()    # missing = infinity (RFC 4918)
        if depth not in ("0", "1"):
            self._dav_error("FORBIDDEN", 403, "Depth: infinity wird nicht unterstützt")
            return
        node, segs = self._dav_node(rel)
        if node is None:
            self._dav_error("NOT_FOUND")
            return
        items = [(segs, node)]
        if depth == "1" and node.is_dir:
            items += [(segs + [c.name], c) for c in node.children.values() if not is_hidden(c.name)]
        body = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                + "".join(self._dav_response(s, n) for s, n in items) + "</D:multistatus>")
        self._info = f"depth {depth}, {len(items)} entries"
        self._send(207, body.encode("utf-8"), 'application/xml; charset="utf-8"')

    def _dav_get(self, method, rel, clen):
        self._drain(clen)
        node, segs = self._dav_node(rel)
        if node is None:
            self._dav_error("NOT_FOUND")
            return
        if node.is_dir:
            self._dav_error("IS_DIR", 405)
            return
        mtime, etag, ctype = self._dav_meta(node)
        self._info = f"{len(node.data)} B"
        self._send(200, node.data, ctype, headers=[("Last-Modified", formatdate(mtime, usegmt=True)), ("ETag", etag)])

    def _dav_put(self, method, rel, clen):
        dev = self.dev
        if clen is None or self.headers.get("Transfer-Encoding"):
            self._dav_error("BAD_REQUEST", 411, "Content-Length fehlt (chunked wird nicht angenommen)")
            return
        if self._dav_junk(rel):                        # macOS ._* / .DS_Store, desktop.ini, Thumbs.db
            self._drain(clen)
            self._dav_error("FORBIDDEN", 403, "Dieser Name ist nicht erlaubt.")
            return
        segs, err = parse_path(rel)
        if not err and not segs:
            self._drain(clen)
            self._dav_error("IS_DIR", 405)
            return
        err = err or check_name(segs[-1], dev.types) or ("TOO_LARGE" if clen > 0xFFFFFFFF else None)
        if err:
            self._drain(clen)
            self._dav_error(err)
            return
        parent = dev.resolve(segs[:-1])
        if parent is None or not parent.is_dir:
            self._drain(clen)
            self._dav_error("NOT_FOUND", 409, "Der Zielordner fehlt.")
            return
        old = parent.children.get(segs[-1].lower())
        if old is not None and old.is_dir:
            self._drain(clen)
            self._dav_error("IS_DIR", 405)
            return
        if clen + FREE_RESERVE > dev.free():
            self._drain(clen)
            self._dav_error("NO_SPACE")
            return
        dev.discard_partial("another upload")
        keep_limit = clen if clen <= STORE_MAX else STORE_HEAD
        keep, tail = bytearray(), bytearray()

        def on_data(data: bytes):
            if len(keep) < keep_limit:
                keep.extend(data[:keep_limit - len(keep)])
            tail.extend(data)
            del tail[:-TAIL_SIZE]
        self._read_body(clen, throttled=True, on_data=on_data)
        if clen > 0 and has_suffix_ci(segs[-1], ".3mf"):   # empty files are allowed (Windows creates them first)
            if not (bytes(keep[:4]) == b"PK\x03\x04" and tail.find(b"PK\x05\x06", 0, max(0, len(tail) - 18)) >= 0):
                self._dav_error("INVALID_FILE")
                return
        dev.store_upload(segs, bytes(keep), clen, 0)
        self._info = f"{clen} B {'replaced' if old else 'created'}"
        self._send(204 if old is not None else 201, b"", "text/plain")

    def _dav_delete(self, method, rel, clen):
        self._drain(clen)
        node, segs = self._dav_node(rel)
        if node is None:
            self._dav_error("NOT_FOUND")
        elif not segs:
            self._dav_error("FORBIDDEN", 403, "Das Hauptverzeichnis kann nicht gelöscht werden.")
        else:
            err = self.dev.delete("/".join(segs), True)      # a collection goes with everything in it
            if err:
                self._dav_error(err)
            else:
                self._send(204, b"", "text/plain")

    def _dav_mkcol(self, method, rel, clen):
        if clen:
            self._drain(clen)
            self._dav_error("BAD_REQUEST", 415, "MKCOL mit Inhalt wird nicht unterstützt")
            return
        if not rel:
            self._dav_error("EXISTS", 405)
            return
        if self._dav_junk(rel):
            self._dav_error("FORBIDDEN", 403, "Dieser Name ist nicht erlaubt.")
            return
        err = self.dev.mkdir(rel)
        if err:
            self._dav_error(err, self.DAV_STATUS.get((err, "mkcol")) or self.DAV_STATUS.get((err, "parent")))
        else:
            self._send(201, b"", "text/plain")

    def _dav_move(self, method, rel, clen):
        self._drain(clen)
        dev = self.dev
        dest = self.headers.get("Destination") or ""
        dpath = urlsplit(dest).path
        if not (dpath == DAV_PREFIX or dpath.startswith(DAV_PREFIX + "/")):
            self._dav_error("BAD_REQUEST", 400, "Destination fehlt oder liegt nicht unter /dav/")
            return
        try:
            drel = unquote_to_bytes(dpath[len(DAV_PREFIX):]).decode("utf-8").strip("/")
        except UnicodeDecodeError:
            self._dav_error("BAD_NAME", 403)
            return
        overwrite = (self.headers.get("Overwrite") or "T").strip().upper() != "F"
        node, segs = self._dav_node(rel)
        if node is None:
            self._dav_error("NOT_FOUND")
            return
        dsegs, err = parse_path(drel)
        if not segs or err or not dsegs or drel == rel:
            self._dav_error("BAD_NAME", 403)
            return
        lsrc, ldst = [s.lower() for s in segs], [s.lower() for s in dsegs]
        if len(lsrc) > len(ldst) and lsrc[:len(ldst)] == ldst:
            self._dav_error("BAD_NAME", 403, "Das Ziel enthält die Quelle.")
            return
        dparent = dev.resolve(dsegs[:-1])
        if dparent is None or not dparent.is_dir:
            self._dav_error("NOT_FOUND", 409, "Der Zielordner fehlt.")
            return
        target = dparent.children.get(dsegs[-1].lower())
        if target is node and method == "COPY":
            self._dav_error("BAD_NAME", 403, "Quelle und Ziel sind gleich.")
            return
        existed = target is not None and target is not node     # MOVE onto itself = case-only rename
        if existed and not overwrite:
            self._dav_error("EXISTS", 412)
            return
        if existed:
            dev.delete(drel, True)
        err = dev.move(rel, drel) if method == "MOVE" else dev.copy(rel, drel)
        if err:
            self._dav_error(err, self.DAV_STATUS.get((err, "target")) or self.DAV_STATUS.get((err, "parent")))
        else:
            self._send(204 if existed else 201, b"", "text/plain")

    def _dav_lock(self, method, rel, clen):
        """Answered so that Windows and macOS write, but never enforced (one user at a time anyway)."""
        if clen and clen > 65536:
            self._drain(clen)
            self._dav_error("BAD_REQUEST", 400, "Anfrage zu groß")
            return
        self._drain(clen)
        segs, err = parse_path(rel, existing=True)
        if err or self._dav_junk(rel):
            self._dav_error("FORBIDDEN", 403, "Dieser Name ist nicht erlaubt.")
            return
        parent = self.dev.resolve(segs[:-1]) if segs else None
        if segs and (parent is None or not parent.is_dir):
            self._dav_error("NOT_FOUND", 409, "Der Zielordner fehlt.")
            return
        created = bool(segs) and self.dev.resolve(segs) is None      # firmware: placeholder until the PUT
        m = re.search(r"opaquelocktoken:[0-9a-f-]+", self.headers.get("If") or "")
        token = m.group(0) if m else f"opaquelocktoken:{uuid.uuid4()}"
        self.dev.dav_locks[token] = rel
        href = xml_escape(self._dav_href(segs, False))
        body = ('<?xml version="1.0" encoding="utf-8"?><D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
                "<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth>"
                f"<D:timeout>Second-3600</D:timeout><D:locktoken><D:href>{token}</D:href></D:locktoken>"
                f"<D:lockroot><D:href>{href}</D:href></D:lockroot></D:activelock></D:lockdiscovery></D:prop>")
        self._send(201 if created else 200, body.encode("utf-8"), 'application/xml; charset="utf-8"',
                   headers=[("Lock-Token", f"<{token}>")])

    def _dav_unlock(self, method, rel, clen):
        self._drain(clen)
        self.dev.dav_locks.pop((self.headers.get("Lock-Token") or "").strip("<> "), None)
        self._send(204, b"", "text/plain")

    def _dav_proppatch(self, method, rel, clen):
        """Accepted and ignored (Windows sets its Win32 times): every property is answered with 200."""
        if clen is None or clen > 65536:
            self._drain(clen)
            self._dav_error("BAD_REQUEST", 400, "Anfrage fehlt oder ist zu groß")
            return
        body = bytearray()
        self._read_body(clen, on_data=body.extend)
        node, segs = self._dav_node(rel)
        if node is None:
            self._dav_error("NOT_FOUND")
            return
        try:
            root = ElementTree.fromstring(bytes(body)) if body else None
        except ElementTree.ParseError:
            self._dav_error("BAD_REQUEST", 400, "XML nicht lesbar")
            return
        names = []
        if root is not None:
            for prop in root.iter("{DAV:}prop"):
                names += [child.tag for child in prop]
        props = []
        for i, tag in enumerate(names):
            ns, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
            props.append(f'<x{i}:{local} xmlns:x{i}="{xml_escape(ns)}"/>' if ns else f"<{local}/>")
        out = (f'<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:"><D:response>'
               f"<D:href>{xml_escape(self._dav_href(segs, node.is_dir))}</D:href><D:propstat><D:prop>{''.join(props)}"
               "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>")
        self._send(207, out.encode("utf-8"), 'application/xml; charset="utf-8"')

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
