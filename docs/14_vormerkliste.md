# Vormerkliste für die Version nach 0.4.0

Stand: 2026-11-12. Vom Besitzer ausgewählt, noch nicht geplant und nicht beauftragt. Reihenfolge ohne Rangfolge.

| Nr. | Funktion | Kurzbeschreibung |
|---|---|---|
| V1 | Stick als Netzlaufwerk (WebDAV) | Der Stick erscheint im Windows-Explorer und in der iPhone-App „Dateien“ als Laufwerk; Dateien ohne Browser hineinziehen. |
| V2 | Automatisches Sperren | Das verschlüsselte Laufwerk sperrt sich selbst, wenn der Stick abgezogen wird oder eine einstellbare Zeit nichts passiert. |
| V3 | Uhrzeit aus dem Netz | Dateien bekommen ein richtiges Datum; Sortierung nach „zuletzt hochgeladen“. Vorher prüfen, welches Datum Dateien heute tragen. |
| V4 | Abgebrochene Uploads fortsetzen | Für große Dateien und wackliges WLAN. |
| V5 | Ganze Ordner hochladen, Ordner als ZIP herunterladen | |
| V6 | Einrichtungsseite öffnet sich von selbst | Beim Verbinden mit dem Zugangspunkt des Sticks springt die Seite auf (wie bei Hotel-WLAN). |
| V7 | Display folgt der Sprache der Oberfläche | Deutsch oder Englisch, je nach Einstellung in der Weboberfläche. |

| V8 | HTTPS mit Speicher-Umbau | Code ist vorhanden, aber per Build-Schalter `PD_HTTPS_OPTION` aus. Messung 0.4.0: Uploads scheitern, weil der 16-KiB-TLS-Puffer nicht am Stück frei ist (ca. 50 KiB frei, zerstückelt). Umbau: TLS-Puffer einmalig beim Start reservieren und auf eine TLS-Verbindung begrenzen; Dateiliste ohne 33-KiB-Puffer direkt senden; Umleitungs-Server auf Port 80 prüfen. Danach am Gerät messen: Upload großer Dateien, Ladezeit der Seite, iPhone mit installiertem Zertifikat. |

## Weiter offen, vom Besitzer noch nicht entschieden

- Signierte Updates (vor einer Veröffentlichung empfohlen; schließt die Lücke, dass ein Administrator veränderte Firmware aufspielen kann).
- Einstellungen sichern und wiederherstellen, einstellbarer Gerätename.
- Veröffentlichung auf GitHub: Lizenz (MIT oder GPL), Web-Flasher, automatischer Build.
