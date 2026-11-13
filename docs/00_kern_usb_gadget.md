# WiFi DataDrive – der Kern in Kurzform

Stand: 2026-11-12, Firmware 0.5.0. Die Abschnitte bis „Weboberfläche und API“ beschreiben den Kern seit 0.2.0 und sind auf dem echten Gerät getestet, außer wo „offen“ steht. Anmeldung, Verschlüsselung und die übrigen Neuerungen von 0.3.0 und 0.4.0 stehen in „Version 0.3.0 und 0.4.0 in Kurzform“ weiter unten, die von 0.5.0 in „Version 0.5.0 in Kurzform“, jeweils mit Prüfstand.

Das Projekt hieß bis Version 0.1.0 „WiFi PrintDrive“. Intern bleibt der alte Name stehen: Ordner `firmware/printdrive`, HTTP-Header `X-PrintDrive`, Code-Präfix `pd_`.

## Was es ist

Ein **LILYGO T-Dongle-S3** (ESP32-S3, USB-A-Stecker, kleines LCD, microSD-Slot) verhält sich am USB-Gerät (PC, Drucker – technisch der USB-Host) wie ein normaler **USB-Stick**. Gleichzeitig hängt er im WLAN und bietet im Browser eine **Dateiverwaltung**: beliebige Dateien hochladen (auch per Drag & Drop), herunterladen, umbenennen, löschen. Ein Ziel bleibt: Druckdateien drahtlos auf den „Stick“ im 3D-Drucker legen (Bambu X2D: Erkennen, Anzeigen und Drucken laut Rückmeldung des Besitzers vom 2026-11-12 erfolgreich, siehe „Offen“).

```text
Browser (PC/Handy) --WLAN/HTTP--> [ ESP32-S3: Webserver | FAT | USB-MSC ] --USB--> PC / Drucker
                                             |
                                   microSD (FAT32) oder interner Flash (10,8 MB)
                                   optional verschlüsselt (AES-256-XTS, seit 0.4.0)
```

## Das Kernproblem und die Lösung

Ein FAT-Dateisystem verträgt **nur einen Schreiber**. Der USB-Host greift roh auf Sektoren zu und cacht das Dateisystem; schreibt gleichzeitig die Firmware, ist es kaputt. Espressifs eigenes „Wireless Disk“-Beispiel macht genau das und ist deshalb ungeeignet.

**Lösung: genau ein Besitzer zu jedem Zeitpunkt.**

| Besitzer | Anzeige im Web | Dateisystem | USB-Host sieht | Weboberfläche kann |
|---|---|---|---|---|
| `USB` | „USB-Laufwerk aktiv“ | lokal nicht eingebunden | Laufwerk mit Dateien | nur Status anzeigen |
| `APP` | „Dateiverwaltung aktiv“ | lokal unter `/data` eingebunden | „kein Medium“ (wie leerer Kartenleser) | hochladen, auflisten, umbenennen, löschen, herunterladen |

Umgeschaltet wird nur an einer Stelle (`drive_set_owner()` in `drive.c`), unter einem Mutex. Die Weboberfläche nennt den Host „USB-Gerät“ und die beiden Wechsel „Dateiverwaltung aktivieren“ und „USB-Laufwerk bereitstellen“. Die Sicherungen unten sind seit Version 0.2.0 unverändert. Die Verschlüsselung (0.4.0) liegt
unterhalb dieser Schicht und ändert nichts an der Besitzer-Logik.

### Regeln beim Umschalten

- **APP → USB:** nicht während eines Up- oder Downloads. Danach meldet sich das Gerät kurz ab und wieder an (Soft-Replug), damit der Host es wie einen frisch eingesteckten Stick neu einliest.
- **USB → APP:** sofort erlaubt, wenn der Host ausgeworfen hat oder weg ist. Sonst nur mit ausdrücklicher Bestätigung (Web: Dialog, Taste: zweiter Druck). **Nie** innerhalb von 3 s nach dem letzten Host-Schreibzugriff.
- **Host wirft aus** (Windows „Auswerfen“): automatisch → APP.
- **Start:** Meldet sich innerhalb von 3 s ein Host, bekommt er das Laufwerk, sonst bleibt die Dateiverwaltung aktiv (im Code und Log „Upload-Modus“).

### Die fünf Sicherungen (über esp_tinyusb hinaus)

1. **`auto_mount_off = 1`** – esp_tinyusb schaltet nie selbst um. Sein Automatikmodus würde beim Einstecken das Dateisystem unter einer offenen Upload-Datei aushängen.
2. **Host-Sperre auf SCSI-Ebene** – `READ(10)`, `WRITE(10)` und `TEST UNIT READY` laufen per Linker-Wrap durch eigene Funktionen. Gehört das Medium der App, wird jeder Host-Zugriff mit „Medium nicht vorhanden“ abgewiesen. esp_tinyusb prüft das in `read10`/`write10` nicht selbst.
3. **Barriere** – esp_tinyusb führt Host-Schreibzugriffe verzögert in der TinyUSB-Task aus und wartet beim Umschalten nicht darauf. Vor dem Einbinden für die App werden deshalb zwei Marker durch dieselbe Warteschlange geschickt; erst wenn beide durch sind, ist alles im Speicher.
4. **Medienwechsel-Signal** – nach der Rückgabe meldet das erste `TEST UNIT READY` einmal `UNIT ATTENTION 0x28` („Medium könnte gewechselt haben“), zusätzlich der Soft-Replug.
5. **Atomarer Upload** – Daten gehen in `/data/.pd_upload.tmp`. Erst nach Größenprüfung und – bei `.3mf` – ZIP-Prüfung (Kopf `PK\3\4` und End-of-Central-Directory) wird umbenannt. Abbruch, Stromausfall oder kaputte Datei hinterlassen nie eine halbe Zieldatei; Reste werden beim nächsten Einbinden gelöscht.

### Kern-Code

Linker-Wrap (`main/CMakeLists.txt`). Funktioniert nur bei Callbacks, für die TinyUSB keinen schwachen Default mitbringt:

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_msc_read10_cb"
    "-Wl,--wrap=tud_msc_write10_cb"
    "-Wl,--wrap=tud_msc_test_unit_ready_cb")
```

Host-Sperre (`drive.c`, gekürzt):

```c
int32_t __wrap_tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buf, uint32_t n)
{
    if (!s_host_io_enabled) {                       // App besitzt das Medium
        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3A, 0x00);   // medium not present
        return -1;
    }
    if (!s_host_write_enabled) {                    // Nur-Lese-Modus
        tud_msc_set_sense(lun, SCSI_SENSE_DATA_PROTECT, 0x27, 0x00);
        return -1;
    }
    s_last_host_write_ms = now_ms();                // für die 3-s-Schutzfrist
    return __real_tud_msc_write10_cb(lun, lba, offset, buf, n);
}

bool __wrap_tud_msc_test_unit_ready_cb(uint8_t lun)
{
    if (!s_host_io_enabled) { tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3A, 0x00); return false; }
    if (s_media_changed) {                          // einmalig nach der Rückgabe
        s_media_changed = false;
        tud_msc_set_sense(lun, SCSI_SENSE_UNIT_ATTENTION, 0x28, 0x00);
        return false;
    }
    return __real_tud_msc_test_unit_ready_cb(lun);
}
```

Umschalten USB → APP (`drive.c`, gekürzt):

```c
set_host_io(false);                                         // 1. neue Host-Zugriffe abweisen
if (!usb_dev_barrier(2000) || !usb_dev_barrier(2000)) {     // 2. laufende/verzögerte Schreibzugriffe abwarten
    set_host_io(true);
    return DRIVE_ERR_IO;
}
tinyusb_msc_set_storage_mount_point(s_storage, TINYUSB_MSC_STORAGE_MOUNT_APP);   // 3. FAT lokal einbinden
```

Barriere (`usb_dev.c`): ein Marker in der Warteschlange der TinyUSB-Task, mit Sequenznummer.

```c
static void barrier_fn(void *param) { s_barrier_done = (uint32_t)(uintptr_t)param; xSemaphoreGive(s_barrier); }

bool usb_dev_barrier(uint32_t timeout_ms)
{
    const uint32_t my = ++s_barrier_seq;
    usbd_defer_func(barrier_fn, (void *)(uintptr_t)my, false);   // läuft nach allem bereits Eingereihten
    /* ... warten, bis s_barrier_done == my oder Timeout ... */
}
```

Auswerfen erkennen (freier TinyUSB-Hook, `START STOP UNIT` mit LOEJ=1, START=0):

```c
void tud_msc_scsi_complete_cb(uint8_t lun, uint8_t const cmd[16])
{
    if (cmd[0] == 0x1B && (cmd[4] & 0x02) && !(cmd[4] & 0x01)) { /* Ereignis an drive_task */ }
}
```

## Hardware und Software

| | |
|---|---|
| Board | LILYGO T-Dongle-S3 (LCD), ESP32-S3 Rev. v0.2, 16 MB Flash (W25Q128), kein PSRAM |
| LCD | ST7735, 160×80, SPI: MOSI 3, SCLK 5, CS 4, DC 2, RST 1, Licht 38 (aktiv LOW) |
| microSD | SDMMC 4-Bit, 40 MHz: CLK 12, CMD 16, D0 14, D1 17, D2 21, D3 18 |
| Sonstiges | RGB-LED APA102 (DATA 40, CLK 39), BOOT-Taste GPIO 0 |
| Framework | ESP-IDF v5.5.6, esp_tinyusb 2.4.0, TinyUSB 0.21.0~2, mDNS 1.14.x |
| USB | Full Speed, Verbundgerät MSC + CDC-Logport (VID 303A, PID 4003), 500 mA, USB-Namen „WiFi DataDrive“/„DataDrive“, SCSI-Name „DataDrv WiFi DataDrive“ |

Flash-Aufteilung (`firmware/partitions.csv`, für spätere OTA-Updates vorbereitet):

```text
nvs      0x009000  0x005000   Einstellungen (WLAN, Flags)
otadata  0x00e000  0x002000
ota_0    0x010000  0x280000   Firmware (aktuell 1,06 MB, 60 % frei)
ota_1    0x290000  0x280000
storage  0x510000  0xAF0000   interner Dateispeicher (Wear-Levelling + FAT)
```

## Speichermedium

| | microSD (wenn gesteckt) | interner Flash (Rückfall) |
|---|---|---|
| Größe | Karte, getestet 64 GB | 10,8 MB |
| Dateisystem | FAT32 (exFAT geht nicht) | FAT12, 4-KiB-Cluster |
| Schreiben per USB | 832 KB/s | 47 KB/s |
| Upload per WLAN | ca. 1,3–1,6 MB/s | 46 KB/s |
| Lesen per USB | – | 753 KB/s |
| Automatisch formatieren | nie | nur beim allerersten Start |

Karte nur im ausgesteckten Zustand wechseln. Dateien dürfen höchstens 4 GB groß sein (FAT). Von der Firmware neu formatierte Medien heißen `DATADRIVE`; vorher formatierte behalten ihr Label (z. B. `PRINTDRIVE`).

Warum der interne Flash langsam ist: Jeder 4-KiB-Sektor muss vor dem Schreiben gelöscht werden. Zwei Dinge haben den Durchsatz von 7–12 auf 46–47 KB/s gebracht: Datenbereich auf 4 KiB ausrichten (`MKFS_PARM.align = 8`) und 4-KiB-Cluster, weil FatFs höchstens einen Cluster pro Schreibzugriff durchreicht.

## Weboberfläche und API

Eine einzelne HTML-Datei (`main/web/index.html`, reines JavaScript/CSS, keine externen Ressourcen). Sie wird beim Bauen gzip-komprimiert eingebettet und mit `Content-Encoding: gzip` ausgeliefert: 60 KB roh → 19 KB in Flash und Übertragung, Seitenaufruf vom Gerät 241 ms, freier Heap unverändert ca. 142 KB.

| Bereich | Inhalt |
|---|---|
| „Meine Dateien“ | Statusleiste (Modus + Speicher), Dateiliste mit Typ-Symbol, Größe, Änderungsdatum; Suche; Sortierung (Spaltenköpfe am PC, Auswahlfeld am Handy) |
| Aktionen je Datei | Herunterladen, Umbenennen, Löschen (Kontextmenü am PC, Bottom Sheet am Handy) |
| Hochladen | Knopf oder Drag & Drop, beliebige Dateitypen, Warteschlange mit Fortschritt und Abbrechen; Erfolgsmeldung erst nach Bestätigung durch das Gerät |
| Zustände | leer, gesperrt (USB-Laufwerk aktiv), Fehler (Speicher nicht lesbar, keine Antwort) |
| Einstellungen | Netzwerk, Speicher & USB, Sicherheit, System; getrennt der „Gefahrenbereich“ (Formatieren mit getipptem `LÖSCHEN`, Wartungsmodus = Flash-Modus) |
| Darstellung | hell/dunkel, automatisch oder fest, je Browser gespeichert |

„Nach dem Hochladen wieder bereitstellen“ (je Browser, Standard an): Nur wenn der Upload selbst dem USB-Gerät das Laufwerk genommen hat, wird es danach zurückgegeben.

Das Gerät hat keine Uhr. Der Browser schickt beim Upload das Änderungsdatum der Datei und seine aktuelle Zeit mit (`mtime`, `now`, als Ortszeit gespeichert).

Die API Version 2 steht in [07_api.md](07_api.md); seit 0.3.0 gelten [09_api_v3.md](09_api_v3.md)
und [12_api_v4.md](12_api_v4.md). Kurzfassung der Version 2:

- Lesen per `GET`: `/api/status`, `/api/files` (mit `mtime`, Anzahl ausgeblendeter Ordner `dirs`, `truncated`), `/api/download`, `/api/log`.
- Ändern per `POST` mit Header `X-PrintDrive: 1` (Schutz vor fremden Webseiten): Upload mit Vorabprüfung, Löschen, **Umbenennen** (`/api/rename`), Besitzer wechseln, Einstellungen, WLAN, Formatieren, Neustart.
- Alle Dateitypen sind erlaubt (`CONFIG_PD_UPLOAD_ALL_TYPES=y`); die ZIP-Prüfung gilt weiter für `.3mf`.
- Fehler kommen als JSON `{"ok":false,"code":"…","error":"…"}` mit deutschem Klartext.
- Die Dateiliste entsteht aus einem gepackten 32-KiB-Puffer (ca. 1000 übliche Namen) statt eines festen Felds mit 64 Einträgen.

Seit 0.2.0 war die Weboberfläche ohne Anmeldung und ohne Ordner. Beides kam mit 0.3.0 (siehe unten). Die API-Beschreibung für Version 2 steht in [07_api.md](07_api.md) und gilt nur noch als Geschichte; aktuell sind [09_api_v3.md](09_api_v3.md) und [12_api_v4.md](12_api_v4.md).

## WLAN, Display, Taste

- **WLAN:** Zugangsdaten liegen im NVS auf dem Dongle. Ohne Verbindung startet nach 20 s ein Einrichtungs-WLAN `DataDrive-XXXX` (WPA2, Passwort nur auf dem Display), Adresse `http://192.168.4.1`. Im Heimnetz: IP vom Display oder `http://datadrive.local`.
- **Display:** farbige Kopfzeile `BOOT` / `USB` (grün) / `BEREIT` (blau) / `UPLOAD` (orange) / `WLAN` / `FEHLER` (rot), darunter IP, Speicherbelegung, Zustand des USB-Geräts (z. B. `Gerät: verbunden`, `Kein USB-Gerät`), letzte Meldung.
- **BOOT-Taste:** kurz = zwischen USB-Laufwerk und Dateiverwaltung wechseln, 5 s halten = Flash-Modus.
  Seit 0.3.0 zusätzlich: einfacher Klick zeigt 20 s einen QR-Code mit der Adresse, Doppelklick
  wechselt die Betriebsart, 5 bis 10 s halten und loslassen = Wartungsmodus, 10 s halten =
  Benutzer auf `admin` / `admin` zurücksetzen (Dateien und WLAN bleiben). Die Bedienung am
  Gerät ist noch nicht als eigener Test protokolliert (siehe [05](05_testprotokoll.md)).

## Bauen und Flashen (Windows, PowerShell)

```powershell
.\tools\flash.ps1 -Project printdrive -Build     # bauen + flashen, ohne Tastendruck
.\tools\monitor.ps1                              # Log über den CDC-Port
python tools\api_test.py 192.168.2.152 --user admin --password <PW> --drive E:   # End-to-End-Test der API v4 (siehe 04)
python tools\mock_server.py --port 8090 --scenario files   # Weboberfläche ohne Hardware
```

`flash.ps1` öffnet den Logport mit 1200 Baud; die Firmware startet daraufhin in den Download-Modus, wird geflasht und bootet selbst wieder. WLAN-Daten und Dateien bleiben erhalten. `index.html` wird beim Bauen automatisch komprimiert (`main/web/gzip_file.py`).

Zwei Stolpersteine auf diesem Chip:

- Vor dem Software-Neustart in den Download-Modus muss der USB-PHY an USB-Serial/JTAG zurückgegeben werden (`RTC_CNTL_USB_CONF` überlebt Software-Resets), sonst erscheint gar kein USB-Gerät.
- Nach einem Flash mit gedrückter BOOT-Taste bleibt der Chip bis zum nächsten Abziehen im Download-Modus. Einmal aus- und einstecken.

## Dateien im Projekt

```text
firmware/components/   board (Pins), st7735 + display (LCD), pd_wifi, applog
firmware/printdrive/   Hauptfirmware (Ordnername historisch)
                       main/drive.c      Besitzer-Zustandsmaschine, Host-Sperre, Dateioperationen, Upload, Formatierung
                       main/usb_dev.c    TinyUSB, Deskriptoren, Barriere, Logport, 1200-Baud-Touch
                       main/web_server.c HTTP-API        main/web/index.html  Weboberfläche
                       main/web/gzip_file.py  komprimiert index.html beim Bauen
                       main/ui.c         LCD, LED, Taste
firmware/smoke_test/   Hardware-Schnelltest (LCD, LED, Taste, WLAN)
tools/                 idf.ps1, flash.ps1, monitor.ps1
                       api_test.py       End-to-End-Test der API am echten Gerät
                       mock_server.py    Testserver: Geräte-API ohne Hardware (Szenarien, /mock/set)
                       ui_test.py        Browser-Test der Weboberfläche gegen den Testserver
docs/                  Bestandsaufnahme, Architektur, Plan, Build/Flash, Testprotokoll, Risiken, API (07)
```

## Version 0.3.0 und 0.4.0 in Kurzform

Prüfstand: **verifiziert** = am Gerät geprüft (Ergebnis in [05](05_testprotokoll.md)),
**noch nicht geprüft** = gebaut, aber nicht getestet.

**Version 0.3.0** (API-Test am Gerät 182/182; Weboberfläche vom Besitzer im Browser geprüft):

- **Anmeldung:** Benutzer mit Rolle `admin` oder `user`. Standardkonto `admin` / `admin`; beim
  ersten Anmelden muss ein neues Passwort gesetzt werden. Vertraute Geräte (höchstens 4 je
  Benutzer). Nach Fehlversuchen wird die Anmeldung für die Adresse gesperrt, die Wartezeit
  steigt (30 s, dann doppelt, höchstens 15 Minuten). Details: [09](09_api_v3.md) Abschnitt 2.
- **Ordner und Dateiverwaltung:** anlegen, umbenennen, verschieben, kopieren, löschen;
  Mehrfachauswahl und Zwischenablage in der Weboberfläche.
- **Mehrere WLANs:** bis 5 gespeicherte Netze.
- **Taste und QR-Code:** Bedienung siehe oben; am Gerät **noch nicht geprüft**.
- **Senden:** Windows über `tools/sendto` (**noch nicht geprüft**), iPhone-Kurzbefehl nach
  [10](10_senden.md) (**noch nicht auf einem echten iPhone getestet**).
- **Firmware-Update über die Weboberfläche:** eingebaut, **noch nicht geprüft**. Alle
  Versionen bisher wurden per USB aufgespielt.
- **Deutsch und Englisch** in der Weboberfläche, **App-Icon** für den Home-Bildschirm
  (Icon am Handy **noch nicht geprüft**).
- **Bambu X2D:** erkennt den Stick und druckt von ihm (ohne Verschlüsselung, Versionen 0.2.0
  und 0.3.0); Rückmeldung des Besitzers, keine Messung.

**Version 0.4.0** (API-Lauf am Gerät 334/334 mit Verschlüsselungstest):

- **Verschlüsselung des Laufwerks**, optional, Voreinstellung aus. AES-256-XTS je 512-Byte-Sektor,
  gerechnet in der AES-Hardware des ESP32-S3. Der Schlüssel wird beim Entsperren aus dem
  Laufwerks-Passwort abgeleitet (etwa 2 bis 3,5 s) und nicht auf dem Stick gespeichert. Der
  Wiederherstellungsschlüssel wird nur einmal angezeigt. Einrichten löscht die ganze Karte.
  Anleitung: [README.md](../README.md), Abschnitt „Verschlüsselung einrichten und am Drucker
  benutzen“. Was die Verschlüsselung schützt und was nicht: [12](12_api_v4.md) Abschnitt 2.5.
- **Folgen im Alltag:** Nach jedem Einstecken und Stromausfall ist das Laufwerk gesperrt. Der
  Drucker sieht dann kein Medium, bis es im Browser entsperrt ist. Die Karte ist an anderen
  Geräten nicht lesbar. Das Volumen ist etwa 8 bis 12 MiB kleiner.
- **Selbsttest:** Die Verfahren werden beim Start mit den IEEE-1619-Prüfwerten getestet (bestanden).
- **Messungen** (64-GB-microSD): Einrichten 51 s; USB schreiben 820 KB/s (vorher 832 KB/s
  unverschlüsselt); USB lesen 659 KB/s (ein unverschlüsselter Vergleich fehlt, **noch nicht
  geprüft**); Web-Upload 1,0 MB/s, die Datei war danach auf dem PC bitgleich. Reine
  Rechenleistung im Selbsttest: 4,7 MB/s über die Hardware, 0,5 MB/s über die Bibliothek.
- **WLAN-Suche** im Dialog „WLAN hinzufügen“: verifiziert bei bestehender Verbindung (5 Netze in
  2,9 s); im Einrichtungsmodus **noch nicht geprüft**.
- **Anzeige „LIO“** auf dem Display, 48 × 40 Pixel, zeigt den Zustand. Urteil des Besitzers: gut.
- **HTTPS:** im Quelltext vorhanden, im Build abgeschaltet (`PD_HTTPS_OPTION`). Grund: Auf
  diesem Board ohne PSRAM kann der 16-KiB-TLS-Puffer nicht angelegt werden, Uploads scheitern.
  Vorgemerkt in [14](14_vormerkliste.md).
- **Noch nicht geprüft:** Druck am Bambu X2D von einem verschlüsselten, per Handy entsperrten
  Stick; Stromausfall während des Einrichtens oder Passwortwechsels; Verschlüsselung des
  internen Flashs (ohne Karte).

## Version 0.5.0 in Kurzform

API-Test am Gerät 354/354 (6 übersprungen). Schnittstelle: [15](15_api_v5.md), Ergebnisse:
[05](05_testprotokoll.md) Phase 9, Anleitungen in der [README.md](../README.md).

- **Netzlaufwerk (WebDAV)** unter `http://<Stick>/dav/`: nur per API-Test geprüft, mit echten
  Programmen **noch nicht geprüft**. Nimmt dem Drucker das Laufwerk nie weg. Windows braucht über
  `http` eine Registry-Änderung (`tools/webdav/Connect-DataDrive.ps1`), die iPhone-App „Dateien“
  kann kein WebDAV, der macOS-Finder kann nicht hineinkopieren (alles aus Recherche).
- **Uhrzeit aus dem Netz** mit Zeitzone: verifiziert. Alle Zeiten der API sind jetzt echte
  UTC-Sekunden. Der Abschnitt „Weboberfläche und API“ oben („Das Gerät hat keine Uhr“) gilt seit
  0.5.0 nur noch ohne Internet.
- **Automatisches Sperren** des verschlüsselten Laufwerks (Voreinstellung aus): Einstellungen
  verifiziert, Sperren nach Ablauf der Zeit **noch nicht geprüft**. Seit 0.6.0 läuft die Zeit nur in der
  Dateiverwaltung; am Drucker (USB-Modus) sperrt der Zeitablauf nie.
- **Display-Sprache** folgt der Weboberfläche: Einstellung verifiziert, englische Texte auf dem
  LCD **noch nicht geprüft**.
- **Einrichtungsseite öffnet sich von selbst:** gebaut, mit echtem Handy **noch nicht geprüft**.
- **Upload fortsetzen** und **Ordner als ZIP**: per API-Test verifiziert. Ordner hochladen nur am
  Testserver geprüft.
- **HTTPS:** im Build, Schalter aus bis zum Einschalten. Gemessen mit entsperrtem, verschlüsseltem
  Laufwerk: Upload 864 kB/s (30 MB, bitgleich), Download 389 kB/s (HTTP 487 kB/s), 66 KiB frei im
  Leerlauf, 22 KiB als Tiefstwert. iPhone mit Zertifikat, WebDAV über https und Dauerbetrieb
  **noch nicht geprüft**.
- Eine unabhängige Sicherheitsprüfung der Neuerungen läuft; Ergebnis steht aus.

## Offen

- **Verschlüsselung:** die Punkte oben unter „noch nicht geprüft“ und die Grenzen in
  [12](12_api_v4.md) Abschnitt 2.5 (Passwortstärke, Klartext am Drucker, keine Signatur der Updates).
- **Bambu X2D:** Grundkompatibilität am 2026-11-12 vom Besitzer bestätigt (Rückmeldung „alles geklappt inklusive Drucken“, keine Messung des Entwicklers): Standard-Build (MSC + CDC-Logport, USB-Gerät darf schreiben), microSD 64 GB FAT32; Drucker erkennt das Laufwerk, zeigt die per Web hochgeladene `.gcode.3mf` und druckt daraus. Ein Build ohne Logport ist für den X2D nicht nötig. Upload vom Handy bei eingestecktem Dongle mit anschließend sichtbarer neuer Datei: laut Rückmeldung bestanden, Einzelschritt nicht separat bestätigt. Nicht geprüft bzw. nicht gemessen: lange Drucke, Nur-Lese-Modus am Drucker, „Dateiverwaltung aktivieren“ während eines Drucks, Stromaufnahme, interner Flash als Medium am Drucker, andere Druckermodelle; keine Logs oder Datenraten vom Drucker.
- **Nur-Lese-Modus für das USB-Gerät:** eingebaut, am Gerät noch nicht geprüft.
- **Anmeldung (seit 0.3.0):** Passwörter gehen ohne HTTPS unverschlüsselt durchs WLAN. Die Anmeldung schützt vor anderen Nutzern im selben Netz, nicht vor jemandem, der den Funkverkehr mitschneidet. Seit 0.5.0 lässt sich HTTPS einschalten (Voreinstellung aus).
- **Neu offen nach 0.5.0:** siehe [14](14_vormerkliste.md), O1 bis O10.
- **Ordner (seit 0.3.0):** in der Weboberfläche vorhanden. Ob Ordner, die am PC angelegt wurden, dort richtig erscheinen: **noch nicht geprüft**.
- **Weboberfläche am echten Handy:** Der Browser-Test (`tools/ui_test.py`, 566/566 am Testserver) lief mit Chromium im Handyformat; echtes iOS Safari und echtes Drag & Drop sind ungeprüft.
- **3MF ist nicht automatisch druckbar:** Der Drucker braucht eine geslicete Datei (`.gcode.3mf`); eine solche Datei hat der X2D vom Dongle gedruckt (2026-11-12). Andere Dateiarten am Drucker sind ungeprüft.
