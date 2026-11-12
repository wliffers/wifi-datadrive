# 03 – Umsetzungsplan und Status

Stand: 2026-11-12, Firmware 0.2.0 (WiFi DataDrive). Das Projekt hieß bis Version 0.1.0
„WiFi PrintDrive“; ältere Einträge nennen deshalb noch `PrintDrive-54D5`, `PRINTDRIVE` oder
`printdrive.local`. Der Ordner `firmware/printdrive` behält seinen Namen.

Statuswerte:

| Status | Bedeutung |
|---|---|
| **implementiert** | Code geschrieben |
| **gebaut** | kompiliert und gelinkt |
| **verifiziert** | auf echter Hardware geprüft, Ergebnis im [Testprotokoll](05_testprotokoll.md) |
| **offen** | noch nicht begonnen bzw. nicht geprüft |

Gesamtbild: Phasen 0–3 und 5 sind implementiert und gebaut. Am 2026-11-11 auf der
Hardware unter Windows 11 verifiziert: Phase 0, 1 (außer Schreibschutz-Modus) und 2
(`tools/api_test.py`: Flash 18/18, microSD 21/21), Phase 3 (LCD per Foto, außer Nuancen der
LED-Farben), Phase 5 microSD (Grundfunktion), Phase 6 (Umbenennung, API v2 mit
`api_test.py` 28/28, gzip-Auslieferung der neuen Weboberfläche). Am 2026-11-12 hat die neue
Weboberfläche den Browser-Test am Testserver vollständig bestanden (`tools/ui_test.py`
566/566, T6.4); die Endfassung ist auf dem Gerät. Nicht geprüft sind die Abläufe der
Oberfläche am echten Gerät und an einem echten Handy. Offen ist Phase 4 (Bambu Lab X2D).
Gefundene und behobene Fehler: FAT-Ausrichtung, USB-PHY beim Download-Modus, beide
Modus-Knöpfe sichtbar, Verbindungsabbruch statt Fehlerantwort (`drain_body`), langsamer
Web-Upload (2-KiB-Cluster), 32-Bit-Überlauf der Upload-Größe; siehe [06](06_risiken.md).

## Phase 0 – Hardware und Smoke-Test

Ziel: Hardware kennen, Werkszustand sichern, Toolchain und Pinbelegung bestätigen.

| Abnahmekriterium | Status |
|---|---|
| Chip, Flash-Größe/-Typ, MAC, USB-ID per esptool ermittelt | **verifiziert** (2026-11-11, siehe [01](01_bestandsaufnahme.md)) |
| ESP-IDF v5.5.6 installiert, Projekt baut | **gebaut** (smoke_test, printdrive) |
| Vollständiges Flash-Backup in `backup/` mit SHA256 | **Befund**: über USB-Serial/JTAG an diesem PC unzuverlässig; Ersatz: LILYGO-Werksabbild in `backup/` mit SHA256 (T0.1) |
| Smoke-Test: LCD zeigt Farbbalken rot/grün/blau/weiß korrekt orientiert | **verifiziert** (2026-11-11) |
| Smoke-Test: LED leuchtet in den Farben, BOOT-Taste wird erkannt | **verifiziert** (2026-11-11) |
| Smoke-Test: Log `SMOKE: flash size 16777216 bytes`, Partitionstabelle korrekt | **verifiziert** (16 MB per esptool, kein Absturz, ca. 220 KB Heap; Partitionszeilen nicht einzeln protokolliert) |
| Smoke-Test: WLAN STA verbindet bzw. Setup-AP erscheint | Setup-AP **verifiziert** (`PrintDrive-54D5`); STA **verifiziert** mit der Hauptfirmware (Heimnetz, IP `192.168.2.152`, T0.5) |

## Phase 1 – USB-Massenspeicher unter Windows

Ziel: Dongle erscheint als normaler USB-Stick, Dateien lassen sich sicher kopieren.

| Abnahmekriterium | Status |
|---|---|
| Laufwerk erscheint im Explorer, Label `PRINTDRIVE` (seit 0.2.0 bei Neuformatierung `DATADRIVE`), ca. 10–11 MB | **verifiziert** (11.288.576 Byte, damals FAT16, T1.2; heute FAT12 mit 4-KiB-Clustern) |
| Dateien kopieren, lesen, auswerfen, neu einstecken – Inhalt unverändert | **verifiziert** (SHA256, Re-Plug per BOOT, T1.3) |
| Erststart formatiert automatisch; defektes FS wird nie still formatiert | Erststart **verifiziert** (T1.1); Schutz vor stillem Formatieren **offen** |
| Schreibschutz-Modus: Host kann nur lesen | implementiert, **offen** (T1.4, Web jetzt verfügbar) |
| Log über CDC-Port lesbar (`tools/monitor.ps1`) | **verifiziert**, inkl. nachgeliefertem Start-Log |
| Flashen über 1200-Baud-Touch (`tools/flash.ps1`) | **verifiziert** (T1.5, nach Fix USB-PHY); Update-Schleife `-Build` mehrfach vollautomatisch |
| Datenrate USB gemessen | **verifiziert**: Flash schreiben 47 KB/s, lesen 753 KB/s; microSD schreiben 832 KB/s (T2.10) |

## Phase 2 – Weboberfläche und sicheres Umschalten

Ziel: Upload per Browser, ohne dass Host und Firmware gleichzeitig schreiben.
Automatisiert geprüft mit `tools/api_test.py` (T2.11).

| Abnahmekriterium | Status |
|---|---|
| Weboberfläche unter `http://datadrive.local` (bis 0.1.0 `printdrive.local`) bzw. IP erreichbar | **verifiziert** (IP `192.168.2.152`, Handy); Fehler „beide Modus-Knöpfe sichtbar“ behoben |
| Upload einer `.3mf` (Temp-Datei, Prüfung, Umbenennen) | **verifiziert** (Umlaut-Name, Download identisch, T2.11) |
| Andere Dateitypen | bis 0.1.0 abgewiesen (415, **verifiziert**); seit 0.2.0 standardmäßig erlaubt (Phase 6) |
| Upload-Abbruch hinterlässt keine Datei und keinen Temp-Rest | **verifiziert** (kein Temp-Rest, kein Busy-Zustand, T2.5/T2.11) |
| Ungültige Datei (kein ZIP) wird abgewiesen (422) | **verifiziert** (kein ZIP und abgeschnittenes ZIP, T2.7) |
| Speicher voll wird vor dem Upload erkannt (507) | **verifiziert** per `upload/check` (T2.6); Datei ≥ 4 GiB → 413 |
| Umschalten abgewiesen, solange der Host schreibt oder ein Upload läuft | Übernahme ohne Bestätigung → `HOST_ACTIVE` **verifiziert**; `HOST_WRITING` und `BUSY` **offen** |
| Auswurf am Host schaltet automatisch in den Upload-Modus | **verifiziert** (T1.3) |
| Nach Bereitstellen sieht der Host die neuen Dateien (Medienwechsel/Re-Plug) | **verifiziert** (Datei aus Web-Upload auf `E:` identisch, T2.11) |
| Stromausfall während Upload: Temp-Datei wird beim nächsten Start entfernt | implementiert, **offen** |
| WLAN-Einrichtung über Setup-AP und Weboberfläche | **verifiziert** (Handy, T2.9) |
| Löschen, Download, Formatieren, Neustart, Flash-Modus per Web | Löschen, Download, Formatieren (mit Neustart) **verifiziert**; Flash-Modus per Web **offen** (Funktion über 1200-Baud-Touch verifiziert) |
| Upload-Datenrate gemessen | **verifiziert**: Flash 46 KB/s (4-KiB-Cluster; 7 KB/s mit 2 KiB), microSD 1335–1578 KB/s (T2.10) |

## Phase 3 – LCD und Bedienung

| Abnahmekriterium | Status |
|---|---|
| LCD zeigt Zustand (`BOOT/USB/BEREIT/UPLOAD/WLAN/FEHLER`), IP bzw. AP-Daten, Speicher, Host-Status | **verifiziert** (Foto) |
| LED in Zustandsfarbe | **verifiziert**, außer Nuancen der Farben |
| BOOT kurz: Umschalten mit Bestätigung per zweitem Druck | Umschalten nach Auswurf **verifiziert** (T1.3); Bestätigung per zweitem Druck **offen** |
| BOOT 5 s: Download-Modus | implementiert, **offen** (gleiche Funktion wie 1200-Baud-Touch) |
| Fehlerbild bei Init-Fehler | implementiert, **offen** |

## Phase 4 – Test am Bambu Lab X2D

Ziel: Drucker erkennt das Laufwerk und druckt eine hochgeladene Datei.
**Kompatibilität ist nicht verifiziert**, siehe [06_risiken.md](06_risiken.md).

| Abnahmekriterium | Status |
|---|---|
| Drucker erkennt das Laufwerk und listet Dateien | **offen** |
| Druck einer per Web hochgeladenen, gesliceten `.gcode.3mf` startet | **offen** |
| Upload während der Drucker angesteckt ist, danach neue Datei am Drucker sichtbar | **offen** |
| Empfohlene Einstellungen für den Drucker ermittelt (Host nur lesen? CDC aus? Re-Plug?) | **offen** |
| Stromversorgung am Drucker-USB stabil (kein Reset bei WLAN-Last, mit microSD) | **offen** |

Empfehlung für den ersten Test: microSD-Karte verwenden (FAT32, groß, schnell), Host nur
lesen erwägen (vorher T1.4 am PC), bei Problemen mit dem Composite-Gerät mit
`CONFIG_TINYUSB_CDC_ENABLED=n` bauen.

## Phase 5 – microSD

| Abnahmekriterium | Status |
|---|---|
| microSD als größerer und schnellerer Speicher statt des internen Flashs | **verifiziert** (64-GB-Karte FAT32, Windows `E:` 62,5 GB, T5.1) |
| SDMMC 4 Bit an den Pins aus dem Hersteller-Repo | **verifiziert** (40 MHz, Slot auf dieser Board-Revision bestätigt) |
| Ohne Karte Rückfall auf internen Flash | implementiert, nicht einzeln protokolliert (T5.3) |
| Karte wird nie automatisch formatiert; Web-Formatieren erzeugt FAT32 | implementiert; Format-Anforderung je Medium getrennt; FAT32-Formatierung einer Karte **offen** (Testkarte war schon FAT32) |
| Datenrate | **verifiziert**: USB schreiben 832 KB/s, Web-Upload 1335–1578 KB/s |
| Web-API auf microSD | **verifiziert** (`api_test.py` 21/21, inkl. 413 bei ≥ 4 GiB) |
| exFAT-Karten, Kartenqualität, Stromaufnahme | **offen** (exFAT nicht unterstützt, nur per Web auf FAT32 formatierbar) |

## Phase 6 – UI-Redesign / WiFi DataDrive (Firmware 0.2.0)

Ziel: Aus dem Drucker-Stick wird ein allgemeiner WLAN-USB-Stick mit Dateiverwaltung im
Browser. Neuer Name, neue Weboberfläche, API Version 2 ([07_api.md](07_api.md)). Die
Sicherungen des Laufwerks (ein Besitzer, Host-I/O-Sperre, Barrier, Bestätigungsregeln,
Schreib-Schutzfrist) bleiben unverändert. Ergebnisse: [05](05_testprotokoll.md) Phase 6.

| Abnahmekriterium | Status |
|---|---|
| Umbenennung: Produkt „WiFi DataDrive“, USB-Strings „WiFi DataDrive“/„DataDrive“, SCSI-Inquiry „DataDrv / WiFi DataDrive“, Hostname `datadrive`, Setup-WLAN `DataDrive-XXXX`, Label `DATADRIVE` bei Neuformatierung; interne Namen (`firmware/printdrive`, `X-PrintDrive`, `pd_`) bleiben | **verifiziert** (2026-11-11, T6.1) |
| LCD-Texte „Gerät“ statt „Host“ (`Gerät: verbunden`, `Kein USB-Gerät`) | **verifiziert** (T6.1) |
| API v2: alle Dateitypen (`CONFIG_PD_UPLOAD_ALL_TYPES=y`, ZIP-Prüfung weiter für `.3mf`), `POST /api/rename`, Upload-Parameter `mtime`/`now`, `/api/files` mit `mtime`/`dirs`/`truncated`, Status mit `types`, Fehlertexte in einfacher Sprache | **verifiziert** (`api_test.py` 28/28, microSD, T6.2) |
| Dateiliste aus gepacktem 32-KiB-Puffer (ca. 1000 übliche Namen) statt 64 Einträgen | implementiert, Felder **verifiziert** (T6.2); Kürzung bei sehr vielen Dateien am Gerät **offen** |
| Weboberfläche als eine HTML-Datei, gzip-komprimiert eingebettet und ausgeliefert | **verifiziert**: 60 KB → 19 KB, Seitenaufruf 241 ms, Heap unverändert ca. 142 KB, App-Binary 1,06 MB (60 % der Partition frei, T6.3); Endfassung 66 KB → ca. 20,5 KB, auf dem Gerät |
| Hauptansicht „Meine Dateien“: Statusleiste, Dateiliste (Typ-Symbol, Größe, Änderungsdatum), Suche, Sortierung, Aktionsmenü (Herunterladen, Umbenennen, Löschen), Upload-Warteschlange mit Fortschritt/Abbrechen, Drag & Drop, Zustände leer/gesperrt/Fehler | implementiert, Browser-Test am Testserver **bestanden** (T6.4); echtes Drag & Drop und Abläufe am echten Gerät **offen** |
| Einstellungen: Netzwerk, Speicher & USB, Sicherheit, System, „Gefahrenbereich“ (Formatieren mit getipptem `LÖSCHEN`, Wartungsmodus) | implementiert, Browser-Test am Testserver **bestanden** (T6.4); am echten Gerät **offen** |
| Hell-/Dunkel-Darstellung (automatisch oder fest, je Browser), neues Symbol | implementiert, Browser-Test am Testserver **bestanden** (T6.4, inkl. Kontrast) |
| „Nach dem Hochladen wieder bereitstellen“ gibt das Laufwerk nur zurück, wenn der Upload es selbst genommen hat | implementiert, Browser-Test am Testserver **bestanden** (T6.4); am echten Gerät **offen** |
| Erfolgsmeldungen erst nach Bestätigung durch das Gerät | implementiert, Browser-Test am Testserver **bestanden** (T6.4) |
| Bedienung am Handy (Menü als Bottom Sheet, 320–390 px Breite, Touch-Ziele) | Emulation **bestanden** (T6.4); echter Handy-Browser (iOS Safari: Bildschirmtastatur, Safe Areas, `download`-Attribut) **offen** |
| Testserver `tools/mock_server.py` (API-Nachbildung mit Szenarien und `/mock/set`) | vorhanden |
| Browser-Test `tools/ui_test.py` (Playwright mit installiertem Edge, gegen den Testserver) | vorhanden, **566/566 bestanden** (2026-11-12; PC 1280×800, Handy-Emulation 390×844, hell/dunkel; 26 Fehler in zwei Runden gefunden und behoben) |
| Ordner in der Weboberfläche | nicht umgesetzt (machbar, zurückgestellt) |
| Passwortschutz der Weboberfläche | nicht umgesetzt (zurückgestellt, braucht ein Konzept für „Passwort vergessen“) |

## Nächste Schritte

1. Neue Weboberfläche am echten Gerät und an einem echten Handy durchspielen (T6.4 B14–B16:
   Abläufe am Dongle, iOS Safari, echtes Drag & Drop).
2. Nur-Lese-Modus des Hosts am PC prüfen (T1.4).
3. Drucker-Test (Phase 4) mit microSD-Karte.
4. Restpunkte: `HOST_WRITING`/`BUSY` (T2.2, T2.3), Stromausfall (T2.8), Flash-Modus per Web,
   BOOT-Bestätigung, LED-Farbnuancen.

## Backlog nach dem MVP

| Thema | Notiz |
|---|---|
| Authentifizierung | Zugriffsschutz für die Weboberfläche (bisher nur LAN + CSRF-Header); zurückgestellt, braucht ein Konzept für „Passwort vergessen“ |
| Ordner | Unterordner in der Weboberfläche anzeigen und anlegen; machbar, zurückgestellt. Bisher nur Hauptverzeichnis, vorhandene Ordner werden gezählt |
| Captive Portal | Setup-AP öffnet die Einrichtungsseite automatisch |
| OTA-Update | Partitionen sind vorbereitet (`ota_0`/`ota_1`), Update-Weg fehlt |
| microSD-Komfort | Hot-Swap nicht möglich (Slot am Stecker); ggf. Anzeige der Karte in der Weboberfläche ausbauen, exFAT nur mit anderer FatFs-Konfiguration |
| Workflow ALLPLAN / 3MFStudio | Datei direkt aus der Planungs-/Slicer-Umgebung an den Dongle senden; Ablauf offen |
| NVS-Verschlüsselung | WLAN-Passwort liegt unverschlüsselt im NVS |
