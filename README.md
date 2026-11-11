# WiFi PrintDrive

Firmware für den **LILYGO T-Dongle-S3 (LCD-Variante)**, die den Dongle in einen
USB-Stick mit WLAN-Zugang verwandelt: Der Dongle steckt am 3D-Drucker (Ziel: Bambu
Lab X2D) und erscheint dort als normales USB-Laufwerk. Neue `.3mf`-Dateien werden
per Browser über WLAN hochgeladen, ohne den Stick abzuziehen.

Grundprinzip: Das FAT-Dateisystem im internen 16-MB-Flash hat zu jedem Zeitpunkt
genau **einen** Besitzer – entweder den USB-Host (Drucker/PC) oder die Firmware
(Web-Upload). Umgeschaltet wird nur kontrolliert (Details: [docs/02_architektur.md](docs/02_architektur.md)).

## Stand (2026-11-11)

| Bereich | Stand |
|---|---|
| Hardware-Bestandsaufnahme (Chip, Flash, USB-ID) | **verifiziert** per esptool am echten Gerät |
| Smoke-Test (LCD, LED, Taste, Flash, Setup-AP) | **verifiziert**; WLAN STA noch offen |
| USB-Laufwerk unter Windows 11 (FAT16, 10,8 MB, Kopieren, Auswerfen, Re-Plug) | **verifiziert** |
| Flashen per 1200-Baud-Touch, Dateien überleben Updates | **verifiziert** |
| LCD-Statusbild | teilweise verifiziert (Zustand `USB`) |
| Web-Upload, Moduswechsel per Web, Fehlerfälle | implementiert, **nicht getestet** (WLAN-Einrichtung steht aus) |
| Test am Bambu Lab X2D | offen, **Kompatibilität nicht verifiziert** |
| microSD | nicht implementiert (optionale Phase 5) |

Gemessen über USB: schreiben 47 KB/s, lesen 753 KB/s. Schreiben ist durch das interne
Flash begrenzt (grob 90 KB/s theoretisch); eine 5-MB-`.3mf` braucht ca. 1,5–2 min, auch
per Web. Ergebnisse in [docs/05_testprotokoll.md](docs/05_testprotokoll.md), Befunde und
offene Risiken in [docs/06_risiken.md](docs/06_risiken.md).

## Schnellstart

Voraussetzung: ESP-IDF v5.5.6 unter `%USERPROFILE%\esp\esp-idf-v5.5.6`
(Installation siehe [docs/04_build_und_flash.md](docs/04_build_und_flash.md)).
Alle Befehle in PowerShell im Projektordner.

**0. Werks-Firmware** – Ein Vollbackup per `tools\backup_flash.ps1` war über
USB-Serial/JTAG unzuverlässig. Zum Wiederherstellen liegt das offizielle LILYGO-Abbild
`backup\T-Dongle-S3_factory_screen_251218.bin` bereit (SHA256 in `backup\SHA256SUMS.txt`,
Befehl in [docs/04_build_und_flash.md](docs/04_build_und_flash.md) Abschnitt 5).

**1. Smoke-Test bauen und flashen** (Werks-Firmware oder Smoke-Test läuft, Port COM4)

```powershell
.\tools\idf.ps1 smoke_test build
.\tools\idf.ps1 smoke_test -p COM4 flash monitor
```

Erwartet: Farbbalken rot/grün/blau/weiß auf dem LCD, im Log
`SMOKE: flash size 16777216 bytes`. Monitor beenden mit `Ctrl+]`.

**2. WLAN-Zugangsdaten (optional)**

```powershell
copy firmware\pd_secrets.h.example firmware\pd_secrets.h   # dann SSID/Passwort eintragen
```

Ohne diese Datei startet der Dongle einen Setup-Access-Point `PrintDrive-XXXX`
(Passwort steht auf dem LCD), WLAN wird dann über die Weboberfläche gesetzt.

**3. Hauptfirmware bauen und flashen**

```powershell
.\tools\flash.ps1 -Project printdrive -Build -Monitor
```

`flash.ps1` findet den Port selbst und schaltet einen laufenden `printdrive` per
1200-Baud-Touch in den Download-Modus. Klappt das nicht: BOOT-Taste gedrückt halten,
Dongle einstecken, loslassen, Befehl wiederholen – danach einmal aus- und einstecken,
sonst bleibt der Dongle im Download-Modus.

**4. Benutzen**

- Am PC/Drucker einstecken → Laufwerk `PRINTDRIVE` erscheint (LCD: `USB`).
- Im Browser `http://printdrive.local` oder die IP vom LCD öffnen.
- Datei hochladen: Die Weboberfläche holt sich das Laufwerk (ggf. mit Rückfrage),
  speichert die Datei und stellt es danach wieder am USB bereit.

## Ordnerstruktur

```
README.md
docs/                        Projektdokumentation (deutsch)
firmware/
  partitions.csv             gemeinsame Partitionstabelle (16 MB, OTA-fähig)
  pd_secrets.h.example       Vorlage für WLAN-Zugangsdaten (Kopie pd_secrets.h ist git-ignoriert)
  components/
    board/                   Pinbelegung, APA102-LED, BOOT-Taste, Download-Modus
    st7735/                  ST7735-Paneltreiber (von LILYGO, MIT)
    display/                 Framebuffer, 5x7-Font (Adafruit, BSD), Text/Rechtecke
    pd_wifi/                 WLAN STA/AP, mDNS
    applog/                  Log-Ringpuffer (Web/CDC)
  smoke_test/                Phase-0-Hardwaretest
  printdrive/                Hauptfirmware (main/: drive, usb_dev, web_server, ui, web/index.html)
tools/
  idf.ps1                    idf.py mit geladener ESP-IDF-Umgebung
  flash.ps1                  Flashen mit automatischer Port-/Download-Modus-Erkennung
  monitor.ps1 / monitor.py   Log-Leser (TinyUSB-CDC oder USB-Serial/JTAG)
  backup_flash.ps1 / .py     vollständiges Flash-Backup nach backup/ (über USB-Serial/JTAG unzuverlässig)
backup/                      LILYGO-Werksabbild, Flash-Abbilder (*.bin git-ignoriert) + SHA256SUMS.txt
```

## Dokumentation

| Datei | Inhalt |
|---|---|
| [docs/01_bestandsaufnahme.md](docs/01_bestandsaufnahme.md) | Hardware, verifizierte Daten, Technologieentscheidung |
| [docs/02_architektur.md](docs/02_architektur.md) | Module, Besitzer-Zustandsautomat, Upload, HTTP-API, LCD, USB |
| [docs/03_umsetzungsplan.md](docs/03_umsetzungsplan.md) | Phasen 0–5, Abnahmekriterien, Status, Backlog |
| [docs/04_build_und_flash.md](docs/04_build_und_flash.md) | ESP-IDF-Installation, Bauen, Flashen, Recovery, Konfiguration |
| [docs/05_testprotokoll.md](docs/05_testprotokoll.md) | Testprozeduren mit erwarteten Ergebnissen |
| [docs/06_risiken.md](docs/06_risiken.md) | offene Risiken und Annahmen |

## Lizenzen von Fremdcode

- `firmware/components/st7735/` – ST7735-Treiber aus dem LILYGO-Repository, MIT (`LICENSE.LILYGO`).
- `firmware/components/display/font5x7.c` – Adafruit-GFX-Font, BSD (`LICENSE.Adafruit_GFX`).
