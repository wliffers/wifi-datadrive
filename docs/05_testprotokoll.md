# 05 – Testprotokoll

Prozeduren für die manuelle Prüfung auf der Hardware. Erwartete Log-Zeilen sind die
tatsächlichen `ESP_LOG`-Texte aus dem Quellcode; im Log stehen sie mit Präfix, z. B.
`I (5123) drive: owner: USB host (read/write)`. Platzhalter in `<…>`.

**Log-Quellen der Hauptfirmware:** `.\tools\monitor.ps1` (CDC-Port; beim Öffnen wird zuerst
der Log-Ringpuffer mit den letzten 8 KB inkl. Start nachgeliefert, danach live – verifiziert),
`http://<ip>/api/log` bzw. Weboberfläche → Einstellungen → „Log anzeigen“ (letzte 8 KB),
UART0. Smoke-Test: `.\tools\idf.ps1 smoke_test -p COM4 monitor` (USB-Serial/JTAG verliert an
diesem PC bei Bursts Blöcke, siehe [04](04_build_und_flash.md) Abschnitt 9).

Ergebnis eintragen als **bestanden**, **Befund** (Abweichung oder Erkenntnis, mit Bemerkung),
**FEHLER** (mit Bemerkung), **offen** oder **n/a**.

Übersicht Stand 2026-11-11: Phase 0 bestanden; Phase 1 bestanden außer T1.4; Phase 2 mit
`tools/api_test.py` weitgehend bestanden (T2.11: Flash 18/18, microSD 21/21), offen T2.2 (3),
T2.3, T2.8, Flash-Modus per Web; Phase 5 microSD bestanden (Grundfunktion); Phase 4 offen.

## Phase 0 – Smoke-Test

### T0.1 Flash-Backup

1. Werks-Firmware läuft (COM4). `.\tools\backup_flash.ps1 -Port COM4`.

Erwartet: `flash_id` zeigt Manufacturer `ef`, Device `4018`; Fortschritt bis `0x1000000 / 0x1000000`;
`wrote 16777216 bytes to …`; Datei in `backup\`, Zeile in `backup\SHA256SUMS.txt`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **Befund** | 2026-11-11 | `flash_id` ok (`ef`/`4018`, 16 MB). Vollständiges Backup über USB-Serial/JTAG an diesem PC nicht zuverlässig: datenabhängiger Byteverlust in `read_flash`, ca. 11 KB/s auch mit kleinen Paketen, Blöcke scheitern wiederholt. Ersatz: offizielles LILYGO-Werksabbild `backup\T-Dongle-S3_factory_screen_251218.bin` (SHA256 in `SHA256SUMS.txt`), siehe [04](04_build_und_flash.md) Abschnitt 5 |

### T0.2 Chip, Flash, Partitionen

1. `.\tools\idf.ps1 smoke_test build`, dann `.\tools\idf.ps1 smoke_test -p COM4 flash monitor`.

Erwartet im Log:
- `SMOKE: chip ESP32-S3 rev v0.2, 2 cores, features 0x<…>`
- `SMOKE: flash size 16777216 bytes`
- `SMOKE: MAC E0:72:A1:C1:54:D4` (Annahme: STA-MAC = Basis-MAC)
- `SMOKE: partition <name> …` für nvs @0x009000, otadata @0x00e000, ota_0 @0x010000,
  ota_1 @0x290000, storage @0x510000 size 0xaf0000

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Flash-ID per esptool: Winbond `0xEF`/`0x4018`, 16 MB. Smoke-Test läuft ohne Absturz, ca. 220 KB freier Heap. Partitionszeilen des Logs nicht einzeln protokolliert; Lage von `storage` @0x510000 durch die Hauptfirmware bestätigt (T1.1) |

### T0.3 LCD und LED

1. Nach dem Flashen bzw. Reset das Display beobachten.

Erwartet: vier Vollbilder je 0,6 s mit Text `ROT`, `GRUEN`, `BLAU`, `WEISS` in genau diesen
Farben, Text oben links lesbar, nicht gespiegelt, kein Rand/Versatz. LED gleichzeitig rot,
grün, blau, aus. Log `SMOKE: LCD ok (check colours: red, green, blue, white)`.
Danach Statusbild mit Kopf `SMOKE TEST` und `Flash 16 MB`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Farbbalken, Textorientierung und Farben korrekt; RGB-LED reagiert |

### T0.4 BOOT-Taste

1. BOOT-Taste drücken und halten, dann loslassen.

Erwartet: unterste Zeile `BOOT gedrückt` (grün) statt `BOOT: drücken`; LED bekommt einen
Grünanteil; im 5-s-Log `button=1`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | BOOT-Taste wird erkannt |

### T0.5 WLAN STA und Setup-AP

1. Mit `firmware\pd_secrets.h` (gültige Daten) bauen und flashen.
2. Ohne gültige Daten (Datei entfernen oder falsches Passwort, `fullclean`) bauen und flashen.

Erwartet (1): `pd_wifi: connected to '<ssid>', IP <ip>`, `pd_wifi: mDNS: http://printdrive.local`;
LCD `WLAN verbunden`, IP, `RSSI <n> dBm`; `ping printdrive.local` antwortet.
Erwartet (2): sofort (ohne Daten) bzw. nach ca. 20 s
`pd_wifi: setup access point on: SSID 'PrintDrive-XXXX', password '<pw>', http://192.168.4.1`;
LCD zeigt SSID, `PW <pw>`, `192.168.4.1`; Handy/PC kann sich mit dem Passwort verbinden.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| (1) **bestanden** | 2026-11-11 | Variante: Hauptfirmware, Zugangsdaten nicht per `pd_secrets.h`, sondern per Handy über Setup-AP + Weboberfläche (T2.9). Dongle im Heimnetz unter `192.168.2.152`, Weboberfläche am Handy erreichbar. RSSI und `ping printdrive.local` nicht protokolliert |
| (2) **bestanden** | 2026-11-11 | Setup-AP `PrintDrive-54D5` gestartet, SSID und Passwort auf dem LCD; Handy verbindet sich (T2.9) |

## Phase 1 – USB-Laufwerk unter Windows

### T1.1 Erststart der Hauptfirmware

1. Keine microSD-Karte eingelegt. `.\tools\flash.ps1 -Project printdrive` (ggf. BOOT halten + einstecken). Dongle am PC lassen.
2. Log über die Weboberfläche bzw. `/api/log` ansehen.

Erwartet (aktueller Stand, `CONFIG_PD_FAT_CLUSTER_KB=4`): `main: WiFi PrintDrive 0.1.0`,
`board: board init done, flash size 16777216 bytes`,
`drive: no usable microSD card (<err>) - using internal flash`,
`drive: storage partition @0x510000, 11468800 bytes raw, WL sector 512, WL size <n>`,
`drive: no valid FAT found (<code>)`, `drive: first boot: creating file system`,
`drive: formatting internal flash (FAT, 4096 byte clusters)`,
`drive: FAT ok (type 1: 1=FAT12 2=FAT16 3=FAT32, label 'PRINTDRIVE')`,
`usb_dev: TinyUSB installed (MSC + CDC log)`, `drive: host present at boot -> providing drive`,
`drive: owner: USB host (read/write)`, `main: start-up complete`. LCD: `BOOT`, dann `USB` (grün).
Zweiter Start: kein `first boot`, `FAT ok (type 1 …)`. Mit 2-KiB-Clustern: `type 2` (FAT16).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Damaliger Stand (2-KiB-Cluster): Erststart formatiert automatisch (`first boot: creating file system`, ca. 1,4 s), `FAT ok (type 2 …)` = FAT16. Dateien überleben spätere Firmware-Updates (kein erneutes Formatieren). LCD-Statusbild siehe T1.2 |
| **Befund** | 2026-11-11 | Umstellung auf 4-KiB-Cluster (FAT12) per Web-Formatieren (T2.9), Grund: Web-Upload-Rate (T2.10) |

### T1.2 Laufwerk im Explorer

1. Explorer öffnen, Eigenschaften des neuen Laufwerks.
2. Geräte-Manager: Anschlüsse (COM & LPT).

Erwartet: Laufwerk `PRINTDRIVE`, Dateisystem FAT, Kapazität ca. 10–11 MB, leer.
Zusätzlicher COM-Port (USB-Serial, 303A:4003). LCD-Host-Zeile `Host: verbunden`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Windows 11: Composite-Gerät 303A:4003 (MSC + CDC „Serielles USB-Gerät“ COM5), Seriennummer `E072A1C154D4`. Datenträger „PrintDrv WiFi PrintDrive“, MBR, 512-B-Sektoren; Volume `E:` `PRINTDRIVE`, FAT (FAT16), 11.288.576 Byte. LCD per Foto: grüner Kopf `USB`, AP-SSID + Passwort, `Frei 9.8MB / 10.8MB` mit Balken, `Host: verbunden`, `1 Datei(en)`, LED grün |
| **Befund** | 2026-11-11 | Dropbox-Desktop-Client schreibt `.dropbox.device` auf jedes Wechsellaufwerk, auch auf den Dongle (harmlos, der Drucker sieht die Datei aber) |

### T1.3 Kopieren, Lesen, Auswerfen, neu Einstecken

1. Eine Datei (ca. 2–5 MB) auf das Laufwerk kopieren, Kopie vergleichen (`fc /b`).
2. Im Explorer „Auswerfen“.
3. Dongle abziehen, wieder einstecken, Datei erneut vergleichen.

Erwartet: (1) LCD `Host schreibt...`, Vergleich ohne Unterschiede.
(2) `drive: host ejected the drive`, `drive: owner: APP (web upload possible), <frei> of <gesamt> bytes free`;
LCD `BEREIT` (bzw. `WLAN`), `Host: ausgeworfen`; Datei in der Weboberfläche sichtbar.
(3) Laufwerk wieder da, Datei identisch.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Variante: 1-MiB-Zufallsdatei, Vergleich per SHA256 statt `fc /b`. (1) identisch. (2) `host ejected the drive` → `owner: APP …` automatisch. (3) statt Abziehen BOOT kurz: `owner: USB host (read/write)`, `soft re-plug`, Laufwerk wieder da, Datei ungecacht mit 753 KB/s gelesen, SHA256 identisch. LCD `Host schreibt...` nicht protokolliert |
| **Befund** | 2026-11-11 | Schreiben 1 MiB: 12 KB/s vor, 47 KB/s nach dem FAT-Ausrichtungs-Fix (`MKFS_PARM.align`), siehe [06](06_risiken.md) Abschnitt 4 |

### T1.5 Download-Modus per Software, Firmware-Update

1. `printdrive` läuft, Dateien auf dem Laufwerk. `.\tools\flash.ps1 -Project printdrive`.
2. Danach Dateien prüfen; `.\tools\monitor.ps1` öffnen.

Erwartet: `TinyUSB-CDC-Port gefunden: COMx (303A:4003)`, 1200-Baud-Touch, innerhalb 15 s
erscheint 303A:1001, `Flashen erfolgreich.`; App startet von selbst, Dateien unverändert.
Monitor zeigt zuerst das nachgelieferte Start-Log.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Touch auf COM5 → ROM-Port COM4 nach 3,4 s → Flashen → App startet von selbst; Dateien überleben das Update. Start-Log kommt beim Öffnen des CDC-Ports aus dem Ringpuffer. Erst nach Fix in `board_restart_to_download_mode()` (USB-PHY-Rückgabe); vorher erschien gar kein USB-Gerät |

### T1.4 Nur-Lese-Modus des Hosts

1. Web → Einstellungen → „Host darf auf das Laufwerk schreiben“ abwählen.
2. „Am USB bereitstellen“ (bzw. aus-/einstecken). Datei auf das Laufwerk kopieren.

Erwartet: `drive: owner: USB host (read-only)`; LCD `Host: nur lesen`; Windows meldet
Schreibschutz, Lesen funktioniert. Danach Einstellung wieder aktivieren.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | Weboberfläche ist inzwischen verfügbar; vor dem Drucker-Test durchführen (Empfehlung Phase 4) |

## Phase 2 – Web und Umschalten

### T2.1 Upload einer kleinen .3mf

1. `http://printdrive.local` (oder IP vom LCD) öffnen, Laufwerk ist am Host ausgeworfen.
2. Eine gültige `.3mf` (< 1 MB) per Drag & Drop hochladen; „Nach dem Upload automatisch
   am USB bereitstellen“ angehakt lassen.

Erwartet: LCD `UPLOAD` mit Prozent; `drive: upload start '<name>' (<n> bytes)`,
`drive: upload ok '<name>'`, `web: upload '<name>' <n> bytes in <t> ms (<x> KB/s): OK`;
danach `drive: owner: USB host (read/write)`, `usb_dev: soft re-plug`; Laufwerk erscheint
in Windows neu und enthält die Datei.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| teilweise **bestanden** | 2026-11-11 | Per `api_test.py` (T2.11): Upload, Bereitstellen per `/api/mode`, Datei auf `E:` identisch – auf Flash und microSD. Drag & Drop mit Haken „automatisch bereitstellen“ in der Weboberfläche nicht einzeln protokolliert |
| **FEHLER** (behoben) | 2026-11-11 | Weboberfläche zeigte beide Modus-Knöpfe gleichzeitig: CSS-`display` überschrieb das `hidden`-Attribut. Fix: `[hidden]{display:none!important}` |

### T2.2 Umschalten ohne Auswurf / während der Host schreibt

1. Laufwerk am Host (nicht ausgeworfen). Web → „Für Upload übernehmen“ → Dialog abbrechen.
2. Erneut, Dialog „Übernehmen“ bestätigen.
3. Bereitstellen, große Datei (ca. 8 MB) per Explorer kopieren und währenddessen
   „Für Upload übernehmen“ bzw. BOOT kurz drücken.

Erwartet: (1) Dialog „Laufwerk vom Host übernehmen?“, nichts passiert.
(2) `drive: taking the drive from the host (confirmed by user)`, `owner: APP …`.
(3) Fehlertext „Der USB-Host schreibt gerade. …“ (409 `HOST_WRITING`), LCD
`Host schreibt - warten`; Kopiervorgang läuft ungestört weiter.
BOOT-Taste ohne Auswurf: `Host aktiv! Nochmal=OK`, zweiter Druck < 4 s übernimmt.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| (1)(2) **bestanden** | 2026-11-11 | Per API (T2.11): ohne `force` 409 `HOST_ACTIVE`, mit `force` übernommen (`owner: app`). Dialog in der Weboberfläche nicht einzeln protokolliert |
| (3) **offen** | – | `HOST_WRITING` während eines Explorer-Kopiervorgangs und BOOT-Bestätigung per zweitem Druck noch nicht geprüft |

### T2.3 Umschalten während eines Uploads

1. Größere `.3mf` hochladen, währenddessen BOOT kurz drücken (oder in zweitem Tab bereitstellen).

Erwartet: LCD `Upload läuft` bzw. 409 `BUSY` „Upload oder Download läuft gerade“; Upload läuft fertig.
Hinweis: Der HTTP-Server bearbeitet nur eine Anfrage gleichzeitig; eine Anfrage aus einem
zweiten Tab wird erst nach dem Upload beantwortet. Aussagekräftig ist daher die BOOT-Taste.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | |

### T2.4 Auswurf am Host → Upload-Modus

1. Laufwerk bereitstellen, im Explorer auswerfen.

Erwartet: `drive: host ejected the drive`, `drive: owner: APP (web upload possible), …`; LCD `BEREIT`/`WLAN`.
Danach „Am USB bereitstellen“: `usb_dev: soft re-plug`, Laufwerk erscheint wieder.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Auswurf → APP automatisch und Bereitstellen per BOOT kurz mit Re-Plug verifiziert (T1.3); Bereitstellen per Web (`/api/mode?target=usb`) mit anschließend sichtbarer Datei auf `E:` verifiziert (T2.11). LCD-Zustände per Foto (Phase 3) |

### T2.5 Upload-Abbruch

1. Große `.3mf` hochladen, bei ca. 50 % den Browser-Tab schließen.

Erwartet: `web: upload connection lost (<n>)`, `drive: upload aborted '<name>' after <n> bytes`;
Datei nicht in der Liste, freier Speicher wie vorher; LCD verlässt `UPLOAD`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Variante per `api_test.py`: 100 von 300 KB gesendet, Socket geschlossen. Danach kein Busy-Zustand (`upload.active = false`), keine Datei, keine Temp-Datei – auf Flash und microSD |

### T2.6 Speicher voll

1. Laufwerk über den Host fast füllen, dann eine `.3mf` hochladen, die nicht mehr passt.

Erwartet: Meldung „Nicht genug freier Speicher“ (507 `NO_SPACE`) vor dem Senden der Daten;
keine Temp-Datei; Speicherbalken rot bei < 10 % frei.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Variante per `api_test.py`: `upload/check` mit Größe = frei + 1 MB → 507 `NO_SPACE` (Flash). Auf der 64-GB-Karte liegt diese Größe über 4 GiB → 413 `TOO_LARGE` (T5.4). Roter Speicherbalken nicht geprüft |

### T2.7 Ungültige Datei

1. Eine Textdatei in `test.3mf` umbenennen und hochladen. 2. Eine `.txt` hochladen.

Erwartet: (1) `drive: upload failed 'test.3mf': Datei ist beschädigt oder kein gültiges 3MF (ZIP)`
(422 `INVALID_FILE`), Datei nicht in der Liste. (2) Browser lehnt ab: „Dateityp nicht erlaubt“.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Per `api_test.py`: (1) Zufallsdaten als `.3mf` und abgeschnittenes ZIP → je 422. (2) `.txt` serverseitig → 415 `BAD_TYPE`; Name mit Pfad (`../x.3mf`) → 400. Ablehnung im Browser-Dialog nicht einzeln protokolliert |

### T2.8 Stromausfall während des Uploads

1. Große `.3mf` hochladen, bei ca. 50 % den Dongle abziehen. 2. Wieder einstecken.

Erwartet: beim Start `drive: removing stale upload temp file (<n> bytes)` (falls der
Verzeichniseintrag schon geschrieben war); Datei nicht in der Liste; Dateisystem mountet
(`FAT ok …`). Freien Speicher notieren (verlorene Cluster möglich, siehe Risiken).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | |

### T2.9 WLAN-Einrichtung per Web, Flash-Modus, Formatieren

1. Über den Setup-AP `http://192.168.4.1` → Einstellungen → WLAN speichern.
2. Einstellungen → „Flash-Modus“; danach aus-/einstecken.
3. Einstellungen → „Formatieren …“, `FORMAT` eingeben.

Erwartet: (1) `pd_wifi: new credentials stored for '<ssid>'`, dann `connected to …`; AP geht
nach ca. 120 s aus (`setup access point off`). (2) `board: restarting into ROM download mode`; Laufwerk und
CDC-Port verschwinden, 303A:1001 erscheint (dieselbe Funktion wie beim 1200-Baud-Touch, T1.5); nach
Aus-/Einstecken normaler Start. (3) `drive: format requested - restarting`, nach Neustart
`drive: formatting internal flash (FAT, 4096 byte clusters)` bzw. mit Karte
`drive: formatting microSD card (FAT32)`, Laufwerk leer.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| (1) **bestanden** | 2026-11-11 | Per Handy: Setup-AP → Weboberfläche → WLAN gespeichert; Dongle verbindet sich mit dem Heimnetz (`192.168.2.152`), Weboberfläche am Handy bedienbar |
| (2) **offen** | – | Software-Neustart in den Download-Modus per 1200-Baud-Touch verifiziert (T1.5), Auslösung per Web noch nicht |
| (3) **bestanden** | 2026-11-11 | Formatieren des internen Flashs per `/api/format` mit Neustart; danach FAT12 mit 4-KiB-Clustern. Formatieren einer microSD-Karte noch nicht geprüft (T5.5) |

### T2.10 Geschwindigkeit

1. Web-Upload einer ca. 8-MB-Datei; Wert aus `web: upload … (<x> KB/s)` notieren.
2. Kopieren derselben Datei per Explorer auf das Laufwerk; Dauer stoppen.

Erwartet: Messwerte, keine Sollwerte. Schreiben ins interne Flash ist durch das Löschen der
4-KiB-Flash-Sektoren begrenzt (Obergrenze grob 90 KB/s, siehe [06](06_risiken.md) Abschnitt 4);
Web-Uploads stoßen an dieselbe Grenze. USB Full Speed ist beim Schreiben ins Flash nicht der Engpass.

| Medium | Web KB/s | USB schreiben KB/s | USB lesen KB/s | Datum |
|---|---|---|---|---|
| Flash, FAT16 2 KiB, vor FAT-Ausrichtungs-Fix | – | 12 | – | 2026-11-11 |
| Flash, FAT16 2 KiB, nach Fix | 7 | 47 (1-MiB-Zufallsdatei) | 753 (ungecacht) | 2026-11-11 |
| Flash, FAT12 4 KiB | 46 | – | – | 2026-11-11 |
| microSD 64 GB, FAT32 | 1335–1578 (4 MB) | 832 (10 MB) | – | 2026-11-11 |

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **Befund** | 2026-11-11 | Web-Upload auf Flash anfangs nur 7 KB/s: FatFs schreibt höchstens einen Cluster pro Disk-Schreibzugriff, ein 2-KiB-Cluster ist immer ein halber 4-KiB-Flash-Sektor → langsamer Lesen-Löschen-Schreiben-Pfad im Wear Levelling. Mit 4-KiB-Clustern (`CONFIG_PD_FAT_CLUSTER_KB=4`, Volume wird FAT12) 46 KB/s |

### T2.11 Automatischer API-Test (`tools/api_test.py`)

1. Dongle am PC, Laufwerk bereitgestellt (Laufwerksbuchstabe z. B. `E:`).
2. `python tools\api_test.py <ip> --drive E:` (Details: [04](04_build_und_flash.md) Abschnitt 10).

Erwartet: alle Prüfungen `[OK ]`, Abschluss `<n>/<n> checks passed`. Testdateien werden
angelegt und wieder gelöscht, die hochgeladene Testdatei bleibt auf dem Laufwerk.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Flash: 18/18 – CSRF 403; Übernahme ohne Bestätigung 409 `HOST_ACTIVE`, mit `force` ok; Endung 415; Pfad im Namen 400; zu groß 507; kein ZIP 422; abgeschnittenes ZIP 422; Upload mit Umlaut-Namen; Liste; Download identisch; vorhandene Datei 409 `EXISTS`; Überschreiben; Abbruch ohne Rest; Bereitstellen; Dateioperation bei Host-Besitz 409 `WRONG_OWNER`; Datei auf `E:` identisch |
| **bestanden** | 2026-11-11 | microSD: 21/21 (zusätzlich u. a. 413 `TOO_LARGE` für ≥ 4 GiB, siehe T5.4) |
| **FEHLER** (behoben) | 2026-11-11 | Bei 409 `EXISTS` (Abweisung vor dem Lesen der Daten) setzte der Server die Verbindung zurück, der Client bekam keine Antwort. Fix: `drain_body()` liest den restlichen Body und verwirft ihn, danach kommt die Fehlerantwort an |
| **Befund** | 2026-11-11 | Der ESP-IDF-HTTP-Server bearbeitet nur eine Anfrage gleichzeitig: Während eines Uploads wird `/api/status` nicht beantwortet. Die Weboberfläche setzt das Polling während des Uploads aus, das LCD zeigt den Fortschritt |

## Phase 4 – Bambu Lab X2D (nicht verifiziert)

Vorbereitung: zuerst T1.4 (Host nur lesen) am PC prüfen. Empfohlen für den Drucker:
microSD-Karte (FAT32, groß, schnell), Host nur lesen erwägen. Bei Problemen nacheinander
`CONFIG_TINYUSB_CDC_ENABLED=n` (Composite-Gerät vermeiden), Host nur lesen,
`CONFIG_PD_REPLUG_ON_PROVIDE` umschalten.

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| X1 | Dongle am Drucker: LCD bleibt stabil (kein Neustart-Zyklus), Kopf `USB` | | |
| X2 | Drucker erkennt Speichermedium, Dateiliste zeigt vorhandene Datei | | |
| X3 | Per Web hochgeladene, gesliceste `.gcode.3mf` erscheint am Drucker | | |
| X4 | Druck startet aus dieser Datei | | |
| X5 | Upload während Drucker angeschlossen → nach Bereitstellen neue Datei sichtbar | | |
| X6 | Übernahme ohne Auswurf (force) – Verhalten des Druckers notieren (kein Druck aktiv!) | | |
| X7 | Variante ohne CDC (PID 4002) – Unterschied? | | |
| X8 | Variante Host nur lesen – Drucker akzeptiert schreibgeschütztes Medium? | | |
| X9 | WLAN-Upload-Rate am Drucker-Standort | | |
| X10 | Mit microSD: stabile Versorgung, Drucker liest FAT32-Karte (Kapazität notieren) | | |

## Phase 5 – microSD

Testkarte des Nutzers: 64 GB, CID-Name „SDABC“, 59.645 MB, ab Werk bzw. vorher schon FAT32
mit Label `DATA`; enthielt bereits `autorun.inf`/`autorun.ico` (nicht von der Firmware).

### T5.1 Karte erkannt, Laufwerk unter Windows

1. Dongle ausgesteckt, Karte einlegen, einstecken. Log ansehen, Explorer öffnen.

Erwartet: `drive: microSD card '<name>': <n> MB, <f> kHz, 4-bit`, `FAT ok (type 3 …)`;
Laufwerk mit Kartengröße; LCD-Speicherzeile `SD x / y`; `/api/status` → `medium.sd = true`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | SDMMC 4 Bit mit 40 MHz an CLK 12, CMD 16, D0 14, D1 17, D2 21, D3 18 → Slot auf dieser Board-Revision bestätigt. Windows: `E:` FAT32, 62,5 GB |

### T5.2 Geschwindigkeit

Siehe T2.10: USB schreiben 832 KB/s (10 MB), Web-Upload 1335–1578 KB/s (4 MB) – gegenüber
47 bzw. 46 KB/s auf dem internen Flash.

### T5.3 Rückfall ohne Karte

1. Dongle ausgesteckt, Karte entnehmen, einstecken.

Erwartet: `drive: no usable microSD card (<err>) - using internal flash`, Laufwerk mit ca. 11 MB,
Dateien des internen Flashs unverändert; Karte wird zu keinem Zeitpunkt formatiert.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | Verhalten implementiert, nicht einzeln protokolliert |

### T5.4 Dateien ≥ 4 GiB

1. `upload/check` mit Größe > 4 GiB (im API-Test: frei + 1 MB sowie 5 GiB).

Erwartet: 413 `TOO_LARGE` „Datei zu groß (FAT erlaubt höchstens 4 GB pro Datei)“.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **FEHLER** (behoben) | 2026-11-11 | Größe wurde als 32-Bit-Zahl gelesen: 62 GB + 1 MB liefen über und wurden angenommen. Fix: 64-Bit-Auswertung, ≥ 4 GiB → 413 |
| **bestanden** | 2026-11-11 | nach Fix im API-Test (21/21) |

### T5.5 exFAT-Karte, Formatieren der Karte

1. exFAT-Karte (typisch > 32 GB ab Werk) einlegen und einstecken.
2. Weboberfläche → Einstellungen → „Formatieren …“, `FORMAT`.

Erwartet: (1) `no valid FAT found`, LCD „Kein Dateisystem! / Im Web formatieren“, Laufwerk
nicht am Host. (2) `drive: formatting microSD card (FAT32)`, danach `FAT ok (type 3 …)`, Label
`PRINTDRIVE`. Die Format-Anforderung gilt nur für das Medium, auf dem sie gestellt wurde.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | Testkarte war bereits FAT32 |
