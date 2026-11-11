# 04 – Bauen, Flashen, Wiederherstellen

Alle Befehle in **PowerShell**, Arbeitsverzeichnis = Projektordner
(der Ordner mit dieser Anleitung). Pfade mit Leerzeichen immer in Anführungszeichen.

## 1. ESP-IDF installieren (so durchgeführt)

Voraussetzungen: Git und Python 3 im `PATH`.

```powershell
mkdir $env:USERPROFILE\esp -Force
git clone -b v5.5.6 --recursive --depth 1 --shallow-submodules `
    https://github.com/espressif/esp-idf.git $env:USERPROFILE\esp\esp-idf-v5.5.6
cd $env:USERPROFILE\esp\esp-idf-v5.5.6
.\install.ps1 esp32s3
```

Die Umgebung muss nicht manuell geladen werden: Alle Skripte unter `tools\` rufen
`export.ps1` selbst auf. Anderer Installationsort: `$env:PD_IDF_PATH = "D:\pfad\esp-idf"`.

Beim ersten Build lädt der Component Manager `esp_tinyusb`, `tinyusb` und `mdns` in
`firmware\<projekt>\managed_components\` (Internet nötig; Versionen fest in `dependencies.lock`).

## 2. Werkzeuge im Überblick

| Skript | Zweck |
|---|---|
| `tools\idf.ps1 <smoke_test\|printdrive> <idf.py-Argumente>` | beliebiger `idf.py`-Aufruf im Projektordner (`build`, `flash`, `monitor`, `menuconfig`, `fullclean`, …) |
| `tools\flash.ps1 -Project <…> [-Port COMx] [-Build] [-Monitor]` | Flashen mit automatischer Port-Suche und Download-Modus per 1200-Baud-Touch |
| `tools\monitor.ps1 [COMx] [--seconds N] [--out datei]` | Log-Leser (`monitor.py`), verbindet nach Neustart automatisch neu, setzt den Chip nie zurück |
| `tools\backup_flash.ps1 -Port COM4` | komplettes 16-MB-Flash nach `backup\` lesen |

## 3. Smoke-Test bauen und flashen

Der Smoke-Test nutzt die eingebaute USB-Serial/JTAG-Schnittstelle (VID 303A, PID 1001,
bisher **COM4**). Darüber funktioniert der automatische Reset in den Download-Modus.

```powershell
.\tools\idf.ps1 smoke_test build
.\tools\idf.ps1 smoke_test -p COM4 flash monitor     # Monitor beenden: Ctrl+]
```

## 4. Hauptfirmware bauen und flashen

```powershell
.\tools\idf.ps1 printdrive build
.\tools\flash.ps1 -Project printdrive            # oder in einem Schritt: -Build -Monitor
```

**Wichtig:** Sobald `printdrive` läuft, belegt TinyUSB den USB-Anschluss. Die
USB-Serial/JTAG-Schnittstelle (COM4) ist dann weg; stattdessen erscheint ein
CDC-Port „PrintDrive Log“ (303A:4003) und das Laufwerk. Wege in den Download-Modus:

| Weg | Ablauf |
|---|---|
| `tools\flash.ps1` (automatisch) | findet den CDC-Port, öffnet ihn mit 1200 Baud und lässt DTR fallen → Neustart in den Download-Modus → wartet bis 15 s auf 303A:1001 → `esptool write_flash @flash_args` |
| Weboberfläche | Einstellungen → „Flash-Modus“ (`POST /api/reboot?mode=download`) |
| BOOT-Taste | im Betrieb **5 s halten** (LCD zeigt `FLASH`) |
| manuell (immer möglich) | BOOT gedrückt halten, Dongle einstecken, loslassen |

Danach erscheint 303A:1001 als COM-Port (Nummer kann von COM4 abweichen). `flash.ps1`
wartet im manuellen Fall bis zu 60 s darauf. Alternativ von Hand:
`.\tools\idf.ps1 printdrive -p COMx flash`.

Mit deaktiviertem CDC (`CONFIG_TINYUSB_CDC_ENABLED=n`, z. B. für den Drucker-Test) gibt
es keinen 1200-Baud-Touch mehr – dann Web, BOOT 5 s oder manuell.

Flashen schreibt Bootloader, Partitionstabelle, `otadata` und App. **NVS und Dateispeicher
bleiben erhalten.** `idf.py erase-flash` löscht dagegen alles inklusive Dateien.

Logs der Hauptfirmware: `.\tools\monitor.ps1` (CDC-Port), `http://<ip>/api/log` oder
UART0 an GPIO43/44 (115200 Baud).

## 5. Recovery

- **Aus- und Einstecken** startet immer normal (der erzwungene Download-Modus gilt nur
  für einen Neustart). Ein „hängender“ Download-Modus wird so verlassen.
- Firmware startet nicht/Boot-Schleife: manuell in den Download-Modus (BOOT halten +
  einstecken) und neu flashen.
- Werkszustand wiederherstellen (überschreibt das **gesamte** Flash inkl. Dateien):

```powershell
& "$env:USERPROFILE\esp\esp-idf-v5.5.6\export.ps1"
python -m esptool --chip esp32s3 -p COM4 write_flash 0x0 backup\flash_backup_<datum>.bin
```

Statt des eigenen Backups kann auch eine Werks-Firmware aus dem LILYGO-Repository an
Adresse 0x0 geschrieben werden (genaue Datei: offen, im Repo prüfen).

## 6. Flash-Backup

```powershell
.\tools\backup_flash.ps1 -Port COM4
```

Funktioniert nur, solange USB-Serial/JTAG aktiv ist (Werks-Firmware, Smoke-Test oder
Download-Modus). Ergebnis: `backup\flash_backup_<yyyyMMdd-HHmmss>.bin`, SHA256 wird an
`backup\SHA256SUMS.txt` angehängt (die `.bin` sind git-ignoriert, die Prüfsummenliste nicht).

Hintergrund: Ein einzelnes großes `esptool read_flash` über die USB-Serial/JTAG des
ESP32-S3 bricht reproduzierbar ab („Corrupt data, expected 0x1000 bytes“). Vermutete
Ursache: FIFO-Überlauf, weil SLIP-Escape-Bytes (0xC0/0xDB) Pakete auf doppelte Größe
aufblähen. `backup_flash.py` umgeht das:

- liest in 256-KiB-Blöcken mit kleinen Paketen (1024 B, max. 2 unbestätigte Pakete),
- prüft jeden Block per MD5 des esptool-Stubs gegen das Flash,
- wiederholt fehlerhafte Blöcke bis zu 6-mal, notfalls mit neuer Verbindung.

## 7. WLAN einrichten

**Variante A – fest einkompiliert** (Standardwerte, solange im NVS nichts steht):

```powershell
copy firmware\pd_secrets.h.example firmware\pd_secrets.h    # SSID/Passwort eintragen
.\tools\idf.ps1 printdrive fullclean                        # nötig, damit die neue Datei erkannt wird
.\tools\idf.ps1 printdrive build
```

`firmware\pd_secrets.h` ist git-ignoriert. Gilt für Smoke-Test und Hauptfirmware.

**Variante B – über den Setup-AP:** Ohne Zugangsdaten startet sofort (sonst nach 20 s
ohne Verbindung) das WLAN `PrintDrive-XXXX`; Passwort auf dem LCD (`PW …`). Verbinden,
`http://192.168.4.1` öffnen, unter Einstellungen SSID/Passwort speichern. Die Daten
landen im NVS und haben ab dann Vorrang vor `pd_secrets.h`.

## 8. Konfiguration (Kconfig)

Menü „PrintDrive“ (`firmware/printdrive/main/Kconfig.projbuild`):

| Option | Default | Wirkung |
|---|---|---|
| `CONFIG_PD_HOST_WRITABLE_DEFAULT` | y | Host darf schreiben (Startwert; per Web änderbar, in NVS gespeichert) |
| `CONFIG_PD_BOOT_WAIT_HOST_MS` | 3000 (0–20000) | Wartezeit beim Start auf einen USB-Host |
| `CONFIG_PD_PROVIDE_ON_ATTACH` | y | bei Host-Anschluss automatisch bereitstellen |
| `CONFIG_PD_EJECT_TO_APP` | y | nach Auswurf am Host automatisch Upload-Modus |
| `CONFIG_PD_REPLUG_ON_PROVIDE` | y | beim Bereitstellen immer Soft-Re-Plug |
| `CONFIG_PD_HOST_WRITE_GUARD_MS` | 3000 (500–60000) | Sperrzeit nach dem letzten Host-Schreibzugriff |
| `CONFIG_PD_ALLOW_GCODE` | n | zusätzlich `.gcode` erlauben |

Menü „PrintDrive Wi-Fi“ (`firmware/components/pd_wifi/Kconfig`):

| Option | Default | Wirkung |
|---|---|---|
| `CONFIG_PD_WIFI_HOSTNAME` | `printdrive` | DHCP- und mDNS-Name |
| `CONFIG_PD_WIFI_AP_FALLBACK_S` | 20 (5–600) | Setup-AP nach so vielen Sekunden ohne Verbindung |
| `CONFIG_PD_WIFI_AP_LINGER_S` | 120 (10–3600) | Setup-AP bleibt nach STA-Verbindung so lange an |

Weitere relevante Optionen in `firmware/printdrive/sdkconfig.defaults`: TinyUSB
(`CONFIG_TINYUSB_CDC_ENABLED`, USB-Strings, MSC-Puffer), Wear Levelling
(`CONFIG_WL_SECTOR_SIZE_512`, `CONFIG_WL_SECTOR_MODE_SAFE`), FatFs, HTTP, lwIP.

Ändern:

- **interaktiv:** `.\tools\idf.ps1 printdrive menuconfig` → schreibt `sdkconfig` (lokal, git-ignoriert);
- **dauerhaft fürs Projekt:** Zeile in `sdkconfig.defaults` eintragen, dann
  `firmware\printdrive\sdkconfig` löschen und neu bauen. Solange `sdkconfig` existiert,
  werden geänderte Defaults **nicht** übernommen.

**Achtung:** Änderungen an WL-Sektorgröße/-Modus oder an `partitions.csv` machen den
vorhandenen Dateispeicher unlesbar. Da der Erststart schon vorbei ist, formatiert die
Firmware nicht automatisch: LCD zeigt „Kein Dateisystem!“, Formatieren über die
Weboberfläche. Dateien vorher sichern.
