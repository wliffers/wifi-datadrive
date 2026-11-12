# Plan Version 0.4.0

Stand: 2026-11-12. Status: **Entwurf, wartet auf Freigabe des Besitzers.** Es wird nichts umgesetzt, bevor die Freigabe da ist und die offenen Entscheidungen (Abschnitt „Entscheidungen“) beantwortet sind.

## Ziel

Zwei neue Funktionen und der Abschluss von 0.3.0:

1. **Verschlüsseltes Laufwerk.** Wer die Speicherkarte oder den Stick in die Hand bekommt, kann die Dateien ohne Passwort nicht lesen. Drucker und PC merken davon nichts, solange das Laufwerk entsperrt ist.
2. **WLAN-Suche.** Beim Hinzufügen eines WLANs zeigt die Oberfläche die Netze in Reichweite zur Auswahl.
3. **Restarbeiten 0.3.0.** Start-Passwort, Gerätetest, Browser-Test, Dokumentation.

## Ausgangslage

Auf dem Stick läuft 0.3.0 mit den Korrekturen aus der Sicherheitsprüfung (Anmeldesperre je Gerät, sichere Reaktion bei unlesbarer Benutzerdatenbank, dauerhaftes Abmelden vertrauter Geräte, Korrekturen an `push`, Download und Update). Noch offen aus 0.3.0: API-Test am Gerät, Browser-Test der neuen Oberfläche, Abgleich von Vertrag, Testserver und Oberfläche mit der geänderten Anmeldesperre, Dokumentation.

## F1 Verschlüsseltes Laufwerk

### Was es schützt und was nicht

| Fall | Geschützt? |
|---|---|
| Speicherkarte wird entnommen und am PC gelesen | Ja |
| Stick samt Karte wird gestohlen, Entsperren per Passwort | Ja, so gut wie das Passwort |
| Stick samt Karte wird gestohlen, automatisches Entsperren | Nein |
| Jemand im selben WLAN, während das Laufwerk entsperrt ist | Nein, hier schützt nur die Anmeldung |
| Der Drucker oder PC, an dem der Stick steckt | Nein, er sieht die Dateien im Klartext (muss er, sonst kann er nicht drucken) |
| Jemand verändert verschlüsselte Daten gezielt | Nein, das Verfahren erkennt keine Manipulation |

### Lösung

- **Verfahren:** AES-256-XTS, je 512-Byte-Sektor, Sektornummer als Startwert. Das ist das Verfahren von BitLocker, LUKS und VeraCrypt. Der ESP32-S3 rechnet AES in Hardware.
- **Schlüssel:** Ein zufälliger Hauptschlüssel verschlüsselt die Daten. Er liegt selbst verschlüsselt in einem Kopfbereich am Anfang der Karte (mit Sicherungskopie am Ende), geschützt durch einen Schlüssel aus dem Laufwerks-Passwort (PBKDF2-HMAC-SHA256, Rundenzahl so gewählt, dass das Entsperren am Stick etwa 1 Sekunde rechnet). Passwort ändern heißt nur: Hauptschlüssel neu verpacken, die Daten bleiben unberührt.
- **Ort im System:** Eine neue Schicht direkt über dem Speichermedium. Dateiverwaltung (FatFs) und USB-Laufwerk gehen beide durch sie. Die bestehende Besitzer-Logik (nur einer schreibt) liegt darüber und bleibt unverändert.
- **Gesperrter Zustand:** Nach dem Einstecken ist das Laufwerk gesperrt. Der Drucker sieht „kein Medium“, das Display zeigt „Gesperrt“, die Weboberfläche zeigt nach der Anmeldung den Entsperr-Dialog. Nach dem Entsperren meldet sich das Laufwerk wie nach einem Moduswechsel.
- **Einschalten und Ausschalten** der Verschlüsselung formatiert das Laufwerk. Eine Umwandlung mit Erhalt der Daten gibt es in dieser Version nicht.
- **Wiederherstellungsschlüssel:** Beim Einrichten wird einmalig ein Schlüssel angezeigt (zum Aufschreiben). Damit lässt sich das Laufwerk entsperren, wenn das Passwort vergessen ist. Ohne Passwort und ohne diesen Schlüssel sind die Daten endgültig verloren.
- **Laufwerks-Passwort und Anmeldung sind getrennt.** Der Benutzer-Reset per Taste (10 s) öffnet das Laufwerk nicht.

### Folgen im Alltag

- Die Karte ist an keinem anderen Gerät mehr lesbar, nur noch in diesem Projekt.
- Am Drucker: einstecken, mit dem Handy entsperren, dann drucken. Nach jedem Stromverlust wieder (außer mit automatischem Entsperren, siehe Entscheidung E2).
- Das Laufwerks-Passwort geht beim Entsperren unverschlüsselt durchs WLAN, solange es kein HTTPS gibt (siehe Entscheidung E4).
- PBKDF2 auf einem kleinen Prozessor ist für Angreifer mit PC schnell durchzuprobieren. Das Laufwerks-Passwort braucht deshalb mindestens 12 Zeichen; die Oberfläche erzwingt das.

### Technisches Risiko

Das USB-Laufwerk greift heute innerhalb der Espressif-Komponente direkt auf die Karte zu. Die Verschlüsselung muss dort dazwischen. Ob das über die vorhandenen Einhängepunkte (`--wrap` der Lese- und Schreibaufrufe) geht oder ein eigener Speicher-Anschluss nötig ist, klärt ein Vorversuch (Paket K0), bevor der Rest startet. Gemessen wird dabei auch die Geschwindigkeit; Ziel ist, dass sie über USB nicht merklich sinkt.

## F2 WLAN-Suche

- Neuer Endpunkt `GET /api/wifi/scan` (nur Administrator): Netze in Reichweite mit Name, Signalstärke, verschlüsselt ja/nein, bereits gespeichert ja/nein; nach Stärke sortiert, Doppelte zusammengefasst.
- Im Dialog „WLAN hinzufügen“ eine Liste mit Schaltfläche „Erneut suchen“. Ein Tipp auf ein Netz übernimmt den Namen, dann nur noch das Passwort eingeben. Die Eingabe von Hand bleibt für versteckte Netze.
- Die Suche dauert 2 bis 3 Sekunden. Im Einrichtungsmodus (Handy hängt am Zugangspunkt des Sticks) kann die Verbindung dabei kurz stocken; die Oberfläche wartet und versucht es erneut.

## F3 Restarbeiten 0.3.0

- Start-Passwort nach Entscheidung E1.
- Vertrag, Testserver und Oberfläche an die geänderte Anmeldesperre anpassen (Wartezeit steigt, statt fest 30 s; höchstens 4 vertraute Geräte je Benutzer; neuer Fehler „Anmeldung nicht verfügbar“).
- `tools/ui_test.py` auf die neue Oberfläche bringen, Browser-Test in Desktop- und Handyformat.
- API-Test am echten Gerät, Testreste von der Karte räumen.
- Dokumentation auf 0.3.0 und danach 0.4.0.

## Entscheidungen

| Nr. | Frage | Empfehlung |
|---|---|---|
| E1 | Start-Passwort: A zufälliges Passwort auf dem Display, B `admin`/`admin` plus Bestätigung per Taste, C wie jetzt (`admin`/`admin` mit erzwungenem Wechsel) | A |
| E2 | Entsperren: nur per Passwort nach jedem Einstecken, oder zusätzlich wählbar „automatisch an diesem Stick“ (Schlüssel liegt dann im Stick; schützt nur noch die entnommene Karte) | Beides anbieten, Voreinstellung Passwort |
| E3 | Verschlüsselung für: nur Speicherkarte, oder auch den internen Speicher (Betrieb ohne Karte) | Beide, gleiche Schicht |
| E4 | HTTPS in dieser Version? Vorteil: Passwörter gehen nicht mehr lesbar durchs WLAN. Nachteil: Jeder Browser warnt einmal vor dem selbst erstellten Zertifikat, das iPhone-App-Icon und der Kurzbefehl müssen damit neu geprüft werden, mehr Speicherbedarf. | Ja, als abschaltbare Option, letztes Paket; fällt weg, wenn es am iPhone nicht sauber läuft |
| E5 | Wiederherstellungsschlüssel anbieten? | Ja |

## Arbeitspakete

| Stufe | Paket | Inhalt | Dateien (Eigentümer) |
|---|---|---|---|
| 0 | R1 Abgleich | Vertrag v3, Testserver und Texte der Oberfläche an die Anmeldesperre anpassen | `docs/09_api_v3.md`, `tools/mock_server.py`, Texte in `index.html` |
| 0 | R2 Browser-Test | `ui_test.py` auf v3, Lauf am Testserver | `tools/ui_test.py` |
| 0 | R3 Start-Passwort | Umsetzung nach E1 | `main/auth.c/h`, `main/ui.c` |
| 0 | K0 Vorversuch | Einhängepunkt für die Verschlüsselung im USB-Pfad und im FatFs-Pfad finden, Geschwindigkeit messen, Bericht | eigener Zweig im Build-Ordner, kein Eingriff in `main/` |
| 1 | K1 Verschlüsselungsschicht | AES-XTS-Schicht, Kopfbereich, Schlüsselableitung, Sperren/Entsperren, Einrichten mit Formatieren, Passwort ändern, Wiederherstellungsschlüssel; Selbsttest mit bekannten Prüfwerten | `components/pd_crypt/*`, `main/drive.c/h`, `main/usb_dev.c` |
| 1 | W1 WLAN-Suche | Suchfunktion und Endpunkt | `components/pd_wifi/*`, `main/web_wifi.c` |
| 1 | V1 Vertrag v4 | Endpunkte für Verschlüsselung und WLAN-Suche festschreiben, Testserver erweitern | `docs/12_api_v4.md`, `tools/mock_server.py` |
| 2 | K2 Endpunkte und Display | `/api/crypt/*` (Zustand, einrichten, entsperren, sperren, Passwort ändern, ausschalten), Zustand „Gesperrt“ auf dem Display | `main/web_crypt.c`, `main/ui.c` |
| 2 | U1 Oberfläche | Entsperr-Dialog, Einrichtung mit Warnungen, WLAN-Suchliste, Texte DE/EN | `main/web/index.html` |
| 3 | H1 HTTPS (nur bei E4 = ja) | Zertifikat je Gerät beim ersten Start, HTTPS-Server, Umleitung, Schalter | `main/web_server.c`, `main/tls.c` |
| 3 | T1 Tests | `api_test.py` und `ui_test.py` für v4 | `tools/api_test.py`, `tools/ui_test.py` |
| 3 | P1 Prüfung | Unabhängige Durchsicht der Verschlüsselung: Schlüsselwege, Speicher löschen nach Gebrauch, Zustandswechsel gesperrt/entsperrt gegen Besitzer-Logik | nur Bericht |
| 3 | D1 Doku | README, Testprotokoll, Risiken, Bedienung | `README.md`, `docs/*` |

Stufe 0 läuft parallel (vier Arbeitspakete). K1 startet erst, wenn K0 einen gangbaren Weg zeigt. Zusammenführen, Bauen, Aufspielen und Tests am Gerät erfolgen danach in einem Schritt; das Aufspielen von 0.4.0 geht bereits über die Weboberfläche und ist damit zugleich der Test des Web-Updates.

## Zeit

Geschätzt 4 bis 6 Stunden Arbeitszeit, dazu die Tests am Gerät. Davon etwa 1 Stunde für Stufe 0. Der Vorversuch K0 kann die Schätzung für die Verschlüsselung verschieben.

## Abnahme

- Bekannte AES-XTS-Prüfwerte stimmen (Selbsttest im Build und am Gerät).
- Gesperrt: Drucker/PC sehen kein Medium, die API liefert keine Dateien. Entsperrt: Verhalten wie 0.3.0, API-Test besteht vollständig.
- Karte am PC-Kartenleser: keine lesbaren Dateinamen oder Inhalte.
- Falsches Passwort wird abgewiesen, mit steigender Wartezeit. Wiederherstellungsschlüssel entsperrt.
- Stromverlust während des Schreibens beschädigt höchstens die gerade geschriebene Datei, nicht den Kopfbereich.
- Geschwindigkeit über USB und Upload gemessen und dokumentiert.
- WLAN-Suche zeigt das Heimnetz, Auswahl und Verbinden funktionieren, auch im Einrichtungsmodus.
- Durch den Besitzer: Drucken am X2D von einem verschlüsselten, per Handy entsperrten Stick.
