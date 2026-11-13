# 05 – Testprotokoll

Prozeduren für die manuelle Prüfung auf der Hardware. Erwartete Log-Zeilen sind die
tatsächlichen `ESP_LOG`-Texte aus dem Quellcode; im Log stehen sie mit Präfix, z. B.
`I (5123) drive: owner: USB host (read/write)`. Platzhalter in `<…>`.

**Log-Quellen der Hauptfirmware:** `.\tools\monitor.ps1` (CDC-Port; beim Öffnen wird zuerst
der Log-Ringpuffer mit den letzten 8 KB inkl. Start nachgeliefert, danach live – verifiziert),
`http://<ip>/api/log` bzw. Weboberfläche → Einstellungen → System → Protokoll „Anzeigen“
(letzte 8 KB), UART0. Smoke-Test: `.\tools\idf.ps1 smoke_test -p COM4 monitor` (USB-Serial/JTAG verliert an
diesem PC bei Bursts Blöcke, siehe [04](04_build_und_flash.md) Abschnitt 9).

Ergebnis eintragen als **bestanden**, **Befund** (Abweichung oder Erkenntnis, mit Bemerkung),
**FEHLER** (mit Bemerkung), **offen** oder **n/a**.

Das Projekt heißt seit Firmware 0.2.0 „WiFi DataDrive“ (vorher „WiFi PrintDrive“). Die
Prozeduren und erwarteten Texte unten beschreiben den Stand 0.2.0; Ergebniszeilen bleiben,
wie sie am Testtag eingetragen wurden, und nennen deshalb zum Teil die alten Namen
(`PrintDrive-54D5`, `PRINTDRIVE`, „Host …“ auf dem LCD) und die alte Weboberfläche.

Stand 0.6.0: Die Ergebnisse für Anmeldung, Verschlüsselung und WLAN-Suche stehen in Phase 7 und 8,
die für 0.5.0 (Netzlaufwerk, Uhrzeit, automatisches Sperren, Upload fortsetzen, ZIP, HTTPS) in
Phase 9, die für 0.6.0 (Update über den Browser, signierte Updates, Restzeit, Netzwerkeinstellungen,
neue Oberfläche) in Phase 10 am Ende dieses Dokuments. Die Übersicht gilt für 0.2.0: Phase 0 bestanden; Phase 1 bestanden außer T1.4; Phase 2 mit
`tools/api_test.py` weitgehend bestanden (T2.11: Flash 18/18, microSD 21/21), offen T2.2 (3),
T2.3, T2.8, Flash-Modus per Web; Phase 5 microSD bestanden (Grundfunktion); Phase 6
(Umbenennung, API v2 28/28, gzip-Auslieferung) bestanden, Browser-Test der neuen
Weboberfläche am Testserver bestanden (T6.4: `tools/ui_test.py` 566/566), offen dort die
Abläufe am echten Gerät und am echten Handy (B14–B16); Phase 4 (Bambu Lab X2D):
Grundkompatibilität laut Rückmeldung des Besitzers bestanden (X2–X4), X5 nicht separat
bestätigt, X6, X8, X9 offen.

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

Erwartet (1): `pd_wifi: connected to '<ssid>', IP <ip>`, `pd_wifi: mDNS: http://datadrive.local`;
LCD `WLAN verbunden`, IP, `RSSI <n> dBm`; `ping datadrive.local` antwortet.
Erwartet (2): sofort (ohne Daten) bzw. nach ca. 20 s
`pd_wifi: setup access point on: SSID 'DataDrive-XXXX' (password on the LCD), http://192.168.4.1`;
LCD zeigt SSID, `PW <pw>`, `192.168.4.1`; Handy/PC kann sich mit dem Passwort verbinden.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| (1) **bestanden** | 2026-11-11 | Variante: Hauptfirmware, Zugangsdaten nicht per `pd_secrets.h`, sondern per Handy über Setup-AP + Weboberfläche (T2.9). Dongle im Heimnetz unter `192.168.2.152`, Weboberfläche am Handy erreichbar. RSSI und `ping printdrive.local` nicht protokolliert |
| (2) **bestanden** | 2026-11-11 | Setup-AP `PrintDrive-54D5` gestartet, SSID und Passwort auf dem LCD; Handy verbindet sich (T2.9) |

## Phase 1 – USB-Laufwerk unter Windows

### T1.1 Erststart der Hauptfirmware

1. Keine microSD-Karte eingelegt. `.\tools\flash.ps1 -Project printdrive` (ggf. BOOT halten + einstecken). Dongle am PC lassen.
2. Log über die Weboberfläche bzw. `/api/log` ansehen.

Erwartet (aktueller Stand, `CONFIG_PD_FAT_CLUSTER_KB=4`): `main: WiFi DataDrive 0.2.0`,
`board: board init done, flash size 16777216 bytes`,
`drive: no usable microSD card (<err>) - using internal flash`,
`drive: storage partition @0x510000, 11468800 bytes raw, WL sector 512, WL size <n>`,
`drive: no valid FAT found (<code>)`, `drive: first boot: creating file system`,
`drive: formatting internal flash (FAT, 4096 byte clusters)`,
`drive: FAT ok (type 1: 1=FAT12 2=FAT16 3=FAT32, label 'DATADRIVE')`,
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

Erwartet: Laufwerk `DATADRIVE` (vor 0.2.0 formatiert: `PRINTDRIVE`), Dateisystem FAT,
Kapazität ca. 10–11 MB, leer. Datenträger „DataDrv WiFi DataDrive“. Zusätzlicher COM-Port
(USB-Serial, 303A:4003). LCD-Host-Zeile `Gerät: verbunden`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Windows 11: Composite-Gerät 303A:4003 (MSC + CDC „Serielles USB-Gerät“ COM5), Seriennummer `E072A1C154D4`. Datenträger „PrintDrv WiFi PrintDrive“, MBR, 512-B-Sektoren; Volume `E:` `PRINTDRIVE`, FAT (FAT16), 11.288.576 Byte. LCD per Foto: grüner Kopf `USB`, AP-SSID + Passwort, `Frei 9.8MB / 10.8MB` mit Balken, `Host: verbunden`, `1 Datei(en)`, LED grün |
| **Befund** | 2026-11-11 | Dropbox-Desktop-Client schreibt `.dropbox.device` auf jedes Wechsellaufwerk, auch auf den Dongle (harmlos, der Drucker sieht die Datei aber) |

### T1.3 Kopieren, Lesen, Auswerfen, neu Einstecken

1. Eine Datei (ca. 2–5 MB) auf das Laufwerk kopieren, Kopie vergleichen (`fc /b`).
2. Im Explorer „Auswerfen“.
3. Dongle abziehen, wieder einstecken, Datei erneut vergleichen.

Erwartet: (1) LCD `Gerät schreibt...`, Vergleich ohne Unterschiede.
(2) `drive: host ejected the drive`, `drive: owner: APP (web upload possible), <frei> of <gesamt> bytes free`;
LCD `BEREIT` (bzw. `WLAN`), `Gerät: ausgeworfen`; Datei in der Weboberfläche sichtbar.
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

1. Web → Einstellungen → Speicher & USB → „USB-Gerät darf Dateien ändern“ ausschalten.
2. „USB-Laufwerk bereitstellen“ (bzw. aus-/einstecken). Datei auf das Laufwerk kopieren.

Erwartet: `drive: owner: USB host (read-only)`; LCD `Gerät: nur lesen`; Web-Statusleiste
„Das angeschlossene USB-Gerät kann die Dateien lesen.“; Windows meldet Schreibschutz, Lesen
funktioniert. Danach Einstellung wieder aktivieren.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | Weboberfläche ist inzwischen verfügbar. Der Drucker-Test vom 2026-11-12 lief ohne Nur-Lese-Modus; vor X8 am PC durchführen |

## Phase 2 – Web und Umschalten

### T2.1 Upload einer kleinen .3mf

1. `http://datadrive.local` (oder IP vom LCD) öffnen; das Laufwerk ist am Host
   bereitgestellt („USB-Laufwerk aktiv“).
2. Eine gültige `.3mf` (< 1 MB) per Drag & Drop hochladen, die Rückfrage „Laufwerk vom
   USB-Gerät trennen?“ bestätigen (entfällt nach Auswerfen am Host). Einstellung „Nach dem
   Hochladen wieder bereitstellen“ eingeschaltet lassen.

Erwartet: `drive: taking the drive from the host (confirmed by user)`; LCD `UPLOAD` mit
Prozent; `drive: upload start '<name>' (<n> bytes)`, `drive: upload ok '<name>'`,
`web: upload '<name>' <n> bytes in <t> ms (<x> KB/s): OK`; Meldung „1 Datei gespeichert.
Das USB-Laufwerk ist wieder bereitgestellt.“; `drive: owner: USB host (read/write)`,
`usb_dev: soft re-plug`; Laufwerk erscheint in Windows neu und enthält die Datei.
War die Dateiverwaltung schon vor dem Upload aktiv (z. B. nach Auswerfen), bleibt sie es:
Zurückgegeben wird nur, wenn der Upload selbst das Laufwerk genommen hat.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| teilweise **bestanden** | 2026-11-11 | Per `api_test.py` (T2.11): Upload, Bereitstellen per `/api/mode`, Datei auf `E:` identisch – auf Flash und microSD. Drag & Drop mit Haken „automatisch bereitstellen“ in der Weboberfläche nicht einzeln protokolliert |
| **FEHLER** (behoben) | 2026-11-11 | Weboberfläche zeigte beide Modus-Knöpfe gleichzeitig: CSS-`display` überschrieb das `hidden`-Attribut. Fix: `[hidden]{display:none!important}` |

### T2.2 Umschalten ohne Auswurf / während der Host schreibt

1. Laufwerk am Host (nicht ausgeworfen). Web → „Dateiverwaltung aktivieren“ → Dialog abbrechen.
2. Erneut, Dialog mit „Trennen und fortfahren“ bestätigen.
3. Bereitstellen, große Datei (ca. 8 MB) per Explorer kopieren und währenddessen
   „Dateiverwaltung aktivieren“ bzw. BOOT kurz drücken.

Erwartet: (1) Dialog „Laufwerk vom USB-Gerät trennen?“, nichts passiert.
(2) `drive: taking the drive from the host (confirmed by user)`, `owner: APP …`.
(3) Meldung „Das USB-Gerät schreibt gerade auf das Laufwerk. …“ (409 `HOST_WRITING`), LCD
`Gerät schreibt - warten`; Kopiervorgang läuft ungestört weiter.
BOOT-Taste ohne Auswurf: `Gerät aktiv! Nochmal=OK`, zweiter Druck < 4 s übernimmt.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| (1)(2) **bestanden** | 2026-11-11 | Per API (T2.11): ohne `force` 409 `HOST_ACTIVE`, mit `force` übernommen (`owner: app`). Dialog in der Weboberfläche nicht einzeln protokolliert |
| (3) **offen** | – | `HOST_WRITING` während eines Explorer-Kopiervorgangs und BOOT-Bestätigung per zweitem Druck noch nicht geprüft |

### T2.3 Umschalten während eines Uploads

1. Größere `.3mf` hochladen, währenddessen BOOT kurz drücken (oder in zweitem Tab bereitstellen).

Erwartet: LCD `Up-/Download läuft` bzw. 409 `BUSY` „Es läuft gerade eine Dateiübertragung. …“; Upload läuft fertig.
Hinweis: Der HTTP-Server bearbeitet nur eine Anfrage gleichzeitig; eine Anfrage aus einem
zweiten Tab wird erst nach dem Upload beantwortet. Aussagekräftig ist daher die BOOT-Taste.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | |

### T2.4 Auswurf am Host → Upload-Modus

1. Laufwerk bereitstellen, im Explorer auswerfen.

Erwartet: `drive: host ejected the drive`, `drive: owner: APP (web upload possible), …`; LCD `BEREIT`/`WLAN`.
Danach „USB-Laufwerk bereitstellen“: `usb_dev: soft re-plug`, Laufwerk erscheint wieder.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Auswurf → APP automatisch und Bereitstellen per BOOT kurz mit Re-Plug verifiziert (T1.3); Bereitstellen per Web (`/api/mode?target=usb`) mit anschließend sichtbarer Datei auf `E:` verifiziert (T2.11). LCD-Zustände per Foto (Phase 3) |

### T2.5 Upload-Abbruch

1. Große `.3mf` hochladen, bei ca. 50 % den Browser-Tab schließen.

Erwartet: `web: upload connection lost (<n>)`, `drive: upload aborted '<name>' after <n> bytes`;
Datei nicht in der Liste, freier Speicher wie vorher; LCD verlässt `UPLOAD`. Abbrechen
über das Kreuz in der Upload-Warteschlange hat dieselbe Wirkung.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Variante per `api_test.py`: 100 von 300 KB gesendet, Socket geschlossen. Danach kein Busy-Zustand (`upload.active = false`), keine Datei, keine Temp-Datei – auf Flash und microSD |

### T2.6 Speicher voll

1. Laufwerk über den Host fast füllen, dann eine `.3mf` hochladen, die nicht mehr passt.

Erwartet: Meldung „Nicht genug freier Speicher.“ (507 `NO_SPACE`) vor dem Senden der Daten;
keine Temp-Datei; Speicherbalken rot bei < 10 % frei.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Variante per `api_test.py`: `upload/check` mit Größe = frei + 1 MB → 507 `NO_SPACE` (Flash). Auf der 64-GB-Karte liegt diese Größe über 4 GiB → 413 `TOO_LARGE` (T5.4). Roter Speicherbalken nicht geprüft |

### T2.7 Ungültige Datei

1. Eine Textdatei in `test.3mf` umbenennen und hochladen. 2. Eine `.txt` hochladen.

Erwartet: (1) `drive: upload failed 'test.3mf': Die Datei ist unvollständig oder beschädigt (keine gültige 3MF-Datei).`
(422 `INVALID_FILE`), Datei nicht in der Liste. (2) Seit 0.2.0 (Default
`CONFIG_PD_UPLOAD_ALL_TYPES=y`): Die `.txt` wird gespeichert. Nur mit `=n`: Ablehnung
„Dieser Dateityp ist nicht erlaubt …“ (415 `BAD_TYPE`).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Firmware 0.1.0, per `api_test.py`: (1) Zufallsdaten als `.3mf` und abgeschnittenes ZIP → je 422. (2) `.txt` serverseitig → 415 `BAD_TYPE`; Name mit Pfad (`../x.3mf`) → 400. Ablehnung im Browser-Dialog nicht einzeln protokolliert |
| **bestanden** | 2026-11-11 | Firmware 0.2.0, per `api_test.py` (T6.2): (1) unverändert je 422. (2) `.txt` wird angenommen (`types = "all"`). Variante mit `CONFIG_PD_UPLOAD_ALL_TYPES=n` nicht geprüft |

### T2.8 Stromausfall während des Uploads

1. Große `.3mf` hochladen, bei ca. 50 % den Dongle abziehen. 2. Wieder einstecken.

Erwartet: beim Start `drive: removing stale upload temp file (<n> bytes)` (falls der
Verzeichniseintrag schon geschrieben war); Datei nicht in der Liste; Dateisystem mountet
(`FAT ok …`). Freien Speicher notieren (verlorene Cluster möglich, siehe Risiken).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | |

### T2.9 WLAN-Einrichtung per Web, Flash-Modus, Formatieren

1. Über den Setup-AP `http://192.168.4.1` → Einstellungen → Netzwerk → „Speichern und verbinden“.
2. Einstellungen → Gefahrenbereich → Wartungsmodus „Starten …“; danach aus-/einstecken.
3. Einstellungen → Gefahrenbereich → „Formatieren …“, `LÖSCHEN` eintippen (die Oberfläche
   sendet daraufhin `confirm=FORMAT`).

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

## Phase 4 – Bambu Lab X2D (Grundkompatibilität vom Besitzer bestätigt)

Empfohlen für den Drucker: microSD-Karte (FAT32, groß, schnell), Standard-Build. Für X8
zuerst T1.4 (Host nur lesen) am PC prüfen. Bei Problemen an anderen Druckern nacheinander
`CONFIG_TINYUSB_CDC_ENABLED=n` (Composite-Gerät vermeiden), Host nur lesen,
`CONFIG_PD_REPLUG_ON_PROVIDE` umschalten.

Test am 2026-11-12 durch den Besitzer: Firmware 0.2.0 im Standard-Build (Verbundgerät
MSC + CDC-Logport, PID 4003, USB-Gerät darf schreiben), microSD 64 GB FAT32, Bambu Lab X2D.
Ablauf laut Testplan: (1) gesliceste `.gcode.3mf` per Weboberfläche hochladen, Dongle in den
Drucker stecken, Datei am Drucker auswählen und drucken; (2) Dongle im Drucker lassen, vom
Handy eine weitere Datei hochladen, prüfen, ob der Drucker sie sieht.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** (Rückmeldung des Besitzers) | 2026-11-12 | Wörtlich: „es hat am Drucker alles geklappt inklusive Drucken“. Der Drucker hat das USB-Laufwerk erkannt, die hochgeladene `.gcode.3mf` angezeigt und daraus erfolgreich gedruckt. Vom Besitzer gemeldet, vom Entwickler nicht gemessen |
| **Befund** | 2026-11-12 | Schritt (2) laut Rückmeldung bestanden, Einzelschritt nicht separat bestätigt. Am Drucker keine Logs, keine Datenraten, keine Strommessung; LCD-Anzeige am Drucker nicht gemeldet |

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| X1 | Dongle am Drucker: LCD bleibt stabil (kein Neustart-Zyklus), Kopf `USB` | nicht separat gemeldet; Druck lief durch, kein Ausfall gemeldet | 2026-11-12 |
| X2 | Drucker erkennt Speichermedium, Dateiliste zeigt vorhandene Datei | **bestanden** (Besitzer) | 2026-11-12 |
| X3 | Per Web hochgeladene, gesliceste `.gcode.3mf` erscheint am Drucker | **bestanden** (Besitzer) | 2026-11-12 |
| X4 | Druck startet aus dieser Datei | **bestanden** (Besitzer; Druck erfolgreich) | 2026-11-12 |
| X5 | Upload während Drucker angeschlossen → nach Bereitstellen neue Datei sichtbar | laut Rückmeldung bestanden, Einzelschritt nicht separat bestätigt | 2026-11-12 |
| X6 | Übernahme ohne Auswurf (force) – Verhalten des Druckers notieren (kein Druck aktiv!) | **offen**; ebenso „Dateiverwaltung aktivieren“ während eines Drucks | – |
| X7 | Variante ohne CDC (PID 4002) – Unterschied? | **n/a** – Standard-Build mit CDC funktioniert am X2D, Variante nicht nötig | 2026-11-12 |
| X8 | Variante Host nur lesen – Drucker akzeptiert schreibgeschütztes Medium? | **offen** (vorher T1.4) | – |
| X9 | WLAN-Upload-Rate am Drucker-Standort | **offen** (nicht gemessen) | – |
| X10 | Mit microSD: stabile Versorgung, Drucker liest FAT32-Karte (Kapazität notieren) | FAT32-Karte 64 GB gelesen: **bestanden** (Besitzer); Stromaufnahme nicht gemessen, angezeigte Kapazität nicht notiert | 2026-11-12 |

Nicht geprüft: lange Drucke, Nur-Lese-Modus am Drucker, Verhalten bei „Dateiverwaltung
aktivieren“ während eines Drucks, Stromaufnahme, interner Flash (FAT12) als Medium am
Drucker, andere Druckermodelle.

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

Erwartet: 413 `TOO_LARGE` „Die Datei ist zu groß – möglich sind höchstens 4 GB pro Datei.“

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **FEHLER** (behoben) | 2026-11-11 | Größe wurde als 32-Bit-Zahl gelesen: 62 GB + 1 MB liefen über und wurden angenommen. Fix: 64-Bit-Auswertung, ≥ 4 GiB → 413 |
| **bestanden** | 2026-11-11 | nach Fix im API-Test (21/21) |

### T5.5 exFAT-Karte, Formatieren der Karte

1. exFAT-Karte (typisch > 32 GB ab Werk) einlegen und einstecken.
2. Weboberfläche → Einstellungen → Gefahrenbereich → „Formatieren …“, `LÖSCHEN`.

Erwartet: (1) `no valid FAT found`, LCD „Kein Dateisystem! / Im Web formatieren“, Laufwerk
nicht am Host; Weboberfläche „Speicher nicht lesbar“. (2) `drive: formatting microSD card (FAT32)`,
danach `FAT ok (type 3 …)`, Label `DATADRIVE`. Die Format-Anforderung gilt nur für das Medium, auf dem sie gestellt wurde.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **offen** | – | Testkarte war bereits FAT32 |

## Phase 6 – UI-Redesign / WiFi DataDrive (Firmware 0.2.0)

Umbenennung, neue Weboberfläche und API Version 2 ([07_api.md](07_api.md)). Die Sicherungen
des Laufwerks (ein Besitzer, Host-I/O-Sperre, Barrier, Bestätigungsregeln, Schreib-Schutzfrist)
sind unverändert; ihre Prozeduren stehen in Phase 1 und 2.

### T6.1 Umbenennung

1. Firmware 0.2.0 flashen (`.\tools\flash.ps1 -Project printdrive -Build`), Dongle am PC.
2. Log, Geräte-Manager/Datenträgerverwaltung, LCD und `/api/status` ansehen.

Erwartet: `main: WiFi DataDrive 0.2.0`; `/api/status` → `version "0.2.0"`,
`wifi.hostname "datadrive"`, `wifi.ap_ssid "DataDrive-XXXX"`; Weboberfläche unter
`http://datadrive.local`; USB-Strings Hersteller „WiFi DataDrive“, Produkt „DataDrive“;
Datenträger „DataDrv WiFi DataDrive“; LCD-Startbild „WiFi DataDrive“, Host-Zeile mit „Gerät“
(`Gerät: verbunden`, `Kein USB-Gerät`). Neu formatierte Medien heißen `DATADRIVE`, vorhandene
behalten ihr Label. Unverändert: Ordner `firmware/printdrive`, Header `X-PrintDrive`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | Am Gerät geprüft: Produktname, USB-Strings, SCSI-Inquiry, Hostname `datadrive` (`http://datadrive.local`), Setup-WLAN `DataDrive-XXXX`, Label `DATADRIVE` nach Neuformatierung des internen Flashs, LCD-Texte mit „Gerät“. Einzelwerte nicht protokolliert |

### T6.2 API v2 (`tools/api_test.py`)

1. Dongle mit microSD-Karte am PC, Laufwerk bereitgestellt.
2. `python tools\api_test.py <ip> --drive E:` (Details: [04](04_build_und_flash.md) Abschnitt 10).

Erwartet: `28/28 checks passed`. Gegenüber T2.11 neu: beliebiger Dateityp wird angenommen
(`types = "all"`), `mtime` kommt in der Liste unverändert zurück (FAT-Auflösung 2 s),
`/api/files` enthält `dirs` und `truncated`, Umbenennen (Erfolg, ungültiger Zielname 400,
fehlende Datei 404, vorhandenes Ziel 409 `EXISTS`, zurück).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | microSD 64 GB: 28/28, Web-Upload 1,5 MB/s. Übernahme mit Bestätigung, Bereitstellen und identische Datei auf `E:` wie in T2.11 – die Sicherungen des Laufwerks verhalten sich unverändert. Interner Flash mit API v2 nicht gesondert protokolliert |

### T6.3 Auslieferung der Weboberfläche (gzip)

1. Firmware bauen; Größe von `index.html` und `index.html.gz` (Build-Ordner) vergleichen.
2. `http://<ip>/` laden, Ladezeit und Antwort-Header ansehen; `/api/status` → `heap`.

Erwartet: Antwort mit `Content-Encoding: gzip`, die Seite erscheint vollständig ohne weitere
Anfragen an fremde Server. Messwerte, keine Sollwerte.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-11 | `index.html` 60 KB roh → 19 KB in Flash und Übertragung; Seitenaufruf vom Gerät 241 ms; freier Heap unverändert ca. 142 KB; App-Binary 1,06 MB (60 % der App-Partition frei) |
| **bestanden** | 2026-11-12 | Endfassung der Weboberfläche nach dem Browser-Test (T6.4) geflasht: `index.html` 66 KB roh → ca. 20,5 KB gzip. Am Gerät: Seite lädt (241 ms), `api_test.py` 28/28, `http://datadrive.local` wird vom Windows-PC aufgelöst |

### T6.4 Browser-Test der Weboberfläche

Vorgehen: Zustände am Testserver durchspielen (`python tools\mock_server.py --port 8090
--scenario <name>`, Steuerung über `/mock/set`, siehe [04](04_build_und_flash.md)
Abschnitt 11), automatisiert mit `tools/ui_test.py` (Playwright mit dem installierten Edge;
einmalig `pip install playwright`): `python tools\ui_test.py --base http://localhost:8090`.
Danach Stichprobe am echten Gerät. Jeweils am PC und in Handy-Breite, hell und dunkel.

Erwartet: alle Prüfungen bestanden. Laufzeit ca. 19 min für beide Profile.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-12 | 566/566 Prüfungen gegen den Testserver: PC 1280×800 und Handy-Emulation 390×844 (zusätzlich 320 px Breite), hell und dunkel. Abgedeckt: alle Szenarien (USB aktiv/kein USB-Gerät, Dateien, leer, voll, kein Dateisystem, Einrichtungs-WLAN, interner Flash, 300 Dateien, gekürzte Liste); Umschalten in beide Richtungen mit Rückfrage und Abweisung „Gerät schreibt“; Uploads; Dateiaktionen; Suche und Sortierung; alle Abschnitte der Einstellungen; Neustart, Formatieren, Wartungsmodus; Verbindungsverlust und langsames Gerät; Grundlagen der Barrierefreiheit (Namen, Fokus, Touch-Ziele, Kontrast) |
| **FEHLER** (behoben) | 2026-11-12 | Zwei Testrunden, 26 Fehler gefunden und behoben. Die wichtigen: (1) Dialoge überschrieben sich gegenseitig. (2) Datei- und Einstellungsaktionen hingen während eines Uploads ohne Meldung (das Gerät bearbeitet nur eine Anfrage gleichzeitig) – jetzt mit Meldung gesperrt. (3) Abgebrochenes Formatieren ließ das USB-Gerät ohne Laufwerk zurück – jetzt wird wieder bereitgestellt. (4) „Abgebrochen“ bei einer Datei, die tatsächlich gespeichert war – Abbrechen entfällt, sobald die Datei vollständig gesendet ist. (5) Kontrast der roten Knöpfe im dunklen Design. (6) Upload-Leiste und Meldungen verdeckten Inhalt |
| **Befund** (behoben) | 2026-11-12 | Die Dateiliste wurde bei jeder Statusabfrage (alle 3 s) neu aufgebaut: 147 ms bei 300 Dateien. Jetzt nur noch bei Änderungen: unter 1 ms je Abfrage, vollständiger Neuaufbau 45–70 ms |
| **Befund** | 2026-11-12 | Grenzen des Tests: Chromium (Edge) mit iPhone-User-Agent statt echtem Handy-Browser, synthetische Drag-&-Drop-Ereignisse statt Ziehen aus dem Betriebssystem, Testserver statt Gerät |

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| B1 | Hauptansicht „Meine Dateien“ lädt; Statusleiste zeigt Modus („USB-Laufwerk aktiv“ / „Dateiverwaltung aktiv“) und Speicherbelegung (Szenarien `usb_active`, `usb_no_host`, `files`, `flash_medium`) | **bestanden** (Testserver) | 2026-11-12 |
| B2 | Dateiliste: Typ-Symbol, Größe, Änderungsdatum; Suche; Sortierung über Spaltenköpfe (PC) und Auswahlfeld (Handy) | **bestanden** (Testserver) | 2026-11-12 |
| B3 | Aktionsmenü je Datei: Herunterladen, Umbenennen (auch ungültiger und schon vorhandener Name), Löschen mit Rückfrage; Kontextmenü am PC, Bottom Sheet am Handy | **bestanden** (Testserver, auch per Tastatur) | 2026-11-12 |
| B4 | Hochladen per Knopf und Drag & Drop, mehrere Dateien: Warteschlange mit Fortschritt, Abbrechen; „Gespeichert“ erst nach Antwort des Geräts | **bestanden** (Testserver; Drag & Drop nur synthetisch, siehe B16) | 2026-11-12 |
| B5 | Vorhandene Datei: Rückfrage „Vorhandene Datei ersetzen?“ mit Ersetzen/Überspringen | **bestanden** (Testserver) | 2026-11-12 |
| B6 | Upload bei „USB-Laufwerk aktiv“: Rückfrage „Laufwerk vom USB-Gerät trennen?“, danach wieder bereitgestellt; mit ausgeschaltetem „Nach dem Hochladen wieder bereitstellen“ bleibt die Dateiverwaltung aktiv; war sie schon vorher aktiv, wird nicht bereitgestellt | **bestanden** (Testserver) | 2026-11-12 |
| B7 | USB-Gerät schreibt (`host_writing=hold`): Meldung, kein Erzwingen möglich | **bestanden** (Testserver) | 2026-11-12 |
| B8 | Zustände: leer (`empty`), gesperrt (`usb_active`), Speicher nicht lesbar (`no_fs`), fast voll (`full`), keine Antwort vom Laufwerk (`online=0`) | **bestanden** (Testserver, auch langsames Gerät) | 2026-11-12 |
| B9 | Fehlerfälle: Upload scheitert (`fail_next`, `upload_fail_at`), Upload eines anderen Clients (`other_upload=1`) | **bestanden** (Testserver: ungültige 3MF, kein Platz, Verbindungsabbruch) | 2026-11-12 |
| B10 | Hinweise: ausgeblendete Ordner (`dirs`), gekürzte Liste (`truncated`); 300 Dateien flüssig (`many`) | **bestanden** (Testserver; Messwerte siehe Befund oben) | 2026-11-12 |
| B11 | Einstellungen: Netzwerk (WLAN speichern, Einrichtungs-WLAN im Szenario `ap_setup`), Speicher & USB (beide Schalter), Sicherheit, System (Protokoll, Neu starten) | **bestanden** (Testserver) | 2026-11-12 |
| B12 | Gefahrenbereich: Formatieren nur mit getipptem `LÖSCHEN`; Wartungsmodus mit Rückfrage und Hinweis zum Aus-/Einstecken | **bestanden** (Testserver) | 2026-11-12 |
| B13 | Darstellung Automatisch/Hell/Dunkel; Auswahl bleibt nach Neuladen im selben Browser erhalten | **bestanden** (Testserver) | 2026-11-12 |
| B14 | Echter Dongle: Stichprobe B1–B6 und B12 (Wartungsmodus = T2.9 (2)) | **offen**; bisher nur: Endfassung geflasht, Seite lädt, `api_test.py` 28/28, `datadrive.local` (T6.3) | – |
| B15 | Echter Handy-Browser (iOS Safari): Bildschirmtastatur, Safe Areas, Herunterladen (`download`-Attribut), Bottom Sheet | **offen** | – |
| B16 | Echtes Drag & Drop aus dem Explorer/Finder in den Browser | **offen** | – |

Weiter offen (nicht Teil des Browser-Tests): Restpunkte am Bambu Lab X2D (Phase 4, X5–X10), Nur-Lese-Modus
des Hosts auf der Hardware (T1.4), BOOT-Bestätigung per zweitem Druck am Gerät (T2.2). Ordner und
Anmeldung sind seit 0.3.0 umgesetzt, siehe Phase 7 unten.

## Phase 7 – Anmeldung, Ordner, WLANs, Bedienung (Firmware 0.3.0)

Stand 2026-11-12, Commit `9de5179`. Die Prüfungen laufen mit `tools/api_test.py` (Version 3) und
der Rückmeldung des Besitzers. Die Schritte der Oberfläche sind für 0.3.0 nicht einzeln protokolliert.

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| T7.1 | API-Test Version 3 am Gerät (Umfang laut Kopfkommentar von `tools/api_test.py`: Anmeldung, Rollen, Benutzer, vertraute Geräte, Anmeldesperre, Dateien und Ordner, WLAN, Status) | **bestanden** 182/182 | 2026-11-12 |
| T7.2 | Weboberfläche im Browser (Anmeldung, Ordner, Sprache) | **bestanden** laut Rückmeldung des Besitzers; keine Einzelprotokollierung | 2026-11-12 |
| T7.3 | Taste: Klick (QR-Code 20 s), Doppelklick, Wartungsmodus (5 bis 10 s halten), Benutzer-Reset (10 s halten) | **noch nicht geprüft** | – |
| T7.4 | App-Icon auf dem Home-Bildschirm eines Handys | **noch nicht geprüft** | – |
| T7.5 | „Senden an“ für Windows (`tools/sendto`) | **noch nicht geprüft** | – |
| T7.6 | iPhone-Kurzbefehl nach [10_senden.md](10_senden.md) auf einem echten iPhone | **noch nicht geprüft** | – |
| T7.7 | Firmware-Update über die Weboberfläche (ein 0.3.x-Testbuild) | **noch nicht geprüft** | – |
| T7.8 | Bambu X2D erkennt den Stick und druckt von ihm (ohne Verschlüsselung, Version 0.2.0 und 0.3.0) | Rückmeldung des Besitzers, keine Messung | 2026-11-12 |

## Phase 8 – Verschlüsselung, WLAN-Suche, Restarbeiten (Firmware 0.4.0)

Stand 2026-11-12, Commits `60668c9` bis `0de2267`. Verfahren und Grenzen: [12_api_v4.md](12_api_v4.md)
Abschnitt 2.5.

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| T8.1 | AES-256-XTS-Selbsttest beim Start (IEEE-1619-Prüfwerte, beide Wege) | **bestanden** | 2026-11-12 |
| T8.2 | Rechenleistung aus dem Selbsttest | Hardware-Pfad 4,7 MB/s, Bibliothek 0,5 MB/s | 2026-11-12 |
| T8.3 | API-Test Version 4 ohne Formatieren (`tools/api_test.py`) | **bestanden** 250/250 | 2026-11-12 |
| T8.4 | API-Test mit Verschlüsselungslauf (`--crypt-test`): Einrichten, Sperren, falsches Passwort mit Wartezeit, Entsperren mit Passwort und mit Wiederherstellungsschlüssel, Passwort ändern, neuer Wiederherstellungsschlüssel, Formatieren, Ausschalten, Löschen | **bestanden** 334/334 | 2026-11-12 |
| T8.5 | Einrichten auf 64-GB-microSD | **bestanden**; Dauer 51 s | 2026-11-12 |
| T8.6 | Datenrate USB, verschlüsselt: schreiben 820 KB/s (unverschlüsselt vorher 832 KB/s), lesen 659 KB/s | Messung. Vergleich lesen unverschlüsselt: **noch nicht geprüft** | 2026-11-12 |
| T8.7 | Web-Upload per WLAN, verschlüsselt: 1,0 MB/s; Datei danach auf dem PC bitgleich | **bestanden** | 2026-11-12 |
| T8.8 | WLAN-Suche bei bestehender Verbindung: 5 Netze in 2,9 s | **bestanden** | 2026-11-12 |
| T8.9 | WLAN-Suche im Einrichtungsmodus (Handy am Zugangspunkt des Sticks) | **noch nicht geprüft** | – |
| T8.10 | Browser-Test der Weboberfläche gegen den Testserver (`tools/ui_test.py`, Version 4): PC-Profil | **bestanden** 548/548 | 2026-11-12 |
| T8.11 | Browser-Test, Handy-Profil (Emulation) | **bestanden** 561/561 | 2026-11-12 |
| T8.12 | Anzeige „LIO“ auf dem Display (48 × 40 Pixel, Zustandsfarben) | Urteil des Besitzers: gut. Kein Messprotokoll | 2026-11-12 |
| T8.13 | Stromausfall während der Einrichtung oder des Passwortwechsels | **noch nicht geprüft** | – |
| T8.14 | Drucken am Bambu X2D von einem verschlüsselten, per Handy entsperrten Stick | **noch nicht geprüft** | – |
| T8.15 | Verschlüsselung auf dem internen Flash (ohne Karte) | **noch nicht geprüft** | – |
| T8.16 | Unabhängige Prüfung der Verfahren mit `firmware/components/pd_crypt/tools/pd_crypt_check.py` | **noch nicht geprüft** (Ergebnis nicht eingetragen) | – |
| T8.17 | HTTPS (Build-Schalter `PD_HTTPS_OPTION`) | Uploads scheitern mit TLS; Schalter aus. Siehe [14](14_vormerkliste.md). In 0.5.0 umgebaut, siehe T9.11 | 2026-11-12 |

Während der Tests am Gerät gefunden und behoben:

- Abgewiesene Anfragen mit Formulardaten schlossen die Verbindung, statt zu antworten.
- Eine Übernahme direkt nach dem Entsperren wurde durch das Neu-Einstecken des USB-Geräts rückgängig gemacht.

## Phase 9 – Netzlaufwerk, Uhrzeit, automatisches Sperren, HTTPS (Firmware 0.5.0)

Stand 2026-11-12, Commits `da1df2c` bis `af2a675`. Schnittstelle: [15_api_v5.md](15_api_v5.md).
Version 0.5.0 entstand in sieben parallelen Arbeitspaketen. Der erste gemeinsame Build lief ohne
Fehler durch. Gerät: LILYGO T-Dongle-S3 (LCD-Variante); bei den Läufen war das Laufwerk
verschlüsselt und entsperrt. Welches Speichermedium (microSD oder interner Flash) dabei steckte,
ist nicht protokolliert (noch nicht geprüft).

Aufruf des API-Tests, Beispiel (Version 3 bis 5; nur Version 5: zusätzlich `--only v5`; die
genaue Befehlszeile des Laufs ist nicht protokolliert):

```powershell
python tools\api_test.py <ip> --user admin --password <Passwort> --drive-password <Laufwerks-Passwort>
```

### T9.1 API-Test am Gerät

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **FEHLER** (behoben) | 2026-11-12 | Erster Lauf am Gerät: 349/355, sechs Abweichungen. Ursachen: (1) Die Einstellung für das automatische Sperren verlangte beide Parameter, statt einen allein anzunehmen. (2) Ein nicht plausibler Uhrzeit-Wert wurde stillschweigend ignoriert statt mit `400` abgewiesen. (3) Eine falsche Erwartung im Test selbst. Alle drei behoben (Commit `8417f9e`) |
| **bestanden** | 2026-11-12 | 354/354. Übersprungen (6): Schutz des letzten Administrators, Test-WLAN, OTA-Probe, Prüfungen, die ein unverschlüsseltes Laufwerk brauchen, HTTPS-Felder (der Lauf war vor Commit `af2a675`, der HTTPS in den Build nimmt), der Zeitablauf des automatischen Sperrens |

### T9.2 bis T9.14 Einzelpunkte

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| T9.2 | Netzlaufwerk (WebDAV) per API-Test: Liste (`PROPFIND`), Hochladen (`PUT`), Verschieben (`MOVE`), Löschen (`DELETE`), Anmeldung nötig (`401`), abgewiesen bei gesperrtem Laufwerk (`423`) | **bestanden** (API-Test) | 2026-11-12 |
| T9.3 | Netzlaufwerk mit echten Programmen: Windows-Explorer (mit `tools/webdav/Connect-DataDrive.ps1`), iPhone-App mit WebDAV, Android, macOS-Finder | **noch nicht geprüft** | – |
| T9.4 | Uhrzeit aus dem Netz (NTP): Uhr nach dem Start gestellt | **bestanden** | 2026-11-12 |
| T9.5 | Uhrzeit ohne Internet: Browser stellt die Uhr nach dem Anmelden; nach Stromausfall Uhr unbekannt, bis das Netz wieder da ist | **noch nicht geprüft** am Gerät (Verhalten laut [15](15_api_v5.md) Abschnitt 1) |
| T9.6 | Automatisches Sperren: Einstellungen setzen, einzeln und gemeinsam, ungültige Werte abgewiesen | **bestanden** (API-Test) | 2026-11-12 |
| T9.7 | Automatisches Sperren: Laufwerk sperrt nach Ablauf der Zeit; beim Trennen des USB-Geräts; nicht während eines Schreibvorgangs | **noch nicht geprüft** | – |
| T9.8 | Display-Sprache: Einstellung `de`/`en` per API | **bestanden** (API-Test) | 2026-11-12 |
| T9.9 | Display-Sprache: Aussehen der englischen Texte auf dem LCD | **noch nicht geprüft** | – |
| T9.10 | Einrichtungsseite öffnet sich von selbst (Captive Portal), mit einem echten Handy am Einrichtungs-WLAN | **noch nicht geprüft** | – |
| T9.11 | HTTPS am Gerät (Schalter eingeschaltet, Laufwerk verschlüsselt und entsperrt): Upload, Download, Dateiliste, Seitenaufruf, Speicher | **bestanden**; Messwerte unten | 2026-11-12 |
| T9.12 | Abgebrochenen Upload fortsetzen: Abbruch, Fortsetzen ab dem gemeldeten Stand, falscher Stand (`409 RESUME_MISMATCH`) | **bestanden** (API-Test) | 2026-11-12 |
| T9.13 | Ordner als ZIP: verschachtelte Ordner, Umlaute, Inhalt und Prüfsummen | **bestanden** (API-Test) | 2026-11-12 |
| T9.14 | Ganze Ordner hochladen in der Weboberfläche | nur gegen den Testserver geprüft; am Gerät **noch nicht geprüft** | – |
| T9.15 | Browser-Test der Weboberfläche Version 5 (`tools/ui_test.py`, PC- und Handy-Profil) | **noch nicht geprüft** (kein Ergebnis eingetragen) | – |
| T9.16 | Unabhängige Sicherheitsprüfung der Neuerungen von 0.5.0 | läuft, Ergebnis steht aus | – |

### T9.11 Messwerte HTTPS

| Messung | Wert |
|---|---|
| Upload 30 MB über HTTPS | 864 kB/s; Datei nach dem Herunterladen bitgleich |
| Download über HTTPS | 389 kB/s |
| Download über HTTP (Vergleich) | 487 kB/s |
| Dateiliste und Seitenaufruf über HTTPS | funktionieren (Zeiten nicht gemessen) |
| Freier Heap im Leerlauf | 66 KiB |
| Tiefster freier Heap während des Uploads | 22 KiB |
| Fehlgriffe beim reservierten TLS-Puffer | keine |

Umbau gegenüber 0.4.0: Die TLS-Puffer werden einmal beim Start reserviert, es wird nur eine
TLS-Verbindung gleichzeitig bedient, und die Dateiliste wird ohne den 33-KiB-Puffer direkt
gesendet. Danach wurde `PD_HTTPS_OPTION` im Build eingeschaltet (Commit `af2a675`); der Schalter
in den Einstellungen bleibt aus, bis der Benutzer ihn einschaltet.

Noch nicht geprüft: iPhone mit installiertem Zertifikat (Profil installieren und volles Vertrauen
unter „Zertifikatsvertrauenseinstellungen“), Home-Bildschirm-Symbol und Kurzbefehl über `https`,
WebDAV über `https`, Dauerbetrieb. Ob der API-Test mit eingeschaltetem HTTPS läuft, ist ebenfalls
noch nicht geprüft (er baut seine Adressen mit `http://`).

### Weiter offen aus 0.4.0

| Nr. | Prüfpunkt | Ergebnis |
|---|---|---|
| T8.14 | Drucken am Bambu X2D von einem verschlüsselten, per Handy entsperrten Stick | **noch nicht geprüft** (der Besitzer erwartet, dass es geht) |
| T7.7 | Firmware-Update über die Weboberfläche | **noch nicht geprüft** |
| T8.9 | WLAN-Suche im Einrichtungsmodus | **noch nicht geprüft** |
| T8.13 | Stromausfall während der Einrichtung der Verschlüsselung | **noch nicht geprüft** |

## Phase 10 – Update über den Browser, signierte Updates, Restzeit, Netzwerk (Firmware 0.6.0)

Stand 2026-11-13. Schnittstelle: [16_api_v6.md](16_api_v6.md), signierte Updates:
[17_signierte_updates.md](17_signierte_updates.md). Gerät: LILYGO T-Dongle-S3 (LCD-Variante),
microSD 64 GB, Laufwerk verschlüsselt. Welcher Commit genau geflasht war und mit welcher
Befehlszeile des API-Tests: `python tools/api_test.py <Gerät> --user <admin> --password … --drive E: --drive-password … --ota-probe` über HTTP (HTTPS für den Lauf ausgeschaltet).

### T10.1 Update über die Weboberfläche (0.5.0 → 0.6.0)

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-13 | Die laufende 0.5.0 nahm die signierte 0.6.0 über die Weboberfläche an: angenommen nach 13 s, Neustart in den zweiten Firmware-Bereich, nach 15 s als gut bestätigt (Rücksprung abgebrochen). Erstes am Gerät geprüftes Web-Update; damit ist T7.7 erledigt. Ob die Verbindung dabei HTTP oder HTTPS war, ist nicht protokolliert |

### T10.2 Signierte Updates

Nummern der Schritte wie in [17_signierte_updates.md](17_signierte_updates.md) Abschnitt 9. Die
Schlüssel-ID des Geräts steht hier bewusst nicht.

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| T10.2a | Schritt 1: signierte 0.6.0 auf 0.5.0 über die Weboberfläche; danach `/api/status` | **bestanden**: siehe T10.1; `ota.signed_required: true`, `ota.key_id` = ID des Schlüssels des Besitzers | 2026-11-13 |
| T10.2b | Schritt 2 oder 3: unsignierte 0.6.0 einspielen | **bestanden**: `422 UNSIGNED`; Gerät lief ohne Neustart weiter. Beide unsignierten Dateien wurden geprüft (normaler Build `build\printdrive.bin` und `build_signed\printdrive-unsigned.bin`), beide `422 UNSIGNED`. | 2026-11-13 |
| T10.2c | Schritt 4: dieselbe 0.6.0 mit einem anderen Schlüssel signiert | **bestanden**: `422 BAD_SIGNATURE`; Gerät lief ohne Neustart weiter | 2026-11-13 |
| T10.2d | Schritte 5 bis 12: veränderte Datei, Text- oder Bilddatei, erneutes Einspielen, Rücksprung nach Abziehen, Flashen per USB mit `-Signed`, Schutz von `flash.ps1`, Build ohne Schlüssel, Update über HTTPS | **noch nicht geprüft** | – |

### T10.3 API-Test am Gerät

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| **bestanden** | 2026-11-13 | 415/415. Übersprungen (7): Schutz des letzten Administrators, Test-WLAN, Prüfungen, die ein unverschlüsseltes Laufwerk brauchen, das Sperren selbst nach Ablauf der Zeit, der Zustand `waiting`, die Prüfungen von Maske und Subnetz und das Übernehmen von Netzwerkeinstellungen (beide nur mit `--network-test`) |

### T10.4 bis T10.13 Einzelpunkte

| Nr. | Prüfpunkt | Ergebnis | Datum |
|---|---|---|---|
| T10.4 | Restzeit bis zum Sperren (`autolock.remaining_s`) und `POST /api/autolock/touch` | **bestanden**: Sperrzeit 15 Minuten, nach wenigen Sekunden `remaining_s` 895, nach `touch` 900 | 2026-11-13 |
| T10.5 | Laufwerk sperrt nach Ablauf der Zeit (= T9.7) | **noch nicht geprüft** | – |
| T10.6 | Zustand `waiting` (Sperren aufgeschoben, solange das USB-Gerät das Laufwerk hält oder schreibt) | **noch nicht geprüft** | – |
| T10.7 | `GET /api/network` | **bestanden**: DHCP-Modus, aktuelle Adresse, Maske, Gateway, DNS und Gerätename | 2026-11-13 |
| T10.8 | Feste Adresse einstellen, zurück auf DHCP, Gerätename ändern | **noch nicht geprüft** (bewusst: ein Fehler bräuchte die 10-s-Taste vor Ort) | – |
| T10.9 | Taste 10 s halten setzt Benutzer und Netzwerkeinstellungen zurück | **noch nicht geprüft** | – |
| T10.10 | Freier Heap mit HTTPS an, Laufwerk entsperrt | 67 KiB im Leerlauf (0.5.0: 66 KiB, T9.11). Tiefster Wert während eines Uploads mit 0.6.0 nicht gemessen | 2026-11-13 |
| T10.11 | Browser-Test der Weboberfläche gegen den Testserver (`tools/ui_test.py`) | **bestanden**: PC 872/872, Handy-Profil 889/889 | 2026-11-13 |
| T10.12 | Nach dem Ändern der Sperrzeit meldet die Oberfläche „Stick nicht erreichbar“ | **FEHLER** (behoben): Die Seite schickte parallele Anfragen, der Stick bedient mit HTTPS nur eine Verbindung. Fix: eine Warteschlange für die ganze Seite (eine Anfrage zur Zeit, Anfragen, die gefahrlos wiederholt werden können, einmal still wiederholt). Nachgestellt und als behoben geprüft mit einem Proxy-Modell, das nur eine Verbindung annimmt. Am iPhone vom Besitzer **noch nicht nachgeprüft** | 2026-11-13 |
| T10.13 | Download oder ZIP-Download über HTTPS, währenddessen Statusabfrage der Oberfläche | **Befund** (offen): Downloads öffnen eine eigene Verbindung im Browser und laufen nicht durch die Warteschlange; eine Statusabfrage während eines langen Downloads kann diese Verbindung noch schließen. Am Gerät **noch nicht geprüft** | – |

**Hinweis: Regel nach dem Gerätetest geändert.** Nach den Tests dieser Phase wurde die Zeitregel des
automatischen Sperrens geändert: Sie läuft nur noch, solange die Dateiverwaltung das Laufwerk hat. Ist es
dem USB-Gerät bereitgestellt, gibt es keinen Countdown (`remaining_s` -1) und kein Sperren nach Zeit; der
Wechsel zurück in die Dateiverwaltung startet die Frist neu. Lesen und Schreiben des USB-Geräts zählen
nicht mehr als Aktivität. Entfallen sind „ab 10 Minuten nimmt die Zeitregel einem untätigen USB-Gerät das
Laufwerk weg“ und „bei 1 bis 9 Minuten wartet sie (`waiting`)“; `waiting` heißt nur noch: Zeit um, letzter
Sperrversuch abgewiesen (beschäftigt). `on_detach` ist unverändert. T10.3, T10.4 und T10.11 liefen mit der
Regel davor. Die neue Regel ist gegen den Testserver geprüft (API-Test, Browser-Test PC 878/878,
Handy-Profil 895/895) und am Gerät **bestanden** (2026-11-13, signierte 0.6.0 per Web-Update installiert: Dateiverwaltung mit 5 Minuten 294 s, USB-Modus `remaining_s` -1 und „Zeit zurücksetzen“ ohne Wirkung, zurück in der Dateiverwaltung 293 s; danach API-Test 415/415, 8 übersprungen). T10.5 und T10.6 gelten mit der neuen Regel.

### Nachträge zu früheren Punkten

| Nr. | Prüfpunkt | Stand nach 0.6.0 |
|---|---|---|
| T7.7 | Firmware-Update über die Weboberfläche | erledigt durch T10.1 |
| T9.7 | Automatisches Sperren am Gerät | Restzeit geprüft (T10.4); das Sperren selbst, beim Trennen des USB-Geräts und nicht während eines Schreibvorgangs **noch nicht geprüft** |

### Weiter offen nach 0.6.0

| Nr. | Prüfpunkt | Ergebnis |
|---|---|---|
| T10.8 | Netzwerkeinstellungen am Gerät ändern | **noch nicht geprüft** |
| T10.5 | Sperren nach Ablauf der Zeit | **noch nicht geprüft** |
| T10.12 | Warteschlange am iPhone nachprüfen | **noch nicht geprüft** |
| T8.14 | Drucken am Bambu X2D von einem verschlüsselten, per Handy entsperrten Stick | **noch nicht geprüft** |
| T9.10 | Einrichtungsseite mit einem echten Handy | **noch nicht geprüft** |
| T9.9 | Englische Texte auf dem LCD | **noch nicht geprüft** |
| T9.11 | HTTPS: Zertifikat auf dem iPhone, Dauerbetrieb über viele Stunden | **noch nicht geprüft** |
| T9.3 | Netzlaufwerk mit dem Windows-Explorer (und anderen Programmen) | **noch nicht geprüft** |
| T8.13 | Stromausfall während der Einrichtung der Verschlüsselung | **noch nicht geprüft** |
