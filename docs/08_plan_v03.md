# Plan Version 0.3.0

Stand: 2026-11-12. Auftrag des Besitzers nach dem erfolgreichen Test am Bambu X2D. Der verbindliche Schnittstellen-Vertrag steht in [09_api_v3.md](09_api_v3.md).

## Ziel

WiFi DataDrive wird vom Einzelplatz-Werkzeug zum allgemeinen WLAN-Stick: Anmeldung mit Benutzerverwaltung, Ordner und vollwertige Dateiverwaltung, mehrere WLANs, Bedienung per Taste mit QR-Code, App-Icon am Handy, Senden direkt vom PC und iPhone, Update über die Weboberfläche. Der Stick muss danach am Drucker weiter funktionieren wie bisher.

Nicht Teil dieser Version: Vorschaubilder, Druckerstatus, Nur-Lese-Modus-Test.

## Grundsätze

- Die Schutzmechanismen des Laufwerks (ein Besitzer, Host-Sperre, Barriere, Schreib-Schutzfrist) bleiben unverändert. Jede neue Dateioperation läuft unter demselben Schloss und nur, solange die Dateiverwaltung das Laufwerk besitzt.
- Weiterhin eine einzelne HTML-Datei ohne Fremdbibliotheken, alles lokal.
- Jeder Arbeitsschritt endet mit einem Build; am Ende laufen API-Test am Gerät und Browser-Test am Testserver.

## Funktionen

| Nr. | Funktion | Kern der Lösung |
|---|---|---|
| F1 | Anmeldung und Benutzerverwaltung | Benutzer mit Rolle `admin` oder `user` im Einstellungsspeicher (Passwort als PBKDF2-Hash mit Salz). Standardkonto `admin` / `admin`, die Oberfläche fordert zum Ändern auf. Anmeldung per Sitzungs-Cookie, für Skripte zusätzlich HTTP Basic. |
| F2 | Ordner und Dateiverwaltung | Ordner anzeigen, anlegen, löschen; Dateien und Ordner umbenennen, verschieben, kopieren; Ausschneiden/Kopieren/Einfügen und Mehrfachauswahl in der Oberfläche; Upload in den aktuellen Ordner. |
| F3 | Mehrere WLANs | Bis zu 5 gespeicherte Netze; verbunden wird mit dem stärksten bekannten. Verwaltung in den Einstellungen. |
| F4 | Taste und QR-Code | Einfacher Klick zeigt 20 s einen QR-Code mit der Adresse (im Einrichtungsmodus den WLAN-Beitritt). Doppelklick wechselt die Betriebsart, bei aktivem USB-Gerät erst nach zweitem Doppelklick. |
| F5 | App-Icon | Web-App-Manifest und Apple-Touch-Icon; vom Home-Bildschirm öffnet die Oberfläche ohne Browserleiste. |
| F6 | Senden vom PC und iPhone | Neuer Ein-Schritt-Endpunkt `push` (übernehmen, speichern, wieder bereitstellen). Dazu ein Windows-Skript mit „Senden an“-Eintrag und eine Anleitung für einen iPhone-Kurzbefehl. |
| F7 | Update über die Weboberfläche | Firmware-Datei hochladen, Prüfung, Umschalten auf die zweite Firmware-Partition, automatische Rückkehr zur alten Version, wenn die neue nicht startet. Nur für Administratoren. |
| F8 | Erzwungener Passwortwechsel | Mit dem Auslieferungspasswort `admin` / `admin` ist nur das Ändern des Passworts möglich; erst danach ist das Gerät bedienbar. Wichtig für eine Veröffentlichung. |
| F9 | Deutsch und Englisch | Die Weboberfläche ist zweisprachig, Auswahl nach Browsersprache, umstellbar in den Einstellungen. Das Display bleibt deutsch. |

## Entscheidungen, die der Besitzer kennen sollte

- **Passwörter gehen unverschlüsselt durchs WLAN.** Das Gerät spricht nur HTTP. Die Anmeldung schützt vor anderen Nutzern im selben Netz, nicht vor jemandem, der den Funkverkehr mitschneidet.
- **Passwort vergessen:** BOOT-Taste 10 Sekunden halten setzt die Benutzer auf `admin` / `admin` zurück (Dateien und WLAN bleiben). 5 bis 10 Sekunden halten und loslassen startet wie bisher den Wartungsmodus.
- **Rollen:** `user` darf Dateien verwalten und die Betriebsart wechseln. `admin` darf zusätzlich Einstellungen, Benutzer, Formatieren, Neustart, Protokoll und Update.
- **Kopieren blockiert das Gerät.** Es bearbeitet eine Anfrage nach der anderen; während eine große Datei kopiert wird, antwortet es auf nichts anderes. Die Oberfläche zeigt das an.
- **iPhone-Kurzbefehl:** Kurzbefehle lassen sich nicht als fertige Datei mitliefern (Apple signiert sie). Es gibt eine Schritt-für-Schritt-Anleitung.
- **Erstes Aufspielen von 0.3.0 per USB.** Die automatische Rückkehr zur alten Version braucht einen neuen Bootloader, und den kann ein Web-Update nicht tauschen. Ab 0.3.0 gehen Updates über die Weboberfläche.

## Arbeitspakete und Reihenfolge

Stufe 0 schafft die Struktur, damit die Pakete der Stufe 1 getrennte Dateien bearbeiten und parallel laufen können.

| Stufe | Paket | Inhalt | Dateien (Eigentümer) |
|---|---|---|---|
| 0 | P0 Umbau Webserver | `web_server.c` in Module mit zentraler Routentabelle (Methode, Pfad, Mindestrolle) aufteilen; Verhalten unverändert | `main/web_*.c/h` |
| 1 | P1 Anmeldung | Benutzer, Sitzungen, Basic, Rollenprüfung im zentralen Verteiler, Endpunkte | `main/auth.c/h`, `main/web_auth.c` |
| 1 | P2 Dateien und Ordner | Pfadprüfung, Auflisten, mkdir, löschen (rekursiv), verschieben, kopieren, Upload/Download mit Pfad, `push` | `main/drive.c/h`, `main/web_files.c` |
| 1 | P3 Mehrere WLANs | Liste im Einstellungsspeicher, Auswahl nach Signalstärke, Endpunkte | `components/pd_wifi/*`, `main/web_wifi.c` |
| 1 | P4 Taste und QR | Klick-Erkennung, QR-Anzeige, Benutzer-Reset | `main/ui.c`, `components/display/*` |
| 1 | P5 Web-Update | OTA-Endpunkt, Rollback, Bestätigung nach gutem Start | `main/ota.c/h`, `main/web_ota.c`, `sdkconfig.defaults` |
| 1 | P6 Testserver | `tools/mock_server.py` auf Vertrag v3 | `tools/mock_server.py` |
| 1 | P7 Icons und Manifest | PNG-Icons erzeugen, Manifest | `main/web/icons/*`, `main/web/manifest.webmanifest`, `tools/make_icons.py` |
| 2 | P8 Oberfläche | Anmeldung, Benutzer, Ordner-Navigation, Mehrfachauswahl, Zwischenablage, WLAN-Liste, Update, App-Metadaten | `main/web/index.html` |
| 2 | P9 Senden an | Windows-Skript mit Installer; Anleitung iPhone-Kurzbefehl | `tools/sendto/*`, `docs/10_senden.md` |
| 3 | P10 Tests | `api_test.py` und `ui_test.py` auf v3 | `tools/api_test.py`, `tools/ui_test.py` |
| 3 | P11 Prüfung | Unabhängige Durchsicht mit Schwerpunkt Anmeldung und Pfadprüfung | nur Bericht |
| 3 | P12 Doku | README und `docs/` | `README.md`, `docs/*` |

Zusammenführen, Bauen, Aufspielen und die Tests am Gerät erfolgt danach in einem Schritt.

## Abnahme

- API-Test am echten Gerät besteht, einschließlich: ohne Anmeldung kein Zugriff, Rolle `user` kann keine Einstellungen ändern, Pfade mit `..` werden abgewiesen, Ordner-Operationen, `push`.
- Browser-Test am Testserver besteht in Desktop- und Handyformat.
- Am Gerät geprüft: Anmelden, Passwort ändern, Benutzer-Reset per Taste, QR-Code scannbar, Doppelklick wechselt die Betriebsart, zweites WLAN, Update über die Weboberfläche von 0.3.0 auf einen 0.3.1-Testbuild.
- Am Drucker geprüft (durch den Besitzer): Stick wird weiter erkannt, Druck von einer Datei in einem Unterordner.
