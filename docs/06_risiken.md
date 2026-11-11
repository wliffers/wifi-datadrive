# 06 – Risiken und Annahmen

Stand: 2026-11-11. Bewertung: **hoch** = kann das Projektziel verhindern,
**mittel** = erfordert Nacharbeit, **niedrig** = Komfort/Randfall.

## 1. Drucker-Kompatibilität (Bambu Lab X2D) – nicht verifiziert

| Risiko | Bew. | Details | Gegenmaßnahme / Prüfung |
|---|---|---|---|
| Dateisystem-Erwartung | hoch | Firmware formatiert MBR + **FAT16**. Ob der X2D FAT16 akzeptiert oder FAT32/exFAT erwartet, ist unbekannt. FAT32 ist bei ca. 11 MB nicht normgerecht erzeugbar. | Test X2 in [05](05_testprotokoll.md); notfalls microSD (Phase 5) mit FAT32/exFAT |
| Kapazität | hoch | Nutzbar ca. 10–11 MB. Viele `.3mf` (mit Vorschaubildern, Mehrplatten-Projekten) sind größer. | Dateigrößen typischer Druckaufträge erfassen; microSD als Ausweg |
| Sektorgröße | mittel | Host sieht 512-B-Sektoren (gängig für USB-Sticks). Annahme: unproblematisch. | Test X2 |
| Composite-Gerät mit CDC | mittel | MSC + CDC-Log-Port in einem Gerät könnte einen Drucker-USB-Stack verwirren. | Für den Drucker-Test `CONFIG_TINYUSB_CDC_ENABLED=n` (PID wird 4002, kein 1200-Baud-Touch) |
| Stromaufnahme | mittel | Deskriptor meldet 500 mA; WLAN-Spitzen beim Senden. Hersteller nennt max. 800 mA. Ob der Drucker-USB-Port genug liefert, ist offen. | Test X1 (Neustart-Zyklen?), ggf. WLAN-Sendeleistung reduzieren |
| Medienwechsel | mittel | Unklar, ob der Drucker UNIT ATTENTION / Soft-Re-Plug korrekt verarbeitet und neue Dateien sieht. | Test X5; Varianten mit/ohne `CONFIG_PD_REPLUG_ON_PROVIDE` |
| Druckabbruch durch Übernahme | mittel | „Übernehmen“ (force) entzieht dem Drucker das Medium. Liest der Drucker während des Drucks vom Stick, bricht der Druck ab. | Weboberfläche warnt; am Drucker vor der Übernahme auswerfen. Verhalten beim X2D offen |
| Full Speed | niedrig | USB 1.1 Full Speed (12 Mbit/s brutto). Laden großer Dateien am Drucker dauert. | Messung T2.10 |

## 2. Dateiformat

- Der X2D druckt (Annahme) nur **gesliceste** Dateien (`.gcode.3mf` aus Bambu Studio).
  Ein reines Projekt-`.3mf` ohne G-Code ist am Drucker nicht druckbar, wird von der
  Firmware aber genauso angenommen (Prüfung nur auf ZIP-Struktur, nicht auf Inhalt).
- `.gcode` ist standardmäßig gesperrt (`CONFIG_PD_ALLOW_GCODE=n`).

## 3. Abhängigkeit von esp_tinyusb-Interna

| Punkt | Risiko |
|---|---|
| Linker-`--wrap` von `tud_msc_read10_cb`, `tud_msc_write10_cb`, `tud_msc_test_unit_ready_cb` | Funktioniert nur, solange esp_tinyusb diese Callbacks stark definiert und TinyUSB keine schwache Default-Version hat. Bei Umbenennung/Umbau greift die Host-I/O-Sperre still nicht mehr. |
| Barrier über `usbd_defer_func` (`device/usbd_pvt.h`, privater TinyUSB-Header) | Setzt voraus, dass esp_tinyusb Host-Schreibzugriffe per `usbd_defer_func` in die TinyUSB-Task verschiebt (so in 2.4.0). |
| Überschriebene schwache Callbacks (`tud_msc_scsi_complete_cb`, `tud_msc_is_writable_cb`, `tud_msc_inquiry2_cb`) | Würde esp_tinyusb sie künftig selbst definieren → Linkfehler (gut sichtbar). |
| `auto_mount_off` | esp_tinyusbs Auswurf-Handler (`tud_msc_start_stop_cb`) mountet nur ohne dieses Flag selbst um. |

**Regel:** Bei jedem Update von `esp_tinyusb` oder `tinyusb` (Versionen sind gepinnt)
diese Punkte im Quellcode der Komponente prüfen und T1.3, T2.2, T2.4 wiederholen.

## 4. Firmware-Verhalten (unverifiziert)

| Risiko | Bew. | Details |
|---|---|---|
| Download-Modus per Software | mittel | 1200-Baud-Touch, Web „Flash-Modus“ und BOOT 5 s nutzen `RTC_CNTL_FORCE_DOWNLOAD_BOOT` (wie Arduino-Core). Noch nicht getestet. Rückfall: BOOT halten + einstecken – das funktioniert immer. |
| Windows bei Medienwechsel | mittel | Wie Windows auf „kein Medium“ ↔ Medium und Soft-Re-Plug reagiert (Explorer-Fenster, Cache, Meldungen), ist ungeprüft. Ebenso, ob „Auswerfen“ tatsächlich START STOP UNIT mit LOEJ sendet (Annahme). |
| Schreibgeschwindigkeit WL SAFE | mittel | SAFE-Modus (stromausfallsicher) schreibt langsamer als PERF (Espressifs Beispiel). Werte unbekannt; Messung T2.10. |
| Barrier-Timeout | niedrig | Bei sehr vielen ausstehenden Host-Schreibzugriffen könnte die Barrier (2 s) ablaufen → Umschaltung wird abgebrochen (sicher, aber lästig). |
| Erststart-Erkennung über NVS | niedrig | „Erster Start“ heißt: NVS-Flag `fmt_done` fehlt. Wird das NVS gelöscht (Format-Wechsel in `app_main`, `erase-flash` der NVS-Partition) und ist das FAT gleichzeitig unlesbar, formatiert die Firmware ohne Rückfrage. |
| FAT bei Stromausfall | mittel | Stromausfall während eines Schreibvorgangs (Host oder Upload) kann **verlorene Cluster** oder inkonsistente FAT-Einträge hinterlassen. Die Upload-Temp-Datei wird zwar gelöscht, verlorene Cluster aber nicht zurückgewonnen. Reparatur: `chkdsk X: /f` am PC (Host schreibend) oder Formatieren. |
| Boot-Politik | niedrig | Beim Start wird ein vorhandener Host immer bedient, auch wenn `CONFIG_PD_PROVIDE_ON_ATTACH=n`. |
| Logs vor CDC-Verbindung | niedrig | Der CDC-Port verwirft Logs, solange kein Terminal offen ist; Startmeldungen nur über `/api/log` (8 KB) oder UART0. |

## 5. Sicherheit

| Risiko | Bew. | Details |
|---|---|---|
| Keine Authentifizierung | mittel | Jeder im selben (W)LAN kann Dateien hochladen, löschen, formatieren, neu starten und den Flash-Modus auslösen. Nur für vertrauenswürdige Heimnetze gedacht. |
| CSRF | niedrig | POST-Anfragen brauchen `X-PrintDrive: 1`. Fremde Webseiten können diesen Header ohne CORS-Freigabe nicht setzen; GET-Endpunkte (Status, Liste, Download, Log) sind ungeschützt lesbar. |
| WLAN-Zugangsdaten | mittel | STA-Passwort und AP-Passwort liegen **unverschlüsselt** im NVS (keine NVS-Verschlüsselung, kein Flash Encryption). Wer den Dongle hat, kann sie per esptool auslesen. Gleiches gilt für Flash-Backups in `backup\`. |
| Setup-AP | niedrig | WPA2 mit gerätespezifischem 10-Zeichen-Passwort; das Passwort steht jedoch offen auf dem LCD und im Log (`setup access point on: …`). |
| Kein HTTPS | niedrig | Übertragung im LAN unverschlüsselt. |

## 6. Annahmen (Zusammenfassung)

1. Pinbelegung und LCD-Parameter aus dem Hersteller-Repo stimmen für dieses Exemplar (Prüfung T0.3/T0.4).
2. UART0 ist an GPIO43/44 (QWIIC) erreichbar.
3. Der ROM-Bootloader meldet sich nach Software-Neustart in den Download-Modus als 303A:1001.
4. Windows sendet beim Auswerfen START STOP UNIT mit LOEJ = 1, START = 0.
5. Der X2D liest FAT16 von einem USB-MSC-Gerät mit 512-B-Sektoren.
6. Der X2D druckt gesliceste `.gcode.3mf` vom USB-Stick.
