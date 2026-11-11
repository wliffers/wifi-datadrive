# 03 – Umsetzungsplan und Status

Stand: 2026-11-11.

Statuswerte:

| Status | Bedeutung |
|---|---|
| **implementiert** | Code geschrieben |
| **gebaut** | kompiliert und gelinkt |
| **verifiziert** | auf echter Hardware geprüft, Ergebnis im [Testprotokoll](05_testprotokoll.md) |
| **offen** | noch nicht begonnen bzw. nicht geprüft |

Gesamtbild: Phasen 0–3 sind vollständig implementiert. Der Smoke-Test ist gebaut, der
Build der Hauptfirmware steht noch aus. Auf der Hardware verifiziert sind bisher nur
Chip-Infos und Flash-ID (esptool).

## Phase 0 – Hardware und Smoke-Test

Ziel: Hardware kennen, Werkszustand sichern, Toolchain und Pinbelegung bestätigen.

| Abnahmekriterium | Status |
|---|---|
| Chip, Flash-Größe/-Typ, MAC, USB-ID per esptool ermittelt | **verifiziert** (2026-11-11, siehe [01](01_bestandsaufnahme.md)) |
| ESP-IDF v5.5.6 installiert, Projekt baut | **gebaut** (smoke_test) |
| Vollständiges Flash-Backup in `backup/` mit SHA256 | Skript implementiert; Backup **offen** |
| Smoke-Test: LCD zeigt Farbbalken rot/grün/blau/weiß korrekt orientiert | implementiert, gebaut, **offen** |
| Smoke-Test: LED leuchtet in den Farben, BOOT-Taste wird erkannt | implementiert, gebaut, **offen** |
| Smoke-Test: Log `SMOKE: flash size 16777216 bytes`, Partitionstabelle korrekt | implementiert, gebaut, **offen** |
| Smoke-Test: WLAN STA verbindet bzw. Setup-AP erscheint | implementiert, gebaut, **offen** |

## Phase 1 – USB-Massenspeicher unter Windows

Ziel: Dongle erscheint als normaler USB-Stick, Dateien lassen sich sicher kopieren.

| Abnahmekriterium | Status |
|---|---|
| Laufwerk erscheint im Explorer, Label `PRINTDRIVE`, ca. 10–11 MB, FAT16 | implementiert, **offen** |
| Dateien kopieren, lesen, auswerfen, neu einstecken – Inhalt unverändert | implementiert, **offen** |
| Erststart formatiert automatisch; defektes FS wird nie still formatiert | implementiert, **offen** |
| Schreibschutz-Modus: Host kann nur lesen | implementiert, **offen** |
| Log über CDC-Port lesbar (`tools/monitor.ps1`) | implementiert, **offen** |
| Flashen über 1200-Baud-Touch (`tools/flash.ps1`) | implementiert, **offen** |

## Phase 2 – Weboberfläche und sicheres Umschalten

Ziel: Upload per Browser, ohne dass Host und Firmware gleichzeitig schreiben.

| Abnahmekriterium | Status |
|---|---|
| Weboberfläche unter `http://printdrive.local` bzw. IP erreichbar | implementiert, **offen** |
| Upload einer `.3mf` (Temp-Datei, Prüfung, Umbenennen) | implementiert, **offen** |
| Upload-Abbruch hinterlässt keine Datei und keinen Temp-Rest | implementiert, **offen** |
| Ungültige Datei (kein ZIP) wird abgewiesen (422) | implementiert, **offen** |
| Speicher voll wird vor dem Upload erkannt (507) | implementiert, **offen** |
| Umschalten abgewiesen, solange der Host schreibt oder ein Upload läuft | implementiert, **offen** |
| Auswurf am Host schaltet automatisch in den Upload-Modus | implementiert, **offen** |
| Nach Bereitstellen sieht der Host die neuen Dateien (Medienwechsel/Re-Plug) | implementiert, **offen** |
| Stromausfall während Upload: Temp-Datei wird beim nächsten Start entfernt | implementiert, **offen** |
| WLAN-Einrichtung über Setup-AP und Weboberfläche | implementiert, **offen** |
| Löschen, Download, Formatieren, Neustart, Flash-Modus per Web | implementiert, **offen** |

## Phase 3 – LCD und Bedienung

| Abnahmekriterium | Status |
|---|---|
| LCD zeigt Zustand (`BOOT/USB/BEREIT/UPLOAD/WLAN/FEHLER`), IP bzw. AP-Daten, Speicher, Host-Status | implementiert, **offen** |
| LED in Zustandsfarbe | implementiert, **offen** |
| BOOT kurz: Umschalten mit Bestätigung per zweitem Druck | implementiert, **offen** |
| BOOT 5 s: Download-Modus | implementiert, **offen** |
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
| microSD als größerer Speicher statt/zusätzlich zum internen Flash | **offen**, nicht implementiert (Pins in `board.h` reserviert) |

## Nächste Schritte

1. Flash-Backup erstellen (`tools/backup_flash.ps1`).
2. Smoke-Test flashen und Phase-0-Tests protokollieren.
3. Hauptfirmware bauen, flashen, Phase-1/2/3-Tests unter Windows durchführen.
4. Erkenntnisse in Code/Kconfig übernehmen, dann Drucker-Test (Phase 4).

## Backlog nach dem MVP

| Thema | Notiz |
|---|---|
| Authentifizierung | Zugriffsschutz für die Weboberfläche (bisher nur LAN + CSRF-Header) |
| Captive Portal | Setup-AP öffnet die Einrichtungsseite automatisch |
| OTA-Update | Partitionen sind vorbereitet (`ota_0`/`ota_1`), Update-Weg fehlt |
| microSD | größere Kapazität (siehe Phase 5) |
| Workflow ALLPLAN / 3MFStudio | Datei direkt aus der Planungs-/Slicer-Umgebung an den Dongle senden; Ablauf offen |
| NVS-Verschlüsselung | WLAN-Passwort liegt unverschlüsselt im NVS |
