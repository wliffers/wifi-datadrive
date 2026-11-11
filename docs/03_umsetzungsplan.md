# 03 – Umsetzungsplan und Status

Stand: 2026-11-11.

Statuswerte:

| Status | Bedeutung |
|---|---|
| **implementiert** | Code geschrieben |
| **gebaut** | kompiliert und gelinkt |
| **verifiziert** | auf echter Hardware geprüft, Ergebnis im [Testprotokoll](05_testprotokoll.md) |
| **offen** | noch nicht begonnen bzw. nicht geprüft |

Gesamtbild: Phasen 0–3 sind vollständig implementiert und gebaut. Am 2026-11-11 auf der
Hardware verifiziert: Phase 0 (außer WLAN STA) und Phase 1 (außer Schreibschutz-Modus) unter
Windows 11, dazu das LCD-Statusbild im Zustand `USB`. Phase 2 (Web) ist noch nicht auf der
Hardware getestet, weil das WLAN noch nicht eingerichtet ist. Zwei Fehler wurden dabei
gefunden und behoben (FAT-Ausrichtung, USB-PHY beim Download-Modus), siehe [06](06_risiken.md).

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
| Smoke-Test: WLAN STA verbindet bzw. Setup-AP erscheint | Setup-AP **verifiziert** (`PrintDrive-54D5`); STA **offen** |

## Phase 1 – USB-Massenspeicher unter Windows

Ziel: Dongle erscheint als normaler USB-Stick, Dateien lassen sich sicher kopieren.

| Abnahmekriterium | Status |
|---|---|
| Laufwerk erscheint im Explorer, Label `PRINTDRIVE`, ca. 10–11 MB, FAT16 | **verifiziert** (11.288.576 Byte, T1.2) |
| Dateien kopieren, lesen, auswerfen, neu einstecken – Inhalt unverändert | **verifiziert** (SHA256, Re-Plug per BOOT, T1.3) |
| Erststart formatiert automatisch; defektes FS wird nie still formatiert | Erststart **verifiziert** (T1.1); Schutz vor stillem Formatieren **offen** |
| Schreibschutz-Modus: Host kann nur lesen | implementiert, **offen** (braucht Web) |
| Log über CDC-Port lesbar (`tools/monitor.ps1`) | **verifiziert**, inkl. nachgeliefertem Start-Log |
| Flashen über 1200-Baud-Touch (`tools/flash.ps1`) | **verifiziert** (T1.5, nach Fix USB-PHY) |
| Datenrate USB gemessen | **verifiziert**: schreiben 47 KB/s, lesen 753 KB/s (T2.10) |

## Phase 2 – Weboberfläche und sicheres Umschalten

Ziel: Upload per Browser, ohne dass Host und Firmware gleichzeitig schreiben.
Alle Punkte implementiert und gebaut; Hardware-Test wartet auf die WLAN-Einrichtung.

| Abnahmekriterium | Status |
|---|---|
| Weboberfläche unter `http://printdrive.local` bzw. IP erreichbar | implementiert, **offen** |
| Upload einer `.3mf` (Temp-Datei, Prüfung, Umbenennen) | implementiert, **offen** |
| Upload-Abbruch hinterlässt keine Datei und keinen Temp-Rest | implementiert, **offen** |
| Ungültige Datei (kein ZIP) wird abgewiesen (422) | implementiert, **offen** |
| Speicher voll wird vor dem Upload erkannt (507) | implementiert, **offen** |
| Umschalten abgewiesen, solange der Host schreibt oder ein Upload läuft | implementiert, **offen** |
| Auswurf am Host schaltet automatisch in den Upload-Modus | **verifiziert** (T1.3) |
| Nach Bereitstellen sieht der Host die neuen Dateien (Medienwechsel/Re-Plug) | Re-Plug **verifiziert** (BOOT kurz, T1.3); mit Datei aus Web-Upload **offen** |
| Stromausfall während Upload: Temp-Datei wird beim nächsten Start entfernt | implementiert, **offen** |
| WLAN-Einrichtung über Setup-AP und Weboberfläche | implementiert, **offen** |
| Löschen, Download, Formatieren, Neustart, Flash-Modus per Web | implementiert, **offen** (Download-Modus-Funktion selbst über 1200-Baud-Touch verifiziert) |
| Upload-Datenrate gemessen | **offen** (erwartet ähnlich USB-Schreiben, Flash-Grenze) |

## Phase 3 – LCD und Bedienung

| Abnahmekriterium | Status |
|---|---|
| LCD zeigt Zustand (`BOOT/USB/BEREIT/UPLOAD/WLAN/FEHLER`), IP bzw. AP-Daten, Speicher, Host-Status | teilweise **verifiziert**: Bild `USB` mit AP-Daten, Speicher, Host, Dateizahl (Foto, T1.2); übrige Zustände **offen** |
| LED in Zustandsfarbe | teilweise **verifiziert** (grün bei `USB`) |
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
| Stromversorgung am Drucker-USB stabil (kein Reset bei WLAN-Last) | **offen** |

## Phase 5 – microSD (optional)

| Abnahmekriterium | Status |
|---|---|
| microSD als größerer und schnellerer Speicher statt/zusätzlich zum internen Flash | **offen**, nicht implementiert (Pins in `board.h` reserviert) |

## Nächste Schritte

1. WLAN über den Setup-AP per Handy einrichten (T2.9), STA-Verbindung protokollieren (T0.5).
2. Phase-2-Tests durchführen, Web-Upload-Datenrate messen (T2.10).
3. Restliche Punkte aus Phase 1/3 (Schreibschutz T1.4, LCD-Zustände, BOOT-Bestätigung).
4. Drucker-Test (Phase 4).

## Backlog nach dem MVP

| Thema | Notiz |
|---|---|
| Authentifizierung | Zugriffsschutz für die Weboberfläche (bisher nur LAN + CSRF-Header) |
| Captive Portal | Setup-AP öffnet die Einrichtungsseite automatisch |
| OTA-Update | Partitionen sind vorbereitet (`ota_0`/`ota_1`), Update-Weg fehlt |
| microSD | größere Kapazität und schnelleres Schreiben (siehe Phase 5) |
| Workflow ALLPLAN / 3MFStudio | Datei direkt aus der Planungs-/Slicer-Umgebung an den Dongle senden; Ablauf offen |
| NVS-Verschlüsselung | WLAN-Passwort liegt unverschlüsselt im NVS |
