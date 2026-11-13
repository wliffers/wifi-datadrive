# Vormerkliste

Stand: 2026-11-13, nach Firmware 0.6.0 (am Gerät installiert und geprüft, noch nicht
veröffentlicht). Die Punkte V1 bis V8 hatte der Besitzer nach 0.4.0 ausgewählt; sie sind in 0.5.0
umgesetzt. Die Punkte W1 bis W5 sind in 0.6.0 umgesetzt. Was davon am Gerät geprüft ist, steht in
der Spalte „Stand“ (Einzelheiten: [05_testprotokoll.md](05_testprotokoll.md) Phase 9 und 10,
Schnittstelle: [15_api_v5.md](15_api_v5.md) und [16_api_v6.md](16_api_v6.md)).

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

## Erledigt in 0.6.0

| Nr. | Funktion | Kurzbeschreibung | Stand |
|---|---|---|---|
| W1 | Restzeit bis zum Sperren | Die Startseite zeigt, wann das Laufwerk sperrt; „Zeit zurücksetzen“ startet die Frist neu. | **erledigt in 0.6.0.** Restzeit und Zurücksetzen per API am Gerät geprüft (15 Minuten: 895 s, danach 900 s). Das Sperren nach Ablauf der Zeit noch nicht. Nach dem Gerätetest geändert: Die Zeit läuft nur in der Dateiverwaltung; im USB-Modus kein Countdown und kein Sperren nach Zeit, der Wechsel zurück startet die Frist neu (nur am Testserver geprüft). |
| W2 | Feste Adresse, einstellbarer Gerätename | DHCP oder feste IPv4-Adresse (vorbelegt mit den Werten aus DHCP), eigener Name für `name.local`. Die Taste 10 Sekunden halten setzt auch das Netzwerk zurück. | **erledigt in 0.6.0.** Lesen der Einstellungen am Gerät geprüft; Umstellen am Gerät und das Zurücksetzen per Taste noch nicht. |
| W3 | Signierte Updates | RSA-3072, Schlüssel außerhalb des Repositorys; Builds aus dem öffentlichen Repository und aus Forks sind unsigniert. | **erledigt in 0.6.0.** Am Gerät geprüft: signierte 0.6.0 von 0.5.0 angenommen, unsignierte Datei `UNSIGNED`, fremder Schlüssel `BAD_SIGNATURE`, kein Neustart danach. |
| W4 | Neue Oberfläche | Karten, Segment-Umschalter, ⓘ-Kurzhilfe mit lokaler Hilfeseite, keine Beschreibungstexte zwischen den Einstellungen, keine doppelten Aktionen, Warteschlange für Anfragen; Farben des Projekts beibehalten. | **erledigt in 0.6.0.** Browser-Test gegen den Testserver PC 878/878, Handy 895/895. An einem echten Handy nicht systematisch geprüft; der Fix für „Stick nicht erreichbar“ ist am iPhone noch nicht nachgeprüft. |
| W5 | Vorbereitung für GitHub | MIT-Lizenz, eigene Rechte für LIO, Browser-Flasher, automatische Builds. | **vorbereitet.** Repository noch nicht auf GitHub, Workflows und Flasher noch nicht gelaufen ([18](18_veroeffentlichung.md)). |

## Offen nach 0.6.0

Prüfungen und Klärungen, keine neuen Funktionen außer O11. Reihenfolge ohne Rangfolge. Erledigt
seit der Liste nach 0.5.0: Firmware-Update über die Weboberfläche ([05](05_testprotokoll.md) T10.1);
der Browser-Test für 0.5.0 (früher O8) ist durch den Browser-Test für 0.6.0 überholt.

| Nr. | Punkt | Was zu tun ist |
|---|---|---|
| O1 | Netzlaufwerk mit echten Programmen | Zuerst der Windows-Explorer mit `tools/webdav/Connect-DataDrive.ps1`, dann eine iPhone-App mit WebDAV, Android, macOS-Finder (nur Lesen erwartet). Ergebnisse in [05](05_testprotokoll.md) T9.3. |
| O2 | Automatisches Sperren am Gerät | Sperren nach Ablauf der Zeit in der Dateiverwaltung; im USB-Modus kein Countdown und kein Sperren nach Zeit, nach dem Wechsel zurück die volle Frist; Zustand „wartet“ (Sperrversuch abgewiesen); beim Trennen des USB-Geräts; kein Sperren während eines Schreibvorgangs (T9.7, T10.5, T10.6). Die Restzeit ist geprüft (T10.4, noch mit der Regel vor der Änderung). |
| O3 | Verhalten des Bambu X2D beim Drucken | Klären, ob der X2D eine Druckdatei beim Start ganz einliest oder während des Drucks weiter vom Stick liest, und ob er dabei in den USB-Ruhezustand geht. Die Zeitregel betrifft Drucke seit 0.6.0 nicht mehr (sie läuft nur in der Dateiverwaltung); „Sperren, wenn das USB-Gerät getrennt wird“ sperrt aber nach 5 Minuten Ruhezustand ohne Zugriff. |
| O4 | Einrichtungsseite mit echtem Handy | iPhone und Android am Einrichtungs-WLAN des Sticks (T9.10); dabei die WLAN-Suche im Einrichtungsmodus (T8.9). |
| O5 | Englische Texte auf dem LCD | Ansehen, ob alle Texte passen (T9.9). |
| O6 | HTTPS-Restpunkte | iPhone mit installiertem Zertifikat (Profil installieren, volles Vertrauen in den Zertifikatsvertrauenseinstellungen), Home-Bildschirm-Symbol und Kurzbefehl über `https`, WebDAV über `https`, „Senden an“ über `https`, Dauerbetrieb über viele Stunden. |
| O7 | Ganze Ordner hochladen am Gerät | Bisher nur am Testserver geprüft (T9.14). |
| O9 | Ergebnis der Sicherheitsprüfung 0.5.0 | **Erledigt:** Befunde behoben (Firmware 0.5.0, Stand vom 2026-11-13). |
| O10 | Drucken vom verschlüsselten Stick | Am Bambu X2D von einem verschlüsselten, per Handy entsperrten Stick drucken (der Besitzer erwartet, dass es geht; nicht geprüft, T8.14). |
| O11 | Downloads über HTTPS | Downloads und ZIP-Downloads öffnen eine eigene Verbindung; eine Statusabfrage während eines langen Downloads kann sie mit HTTPS noch schließen (T10.13). Lösung suchen, zum Beispiel die Statusabfrage während eines Downloads aussetzen; dann am Gerät prüfen. |
| O12 | Netzwerkeinstellungen am Gerät ändern | Feste Adresse, zurück auf DHCP, neuer Gerätename, am besten mit `--network-test` und dem Stick in Reichweite der Taste; danach das Zurücksetzen per Taste (T10.8, T10.9). |
| O13 | Warteschlange am iPhone | Prüfen, dass „Stick nicht erreichbar“ nach dem Ändern der Sperrzeit nicht mehr auftritt (T10.12). |
| O14 | Stromausfall während der Einrichtung der Verschlüsselung | Stick während der Einrichtung abziehen, danach Zustand prüfen (T8.13). |
| O15 | Restliche Schritte der signierten Updates | Schritte 5 bis 12 aus [17](17_signierte_updates.md) Abschnitt 9, darunter Rücksprung nach Fehlstart und Update über HTTPS. |

## Weiter offen, vom Besitzer noch nicht entschieden

- Einstellungen sichern und wiederherstellen.

Entschieden seit der Liste nach 0.5.0: signierte Updates (W3), einstellbarer Gerätename (W2),
Lizenz MIT, Web-Flasher und automatischer Build (W5).
