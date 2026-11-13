# Änderungen

Alle nennenswerten Änderungen an WiFi DataDrive. Die Versionsnummer steht in
`firmware/printdrive/CMakeLists.txt` (`PROJECT_VER`) und in der Weboberfläche unter Einstellungen › System.

0.6.0 ist das erste veröffentlichte Release; ältere Versionen gibt es nur im Verlauf des Repositorys.
Das Datum ist der Tag, an dem die Version entstand. „Am Gerät geprüft“ heißt: auf dem LILYGO T-Dongle-S3 mit LCD, Ergebnisse in
[docs/05_testprotokoll.md](docs/05_testprotokoll.md).

## [0.6.0] – 2026-11-13

### Neu
- **Restzeit bis zum Sperren:** Ist das automatische Sperren eingestellt, zeigt die Karte „Verschlüsselt“
  auf der Startseite, wann das Laufwerk sperrt („Sperrt in 12:34“). Arbeiten mit Dateien und „Zeit
  zurücksetzen“ starten die Frist neu; eine nur geöffnete Seite hält das Laufwerk nicht offen.
- **Sperren nach Zeit nur in der Dateiverwaltung:** Die eingestellte Zeit läuft nur, solange die
  Dateiverwaltung das Laufwerk hat. Ist es dem USB-Gerät bereitgestellt, gibt es keinen Countdown und kein
  Sperren nach Zeit, damit Drucker oder PC nicht mitten in der Arbeit das Laufwerk verlieren; der Wechsel
  zurück in die Dateiverwaltung startet die Frist neu. Bis 0.5.0 nahm die Zeitregel einem untätigen
  USB-Gerät das Laufwerk weg. „Sperren, wenn das USB-Gerät getrennt wird“ wirkt unverändert. Am Gerät
  geprüft.
- **Feste Adresse und Name im Netzwerk:** Unter Einstellungen › WLAN wählst du DHCP oder eine feste
  IPv4-Adresse (vorbelegt mit den Werten aus DHCP) und einen eigenen Namen für `name.local`. Mit HTTPS
  erzeugt ein neuer Name eine neue Zertifizierungsstelle, die auf jedem Gerät neu installiert werden muss;
  eine neue Adresse erneuert nur das Serverzertifikat. Die Taste 10 Sekunden halten setzt jetzt auch die
  Netzwerkeinstellungen zurück.
- **Signierte Updates:** Mit der Option `CONFIG_PD_OTA_SIGNED` gebaut, nimmt die Firmware über den Browser
  nur noch Updates an, die mit dem Schlüssel des Besitzers signiert sind (RSA-3072). Der Schlüssel liegt
  außerhalb des Repositorys. Im öffentlichen Repository ist die Option aus, damit jeder Klon und jeder
  Fork bauen und per USB flashen kann; solche Builds sind unsigniert. Anleitung:
  [docs/17_signierte_updates.md](docs/17_signierte_updates.md).
- **Neue Oberfläche:** Karten-Layout, Umschalter als Segmente, eine ⓘ-Kurzhilfe an jeder Einstellung und
  die Seite „Hilfe & Anleitungen“, die ohne Internet im Gerät liegt. Keine Beschreibungstexte mehr zwischen
  den Einstellungen, keine doppelten Aktionen. Die Farben des Projekts bleiben.
- **Eine Anfrage nach der anderen:** Die Oberfläche schickt ihre Anfragen über eine gemeinsame
  Warteschlange, weil der Stick mit HTTPS nur eine Verbindung bedient. Anfragen, die gefahrlos wiederholt
  werden können, wiederholt sie einmal ohne Meldung.
- **API Version 6:** [docs/16_api_v6.md](docs/16_api_v6.md).
- **Vorbereitung für GitHub:** MIT-Lizenz, eigene Rechte für das Maskottchen LIO, Browser-Flasher,
  automatische Builds, Vorlagen für Issues und Pull Requests.

### Geprüft
- **Update über den Browser am Gerät:** Die laufende 0.5.0 nahm die signierte 0.6.0 an (13 Sekunden),
  startete in den zweiten Firmware-Bereich und bestätigte die neue Version nach 15 Sekunden. Das ist das
  erste am Gerät geprüfte Web-Update.
- **Signierte Updates am Gerät:** Der Status meldet `signed_required: true` und die Schlüssel-ID. Eine
  unsignierte 0.6.0 wurde mit `422 UNSIGNED` abgewiesen, dieselbe Datei mit einem anderen Schlüssel
  signiert mit `422 BAD_SIGNATURE`. Nach beiden Abweisungen lief der Stick ohne Neustart weiter.
- API-Test am Gerät 415/415, 7 Prüfungen übersprungen (Liste in
  [docs/05_testprotokoll.md](docs/05_testprotokoll.md) Phase 10).
- Restzeit am Gerät: bei 15 Minuten nach wenigen Sekunden 895 s, nach „Zeit zurücksetzen“ 900 s.
- Netzwerkeinstellungen am Gerät gelesen: DHCP, aktuelle Adresse, Maske, Gateway, DNS und Name.
- Freier Speicher mit HTTPS und entsperrtem Laufwerk: 67 KiB im Leerlauf.
- Browser-Test gegen den Testserver: PC 878/878, Handy 895/895.
- Behoben: Nach dem Ändern der Sperrzeit meldete die Oberfläche „Stick nicht erreichbar“. Ursache waren
  parallele Anfragen, während der Stick mit HTTPS nur eine Verbindung bedient. Nachgestellt und behoben mit
  einem Modell, das nur eine Verbindung annimmt; am iPhone noch nicht nachgeprüft.
- Nicht geprüft: feste Adresse und neuer Name am Gerät, das Sperren nach Ablauf der Zeit, der Zustand „wartet“, das Zurücksetzen per Taste, die Rückkehr
  zur alten Version nach einem Fehlstart.

### Bekannte Grenzen
- Downloads und ZIP-Downloads öffnen eine eigene Verbindung im Browser. Mit HTTPS kann eine Statusabfrage
  während eines langen Downloads diese Verbindung noch schließen.
- Eine falsche feste Adresse macht den Stick im WLAN unerreichbar. Zurück geht es nur mit der Taste
  (10 Sekunden halten) am Stick; das ist am Gerät noch nicht geprüft.
- Geht der private Schlüssel verloren, nimmt ein Stick mit signierter Firmware keine Updates über den
  Browser mehr an und muss per USB neu geflasht werden.
- Signierte Updates schützen nicht vor dem Flashen per USB; es gibt kein Secure Boot.

## [0.5.0] – 2026-11-12

### Neu
- **Netzlaufwerk (WebDAV):** Der Stick ist unter `http://<Stick>/dav/` als Netzlaufwerk erreichbar,
  solange die Dateiverwaltung aktiv ist. Dem Drucker nimmt es das Laufwerk nie weg.
- **Uhrzeit aus dem Netz:** Uhr per NTP, Zeitzone wählbar (Voreinstellung Deutschland mit Sommerzeit).
  Ohne Internet stellt der Browser die Uhr.
- **Automatisch sperren:** Das verschlüsselte Laufwerk sperrt sich nach einer Zeit ohne Zugriff und/oder
  beim Trennen des USB-Geräts. Voreinstellung aus.
- **Display-Sprache:** Das Display folgt der Sprache der Weboberfläche.
- **Einrichtungsseite öffnet sich von selbst:** beim Verbinden mit dem Einrichtungs-WLAN, wie bei einem
  Hotel-WLAN.
- **Upload fortsetzen:** Ein abgebrochener Upload geht an der Abbruchstelle weiter.
- **Ordner als ZIP und ganze Ordner hochladen:** ZIP ohne Kompression, höchstens 4096 Dateien.
- **HTTPS als Schalter:** in den Einstellungen, Voreinstellung aus. Der Speicher wurde dafür umgebaut,
  damit Uploads über TLS auf diesem Board durchlaufen.
- **Stärkeres WLAN:** Bei dauerhaft schwachem Signal wechselt der Stick zu einem stärkeren gespeicherten
  Netz.
- **Schneller:** am Gerät gemessen über HTTP Upload 1,26 MB/s und Download 725 kB/s (vorher 487 kB/s),
  über HTTPS Download 573 kB/s (vorher 389 kB/s).

### Geprüft und behoben
- API-Test am Gerät 354/354. Der erste Lauf fand sechs Abweichungen: Die Einstellung zum automatischen
  Sperren verlangte beide Werte statt einen allein, und eine unplausible Uhrzeit wurde still ignoriert
  statt abgewiesen. Beides behoben.
- HTTPS am Gerät: Upload von 30 MB, die Datei war danach bitgleich.
- Korrekturen aus einer Sicherheitsprüfung, unter anderem beim Netzlaufwerk, beim automatischen Sperren
  und beim Stellen der Uhr durch den Browser.
- Browser-Test am Testserver: PC 719 von 720, Handy 731 von 732 Prüfungen.
- Nicht geprüft: Netzlaufwerk mit echten Programmen, Sperren nach Ablauf der Zeit, Einrichtungsseite mit
  einem echten Handy.

## [0.4.0] – 2026-11-12

### Neu
- **Verschlüsselung des Laufwerks:** AES-256-XTS, optional. Laufwerks-Passwort mit mindestens 12 Zeichen
  und ein Wiederherstellungsschlüssel, der nur einmal angezeigt wird. Nach jedem Einstecken entsperrst du
  im Browser.
- **WLAN-Suche:** Beim Hinzufügen eines Netzes wählst du aus den Netzen in Reichweite.
- **Maskottchen LIO:** Eine Animation auf dem Display zeigt den Zustand (bereit, Upload, gesperrt,
  Fehler …).
- **HTTPS vorbereitet:** im Build abgeschaltet, weil Uploads über TLS auf diesem Board scheiterten.

### Geprüft und behoben
- API-Test am Gerät 334/334, einschließlich Einrichten, Sperren, Wiederherstellung und Ausschalten der
  Verschlüsselung. Einrichten auf einer 64-GB-Karte: 51 Sekunden.
- Browser-Test am Testserver: PC 548/548, Handy 561/561.
- Behoben: Abgewiesene Anfragen mit Formulardaten schlossen die Verbindung, statt zu antworten.
- Behoben: Eine Übernahme direkt nach dem Entsperren wurde durch das Neu-Einstecken des USB-Geräts
  rückgängig gemacht.

## [0.3.0] – 2026-11-12

### Neu
- **Anmeldung:** Benutzer mit den Rollen `admin` und `user`, Standardkonto `admin` / `admin` mit
  erzwungenem Passwortwechsel, vertraute Geräte, Sperre nach falschen Eingaben.
- **Ordner:** anlegen, öffnen, umbenennen, verschieben, kopieren, löschen; mehrere Dateien markieren.
- **Mehrere WLANs:** bis zu 5 gespeicherte Netze, das stärkste bekannte wird genommen.
- **Taste:** Klick zeigt die Adresse als QR-Code, Doppelklick wechselt die Betriebsart, langes Halten
  startet den Wartungsmodus oder setzt die Benutzer zurück.
- **Firmware-Update im Browser:** mit Rückkehr zur alten Version bei einem Fehlstart.
- **Deutsch und Englisch, App-Symbol** für den Home-Bildschirm.
- **Senden an:** Rechtsklick in Windows und ein Kurzbefehl für das iPhone.

### Geprüft und behoben
- API-Test am Gerät 182/182. Anmeldung, Ordner und Sprache im Browser vom Autor geprüft.
- Korrekturen aus einer Sicherheitsprüfung.
- Nicht geprüft: Taste, App-Symbol, „Senden an“, iPhone-Kurzbefehl, Update über den Browser.

## [0.2.0] – 2026-11-11

### Neu
- **Neuer Name:** aus „WiFi PrintDrive“ wird „WiFi DataDrive“.
- **Neue Weboberfläche** für PC und Handy, hell und dunkel; Dateien umbenennen; beliebige Dateitypen.
- **Schlanke Auslieferung:** Die Weboberfläche liegt gzip-komprimiert im Gerät (60 KB → 19 KB).
- **Testserver und Browser-Test:** Die Oberfläche lässt sich ohne Hardware entwickeln und prüfen.

### Geprüft und behoben
- Browser-Test am Testserver 566/566. Zwei Runden fanden 26 Fehler, darunter Dialoge, die sich
  überschrieben, Aktionen, die während eines Uploads ohne Meldung hingen, und ein abgebrochenes
  Formatieren, das das USB-Gerät ohne Laufwerk zurückließ.
- API-Test am Gerät 28/28.
- **Bambu Lab X2D:** Laufwerk erkannt, per Browser hochgeladene `.gcode.3mf` angezeigt und gedruckt
  (Rückmeldung des Autors, microSD 64 GB FAT32, ohne Messung).

## [0.1.0] – 2026-11-11

### Erste Fassung („WiFi PrintDrive“)
- USB-Laufwerk mit kontrolliertem Umschalten zwischen USB-Gerät und Weboberfläche; das Laufwerk hat immer
  genau einen Besitzer.
- Upload von 3MF-Dateien im Browser mit Prüfung der Dateistruktur.
- microSD-Karte als Speicher, interner Flash als Rückfall.
- Statusbild auf dem Display; Update per USB ohne Handgriff am Dongle.

### Geprüft und behoben
- Windows 11: kopieren, auswerfen, neu einstecken; Dateien überleben Updates.
- Schreiben ins interne Flash von 12 auf 47 KB/s, Web-Upload von 7 auf 46 KB/s beschleunigt.
- Dateien ab 4 GB werden abgewiesen, statt still angenommen zu werden.
- Die Fehlermeldung bei einem abgewiesenen Upload kommt jetzt beim Browser an.
