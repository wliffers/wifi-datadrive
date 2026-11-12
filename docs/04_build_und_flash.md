# 04 – Bauen, Flashen, Wiederherstellen

Alle Befehle in **PowerShell**, Arbeitsverzeichnis = Projektordner
(der Ordner mit dieser Anleitung). Pfade mit Leerzeichen immer in Anführungszeichen.

Das Produkt heißt seit Firmware 0.2.0 „WiFi DataDrive“ (vorher „WiFi PrintDrive“). Der
Projektordner `firmware\printdrive` und damit der Projektname `printdrive` in allen
Befehlen bleiben unverändert.

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
| `tools\backup_flash.ps1 -Port COM4` | komplettes 16-MB-Flash nach `backup\` lesen – über USB-Serial/JTAG **unzuverlässig** (Abschnitt 6) |
| `python tools\api_test.py <ip> [--drive E:] [--size-kb N]` | End-to-End-Test der Web-API gegen den echten Dongle (Abschnitt 10); **verändert den Laufwerksinhalt** |
| `python tools\mock_server.py [--port N] [--scenario NAME]` | Testserver: bildet die Geräte-API ohne Hardware nach, zum Entwickeln und Testen der Weboberfläche (Abschnitt 11) |
| `tools\ui_test.py` | Browser-Test der Weboberfläche gegen den Testserver (Playwright, `pip install playwright`); 566 Prüfungen, ca. 19 Minuten |

## 3. Smoke-Test bauen und flashen

Der Smoke-Test nutzt die eingebaute USB-Serial/JTAG-Schnittstelle (VID 303A, PID 1001,
bisher **COM4**). Darüber funktioniert der automatische Reset in den Download-Modus.
Erwartetes Bild und Log: [05_testprotokoll.md](05_testprotokoll.md) Phase 0 (bestanden 2026-11-11).

```powershell
.\tools\idf.ps1 smoke_test build
.\tools\idf.ps1 smoke_test -p COM4 flash monitor     # Monitor beenden: Ctrl+]
```

## 4. Hauptfirmware bauen und flashen

```powershell
.\tools\idf.ps1 printdrive build
.\tools\flash.ps1 -Project printdrive            # oder in einem Schritt: -Build -Monitor
```

Übliche Update-Schleife während der Entwicklung (bauen → 1200-Baud-Touch → flashen →
Start, ohne Handgriff am Dongle; mehrfach verifiziert 2026-11-11):

```powershell
.\tools\flash.ps1 -Project printdrive -Build
```

Die Weboberfläche wird beim Bauen **automatisch** komprimiert: `main/CMakeLists.txt` ruft
`main/web/gzip_file.py` auf (gzip Stufe 9, ohne Zeitstempel, also reproduzierbar), erzeugt
`index.html.gz` im Build-Ordner und bettet sie ein. Der Schritt läuft nur, wenn sich
`index.html` geändert hat; ein eigener Befehl ist nicht nötig. Gemessen: 60 KB roh → 19 KB
im Flash, App-Binary 1,06 MB (60 % der App-Partition frei).

**Wichtig:** Sobald `printdrive` läuft, belegt TinyUSB den USB-Anschluss. Die
USB-Serial/JTAG-Schnittstelle (COM4) ist dann weg; stattdessen erscheint ein
CDC-Port „DataDrive Log“ (303A:4003, unter Windows „Serielles USB-Gerät“, bisher **COM5**)
und das Laufwerk. Wege in den Download-Modus:

| Weg | Ablauf |
|---|---|
| `tools\flash.ps1` (automatisch) | findet den CDC-Port, öffnet ihn mit 1200 Baud und lässt DTR fallen → Neustart in den Download-Modus → wartet bis 15 s auf 303A:1001 → `esptool write_flash @flash_args` mit `--after hard_reset`. **Verifiziert** 2026-11-11: COM5 → COM4 nach 3,4 s, App startet nach dem Flashen von selbst |
| Weboberfläche | Einstellungen → Gefahrenbereich → „Wartungsmodus“ (`POST /api/reboot?mode=download`) |
| BOOT-Taste | im Betrieb **5 s halten** (LCD zeigt `FLASH`) |
| manuell (immer möglich) | BOOT gedrückt halten, Dongle einstecken, loslassen |

Danach erscheint 303A:1001 als COM-Port (Nummer kann von COM4 abweichen). `flash.ps1`
wartet im manuellen Fall bis zu 60 s darauf. Alternativ von Hand:
`.\tools\idf.ps1 printdrive -p COMx flash`.

Die drei Software-Wege rufen `board_restart_to_download_mode()` auf. Die Funktion gibt den
internen USB-PHY zuerst an USB-Serial/JTAG zurück (`RTC_CNTL_USB_CONF_REG`,
`USB_SERIAL_JTAG_CONF0_REG`, wie `usb_switch_to_cdc_jtag()` im Arduino-Core), setzt dann
`RTC_CNTL_FORCE_DOWNLOAD_BOOT` und startet neu. Hintergrund: TinyUSB schaltet den PHY über
`RTC_CNTL_USB_CONF` auf den OTG-Controller; dieses RTC-Register übersteht einen
Software-Reset. Ohne die Rückgabe erschien nach dem Neustart **gar kein** USB-Gerät
(verifiziert). Falls das je wieder passiert: aus- und einstecken startet normal.

**Nach dem Flashen per BOOT-Taste einmal aus- und einstecken.** Wurde der Download-Modus
durch BOOT + Einstecken erzwungen, bleibt der Chip bei jedem weiteren Reset über
USB-Serial/JTAG (esptool `hard_reset`, RTS) im Download-Modus – ROM meldet
`boot:0x0 (DOWNLOAD(USB/UART0))`, `GPIO_STRAP_REG` = 0 –, obwohl die Taste losgelassen ist
(`GPIO_IN` Bit 0 = 1). Erst ein echtes Aus-/Einstecken übernimmt die Strapping-Pins neu.
Beim 1200-Baud-Touch tritt das nicht auf.

Mit deaktiviertem CDC (`CONFIG_TINYUSB_CDC_ENABLED=n`, z. B. für den Drucker-Test) gibt
es keinen 1200-Baud-Touch mehr – dann Web, BOOT 5 s oder manuell.

Flashen schreibt Bootloader, Partitionstabelle, `otadata` und App. **NVS und Dateispeicher
bleiben erhalten** (verifiziert 2026-11-11), eine microSD-Karte wird ohnehin nicht berührt.
`idf.py erase-flash` löscht dagegen alles im internen Flash inklusive Dateien.

Logs der Hauptfirmware: `.\tools\monitor.ps1` (CDC-Port; beim Öffnen wird zuerst das Start-Log
aus dem 8-KB-Ringpuffer nachgeliefert, verifiziert), `http://<ip>/api/log` oder
UART0 an GPIO43/44 (115200 Baud).

## 5. Recovery

- **Aus- und Einstecken** startet immer normal (der erzwungene Download-Modus gilt nur
  für einen Neustart). Ein „hängender“ Download-Modus wird so verlassen – auch der nach
  BOOT + Einstecken (Abschnitt 4) und ein Download-Modus ohne USB-Gerät.
- Firmware startet nicht/Boot-Schleife: manuell in den Download-Modus (BOOT halten +
  einstecken) und neu flashen.
- Werkszustand wiederherstellen (überschreibt das **gesamte** Flash inkl. Dateien). Ein
  eigenes Vollbackup gibt es nicht (Abschnitt 6); Rückfall ist das offizielle
  LILYGO-Werksabbild `backup\T-Dongle-S3_factory_screen_251218.bin` aus dem Ordner
  `firmware/` des Hersteller-Repos, SHA256
  `65a9b51487ca721f618a0f22bb4d800f0dedfcb40c29286f55ad85eba81c68cd` (auch in
  `backup\SHA256SUMS.txt`):

```powershell
& "$env:USERPROFILE\esp\esp-idf-v5.5.6\export.ps1"
python -m esptool --chip esp32s3 -p COM4 write_flash 0x0 backup\T-Dongle-S3_factory_screen_251218.bin
```

- Dateispeicher während der Entwicklung komplett zurücksetzen (im Download-Modus):

```powershell
python -m esptool --chip esp32s3 -p COM4 erase_region 0x9000 0x5000       # NVS: WLAN-Daten, Flag "formatiert"
python -m esptool --chip esp32s3 -p COM4 erase_region 0x510000 0xAF0000   # storage, ca. 44 s
```

  Der nächste Start ohne microSD-Karte formatiert automatisch (Erststart). Das Löschen des
  NVS entfernt auch die WLAN-Zugangsdaten; das Setup-AP-Passwort wird neu erzeugt.

- Interner Flash im Betrieb neu formatieren: Karte entfernen (Dongle ausgesteckt), dann
  Weboberfläche → Einstellungen → Gefahrenbereich → „Formatieren …“, zur Bestätigung
  `LÖSCHEN` eintippen (`POST /api/format`, Neustart; Formatieren per API verifiziert). Eine
  Karte wird auf dieselbe Weise formatiert, wenn sie beim Start steckt. Das neue Volume
  heißt `DATADRIVE`.

## 6. Flash-Backup (unzuverlässig über USB-Serial/JTAG)

```powershell
.\tools\backup_flash.ps1 -Port COM4
```

Funktioniert nur, solange USB-Serial/JTAG aktiv ist (Werks-Firmware, Smoke-Test oder
Download-Modus). Ergebnis: `backup\flash_backup_<yyyyMMdd-HHmmss>.bin`, SHA256 wird an
`backup\SHA256SUMS.txt` angehängt (die `.bin` sind git-ignoriert, die Prüfsummenliste nicht).

**Befund 2026-11-11:** Ein vollständiges 16-MB-Backup war an diesem PC nicht zuverlässig
möglich – datenabhängiger Byteverlust in `esptool read_flash`, auch mit kleinen Paketen nur
ca. 11 KB/s, einzelne Blöcke scheiterten wiederholt. Das Skript bleibt im Projekt (an
anderen PCs ggf. brauchbar), Rückfall für die Wiederherstellung ist das Werksabbild
(Abschnitt 5).

Hintergrund: Ein einzelnes großes `esptool read_flash` über die USB-Serial/JTAG des
ESP32-S3 bricht reproduzierbar ab („Corrupt data, expected 0x1000 bytes“). Vermutete
Ursache: FIFO-Überlauf, weil SLIP-Escape-Bytes (0xC0/0xDB) Pakete auf doppelte Größe
aufblähen. `backup_flash.py` versucht das zu umgehen:

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
ohne Verbindung) das WLAN `DataDrive-XXXX`; Passwort auf dem LCD (`PW …`). Verbinden,
`http://192.168.4.1` öffnen, unter Einstellungen → Netzwerk WLAN-Name und Passwort speichern. Die Daten
landen im NVS und haben ab dann Vorrang vor `pd_secrets.h`. **Verifiziert** 2026-11-11 per
Handy (Dongle danach im Heimnetz unter `192.168.2.152`; damals mit der alten Weboberfläche
und dem Namen `PrintDrive-54D5`).

## 8. Konfiguration (Kconfig)

Menü „PrintDrive“ (`firmware/printdrive/main/Kconfig.projbuild`; der Menüname ist noch der alte):

| Option | Default | Wirkung |
|---|---|---|
| `CONFIG_PD_HOST_WRITABLE_DEFAULT` | y | Host darf schreiben (Startwert; per Web änderbar, in NVS gespeichert) |
| `CONFIG_PD_BOOT_WAIT_HOST_MS` | 3000 (0–20000) | Wartezeit beim Start auf einen USB-Host |
| `CONFIG_PD_PROVIDE_ON_ATTACH` | y | bei Host-Anschluss automatisch bereitstellen |
| `CONFIG_PD_EJECT_TO_APP` | y | nach Auswurf am Host automatisch Upload-Modus |
| `CONFIG_PD_REPLUG_ON_PROVIDE` | y | beim Bereitstellen immer Soft-Re-Plug |
| `CONFIG_PD_HOST_WRITE_GUARD_MS` | 3000 (500–60000) | Sperrzeit nach dem letzten Host-Schreibzugriff |
| `CONFIG_PD_USE_SDCARD` | y | microSD-Karte als Speicher nutzen, wenn beim Start eine steckt (sonst interner Flash); Karte wird nie automatisch formatiert |
| `CONFIG_PD_FAT_CLUSTER_KB` | 4 (2–4) | Clustergröße beim Formatieren des internen Flashs: 4 = FAT12, Web-Upload ca. 46 KB/s; 2 = FAT16, Web-Upload nur ca. 7 KB/s. Wirkt erst beim Neuformatieren |
| `CONFIG_PD_UPLOAD_ALL_TYPES` | y | Uploads jedes Dateityps annehmen (`/api/status` meldet `types: "all"`). Aus: nur `.3mf` (und optional `.gcode`). Die Inhaltsprüfung für `.3mf` (vollständiges ZIP) gilt in beiden Fällen |
| `CONFIG_PD_ALLOW_GCODE` | n | zusätzlich `.gcode` erlauben; nur wählbar, wenn `CONFIG_PD_UPLOAD_ALL_TYPES=n` |

Menü „DataDrive Wi-Fi“ (`firmware/components/pd_wifi/Kconfig`):

| Option | Default | Wirkung |
|---|---|---|
| `CONFIG_PD_WIFI_HOSTNAME` | `datadrive` | DHCP- und mDNS-Name (`http://datadrive.local`) |
| `CONFIG_PD_WIFI_AP_FALLBACK_S` | 20 (5–600) | Setup-AP nach so vielen Sekunden ohne Verbindung |
| `CONFIG_PD_WIFI_AP_LINGER_S` | 120 (10–3600) | Setup-AP bleibt nach STA-Verbindung so lange an |

Weitere relevante Optionen in `firmware/printdrive/sdkconfig.defaults`: TinyUSB
(`CONFIG_TINYUSB_CDC_ENABLED`, USB-Strings „WiFi DataDrive“/„DataDrive“, MSC-Puffer), Wear Levelling
(`CONFIG_WL_SECTOR_SIZE_512`, `CONFIG_WL_SECTOR_MODE_SAFE`), FatFs, HTTP, lwIP.

Ändern:

- **interaktiv:** `.\tools\idf.ps1 printdrive menuconfig` → schreibt `sdkconfig` (lokal, git-ignoriert);
- **dauerhaft fürs Projekt:** Zeile in `sdkconfig.defaults` eintragen, dann
  `firmware\printdrive\sdkconfig` löschen und neu bauen. Solange `sdkconfig` existiert,
  werden geänderte Defaults **nicht** übernommen. Das gilt auch für die Umbenennung: Ein
  `sdkconfig` aus der Zeit vor 0.2.0 behält die alten USB-Strings und den Hostnamen
  `printdrive`, bis es gelöscht wird.

**Achtung:** Änderungen an WL-Sektorgröße/-Modus oder an `partitions.csv` machen den
vorhandenen Dateispeicher unlesbar. Da der Erststart schon vorbei ist, formatiert die
Firmware nicht automatisch: LCD zeigt „Kein Dateisystem!“, Formatieren über die
Weboberfläche. Dateien vorher sichern.

Ein Dateisystem, das eine Firmware vor dem FAT-Ausrichtungs-Fix (2026-11-11) erzeugt hat,
bleibt langsam (ca. 12 statt 47 KB/s schreiben), bis es neu formatiert wird (Web oder
Abschnitt 5). Gleiches gilt für ein FAT16 mit 2-KiB-Clustern aus älteren Firmware-Ständen
(Web-Upload ca. 7 statt 46 KB/s). Details: [06_risiken.md](06_risiken.md) Abschnitt 4.

## 9. Diagnose-Build (Konsole über USB-Serial/JTAG)

`firmware\printdrive\sdkconfig.diag` legt die Konsole der Hauptfirmware auf USB-Serial/JTAG
statt UART0 (inkl. Bootloader-Log auf INFO). So sind frühe Startmeldungen und Abstürze vor
dem Start von TinyUSB am COM-Port sichtbar; sobald TinyUSB den PHY übernimmt, verstummt diese
Konsole (dann CDC-Port bzw. `/api/log`). Eigener Build-Ordner `build_diag`, die normale
Konfiguration bleibt unberührt.

Der Befehl im Kopf der Datei (`.\tools\idf.ps1 printdrive … -D …`) funktioniert in
PowerShell nicht, weil `-D` mit der PowerShell-Parameterbindung kollidiert. Stattdessen
`idf.py` direkt aufrufen:

```powershell
. "$env:USERPROFILE\esp\esp-idf-v5.5.6\export.ps1"
cd firmware\printdrive
idf.py -B build_diag "-DSDKCONFIG=build_diag/sdkconfig" "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.diag" build
```

Hinweis: Auch die serielle Ausgabe über USB-Serial/JTAG verliert an diesem PC bei Bursts
ganze Blöcke (ähnlich wie beim Flash-Backup, Ursache nicht geklärt). Fehlende Zeilen im Log sind
daher kein sicheres Zeichen für einen Firmware-Fehler.

## 10. API-Test (`tools/api_test.py`)

```powershell
python tools\api_test.py 192.168.2.152 --drive E:
```

Braucht `requests` (in der ESP-IDF-Python-Umgebung vorhanden). Der Dongle steckt am PC,
`--drive` ist sein Laufwerksbuchstabe; `--size-kb` (Default 512) bestimmt die Größe der
Test-`.3mf`. Das Skript **legt Testdateien an und löscht sie**, übernimmt das Laufwerk
vom Host (erst ohne, dann mit Bestätigung) und stellt es am Ende wieder bereit.

Geprüft (28 Prüfungen, wenn das Laufwerk zu Beginn am Host bereitgestellt ist und `--drive`
angegeben wird): Status erreichbar, CSRF (403), Übernahme `HOST_ACTIVE` → `force`, Besitzer
`app`, beliebiger Dateityp angenommen (bei `types = "all"`; sonst 415), Pfad im Namen (400),
zu groß (507 bzw. 413 ab 4 GiB), 5 GiB nicht auf 32 Bit gekürzt (413), kein ZIP /
abgeschnittenes ZIP als `.3mf` (422), Vorabprüfung, Upload mit Umlaut-Namen inkl. Rate,
Liste, Änderungsdatum (`mtime` kommt unverändert zurück, FAT-Auflösung 2 s), Felder `dirs`
und `truncated`, Umbenennen (Erfolg, ungültiger Zielname 400, fehlende Datei 404,
vorhandenes Ziel 409 `EXISTS`, zurück), Download identisch, vorhandene Datei (409 `EXISTS`),
Überschreiben, Upload-Abbruch ohne Rest, Bereitstellen, Dateioperationen bei Host-Besitz
(409 `WRONG_OWNER`), Datei auf dem Windows-Laufwerk identisch. Ausgabe
`<n>/<m> checks passed`, Exit-Code 1 bei Fehlern. Ergebnisse: [05](05_testprotokoll.md)
T2.11 (API v1) und T6.2 (API v2, 28/28). Die API selbst: [07_api.md](07_api.md).

## 11. Weboberfläche entwickeln (`tools/mock_server.py`)

Die Weboberfläche ist eine einzige Datei: `firmware\printdrive\main\web\index.html`
(HTML, CSS und JavaScript zusammen, keine externen Ressourcen). Zum Entwickeln braucht es
den Dongle nicht. Der Testserver bildet die API aus [07_api.md](07_api.md) mit einem
Dateispeicher im Arbeitsspeicher nach (Python 3.10+, nur Standardbibliothek):

```powershell
python tools\mock_server.py --port 8090 --scenario files    # dann http://127.0.0.1:8090/ öffnen
```

Ablauf beim Ändern der Oberfläche:

1. `index.html` bearbeiten und speichern.
2. Seite im Browser neu laden. Der Testserver liest die Datei bei jedem Aufruf neu von der
   Platte, ein Neustart ist nicht nötig.
3. Zustände über Szenarien und `/mock/set` durchspielen (siehe unten).
4. Auf das Gerät bringen: `.\tools\flash.ps1 -Project printdrive -Build` (komprimiert die
   Datei, baut, flasht, startet).

| Option | Default | Wirkung |
|---|---|---|
| `--port` | 8080 | TCP-Port |
| `--bind` | `127.0.0.1` | `0.0.0.0` macht den Testserver im Netz erreichbar (z. B. für das Handy) |
| `--html` | `firmware/printdrive/main/web/index.html` | andere UI-Datei, relativ zum Projektordner |
| `--scenario` | `usb_active` | Startszenario |
| `--upload-kbps` | 400 | Upload-Bremse in KiB/s, damit Fortschrittsbalken sichtbar sind (0 = aus) |

Szenarien (`--scenario NAME` oder im Betrieb `GET /mock/scenario?name=NAME`):

| Name | Zustand |
|---|---|
| `usb_active` | Laufwerk am verbundenen USB-Gerät bereitgestellt, einige Dateien |
| `usb_no_host` | bereitgestellt, aber kein USB-Gerät verbunden (Übernahme ohne Rückfrage) |
| `files` | Dateiverwaltung aktiv, 14 unterschiedliche Dateien, 2 ausgeblendete Ordner |
| `empty` | Dateiverwaltung aktiv, keine Dateien |
| `full` | fast kein freier Speicher (180 KiB) |
| `no_fs` | kein lesbares Dateisystem (Formatieren nötig), microSD |
| `ap_setup` | WLAN nicht eingerichtet, Einrichtungs-WLAN aktiv |
| `flash_medium` | interner Flash (10,8 MB) statt microSD |
| `many` | 300 Dateien, Liste nicht gekürzt |
| `truncated` | 1200 Dateien, Liste gekürzt (32-KiB-Puffer wie in der Firmware) |

Steuerung im Betrieb (nur im Testserver, per `GET`, ohne Header):

```powershell
curl "http://127.0.0.1:8090/mock/set?host_writing=hold&delay_ms=300"   # USB-Gerät schreibt dauerhaft, 300 ms Latenz
curl "http://127.0.0.1:8090/mock/set?fail_next=NO_SPACE@/api/upload"   # nächster Upload scheitert
curl "http://127.0.0.1:8090/mock/state"                                # interner Zustand
```

`/mock/set` kennt u. a. `owner`, `usb_connected`, `host_ejected`, `host_writable`,
`host_writing`, `fs_ok`, `needs_format`, `sd`, `total`, `fill`, `types`, `dirs`, `wifi`,
`upload_kbps`, `delay_ms`, `online`, `fail_next`, `upload_fail_at`, `other_upload` und
`reset`. Die vollständige Liste steht im Kopfkommentar von `tools/mock_server.py`.

Wie die Firmware bearbeitet der Testserver nur eine Anfrage gleichzeitig, verlangt
`X-PrintDrive: 1` für jedes `POST` und kennt den Besitzer-Automaten mit `HOST_ACTIVE`,
`HOST_WRITING` und `BUSY`. Abweichend meldet er die IP `192.0.2.152` und den Hostnamen
`datadrive-mock`, damit ein Link aus der Oberfläche nie auf einem echten Dongle landet.

Der automatische Browser-Test `tools/ui_test.py` (Playwright, einmalig
`pip install playwright`) läuft gegen diesen Testserver (`python tools/ui_test.py --base http://localhost:8090`).
