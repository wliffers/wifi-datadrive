# Vormerkliste

Stand: 2026-11-12, nach Firmware 0.5.0. Die Punkte V1 bis V8 hatte der Besitzer nach 0.4.0
ausgewählt; sie sind in 0.5.0 umgesetzt. Was davon am Gerät geprüft ist, steht in der Spalte
„Stand“ (Einzelheiten: [05_testprotokoll.md](05_testprotokoll.md) Phase 9, Schnittstelle:
[15_api_v5.md](15_api_v5.md)).

## Erledigt in 0.5.0

| Nr. | Funktion | Kurzbeschreibung | Stand |
|---|---|---|---|
| V1 | Stick als Netzlaufwerk (WebDAV) | Der Stick erscheint unter `http://<Stick>/dav/` als Netzlaufwerk; Dateien ohne Browser hineinziehen. | **erledigt in 0.5.0.** Am Gerät nur per API-Test geprüft; mit Windows-Explorer, Finder und Handy-Apps noch nicht geprüft. Die iPhone-App „Dateien“ kann kein WebDAV (Recherche), eine Zusatz-App ist nötig. |
| V2 | Automatisches Sperren | Das verschlüsselte Laufwerk sperrt sich nach einstellbarer Zeit ohne Zugriff und/oder wenn das USB-Gerät getrennt wird. Voreinstellung aus. | **erledigt in 0.5.0.** Einstellungen per API-Test geprüft; das Sperren nach Ablauf der Zeit noch nicht. |
| V3 | Uhrzeit aus dem Netz | Uhr per NTP, Zeitzone wählbar; alle Zeiten in der API sind echte UTC-Sekunden. | **erledigt in 0.5.0.** Am Gerät verifiziert (Uhr nach dem Start gestellt). |
| V4 | Abgebrochene Uploads fortsetzen | Für große Dateien und wackliges WLAN; ein angefangener Upload zur Zeit. | **erledigt in 0.5.0.** Per API-Test am Gerät verifiziert. |
| V5 | Ganze Ordner hochladen, Ordner als ZIP herunterladen | ZIP unkomprimiert, höchstens 4096 Dateien. | **erledigt in 0.5.0.** ZIP per API-Test am Gerät verifiziert; Ordner hochladen nur am Testserver geprüft. |
| V6 | Einrichtungsseite öffnet sich von selbst | Beim Verbinden mit dem Zugangspunkt des Sticks springt die Seite auf (wie bei Hotel-WLAN). | **erledigt in 0.5.0.** Gebaut, mit einem echten Handy noch nicht geprüft. |
| V7 | Display folgt der Sprache der Oberfläche | Deutsch oder Englisch, je nach Einstellung in der Weboberfläche. | **erledigt in 0.5.0.** Einstellung per API-Test geprüft; Aussehen der englischen Texte auf dem LCD noch nicht. |
| V8 | HTTPS mit Speicher-Umbau | TLS-Puffer einmalig beim Start reserviert, eine TLS-Verbindung gleichzeitig, Dateiliste ohne 33-KiB-Puffer. Option im Build an, Schalter aus bis zum Einschalten. | **erledigt in 0.5.0.** Am Gerät gemessen: Upload 30 MB 864 kB/s, Download 389 kB/s, 22 KiB als tiefster freier Speicher. iPhone mit Zertifikat, WebDAV über https und Dauerbetrieb noch nicht geprüft. |

## Neu offen nach 0.5.0

Keine neuen Funktionen, sondern Prüfungen und Klärungen. Reihenfolge ohne Rangfolge.

| Nr. | Punkt | Was zu tun ist |
|---|---|---|
| O1 | Netzlaufwerk mit echten Programmen | Windows-Explorer mit `tools/webdav/Connect-DataDrive.ps1`, eine iPhone-App mit WebDAV, Android, macOS-Finder (nur Lesen erwartet). Ergebnisse in [05](05_testprotokoll.md) T9.3. |
| O2 | Automatisches Sperren am Gerät | Sperren nach Ablauf der Zeit, beim Trennen des USB-Geräts, kein Sperren während eines Schreibvorgangs (T9.7). |
| O3 | Verhalten des Bambu X2D beim Drucken | Klären, ob der X2D eine Druckdatei beim Start ganz einliest oder während des Drucks weiter vom Stick liest. Davon hängt ab, welche Sperrzeit für lange Drucke sicher ist. |
| O4 | Einrichtungsseite mit echtem Handy | iPhone und Android am Einrichtungs-WLAN des Sticks (T9.10). |
| O5 | Englische Texte auf dem LCD | Ansehen, ob alle Texte passen (T9.9). |
| O6 | HTTPS-Restpunkte | iPhone mit installiertem Zertifikat (Profil installieren, volles Vertrauen in den Zertifikatsvertrauenseinstellungen), Home-Bildschirm-Symbol und Kurzbefehl über `https`, WebDAV über `https`, „Senden an“ über `https`, Dauerbetrieb über viele Stunden. |
| O7 | Ganze Ordner hochladen am Gerät | Bisher nur am Testserver geprüft (T9.14). |
| O8 | Browser-Test der Weboberfläche für 0.5.0 | `tools/ui_test.py` gegen den Testserver laufen lassen und eintragen (T9.15). |
| O9 | Ergebnis der Sicherheitsprüfung 0.5.0 | Die unabhängige Prüfung läuft; Befunde einarbeiten, wenn sie vorliegen. |
| O10 | Offen aus 0.4.0 | Drucken am Bambu X2D von einem verschlüsselten, per Handy entsperrten Stick (der Besitzer erwartet, dass es geht; nicht geprüft); Firmware-Update über die Weboberfläche; WLAN-Suche im Einrichtungsmodus; Stromausfall während der Einrichtung der Verschlüsselung. |

## Weiter offen, vom Besitzer noch nicht entschieden

- Signierte Updates (vor einer Veröffentlichung empfohlen; schließt die Lücke, dass ein Administrator veränderte Firmware aufspielen kann).
- Einstellungen sichern und wiederherstellen, einstellbarer Gerätename.
- Veröffentlichung auf GitHub: Lizenz (MIT oder GPL), Web-Flasher, automatischer Build.
