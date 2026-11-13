# WiFi DataDrive

*Deutsche Fassung: [README.md](README.md). The detailed documentation in `docs/` is German.*

Firmware for the **LILYGO T-Dongle-S3 (LCD variant)** that turns the dongle into a USB stick
with Wi-Fi access. The dongle plugs into a USB host (PC, 3D printer; target printer: Bambu Lab
X2D) and shows up as a normal USB drive. Files are uploaded, downloaded, renamed, moved and
deleted from a browser over Wi-Fi in a built-in file manager, without unplugging the stick.

Features: user login with roles, folders, several saved Wi-Fi networks, optional drive
encryption (AES-256-XTS), WebDAV network drive, NTP clock, resumable uploads, folder ZIP
download, optional HTTPS, German and English web UI, firmware update from the web UI, status
animation on the LCD.

**Core rule:** the FAT file system (on a microSD card, otherwise in the internal 16 MB flash) has
exactly **one** owner at any time: either the USB host ("USB drive active") or the firmware ("file
management active"). Switching is controlled ([docs/02_architektur.md](docs/02_architektur.md)).

## Hardware

- **LILYGO T-Dongle-S3 with LCD** (ESP32-S3 rev v0.2, 16 MB flash, no PSRAM, ST7735 LCD, microSD
  slot, one button). Built and tested only with this variant.
- Other LILYGO models (T-Dongle-S3 Plus, Dual) are **not tested**.
- microSD card recommended (FAT32). Without a card, the internal flash is used (about 11 MB,
  slow: about 46 KB/s for writes).

## Status (firmware 0.5.0)

**verified** = checked on the real device, result recorded in
[docs/05_testprotokoll.md](docs/05_testprotokoll.md). **untested** = built or present in the
source, but not tested on the device or not in this form.

| Area | Status |
|---|---|
| USB drive on Windows 11 (copy, eject, re-plug) | **verified** |
| microSD (64 GB, FAT32) | **verified** (basic function; only one card tested) |
| Login with users and roles, default account with forced password change, lockout (0.3.0) | **verified**: API test 182/182 on the device |
| Folders, rename, move, copy, delete (0.3.0) | **verified** (API test) |
| Several Wi-Fi networks (0.3.0) | **verified** (API test) |
| Drive encryption, AES-256-XTS (0.4.0) | **verified**: API run 334/334 on the device incl. destructive encryption test |
| API test version 5 on the device (0.5.0) | **verified**: 354/354, 6 checks skipped |
| NTP clock with time zone (0.5.0) | **verified** |
| Resumable upload, folder as ZIP (0.5.0) | **verified** by the API test |
| HTTPS (0.5.0, switch in the settings, off by default) | **verified** on the device: 30 MB upload, download, file list. **Untested:** iPhone with installed certificate, WebDAV over HTTPS, long-term operation |
| WebDAV network drive (0.5.0) | on the device only through the API test; with real clients (Windows Explorer, Finder, phone apps) **untested** |
| Automatic lock of the encrypted drive (0.5.0) | settings **verified**; the lock itself after the timeout **untested** |
| Captive portal (setup page opens by itself, 0.5.0) | **untested** with a real phone |
| Firmware update through the web UI | **untested** (all versions so far flashed over USB) |
| Button: click for QR code, double click to switch mode, maintenance mode, user reset | **untested** as a separate test |
| Web UI browser test for 0.5.0 (`tools/ui_test.py`) | not recorded in the test protocol (T9.15) |
| Web UI on a real phone (iOS Safari), real drag and drop | **untested** (browser tests run against the mock server with Chromium) |
| Read-only mode of the USB drive | implemented, **untested** |
| Bambu Lab X2D (unencrypted, versions 0.2.0 and 0.3.0) | **basic compatibility** reported by the owner: drive detected, uploaded `.gcode.3mf` shown and printed (microSD 64 GB FAT32). No measurements. Open: long prints, read-only mode, take-over during a print, power draw, internal flash, other printers |
| Printing from an encrypted, unlocked stick on the X2D | **untested** |
| Encryption on the internal flash; power loss during encryption setup | **untested** |
| Browser flasher (`web-flasher/`) | **untested** with a real device |

Measured rates (microSD 64 GB): USB write 832 KB/s, web upload 1335 to 1578 KB/s; encrypted:
USB write 820 KB/s, USB read 659 KB/s, web upload 1.0 MB/s. Internal flash: USB write 47 KB/s,
web upload 46 KB/s.

## Quick start

### Flash in the browser

Open the web flasher (GitHub Pages of this repository: `https://<OWNER>.github.io/<REPO>/`) in
Chrome or Edge, hold the dongle's button while plugging it in, click "Install". Instructions are
on the page. Flashing the full image resets users, Wi-Fi and settings; files on the microSD card
are not touched.

### Build it yourself

Requires **ESP-IDF v5.5.6**.

```sh
idf.py -C firmware/printdrive build
idf.py -C firmware/printdrive -p PORT flash
```

On Windows the scripts in `tools/` load ESP-IDF themselves:
`.\tools\flash.ps1 -Project printdrive -Build -Monitor`. Details:
[docs/04_build_und_flash.md](docs/04_build_und_flash.md). If the dongle does not enter download
mode by itself, hold the button while plugging it in; unplug and replug after flashing.

### First start

1. Without a saved Wi-Fi network the dongle opens the setup network `DataDrive-XXXX`; the password
   is shown on the LCD. Open `http://192.168.4.1`.
2. Sign in with **`admin` / `admin`**. A new password is required right away.
3. Add your Wi-Fi under Settings → Network. Afterwards the stick is reachable at
   `http://datadrive.local` or at the IP on the LCD.
4. Optional: copy `firmware/pd_secrets.h.example` to `firmware/pd_secrets.h` to build in default
   Wi-Fi credentials (git-ignored; never publish binaries built with it).

### Develop without hardware

```sh
python -m pip install -r requirements-dev.txt
python tools/mock_server.py --port 8090          # then open http://127.0.0.1:8090/ (admin / admin123)
python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123
```

## Security notes

- Meant for **trusted networks**. By default the stick speaks plain HTTP: passwords (login, drive
  password, WebDAV) travel unencrypted over Wi-Fi. HTTPS can be switched on in the settings (own
  device CA; browsers warn until it is installed).
- **Drive encryption** protects the files on a lost or stolen card or stick, as long as the drive
  is locked and the password is strong. It does not protect against offline guessing of weak
  passwords, against an `admin` who installs modified firmware (updates are not signed as of
  0.5.0), and it does not detect tampering. While unlocked, the printer sees plain files.
- Wi-Fi credentials are stored unencrypted in the flash (no flash encryption, no secure boot).
- Holding the button for 10 seconds resets the users to `admin` / `admin` (the drive password is
  not affected).

Details: [.github/SECURITY.md](.github/SECURITY.md) (how to report vulnerabilities),
[docs/12_api_v4.md](docs/12_api_v4.md) section 2.5, [docs/06_risiken.md](docs/06_risiken.md).

## Documentation (German)

| File | Content |
|---|---|
| [README.md](README.md) | full German README: features, quick start, encryption, WebDAV, HTTPS |
| [docs/00_kern_usb_gadget.md](docs/00_kern_usb_gadget.md) | the core in short: owner principle, safeguards |
| [docs/02_architektur.md](docs/02_architektur.md) | architecture |
| [docs/04_build_und_flash.md](docs/04_build_und_flash.md) | install ESP-IDF, build, flash, recover, tests, mock server |
| [docs/05_testprotokoll.md](docs/05_testprotokoll.md) | test procedures and results per version |
| [docs/06_risiken.md](docs/06_risiken.md) | open risks, untested points, assumptions |
| [docs/09_api_v3.md](docs/09_api_v3.md), [docs/12_api_v4.md](docs/12_api_v4.md), [docs/15_api_v5.md](docs/15_api_v5.md) | HTTP API versions 3 to 5 |
| [docs/18_veroeffentlichung.md](docs/18_veroeffentlichung.md) | publishing checklist |

## Contributing and licence

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). The project is licensed under the
[MIT License](LICENSE). Third-party code and its licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
