# 05 – Testprotokoll

Prozeduren für die manuelle Prüfung auf der Hardware. Erwartete Log-Zeilen sind die
tatsächlichen `ESP_LOG`-Texte aus dem Quellcode; im Log stehen sie mit Präfix, z. B.
`I (5123) drive: owner: USB host (read/write)`. Platzhalter in `<…>`.

**Log-Quellen der Hauptfirmware:** `.\tools\monitor.ps1` (CDC-Port; gibt nur aus, solange
der Port offen ist – Startmeldungen gehen dort verloren), `http://<ip>/api/log` bzw.
Weboberfläche → Einstellungen → „Log anzeigen“ (letzte 8 KB, enthält auch den Start),
UART0. Smoke-Test: `.\tools\idf.ps1 smoke_test -p COM4 monitor`.

Ergebnis eintragen als **OK**, **FEHLER** (mit Bemerkung) oder **n/a**.

## Phase 0 – Smoke-Test

### T0.1 Flash-Backup

1. Werks-Firmware läuft (COM4). `.\tools\backup_flash.ps1 -Port COM4`.

Erwartet: `flash_id` zeigt Manufacturer `ef`, Device `4018`; Fortschritt bis `0x1000000 / 0x1000000`;
`wrote 16777216 bytes to …`; Datei in `backup\`, Zeile in `backup\SHA256SUMS.txt`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

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
| | | |

### T0.3 LCD und LED

1. Nach dem Flashen bzw. Reset das Display beobachten.

Erwartet: vier Vollbilder je 0,6 s mit Text `ROT`, `GRUEN`, `BLAU`, `WEISS` in genau diesen
Farben, Text oben links lesbar, nicht gespiegelt, kein Rand/Versatz. LED gleichzeitig rot,
grün, blau, aus. Log `SMOKE: LCD ok (check colours: red, green, blue, white)`.
Danach Statusbild mit Kopf `SMOKE TEST` und `Flash 16 MB`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T0.4 BOOT-Taste

1. BOOT-Taste drücken und halten, dann loslassen.

Erwartet: unterste Zeile `BOOT gedrückt` (grün) statt `BOOT: drücken`; LED bekommt einen
Grünanteil; im 5-s-Log `button=1`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

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
| | | |

## Phase 1 – USB-Laufwerk unter Windows

### T1.1 Erststart der Hauptfirmware

1. `.\tools\flash.ps1 -Project printdrive` (ggf. BOOT halten + einstecken). Dongle am PC lassen.
2. Log über die Weboberfläche bzw. `/api/log` ansehen.

Erwartet: `main: WiFi PrintDrive 0.1.0`, `board: board init done, flash size 16777216 bytes`,
`drive: storage partition @0x510000, 11468800 bytes raw, WL sector 512, WL size <n>`,
`drive: no valid FAT found (<code>)`, `drive: first boot: creating file system`,
`drive: formatting storage (FAT, 2048 byte clusters)`,
`drive: FAT ok (type 2: 1=FAT12 2=FAT16 3=FAT32, label 'PRINTDRIVE')`,
`usb_dev: TinyUSB installed (MSC + CDC log)`, `drive: host present at boot -> providing drive`,
`drive: owner: USB host (read/write)`, `main: start-up complete`. LCD: `BOOT`, dann `USB` (grün).
Zweiter Start: kein `first boot`, `FAT ok (type 2 …)`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T1.2 Laufwerk im Explorer

1. Explorer öffnen, Eigenschaften des neuen Laufwerks.
2. Geräte-Manager: Anschlüsse (COM & LPT).

Erwartet: Laufwerk `PRINTDRIVE`, Dateisystem FAT, Kapazität ca. 10–11 MB, leer.
Zusätzlicher COM-Port (USB-Serial, 303A:4003). LCD-Host-Zeile `Host: verbunden`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

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
| | | |

### T1.4 Nur-Lese-Modus des Hosts

1. Web → Einstellungen → „Host darf auf das Laufwerk schreiben“ abwählen.
2. „Am USB bereitstellen“ (bzw. aus-/einstecken). Datei auf das Laufwerk kopieren.

Erwartet: `drive: owner: USB host (read-only)`; LCD `Host: nur lesen`; Windows meldet
Schreibschutz, Lesen funktioniert. Danach Einstellung wieder aktivieren.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

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
| | | |

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
| | | |

### T2.3 Umschalten während eines Uploads

1. Größere `.3mf` hochladen, währenddessen BOOT kurz drücken (oder in zweitem Tab bereitstellen).

Erwartet: LCD `Upload läuft` bzw. 409 `BUSY` „Upload oder Download läuft gerade“; Upload läuft fertig.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.4 Auswurf am Host → Upload-Modus

1. Laufwerk bereitstellen, im Explorer auswerfen.

Erwartet: `drive: host ejected the drive`, `drive: owner: APP (web upload possible), …`; LCD `BEREIT`/`WLAN`.
Danach „Am USB bereitstellen“: `usb_dev: soft re-plug`, Laufwerk erscheint wieder.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.5 Upload-Abbruch

1. Große `.3mf` hochladen, bei ca. 50 % den Browser-Tab schließen.

Erwartet: `web: upload connection lost (<n>)`, `drive: upload aborted '<name>' after <n> bytes`;
Datei nicht in der Liste, freier Speicher wie vorher; LCD verlässt `UPLOAD`.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.6 Speicher voll

1. Laufwerk über den Host fast füllen, dann eine `.3mf` hochladen, die nicht mehr passt.

Erwartet: Meldung „Nicht genug freier Speicher“ (507 `NO_SPACE`) vor dem Senden der Daten;
keine Temp-Datei; Speicherbalken rot bei < 10 % frei.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.7 Ungültige Datei

1. Eine Textdatei in `test.3mf` umbenennen und hochladen. 2. Eine `.txt` hochladen.

Erwartet: (1) `drive: upload failed 'test.3mf': Datei ist beschädigt oder kein gültiges 3MF (ZIP)`
(422 `INVALID_FILE`), Datei nicht in der Liste. (2) Browser lehnt ab: „Dateityp nicht erlaubt“.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.8 Stromausfall während des Uploads

1. Große `.3mf` hochladen, bei ca. 50 % den Dongle abziehen. 2. Wieder einstecken.

Erwartet: beim Start `drive: removing stale upload temp file (<n> bytes)` (falls der
Verzeichniseintrag schon geschrieben war); Datei nicht in der Liste; Dateisystem mountet
(`FAT ok …`). Freien Speicher notieren (verlorene Cluster möglich, siehe Risiken).

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.9 WLAN-Einrichtung per Web, Flash-Modus, Formatieren

1. Über den Setup-AP `http://192.168.4.1` → Einstellungen → WLAN speichern.
2. Einstellungen → „Flash-Modus“; danach aus-/einstecken.
3. Einstellungen → „Formatieren …“, `FORMAT` eingeben.

Erwartet: (1) `pd_wifi: new credentials stored for '<ssid>'`, dann `connected to …`; AP geht
nach ca. 120 s aus (`setup access point off`). (2) `board: restarting into ROM download mode`; Laufwerk und
CDC-Port verschwinden, 303A:1001 erscheint (Software-Weg bisher unverifiziert); nach
Aus-/Einstecken normaler Start. (3) `drive: format requested - restarting`, nach Neustart
`drive: formatting storage (FAT, 2048 byte clusters)`, Laufwerk leer.

| Ergebnis | Datum | Bemerkung |
|---|---|---|
| | | |

### T2.10 Geschwindigkeit

1. Web-Upload einer ca. 8-MB-Datei; Wert aus `web: upload … (<x> KB/s)` notieren.
2. Kopieren derselben Datei per Explorer auf das Laufwerk; Dauer stoppen.

Erwartet: Messwerte, keine Sollwerte (offen). USB ist Full Speed: theoretisch max.
12 Mbit/s brutto, praktisch deutlich weniger; WL-SAFE-Modus bremst Schreiben zusätzlich.

| Web KB/s | USB schreiben KB/s | USB lesen KB/s | Datum |
|---|---|---|---|
| | | | |

## Phase 4 – Bambu Lab X2D (nicht verifiziert)

Vorbereitung: zuerst ohne Änderungen testen; bei Problemen nacheinander
`CONFIG_TINYUSB_CDC_ENABLED=n`, Host nur lesen, `CONFIG_PD_REPLUG_ON_PROVIDE` umschalten.

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
