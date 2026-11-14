# 06 – Risiken und Annahmen

Stand: 2026-11-13, Firmware 0.6.0. Die Risiken aus 0.2.0 gelten, soweit unten nichts anderes
steht; Abschnitt 8 behandelt die Verschlüsselung (0.4.0), Abschnitt 9 die Neuerungen von 0.5.0,
Abschnitt 10 die von 0.6.0.
Die unabhängige Sicherheitsprüfung der Neuerungen von 0.5.0 ist abgeschlossen: kein kritischer Befund; ein schwerer und mehrere mittlere Befunde (Dateiliste hielt das Laufwerks-Schloss während des Sendens, automatisches Sperren konnte einem arbeitenden Drucker das Laufwerk nehmen, WebDAV-Überschreiben in mehreren Schritten u. a.) sind behoben und am Gerät mit dem API-Test geprüft.
und ist hier nicht eingearbeitet. Bewertung: **hoch** = kann das Projektziel verhindern,
**mittel** = erfordert Nacharbeit, **niedrig** = Komfort/Randfall.

Das Projekt heißt seit Version 0.2.0 „WiFi DataDrive“ (vorher „WiFi PrintDrive“); interne
Namen wie `firmware/printdrive`, `X-PrintDrive` und `pd_` bleiben. Mit der Umbenennung kamen
eine neue Weboberfläche und die API Version 2 ([07_api.md](07_api.md)); die Sicherungen des
Laufwerks sind unverändert. Neue Risiken daraus stehen in den Abschnitten 2, 4 und 5.

## 1. Drucker-Kompatibilität (Bambu Lab X2D) – Grundkompatibilität verifiziert

Am 2026-11-12 hat der Besitzer den Dongle am X2D betrieben (Firmware 0.2.0, Standard-Build
MSC + CDC, USB-Gerät darf schreiben, microSD 64 GB FAT32): Laufwerk erkannt, per Web
hochgeladene `.gcode.3mf` angezeigt und erfolgreich gedruckt ([05](05_testprotokoll.md)
Phase 4). Grundlage ist seine Rückmeldung, keine Messung des Entwicklers; Logs, Datenraten
und Stromaufnahme wurden am Drucker nicht erfasst. Das Kompatibilitätsrisiko sinkt damit von
offen auf **niedrig (Restrisiko)**: lange Drucke, Nur-Lese-Modus, Übernahme während eines
Drucks, interner Flash und andere Druckermodelle sind ungeprüft.

| Risiko | Bew. | Details | Gegenmaßnahme / Prüfung |
|---|---|---|---|
| Dateisystem-Erwartung | niedrig (mit microSD) | microSD: **FAT32** – am X2D akzeptiert (64-GB-Karte, 2026-11-12), unter Windows 11 verifiziert. Interner Flash: MBR + **FAT12** (4-KiB-Cluster; mit `CONFIG_PD_FAT_CLUSTER_KB=2` FAT16, aber langsam). Ob der X2D FAT12/FAT16 akzeptiert, ist unbekannt (nicht geprüft); FAT32 ist bei ca. 11 MB nicht normgerecht erzeugbar. exFAT wird nicht unterstützt (Abschnitt 4). | Für den Drucker microSD (FAT32) verwenden; interner Flash am Drucker offen |
| Kapazität | niedrig (mit microSD) | Interner Flash: nutzbar 11.288.576 Byte – viele `.3mf` (mit Vorschaubildern, Mehrplatten-Projekten) sind größer. Mit microSD entschärft (64-GB-Karte verifiziert); Grenze dann 4 GiB pro Datei (FAT32, 413 `TOO_LARGE`). | microSD verwenden; ohne Karte Dateigrößen beachten |
| Sektorgröße | niedrig (für X2D erledigt) | Host sieht 512-B-Sektoren (gängig für USB-Sticks, Windows problemlos). Am X2D mit microSD unproblematisch (X2, 2026-11-12). | – |
| Fremddateien vom PC | niedrig | Der Dropbox-Desktop-Client schreibt `.dropbox.device` auf jedes Wechsellaufwerk, auch auf den Dongle (beobachtet 2026-11-11). Harmlos, der Drucker sieht die Datei aber. | Datei löschen oder Dropbox-Option für Wechseldatenträger abschalten |
| Composite-Gerät mit CDC | erledigt für X2D | MSC + CDC-Log-Port in einem Gerät könnte einen Drucker-USB-Stack verwirren. Der X2D kommt mit dem Standard-Build (PID 4003) zurecht (2026-11-12); ein Build ohne CDC ist dort nicht nötig. Für andere Drucker unbekannt. | Nur bei Problemen an anderen Druckern: `CONFIG_TINYUSB_CDC_ENABLED=n` (PID wird 4002, kein 1200-Baud-Touch) |
| Stromaufnahme | mittel | Deskriptor meldet 500 mA; WLAN-Spitzen beim Senden, dazu die microSD-Karte beim Schreiben (nicht gemessen). Hersteller nennt max. 800 mA. Am X2D kein Ausfall gemeldet (Upload und Druck mit microSD, 2026-11-12), aber nicht gemessen und nicht über lange Drucke beobachtet. | Messung bzw. langer Druck (X1/X10), ggf. WLAN-Sendeleistung reduzieren |
| Medienwechsel | niedrig bis mittel | Ob der Drucker UNIT ATTENTION / Soft-Re-Plug korrekt verarbeitet und nach einem Upload bei eingestecktem Dongle neue Dateien sieht: am X2D laut Rückmeldung bestanden, Einzelschritt nicht separat bestätigt (X5, 2026-11-12). | X5 einzeln bestätigen; bei Problemen Varianten mit/ohne `CONFIG_PD_REPLUG_ON_PROVIDE` |
| Druckabbruch durch Übernahme | mittel | „Dateiverwaltung aktivieren“ mit Bestätigung (force) entzieht dem Drucker das Medium – auch ein Upload bei aktivem USB-Laufwerk löst das nach Rückfrage aus. Liest der Drucker während des Drucks vom Stick, bricht der Druck ab. | Weboberfläche warnt im Dialog „Laufwerk vom USB-Gerät trennen?“; am Drucker vor der Übernahme auswerfen. Verhalten beim X2D weiter offen (X6; während eines Drucks nicht geprüft) |
| Full Speed | niedrig | USB 1.1 Full Speed (12 Mbit/s brutto). Gemessen: Lesen 753 KB/s am PC (Flash) – Laden einer 5-MB-Datei dauert ca. 7 s; microSD schreiben über USB 832 KB/s. Beim Schreiben ins interne Flash ist nicht USB, sondern das Flash der Engpass (Abschnitt 4). | Messung T2.10 |

## 2. Dateiformat

- Der X2D druckt eine **gesliceste** `.gcode.3mf` vom Dongle (vom Besitzer bestätigt,
  2026-11-12). Annahme bleibt: Ein reines Projekt-`.3mf` ohne G-Code ist am Drucker nicht
  druckbar; es wird von der
  Firmware aber genauso angenommen (Prüfung nur auf ZIP-Struktur, nicht auf Inhalt).
- Seit 0.2.0 werden **alle Dateitypen** angenommen (`CONFIG_PD_UPLOAD_ALL_TYPES=y`,
  Default). Geprüft wird nur der Name (Zeichen, Länge) und bei `.3mf` die ZIP-Struktur;
  jeder andere Inhalt wird ungeprüft gespeichert.

| Risiko | Bew. | Details | Gegenmaßnahme |
|---|---|---|---|
| Beliebige Dateitypen | mittel | Der Stick nimmt jede Datei an, auch ausführbare. Zusammen mit der fehlenden Anmeldung (Abschnitt 5) kann jeder im selben WLAN Dateien auf ein Laufwerk legen, das an einem PC oder Drucker steckt. Eine Inhalts- oder Schadsoftware-Prüfung gibt es nicht. Namen mit führendem Punkt und Pfade bleiben gesperrt. | Nur in vertrauenswürdigen Netzen betreiben; für reinen Druckbetrieb `CONFIG_PD_UPLOAD_ALL_TYPES=n` (dann nur `.3mf`, optional `.gcode`) |
| Fremddateien am Drucker | niedrig | Der Drucker sieht alle hochgeladenen Dateien, nicht nur Druckdateien. Verhalten des X2D bei vielen fremden Dateien unbekannt. | Test X2; Laufwerk aufgeräumt halten |

## 3. Abhängigkeit von esp_tinyusb-Interna

| Punkt | Risiko |
|---|---|
| Linker-`--wrap` von `tud_msc_read10_cb`, `tud_msc_write10_cb`, `tud_msc_test_unit_ready_cb` | Funktioniert nur, solange esp_tinyusb diese Callbacks stark definiert und TinyUSB keine schwache Default-Version hat. Bei Umbenennung/Umbau greift die Host-I/O-Sperre still nicht mehr. |
| Barrier über `usbd_defer_func` (`device/usbd_pvt.h`, privater TinyUSB-Header) | Setzt voraus, dass esp_tinyusb Host-Schreibzugriffe per `usbd_defer_func` in die TinyUSB-Task verschiebt (so in 2.4.0). |
| Überschriebene schwache Callbacks (`tud_msc_scsi_complete_cb`, `tud_msc_is_writable_cb`, `tud_msc_inquiry2_cb`) | Würde esp_tinyusb sie künftig selbst definieren → Linkfehler (gut sichtbar). |
| `auto_mount_off` | esp_tinyusbs Auswurf-Handler (`tud_msc_start_stop_cb`) mountet nur ohne dieses Flag selbst um. |

**Regel:** Bei jedem Update von `esp_tinyusb` oder `tinyusb` (Versionen sind gepinnt)
diese Punkte im Quellcode der Komponente prüfen und T1.3, T2.2, T2.4 wiederholen.

## 4. Firmware-Verhalten und Hardware-Befunde

Stand der Prüfung: Phase 0, 1, 2 und 5 (Grundfunktion) unter Windows 11 am 2026-11-11
getestet, ebenso Umbenennung, API v2 (`api_test.py` 28/28) und gzip-Auslieferung (Phase 6).
Die neue Weboberfläche hat am 2026-11-12 den Browser-Test am Testserver bestanden
(`tools/ui_test.py` 566/566, T6.4). Am Bambu Lab X2D hat der Besitzer am 2026-11-12 die
Grundfunktion bestätigt (Abschnitt 1). Noch nicht geprüft: Nur-Lese-Modus des Hosts, die
Abläufe der Weboberfläche am echten Gerät und an einem echten Handy.

| Risiko | Bew. | Details |
|---|---|---|
| Schreibgeschwindigkeit (Flash-Grenze) | niedrig (mit microSD) | Vor jedem Schreiben muss der betroffene 4-KiB-Flash-Sektor gelöscht werden; W25Q128: Sektor löschen typ. ca. 45 ms (Datenblatt-Typwert, nicht gemessen) → theoretische Obergrenze grob 90 KB/s für Schreiben ins interne Flash. Gemessen: USB **47 KB/s**, Web **46 KB/s**; eine 5-MB-`.3mf` braucht ca. 2 min. Mit microSD: USB 832 KB/s, Web 1335–1578 KB/s. Relevant nur noch ohne Karte. |
| FAT-Clustergröße (interner Flash) | niedrig (behoben) | FatFs schreibt bei Web-Uploads höchstens einen Cluster pro Disk-Schreibzugriff. Mit 2-KiB-Clustern ist jeder Zugriff ein halber 4-KiB-Flash-Sektor → Lesen-Löschen-Schreiben im Wear Levelling: Web-Upload **7 KB/s**. Fix: `CONFIG_PD_FAT_CLUSTER_KB=4` (Default) → **46 KB/s**, das ca. 11-MB-Volume wird dadurch **FAT12**. Ob der Drucker FAT12 liest, ist offen (Abschnitt 1). Ältere FAT16-Volumes bleiben langsam bis zum Neuformatieren. |
| exFAT nicht unterstützt | mittel | ESP-IDF-FatFs ist ohne exFAT gebaut. microSD-Karten > 32 GB kommen meist mit exFAT: Sie erscheinen als „Kein Dateisystem!“ und müssen per Web auf FAT32 formatiert werden (Daten gehen verloren). Die Testkarte (64 GB) war zufällig schon FAT32; die FAT32-Formatierung einer Karte durch die Firmware ist noch nicht geprüft (T5.5). |
| microSD: kein Hot-Swap | niedrig | Der Slot sitzt am USB-Stecker; die Karte lässt sich nur bei ausgestecktem Dongle wechseln. Das Medium wird nur beim Start gewählt. Ziehen der Karte im Betrieb ist nicht vorgesehen (Folgen ungeprüft). |
| microSD: Kartenqualität | mittel | Nur eine Karte getestet. Andere Karten (Hersteller, Geschwindigkeitsklasse, Fälschungen) können langsamer sein, bei 40 MHz scheitern oder mehr Strom ziehen. |
| microSD: Fremddateien | niedrig | Vorhandene Dateien der Karte bleiben erhalten und sind am Drucker sichtbar (Testkarte: `autorun.inf`/`autorun.ico`). |
| Formatieren des falschen Mediums | niedrig (abgesichert) | Die Format-Anforderung (`fmt_req`) speichert das Medium (Flash/SD); beim Neustart wird nur formatiert, wenn dasselbe Medium gefunden wird. Eine Karte wird nie automatisch formatiert. |
| Upload-Größe über 4 GiB | niedrig (behoben) | Die Größe wurde 32-bittig gelesen: 62 GB + 1 MB liefen über und wurden angenommen. Fix: 64-Bit-Auswertung, ≥ 4 GiB → 413 `TOO_LARGE` (T5.4). |
| Ein Request gleichzeitig | niedrig | Der ESP-IDF-HTTP-Server beantwortet während eines Uploads keine anderen Anfragen (auch nicht `/api/status`). Die Weboberfläche pausiert das Polling, das LCD zeigt den Fortschritt; ein zweiter Client wartet. Datei- und Einstellungsaktionen im selben Browser hingen dabei ohne Meldung; seit dem Browser-Test (T6.4) sind sie während eines Uploads mit Meldung gesperrt. Seit 0.6.0 schickt die Oberfläche alle Anfragen über eine Warteschlange, eine nach der anderen; Downloads laufen daran vorbei (Abschnitt 10). |
| FAT-Ausrichtung | niedrig (behoben) | FatFs legt die MBR-Partition bei Sektor 63 an (nicht auf 4 KiB ausgerichtet). Ohne `MKFS_PARM.align` lag der Datenbereich schief, jeder Schreibzugriff lief über den langsamen Lesen-Löschen-Schreiben-Pfad des Wear Levelling: **12 KB/s**. Fix in `probe_and_format()`: `align = 8` Sektoren → Datenbereich auf 4-KiB-Grenzen, **47 KB/s**. Windows meldet weiterhin Partitions-Offset 32256 (= Sektor 63); ausgerichtet ist nur der Datenbereich innerhalb. Ältere Dateisysteme bleiben langsam bis zum Neuformatieren; am PC formatierte Datenträger: Ausrichtung nicht geprüft. |
| Download-Modus per Software | niedrig | 1200-Baud-Touch **verifiziert** (2026-11-11). TinyUSB schaltet den USB-PHY per `RTC_CNTL_USB_CONF` auf den OTG-Controller, das übersteht einen Software-Reset; ohne Gegenmaßnahme erschien im Download-Modus **kein** USB-Gerät (verifiziert). Fix: `board_restart_to_download_mode()` gibt den PHY vorher an USB-Serial/JTAG zurück (wie Arduinos `usb_switch_to_cdc_jtag()`). Web „Flash-Modus“ und BOOT 5 s nutzen dieselbe Funktion, einzeln noch nicht getestet. Update-Schleife `tools/flash.ps1 -Project printdrive -Build` mehrfach vollautomatisch verifiziert. Rückfall: aus-/einstecken (normaler Start), BOOT halten + einstecken. |
| Strapping-Latch nach BOOT + Einstecken | niedrig | Nach manuellem Download-Modus bleibt jeder weitere Reset über USB-Serial/JTAG (esptool `hard_reset`, RTS) im Download-Modus (`boot:0x0 (DOWNLOAD(USB/UART0))`, `GPIO_STRAP_REG` = 0), obwohl die Taste losgelassen ist (`GPIO_IN` Bit 0 = 1) – bis zum echten Aus-/Einstecken. Wirkt wie „Firmware startet nicht“. Beim 1200-Baud-Touch nicht beobachtet. |
| Windows bei Medienwechsel | niedrig | Verifiziert: „Auswerfen“ im Explorer kommt als Auswurf an (`host ejected the drive`), Soft-Re-Plug nach BOOT kurz → Laufwerk erscheint neu, Daten lesbar; Übernahme mit `force` und Bereitstellen per Web mit neuer Datei aus dem Web-Upload (Datei auf `E:` identisch, T2.11). |
| Nur-Lese-Modus des Hosts | mittel | Implementiert (Sense `DATA PROTECT`, `tud_msc_is_writable_cb`), auf Hardware **nicht getestet** (T1.4). Der X2D arbeitet auch mit schreibendem Host (2026-11-12); ob er ein schreibgeschütztes Medium akzeptiert, ist offen (X8), vorher am PC prüfen. |
| Neue Weboberfläche nur am Testserver durchgetestet | niedrig | Die Oberfläche wurde für 0.2.0 komplett neu geschrieben. Der Browser-Test aller Zustände ist bestanden (T6.4, `tools/ui_test.py` 566/566, PC und Handy-Emulation, hell/dunkel; 26 Fehler gefunden und behoben). Grenzen: Testserver statt Gerät, synthetische Drag-&-Drop-Ereignisse. Am Gerät geprüft sind nur Auslieferung der Endfassung, API und `datadrive.local` (T6.2, T6.3); die Abläufe der Oberfläche am echten Gerät (Zeitverhalten, Re-Plug, Formatieren, Wartungsmodus) und echtes Drag & Drop stehen aus (B14, B16). Fehler in der Oberfläche können die Sicherungen des Laufwerks nicht aushebeln, die liegen in der Firmware. |
| Weboberfläche am echten Handy ungeprüft | mittel | Die Handy-Ansicht ist nur emuliert geprüft (Chromium mit iPhone-User-Agent, 390×844 und 320 px Breite). Ein echter Handy-Browser fehlt, vor allem iOS Safari: Bildschirmtastatur (verdeckte Eingabefelder und Dialoge), Safe Areas, Herunterladen über das `download`-Attribut, Bottom Sheet (B15). |
| BOOT-Bestätigung per zweitem Druck | niedrig | Übernahme per BOOT-Taste bei aktivem USB-Gerät verlangt einen zweiten Druck innerhalb von 4 s; am Gerät noch nicht geprüft (T2.2). |
| Ordner in der Weboberfläche ausgeblendet (Stand 0.2.0, seit 0.3.0 überholt) | niedrig | Ordner sind seit 0.3.0 in der Weboberfläche bedienbar. Ob Ordner, die am PC angelegt wurden, dort richtig erscheinen, ist **noch nicht geprüft**. Der Text zur Zeit vor 0.3.0: Die Weboberfläche zeigte nur Dateien im Hauptverzeichnis. Ordner, die ein PC angelegt hat, werden gezählt (`dirs`, Hinweis unter der Liste), ihr Inhalt ist im Web aber weder sichtbar noch herunterladbar oder löschbar. Der belegte Speicher enthält sie trotzdem, und Formatieren löscht sie mit. Ordner-Unterstützung ist machbar, aber zurückgestellt. |
| Änderungsdatum hängt an der Browser-Uhr (Stand bis 0.4.0; seit 0.5.0 teilweise überholt, siehe Abschnitt 9) | niedrig | Das Gerät hat keine Uhr. Beim Upload liefert der Browser das Änderungsdatum der Datei (`mtime`) und seine aktuelle Zeit (`now`); gespeichert wird Ortszeit ohne Zeitzone. Eine falsch gestellte Uhr oder Zeitzone im Browser ergibt falsche Zeitstempel, ein Host in einer anderen Zeitzone zeigt sie verschoben. `now` stellt die Geräteuhr nur einmal je Start; Uploads ohne diese Parameter (eigene API-Clients) bekommen kein verlässliches Datum (Anzeige „–“). Ohne Anmeldung kann jeder Client beliebige Zeitstempel setzen. Sortierung „Neueste zuerst“ ist deshalb nur so gut wie die Uhren der Schreiber. |
| Sehr viele Dateien | niedrig | Die Dateiliste entsteht aus einem 32-KiB-Puffer (ca. 1000 übliche Namen, bei langen Namen weniger). Seit 0.5.0 wird sie ohne eigenen Puffer direkt gesendet; die Grenze ist laut Quelltext dieselbe geblieben. Darüber wird gekürzt (`truncated`, Hinweis in der Oberfläche); die übrigen Dateien sind im Web nicht erreichbar, am USB-Gerät schon. Kürzung und Ladezeit bei sehr vielen Dateien sind am Gerät nicht geprüft (nur im Testserver, Szenarien `truncated` und `many`: 300 Dateien, Neuaufbau der Liste 45–70 ms, seit T6.4 nur noch bei Änderungen). |
| Weboberfläche nur gzip | niedrig | `index.html` liegt nur komprimiert im Flash und wird immer mit `Content-Encoding: gzip` gesendet, unabhängig vom `Accept-Encoding` des Clients. Alle üblichen Browser können das; einfache Werkzeuge (z. B. `curl` ohne `--compressed`) erhalten komprimierte Daten. |
| Einstellungen je Browser | niedrig | „Nach dem Hochladen wieder bereitstellen“ und die Darstellung liegen im Browser (`localStorage`), nicht auf dem Gerät. Ein anderes Gerät oder ein Browser mit gelöschten Daten verhält sich wieder nach dem Standard (bereitstellen: an). |
| Weboberfläche: `hidden` überschrieben | niedrig (behoben) | CSS-`display` überschrieb das `hidden`-Attribut, beide Modus-Knöpfe waren sichtbar. Fix: `[hidden]{display:none!important}`. Weitere Nuancen in UI und LED-Farben möglich. |
| Fehlerantwort bei abgewiesenem Upload | niedrig (behoben) | Wurde ein Upload vor dem Lesen des Bodys abgewiesen (z. B. 409 `EXISTS`), setzte der Server die Verbindung zurück. Fix: `drain_body()` verwirft den restlichen Body, dann kommt die Antwort an (verifiziert). Kostet bei großen Dateien die volle Übertragungszeit. |
| Flash-Backup und serielle Ausgabe über USB-Serial/JTAG | mittel | An diesem PC gehen bei Bursts Daten verloren: `read_flash` mit datenabhängigem Byteverlust (ca. 11 KB/s, Blöcke scheitern wiederholt), Log-Ausgabe mit fehlenden Blöcken. Ein Vollbackup des Werkszustands existiert daher nicht; Rückfall ist das LILYGO-Werksabbild (SHA256 in `backup\SHA256SUMS.txt`, siehe [04](04_build_und_flash.md) Abschnitt 5). |
| Barrier-Timeout | niedrig | Bei sehr vielen ausstehenden Host-Schreibzugriffen könnte die Barrier (2 s) ablaufen → Umschaltung wird abgebrochen (sicher, aber lästig). |
| Erststart-Erkennung über NVS | niedrig | „Erster Start“ heißt: NVS-Flag `fmt_done` fehlt. Wird das NVS gelöscht (Format-Wechsel in `app_main`, `erase-flash` der NVS-Partition) und ist das FAT gleichzeitig unlesbar, formatiert die Firmware ohne Rückfrage. |
| FAT bei Stromausfall | mittel | Stromausfall während eines Schreibvorgangs (Host oder Upload) kann **verlorene Cluster** oder inkonsistente FAT-Einträge hinterlassen. Die Upload-Temp-Datei wird zwar gelöscht, verlorene Cluster aber nicht zurückgewonnen. Reparatur: `chkdsk X: /f` am PC (Host schreibend) oder Formatieren. |
| Boot-Politik | niedrig | Beim Start wird ein vorhandener Host immer bedient, auch wenn `CONFIG_PD_PROVIDE_ON_ATTACH=n`. |
| Logs vor CDC-Verbindung | niedrig | Beim Öffnen des CDC-Ports wird der Log-Ringpuffer (8 KB, inkl. Start) nachgeliefert (verifiziert). Bei langer Laufzeit sind die Startmeldungen dort überschrieben; dann UART0 oder Diagnose-Build. |

## 5. Sicherheit

| Risiko | Bew. | Details |
|---|---|---|
| Anmeldung (seit 0.3.0) | mittel (Restrisiko) | Die Anmeldung mit Benutzern und Rollen ist umgesetzt (API-Test 182/182). Die Passwörter gehen aber **unverschlüsselt** durchs WLAN (nur HTTP), wer den Funkverkehr mitschneidet, kann sie lesen. Die Anmeldesperre gilt je Adresse und verlangsamt nur das Raten über das Netz. Ein vergessenes Passwort setzt der Reset per Taste auf `admin` / `admin` zurück. Die Oberfläche weist darauf hin. Nur für vertrauenswürdige Netze gedacht. Bis 0.5.0 war ein Update über die Weboberfläche nicht signiert; seit 0.6.0 gibt es signierte Updates als Build-Option (Abschnitt 10). |
| CSRF | niedrig | POST-Anfragen brauchen `X-PrintDrive: 1` (Headername unverändert). Fremde Webseiten können diesen Header ohne CORS-Freigabe nicht setzen; GET-Endpunkte (Status, Liste, Download, Log) sind ungeschützt lesbar. |
| WLAN-Zugangsdaten | mittel | STA-Passwort und AP-Passwort liegen **unverschlüsselt** im NVS (keine NVS-Verschlüsselung, kein Flash Encryption). Wer den Dongle hat, kann sie per esptool auslesen. Gleiches gilt für Flash-Backups in `backup\`. |
| Setup-AP | niedrig | `DataDrive-XXXX`, WPA2 mit gerätespezifischem 10-Zeichen-Passwort; das Passwort steht offen auf dem LCD. Im Log steht es nach aktuellem Quelltext nicht (`setup access point on: SSID '…' (password on the LCD)`). |
| Kein HTTPS | mittel | Übertragung im LAN unverschlüsselt, auch Passwörter. Seit 0.4.0 gibt es HTTPS nur als Option, die im Build abgeschaltet ist, weil Uploads mit TLS auf diesem Board scheitern (Speicher, siehe [14](14_vormerkliste.md)). **Seit 0.5.0:** HTTPS ist im Build und lässt sich in den Einstellungen einschalten (Voreinstellung aus). Solange der Schalter aus ist, gilt das Risiko unverändert. Restrisiken mit HTTPS: Abschnitt 9. |

## 6. Annahmen (Zusammenfassung)

1. ~~Pinbelegung und LCD-Parameter aus dem Hersteller-Repo stimmen für dieses Exemplar~~ – verifiziert 2026-11-11 (T0.3/T0.4), microSD-Pins ebenfalls (T5.1).
2. UART0 ist an GPIO43/44 (QWIIC) erreichbar.
3. ~~Der ROM-Bootloader meldet sich nach Software-Neustart in den Download-Modus als 303A:1001~~ – verifiziert 2026-11-11, aber nur mit PHY-Rückgabe (Abschnitt 4).
4. ~~Windows sendet beim Auswerfen START STOP UNIT mit LOEJ = 1, START = 0~~ – verifiziert 2026-11-11 (Auswurf wird erkannt).
5. ~~Der X2D liest FAT32 (microSD) von einem USB-MSC-Gerät mit 512-B-Sektoren~~ – vom Besitzer bestätigt 2026-11-12. Weiter Annahme: FAT12/FAT16 (interner Flash) am X2D.
6. ~~Der X2D druckt gesliceste `.gcode.3mf` vom USB-Stick~~ – vom Besitzer bestätigt 2026-11-12.
7. ~~Der X2D kommt mit dem Composite-Gerät (MSC + CDC) zurecht~~ – vom Besitzer bestätigt 2026-11-12, Build ohne CDC nicht nötig.
8. Die Uhr des hochladenden Browsers stimmt (Quelle aller Zeitstempel, Abschnitt 4). Seit 0.5.0
   gilt das nur noch ohne Internet; mit Internet stellt NTP die Uhr (am Gerät verifiziert).
9. Der X2D stört sich nicht an fremden Dateitypen auf dem Laufwerk.
10. ~~Der X2D druckt von einem verschlüsselten, per Handy entsperrten Stick~~ – vom Besitzer bestätigt 2026-11-14 (Release 0.6.0, T11.3).
11. Ob der X2D eine Druckdatei beim Start ganz einliest oder während des Drucks weiter vom Stick
    liest, ist unbekannt. Bis 0.5.0 hing davon ab, ob das automatische Sperren nach Zeit einen
    Druck abbrechen kann; seit 0.6.0 läuft diese Zeitregel im USB-Modus nicht mehr (Abschnitt 9).
12. Die Angaben zu Windows, iPhone und macOS beim Netzlaufwerk stammen aus Recherche, nicht aus
    eigenen Tests (Abschnitt 9).

## 7. Verbleibende Hauptrisiken (Kurzfassung)

| Risiko | Stand |
|---|---|
| Kompatibilität mit dem Bambu Lab X2D | niedrig (Restrisiko): Grundfunktion inkl. Druck vom Besitzer bestätigt (2026-11-12, microSD FAT32, Standard-Build mit CDC); ungeprüft: lange Drucke, Nur-Lese-Modus, Übernahme während eines Drucks, interner Flash, andere Drucker |
| Upload bei eingestecktem Dongle am X2D (Medienwechsel) | laut Rückmeldung bestanden, Einzelschritt nicht separat bestätigt (X5) |
| Composite-Gerät mit CDC am Drucker | für X2D erledigt, Build ohne CDC nicht nötig |
| Kapazität / Schreibrate des internen Flashs | durch microSD entschärft |
| Kartenqualität, Stromaufnahme mit Karte | nicht gemessen (am X2D kein Ausfall gemeldet), nur eine Karte getestet |
| exFAT | nicht unterstützt, Karte per Web auf FAT32 formatieren |
| Nur-Lese-Modus des Hosts | ungetestet |
| Nuancen in LED-Farben | offen |
| Neue Weboberfläche | Browser-Test am Testserver bestanden (T6.4, 566/566); Abläufe am echten Gerät und echtes Drag & Drop ungetestet |
| Weboberfläche am echten Handy (iOS Safari) | ungetestet (T6.4 B15) |
| BOOT-Bestätigung per zweitem Druck | ungetestet (T2.2) |
| Anmeldung, Passwörter im Klartext durchs WLAN | 0.3.0 umgesetzt; HTTPS offen (Abschnitt 5) |
| Verschlüsselung: Passwortstärke, Klartext bei entsperrtem Laufwerk | 0.4.0, siehe Abschnitt 8 |
| Update-Signatur | seit 0.6.0 als Build-Option; Abweisung unsignierter und fremd signierter Dateien am Gerät geprüft; kein Schutz gegen Flashen per USB (Abschnitt 10) |
| Beliebige Dateitypen ohne Inhaltsprüfung | bewusst so, abschaltbar (Abschnitt 2) |
| Ordner in der Weboberfläche | seit 0.3.0 umgesetzt; PC-Ordner in der Anzeige noch nicht geprüft |
| Zeitstempel aus der Browser-Uhr | seit 0.5.0 Uhr per NTP; ohne Internet weiter Browser-Uhr (Abschnitt 9) |
| Netzlaufwerk (WebDAV), 0.5.0 | nur per API-Test am Gerät geprüft, mit echten Programmen ungetestet (Abschnitt 9) |
| Automatisches Sperren während eines Drucks, 0.5.0 | Voreinstellung aus; seit 0.6.0 sperrt die Zeitregel im USB-Modus nicht mehr, es bleibt nur `on_detach` (Abschnitt 9) |
| HTTPS, 0.5.0 | Grundfunktion gemessen; iPhone mit Zertifikat, WebDAV über https und Dauerbetrieb ungetestet (Abschnitt 9) |
| Unabhängige Sicherheitsprüfung 0.5.0 | abgeschlossen, Befunde behoben (siehe oben) |
| Downloads über HTTPS, 0.6.0 | bekannte Lücke: eine Statusabfrage kann einen langen Download noch abbrechen (Abschnitt 10) |
| Feste IP-Adresse, 0.6.0 | feste Adresse und eigener Name am Gerät vom Besitzer bestätigt (2026-11-14, T11.1); eine falsche Adresse macht den Stick unerreichbar, der Rückweg über die Taste ist am Gerät ungetestet (Abschnitt 10) |
| Verlust des Signierschlüssels, 0.6.0 | keine Web-Updates mehr, jedes Gerät per USB neu flashen (Abschnitt 10) |

## 8. Verschlüsselung des Laufwerks (seit 0.4.0)

Die Verschlüsselung ist optional und standardmäßig aus. Verfahren und Anleitung: [12](12_api_v4.md)
Abschnitt 2 und die Anleitung in der [README](../README.md). Was sie schützt, steht in Abschnitt 2.5
von [12](12_api_v4.md) („Was die Verschlüsselung schützt“). Hier die Risiken und offenen Punkte.

| Risiko | Bew. | Details | Gegenmaßnahme / Stand |
|---|---|---|---|
| Passwortstärke | **hoch**, wenn das Passwort kurz ist | Wer die Karte hat, probiert Passwörter offline durch. Das Gerät bremst dabei nicht. Jede Prüfung eines Passworts braucht eine PBKDF2-Ableitung (am Gerät auf 2 s kalibriert). Auf einem PC geht sie schneller als am Gerät; die Geschwindigkeit eines Angreifers ist **nicht gemessen**. | Mindestlänge 12 Zeichen wird erzwungen. Ein langes, zufälliges Passwort oder mehrere Wörter ist nötig. Wiederherstellungsschlüssel ist praktisch nicht zu erraten (160 Bit). |
| Drosselung nur über das Netz | mittel | Die Wartezeit nach falschen Eingaben (5 s, dann doppelt, höchstens 300 s; die ersten zwei Fehler sind frei) gilt nur für Anfragen an das laufende Gerät. Ein Neustart setzt sie zurück. | Keine Abhilfe gegen den Angriff auf eine entnommene Karte. |
| Klartext bei entsperrtem Laufwerk | mittel | Drucker und PC sehen die Dateien im Klartext, solange das Laufwerk entsperrt ist. Das ist nötig, damit der Drucker drucken kann. | Stick nach Gebrauch im Browser sperren. Seit 0.5.0 gibt es das automatische Sperren (Voreinstellung aus; das Sperren nach Ablauf der Zeit ist seit 2026-11-14 am Gerät bestätigt, T11.2). Seit 0.6.0 läuft die Zeit nur in der Dateiverwaltung: Solange das Laufwerk dem Drucker oder PC bereitgestellt ist, bleibt es entsperrt, bis das USB-Gerät getrennt wird (mit „Sperren, wenn das USB-Gerät getrennt wird“) oder jemand von Hand sperrt. |
| Keine Erkennung von Manipulation | mittel | Das Verfahren erkennt gezielte Änderungen an den verschlüsselten Daten nicht. | Kein Gegenmittel in 0.4.0. |
| Passwörter durchs WLAN | mittel | Das Laufwerks-Passwort geht beim Entsperren im Klartext durchs WLAN, solange kein HTTPS aktiv ist. | Seit 0.5.0 HTTPS in den Einstellungen einschalten (Voreinstellung aus, Abschnitt 5 und 9). |
| Updates nicht signiert | mittel (bis 0.5.0); seit 0.6.0 mit signiertem Build niedrig | Wer Administrator-Zugriff hat, konnte geänderte Firmware aufspielen. Die könnte das Passwort beim nächsten Entsperren mitlesen. | Seit 0.6.0 signierte Updates ([17](17_signierte_updates.md)); am Gerät geprüft, dass unsignierte und fremd signierte Dateien abgewiesen werden. Gilt nur für den signierten Build des Besitzers; Flashen per USB bleibt möglich (Abschnitt 10). |
| Einrichten löscht die Karte | hoch (Datenverlust) | Einrichten und Ausschalten löschen alle Dateien. Ohne Passwort und ohne Wiederherstellungsschlüssel ist der Inhalt endgültig verloren. | Die Oberfläche warnt vor dem Einrichten. Der Wiederherstellungsschlüssel wird nur einmal angezeigt. |
| Stromausfall während Einrichtung oder Passwortwechsel | mittel | Der Kopfbereich wird mit einer Sicherungskopie und Prüfsummen geschrieben. Ein Stromausfall in diesen Vorgängen ist **noch nicht geprüft**. | Vor dem Einrichten die Daten sichern. |
| Verschlüsselung auf dem internen Flash | mittel | Ohne Karte gilt dieselbe Schicht. Ein Test steht **noch aus**. | Nur die microSD-Karte verschlüsseln, bis der interne Flash geprüft ist. |
| Druck von einem verschlüsselten Stick am X2D | niedrig | Der Ablauf „einstecken, im Browser entsperren, drucken“ ist vom Besitzer am Drucker bestätigt (2026-11-14, T11.3). Lange Drucke und Übernahme während eines Drucks sind ungetestet. | – |
| Geschwindigkeit | niedrig | Verschlüsselt: USB schreiben 820 KB/s (vorher 832 KB/s), Web-Upload 1,0 MB/s. Der Vergleich beim Lesen und beim Upload unter gleichen Bedingungen ist **noch nicht geprüft**. | – |
| Größe | niedrig | Das Volumen ist etwa 8 bis 12 MiB kleiner (Kopfbereich, Reserve, Sicherungskopie). | – |
| Unterschied im Alltag | niedrig | Die Karte ist an anderen Geräten nicht mehr lesbar. Nach jedem Stromausfall ist ein Entsperren nötig. | Siehe Anleitung in der [README](../README.md). |

## 9. Neuerungen von 0.5.0

Schnittstelle: [15](15_api_v5.md). Prüfstand: [05](05_testprotokoll.md) Phase 9 (API-Test am Gerät
354/354). Die unabhängige Sicherheitsprüfung dieser Neuerungen ist abgeschlossen, ihre Befunde sind behoben.

| Risiko | Bew. | Details | Gegenmaßnahme / Stand |
|---|---|---|---|
| Netzlaufwerk mit echten Programmen ungetestet | mittel | WebDAV ist am Gerät nur per API-Test geprüft (Liste, Hochladen, Verschieben, Löschen, Anmeldung, gesperrtes Laufwerk). Windows-Explorer, macOS-Finder und Handy-Apps sind **noch nicht geprüft**. Programme schicken lange Folgen von Anfragen und haben eigene Eigenheiten; Fehler zeigen sich erst dort. | Vor dem Alltagseinsatz mit dem eigenen Programm ausprobieren (T9.3). Die Weboberfläche bleibt der geprüfte Weg. |
| Windows: `BasicAuthLevel = 2` | mittel | Ohne HTTPS verbindet sich der Windows-Explorer erst nach dieser Registry-Änderung (Recherche, nicht selbst getestet). Sie gilt für **alle** WebDAV-Server: Windows schickt Benutzername und Passwort dann bei jedem WebDAV-Server über `http` lesbar mit. | `tools/webdav/Connect-DataDrive.ps1` fragt vorher und kann die Änderung mit `-Rueckgaengig` zurücknehmen. Nur in vertrauenswürdigen Netzen; für den Stick ein eigenes Passwort. |
| Windows: Dateigrenze 50 MB | niedrig | Der Windows-WebDAV-Client überträgt ohne Änderung nur Dateien bis etwa 50 MB (Recherche). | Das Skript erhöht die Grenze nach Rückfrage. |
| macOS-Finder kann nicht hineinkopieren | niedrig | Der Finder lädt „chunked“ hoch; der Webserver des Sticks kann das nicht annehmen und antwortet `411` (Recherche und Quelltext, nicht mit einem Mac getestet). | Weboberfläche oder ein WebDAV-Programm, das die Länge mitschickt. |
| iPhone „Dateien“ ohne WebDAV | niedrig | Die eingebaute App kann nur SMB (Recherche). | Zusatz-App nötig; keine davon ist getestet. |
| Netzlaufwerk und Drucker | niedrig | WebDAV arbeitet nur, solange die Dateiverwaltung das Laufwerk hat, und nimmt es dem Drucker nie weg. Ein Programm, das gerade verbunden ist, bekommt Fehler, sobald das Laufwerk dem USB-Gerät gegeben wird. | Bewusst so, damit kein Druck abbricht. |
| WebDAV-Passwort im Klartext | mittel | Anmeldung nur per HTTP Basic. Ohne HTTPS geht das Passwort bei jeder Anfrage lesbar durchs WLAN. WebDAV über `https` ist **noch nicht geprüft**. | Abschaltbar in den Einstellungen (Netzwerk). Eigenes Passwort, vertrauenswürdiges Netz. |
| Automatisches Sperren während eines Drucks | niedrig | Mit 0.5.0 sperrte das Laufwerk nach Ablauf der Zeit auch dann, wenn ein Drucker angeschlossen war, der gerade nichts las; er verlor das Medium. **Seit 0.6.0 läuft die Zeitregel nur, solange die Dateiverwaltung das Laufwerk hat;** einem Drucker oder PC nimmt sie es nicht mehr weg (im USB-Modus kein Countdown, der Wechsel zurück startet die Frist neu). Im USB-Modus wirkt nur noch „Sperren, wenn das USB-Gerät getrennt wird“: 10 Sekunden nach dem Trennen oder nach 5 Minuten im USB-Ruhezustand ohne Zugriff. Ob der X2D während eines Drucks in den Ruhezustand geht, ist **nicht bekannt**. Die neue Regel ist am Gerät geprüft (2026-11-13), das Sperren nach Ablauf der Zeit ebenfalls (vom Besitzer bestätigt 2026-11-14, T11.2). | Voreinstellung aus. |
| Uhr nach Stromausfall | niedrig | Nach einem Stromausfall ist die Uhr unbekannt, bis NTP sie stellt oder ein Browser sie mitteilt. Dateien, die in dieser Zeit geschrieben werden, haben kein Datum. Ohne Internet hängt die Uhr weiter an der Browser-Uhr. NTP am Gerät verifiziert; das Verhalten ohne Internet und nach Stromausfall ist **noch nicht geprüft**. | Hingenommen. |
| Zeitstempel jetzt echte UTC-Sekunden | niedrig | Ab API Version 5 sind alle Zeiten echte UTC-Sekunden. Eigene Skripte, die bisher Ortszeit-Sekunden geschickt haben, liegen um den Zeitzonen-Versatz daneben ([15](15_api_v5.md) Abschnitt 1.3). | Skripte anpassen. „Senden an“ und Kurzbefehl: **noch nicht geprüft**, ob betroffen. |
| Einrichtungsseite öffnet sich von selbst | niedrig | Gebaut, mit einem echten Handy **noch nicht geprüft**. Im Anmeldefenster des Handys geht nur die WLAN-Einrichtung, kein Hoch- oder Herunterladen. | Fenster schließen (iPhone: „Ohne Internet verwenden“), dann `http://192.168.4.1` im Browser. |
| Upload fortsetzen | niedrig | Es gibt nur einen angefangenen Upload. Er wird verworfen durch einen anderen Upload, die Übergabe an das USB-Gerät, Sperren (auch automatisch) und Neustart. | Großen Upload zuerst fertig machen. |
| Ordner hochladen nur am Testserver geprüft | niedrig | Am Gerät **noch nicht geprüft**. Am iPhone gibt es keine Ordnerauswahl. | Ordner einzeln anlegen oder ZIP am PC entpacken. |
| ZIP blockiert andere Vorgänge | niedrig | Solange ein ZIP läuft, sind Ordner anlegen und Besitzerwechsel mit `409 BUSY` gesperrt; höchstens 4096 Dateien je ZIP. | Unterordner einzeln laden. |
| HTTPS: wenig freier Speicher | mittel | Gemessen: 66 KiB frei im Leerlauf, 22 KiB als tiefster Wert beim Upload, keine Puffer-Fehlgriffe. Wie es über viele Stunden oder bei mehreren Geräten gleichzeitig aussieht, ist **noch nicht geprüft**. Es wird nur eine TLS-Verbindung gleichzeitig bedient. | Schalter bleibt aus, bis der Benutzer ihn einschaltet. |
| HTTPS: Skripte und Symbole | niedrig | Mit HTTPS wird `http://` umgeleitet; „Senden an“, Kurzbefehl, Home-Bildschirm-Symbol und `api_test.py` müssen auf `https://` umgestellt werden. iPhone mit installiertem Zertifikat, Symbol und Kurzbefehl über `https` sind **noch nicht geprüft**. | Anleitung in der [README](../README.md), Abschnitt „HTTPS einschalten“. |
| HTTPS: Download langsamer | niedrig | Download über HTTPS 389 kB/s gegenüber 487 kB/s über HTTP (gemessen). | Hingenommen. |
| Display-Sprache Englisch | niedrig | Einstellung per API geprüft; ob die englischen Texte auf das LCD passen, ist **noch nicht geprüft**. | Bei Problemen Deutsch wählen. |

## 10. Neuerungen von 0.6.0

Schnittstelle: [16](16_api_v6.md), signierte Updates: [17](17_signierte_updates.md). Prüfstand:
[05](05_testprotokoll.md) Phase 10 (Update über den Browser von 0.5.0 auf 0.6.0, signierte Updates,
API-Test am Gerät 415/415 mit 7 übersprungenen Prüfungen).

| Risiko | Bew. | Details | Gegenmaßnahme / Stand |
|---|---|---|---|
| Downloads über HTTPS | mittel | Die Oberfläche schickt ihre Anfragen seit 0.6.0 über eine Warteschlange, eine nach der anderen (Fix für „Stick nicht erreichbar“, T10.12). Downloads und ZIP-Downloads öffnen aber eine eigene Verbindung im Browser und laufen an der Warteschlange vorbei. Weil der Stick mit HTTPS nur eine Verbindung bedient, kann eine Statusabfrage während eines langen Downloads diese Verbindung noch schließen. Am Gerät **noch nicht geprüft** (T10.13). | Große Dateien per USB holen; bei Abbruch den Download neu starten. Behebung offen ([14](14_vormerkliste.md)). |
| Warteschlange am echten Handy | niedrig | Der Fehler „Stick nicht erreichbar“ wurde mit einem Modell nachgestellt, das nur eine Verbindung annimmt, und dort behoben. Am iPhone ist die Korrektur **noch nicht nachgeprüft**. | Nachprüfen am iPhone (T10.12). |
| Feste IP-Adresse sperrt aus | mittel | Eine falsche feste Adresse, Maske oder ein falsches Gateway macht den Stick im WLAN unerreichbar. Das Gerät weist nur Formfehler und offensichtlich unpassende Werte ab ([16](16_api_v6.md) Abschnitt 2.2), nicht eine Adresse, die im eigenen Netz belegt ist oder nicht passt. Umstellen auf eine feste Adresse und einen neuen Namen ist am Gerät vom Besitzer bestätigt (2026-11-14, T11.1). | Rückweg: Taste 10 Sekunden halten, setzt Benutzer und Netzwerkeinstellungen zurück. Das geht nur am Stick selbst und ist am Gerät **noch nicht geprüft** (T10.9). Der Einrichtungs-Zugangspunkt behält `192.168.4.1`. |
| Neuer Name mit HTTPS | niedrig | Ein neuer Gerätename erzeugt eine neue Zertifizierungsstelle; sie muss auf jedem Gerät neu installiert werden, bis dahin warnt jeder Browser. Eine neue Adresse erneuert nur das Serverzertifikat. Beides wirkt nach dem nächsten Neustart. Am Gerät **noch nicht geprüft**. | Namen vor dem Einschalten von HTTPS festlegen. |
| Signierschlüssel verloren | hoch (für Web-Updates) | Ohne den privaten Schlüssel nimmt ein Stick mit signierter Firmware keine Updates über den Browser mehr an, auf Dauer. Er läuft aber normal weiter. | Schlüssel an einem zweiten Ort offline sichern. Abhilfe: neuer Schlüssel, jedes Gerät per USB flashen ([17](17_signierte_updates.md) Abschnitt 8). |
| Signierschlüssel verraten | hoch | Wer den privaten Schlüssel hat, kann Updates bauen, die die Geräte annehmen (mit Admin-Zugang). | Schlüssel nie ins Repository, in Cloud-Freigaben oder zu GitHub. Bei Verdacht Admin-Passwörter ändern, neuer Schlüssel, alle Geräte per USB flashen ([17](17_signierte_updates.md) Abschnitt 8). |
| Grenzen der Signatur | mittel | Kein Schutz gegen Flashen per USB (auch per Software über den USB-Log-Port), kein Secure Boot, keine Flash-Verschlüsselung, kein Schutz gegen Rückstufen auf eine ältere signierte Version. Builds aus dem öffentlichen Repository und aus Forks sind unsigniert und prüfen keine Updates. | Bewusst so ([17](17_signierte_updates.md) Abschnitt 2 und 4). Für die eigenen Geräte immer den signierten Build nehmen. |
| Signaturprüfung, weitere Fälle | niedrig | Am Gerät geprüft: Annahme der signierten 0.6.0 auf 0.5.0, `UNSIGNED`, `BAD_SIGNATURE`, kein Neustart nach der Abweisung. **Noch nicht geprüft:** veränderte Datei, Rücksprung nach einem Fehlstart, Flashen per USB mit `-Signed`, Update über HTTPS (Speicher der HTTPS-Aufgabe für die RSA-Prüfung). | Testschritte 5 bis 12 in [17](17_signierte_updates.md) Abschnitt 9. |
| HTTPS: freier Speicher mit 0.6.0 | mittel | Gemessen: 67 KiB frei im Leerlauf (HTTPS an, Laufwerk entsperrt; 0.5.0: 66 KiB). Der tiefste Wert während eines Uploads ist mit 0.6.0 nicht gemessen, Dauerbetrieb **noch nicht geprüft**. | Wie Abschnitt 9. |
| Restzeit und Sperren | mittel | Die Restzeit und `touch` sind am Gerät geprüft (T10.4). Danach wurde die Regel geändert: Die Zeit läuft nur noch in der Dateiverwaltung, im USB-Modus ist sie aus (kein Countdown, `remaining_s:-1`); „wartet“ heißt nur noch, dass der letzte Sperrversuch abgewiesen wurde (beschäftigt). Diese Regel ist am Gerät geprüft (2026-11-13), das Sperren nach Ablauf der Zeit vom Besitzer bestätigt (2026-11-14, T11.2). Der Zustand „wartet“ ist am Gerät **noch nicht geprüft** (T10.6). Drucke: Abschnitt 9. | Voreinstellung aus. |
