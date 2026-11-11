# WiFi DataDrive

Firmware für den **LILYGO T-Dongle-S3 (LCD-Variante)**, die den Dongle in einen
USB-Stick mit WLAN-Zugang verwandelt: Der Dongle steckt an einem USB-Gerät (PC, 3D-Drucker;
Ziel: Bambu Lab X2D) und erscheint dort als normales USB-Laufwerk. Dateien werden per
Browser über WLAN in einer eingebauten Dateiverwaltung hochgeladen, heruntergeladen,
umbenannt und gelöscht, ohne den Stick abzuziehen.

Das Projekt hieß bis Version 0.1.0 „WiFi PrintDrive“; der Ordner `firmware/printdrive`, der
HTTP-Header `X-PrintDrive` und das Code-Präfix `pd_` behalten den alten Namen.

Grundprinzip: Das FAT-Dateisystem – auf einer eingesteckten **microSD-Karte**, sonst im
internen 16-MB-Flash – hat zu jedem Zeitpunkt genau **einen** Besitzer – entweder das
USB-Gerät (Drucker/PC, Anzeige „USB-Laufwerk aktiv“) oder die Firmware (Weboberfläche,
Anzeige „Dateiverwaltung aktiv“). Umgeschaltet wird nur kontrolliert
(Details: [docs/02_architektur.md](docs/02_architektur.md)).

## Stand (2026-11-11, Firmware 0.2.0)

| Bereich | Stand |
|---|---|
| Hardware-Bestandsaufnahme (Chip, Flash, USB-ID) | **verifiziert** per esptool am echten Gerät |
| Smoke-Test (LCD, LED, Taste, Flash, Setup-AP), WLAN STA | **verifiziert** |
| USB-Laufwerk unter Windows 11 (Kopieren, Auswerfen, Re-Plug) | **verifiziert** |
| Flashen per 1200-Baud-Touch, Dateien überleben Updates | **verifiziert**, Update-Schleife vollautomatisch |
| Web-API v2: Upload aller Dateitypen, Umbenennen, Änderungsdatum, Moduswechsel, Fehlerfälle (`tools/api_test.py`) | **verifiziert** (microSD 28/28; API v1: Flash 18/18, microSD 21/21) |
| Umbenennung in WiFi DataDrive (USB-Namen, `datadrive.local`, Setup-WLAN, LCD-Texte) | **verifiziert** |
| Neue Weboberfläche (Dateiverwaltung, Einstellungen, hell/dunkel) | auf dem Gerät ausgeliefert (gzip, Seitenaufruf 241 ms); Browser-Test aller Zustände **in Arbeit** |
| LCD-Statusbild | **verifiziert** (Foto), außer Nuancen der LED-Farben |
| microSD (Phase 5) | **verifiziert** (Grundfunktion, 64-GB-Karte FAT32) |
| Nur-Lese-Modus des USB-Geräts | implementiert, **nicht getestet** |
| Ordner in der Weboberfläche, Passwortschutz | **nicht umgesetzt** (zurückgestellt) |
| Test am Bambu Lab X2D | offen, **Kompatibilität nicht verifiziert** |

Gemessene Schreibraten:

| Medium | USB schreiben | Web-Upload |
|---|---|---|
| interner Flash (FAT12, 4-KiB-Cluster) | 47 KB/s | 46 KB/s |
| microSD 64 GB (FAT32) | 832 KB/s | 1335–1578 KB/s |

Ohne Karte arbeitet der Dongle mit dem internen Flash (ca. 11 MB, eine 5-MB-Datei braucht
ca. 2 min). Empfohlen wird die microSD-Karte (FAT32, groß, schnell). Karte
nur im ausgesteckten Zustand wechseln. Ergebnisse in [docs/05_testprotokoll.md](docs/05_testprotokoll.md),
Befunde und offene Risiken in [docs/06_risiken.md](docs/06_risiken.md).

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

Ohne diese Datei startet der Dongle ein Einrichtungs-WLAN `DataDrive-XXXX`
(Passwort steht auf dem LCD), WLAN wird dann über die Weboberfläche gesetzt
(Einstellungen → Netzwerk).

**3. Hauptfirmware bauen und flashen**

```powershell
.\tools\flash.ps1 -Project printdrive -Build -Monitor
```

`flash.ps1` findet den Port selbst und schaltet einen laufenden `printdrive` per
1200-Baud-Touch in den Download-Modus. Klappt das nicht: BOOT-Taste gedrückt halten,
Dongle einstecken, loslassen, Befehl wiederholen – danach einmal aus- und einstecken,
sonst bleibt der Dongle im Download-Modus.

Danach genügt für jedes Software-Update `.\tools\flash.ps1 -Project printdrive -Build`
(1200-Baud-Touch → Flashen → Start, ohne Handgriff am Dongle). Die Weboberfläche
(`main/web/index.html`) wird dabei automatisch gzip-komprimiert eingebettet.

**4. Benutzen**

- Optional microSD-Karte einlegen (nur bei ausgestecktem Dongle). Ohne Karte wird der
  interne Flash genutzt. exFAT-Karten (meist > 32 GB) einmal im Web auf FAT32 formatieren
  (Einstellungen → Gefahrenbereich).
- Am PC/Drucker einstecken → Laufwerk erscheint (LCD: `USB`).
- Im Browser `http://datadrive.local` oder die IP vom LCD öffnen → „Meine Dateien“.
- Dateien hochladen („Hochladen“ oder Drag & Drop, beliebige Dateitypen): Ist das
  USB-Laufwerk aktiv, aktiviert die Weboberfläche zuerst die Dateiverwaltung (ggf. mit
  Rückfrage), speichert die Dateien und stellt das USB-Laufwerk danach wieder bereit.
- Von Hand umschalten: „Dateiverwaltung aktivieren“ bzw. „USB-Laufwerk bereitstellen“ in
  der Statusleiste, oder BOOT-Taste kurz drücken.

**5. API-Test (optional, verändert den Laufwerksinhalt)**

```powershell
python tools\api_test.py 192.168.2.152 --drive E:
```

**6. Weboberfläche ohne Hardware entwickeln (optional)**

```powershell
python tools\mock_server.py --port 8090 --scenario files    # dann http://127.0.0.1:8090/
```

Der Testserver bildet die Geräte-API nach und liest `index.html` bei jedem Aufruf neu
(Details: [docs/04_build_und_flash.md](docs/04_build_und_flash.md) Abschnitt 11).

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
  printdrive/                Hauptfirmware WiFi DataDrive (Ordnername historisch;
                             main/: drive, usb_dev, web_server, ui, web/index.html, web/gzip_file.py)
tools/
  idf.ps1                    idf.py mit geladener ESP-IDF-Umgebung
  flash.ps1                  Flashen mit automatischer Port-/Download-Modus-Erkennung
  monitor.ps1 / monitor.py   Log-Leser (TinyUSB-CDC oder USB-Serial/JTAG)
  backup_flash.ps1 / .py     vollständiges Flash-Backup nach backup/ (über USB-Serial/JTAG unzuverlässig)
  api_test.py                End-to-End-Test der Web-API gegen den echten Dongle (28 Prüfungen)
  mock_server.py             Testserver: Geräte-API ohne Hardware, zum Entwickeln/Testen der Weboberfläche
  ui_test.py                 Browser-Test der Weboberfläche gegen den Testserver (Playwright)
backup/                      LILYGO-Werksabbild, Flash-Abbilder (*.bin git-ignoriert) + SHA256SUMS.txt
```

## Dokumentation

| Datei | Inhalt |
|---|---|
| [docs/00_kern_usb_gadget.md](docs/00_kern_usb_gadget.md) | der Kern in Kurzform: Besitzer-Prinzip, Sicherungen, Eckdaten |
| [docs/01_bestandsaufnahme.md](docs/01_bestandsaufnahme.md) | Hardware, verifizierte Daten, Technologieentscheidung |
| [docs/02_architektur.md](docs/02_architektur.md) | Module, Besitzer-Zustandsautomat, Upload, Weboberfläche, LCD, USB |
| [docs/03_umsetzungsplan.md](docs/03_umsetzungsplan.md) | Phasen 0–6, Abnahmekriterien, Status, Backlog |
| [docs/04_build_und_flash.md](docs/04_build_und_flash.md) | ESP-IDF-Installation, Bauen, Flashen, Recovery, Konfiguration, Testserver |
| [docs/05_testprotokoll.md](docs/05_testprotokoll.md) | Testprozeduren mit erwarteten Ergebnissen |
| [docs/06_risiken.md](docs/06_risiken.md) | offene Risiken und Annahmen |
| [docs/07_api.md](docs/07_api.md) | HTTP-API Version 2 (verbindlicher Vertrag für Firmware, Weboberfläche, Testserver, API-Test) |

## Lizenzen von Fremdcode

- `firmware/components/st7735/` – ST7735-Treiber aus dem LILYGO-Repository, MIT (`LICENSE.LILYGO`).
- `firmware/components/display/font5x7.c` – Adafruit-GFX-Font, BSD (`LICENSE.Adafruit_GFX`).
