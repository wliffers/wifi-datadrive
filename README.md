# WiFi DataDrive

Firmware für den **LILYGO T-Dongle-S3 (LCD-Variante)**, die den Dongle in einen
USB-Stick mit WLAN-Zugang verwandelt: Der Dongle steckt an einem USB-Gerät (PC, 3D-Drucker;
Ziel: Bambu Lab X2D) und erscheint dort als normales USB-Laufwerk. Dateien werden per
Browser über WLAN in einer eingebauten Dateiverwaltung hochgeladen, heruntergeladen,
umbenannt, verschoben und gelöscht, ohne den Stick abzuziehen.

Seit Version 0.3.0 gibt es Benutzer mit Anmeldung und Ordner. Seit Version 0.4.0 kann das
Laufwerk verschlüsselt werden (optional, Voreinstellung aus).

Das Projekt hieß bis Version 0.1.0 „WiFi PrintDrive“; der Ordner `firmware/printdrive`, der
HTTP-Header `X-PrintDrive` und das Code-Präfix `pd_` behalten den alten Namen.

Grundprinzip: Das FAT-Dateisystem – auf einer eingesteckten **microSD-Karte**, sonst im
internen 16-MB-Flash – hat zu jedem Zeitpunkt genau **einen** Besitzer – entweder das
USB-Gerät (Drucker/PC, Anzeige „USB-Laufwerk aktiv“) oder die Firmware (Weboberfläche,
Anzeige „Dateiverwaltung aktiv“). Umgeschaltet wird nur kontrolliert
(Details: [docs/02_architektur.md](docs/02_architektur.md)).

## Stand (2026-11-12, Firmware 0.4.0)

Jede Zeile trägt eine Markierung. **verifiziert** heißt: am echten Gerät geprüft, mit
Ergebnis in [docs/05_testprotokoll.md](docs/05_testprotokoll.md). **noch nicht geprüft**
heißt: gebaut oder im Quelltext vorhanden, aber nicht am Gerät oder nicht in dieser Form
getestet.

| Bereich | Stand |
|---|---|
| Hardware-Bestandsaufnahme (Chip, Flash, USB-ID) | **verifiziert** per esptool am echten Gerät |
| Smoke-Test (LCD, LED, Taste, Flash, Setup-AP), WLAN STA | **verifiziert** |
| USB-Laufwerk unter Windows 11 (Kopieren, Auswerfen, Re-Plug) | **verifiziert** |
| Flashen per 1200-Baud-Touch, Dateien überleben Updates | **verifiziert**, Update-Schleife vollautomatisch |
| Anmeldung mit Benutzern und Rollen (`admin`, `user`), Standardkonto mit Passwortwechsel, vertraute Geräte, Anmeldesperre (0.3.0) | **verifiziert**: API-Test 182/182 am Gerät; Weboberfläche vom Besitzer im Browser geprüft |
| Ordner, Umbenennen, Verschieben, Kopieren, Löschen; Mehrfachauswahl und Zwischenablage (0.3.0) | **verifiziert**: API-Test 182/182; Weboberfläche vom Besitzer geprüft |
| Mehrere WLANs, bis 5 Netze (0.3.0) | **verifiziert**: API-Test 182/182 |
| Taste: Klick zeigt QR-Code, Doppelklick wechselt Betriebsart, Wartungsmodus, Benutzer-Reset (0.3.0) | **noch nicht geprüft** (Bedienung am Gerät nicht als eigener Test protokolliert) |
| Weboberfläche auf Deutsch und Englisch, App-Icon für den Home-Bildschirm (0.3.0) | Sprachumschaltung vom Besitzer im Browser geprüft; App-Icon am Handy **noch nicht geprüft** |
| „Senden an“ für Windows (`tools/sendto`) | **noch nicht geprüft** |
| iPhone-Kurzbefehl (`docs/10_senden.md`) | **noch nicht geprüft** auf einem echten iPhone |
| Firmware-Update über die Weboberfläche (0.3.0) | **noch nicht geprüft**; alle Versionen bisher per USB aufgespielt |
| Verschlüsselung des Laufwerks, AES-256-XTS (0.4.0) | **verifiziert**: API-Lauf am Gerät 334/334 (mit `--crypt-test`), Messungen auf 64-GB-microSD, siehe unten |
| WLAN-Suche beim Hinzufügen eines Netzes (0.4.0) | **verifiziert** bei bestehender WLAN-Verbindung (5 Netze in 2,9 s); im Einrichtungsmodus **noch nicht geprüft** |
| Animierte Anzeige „LIO“ auf dem Display (0.4.0) | Besitzer: Aussehen gut; kein Messprotokoll |
| HTTPS (0.4.0) | **im Build abgeschaltet** (`PD_HTTPS_OPTION`, Voreinstellung aus), weil Uploads mit TLS auf diesem Board scheitern (siehe [docs/14_vormerkliste.md](docs/14_vormerkliste.md)) |
| LCD-Statusbild | **verifiziert** (Foto), außer Nuancen der LED-Farben |
| microSD (Phase 5) | **verifiziert** (Grundfunktion, 64-GB-Karte FAT32) |
| Verschlüsseltes Laufwerk am Bambu X2D (Druck von einem entsperrten, verschlüsselten Stick) | **noch nicht geprüft** |
| Verschlüsselung: Stromausfall während Einrichtung oder Passwortwechsel | **noch nicht geprüft** |
| Verschlüsselung auf dem internen Flash (ohne Karte) | **noch nicht geprüft** |
| Weboberfläche am echten Handy (iOS Safari), echtes Drag & Drop, Abläufe am echten Gerät | **nicht getestet** (Browser-Tests laufen am Testserver mit Chromium) |
| Nur-Lese-Modus des USB-Geräts | implementiert, **nicht getestet** |
| Test am Bambu Lab X2D (ohne Verschlüsselung, Version 0.2.0 und 0.3.0) | **Grundkompatibilität** laut Rückmeldung des Besitzers: Laufwerk erkannt, per Web hochgeladene `.gcode.3mf` angezeigt und gedruckt (Standard-Build MSC + CDC, microSD 64 GB FAT32). Keine Messung. Offen: lange Drucke, Nur-Lese-Modus, Übernahme während eines Drucks, Stromaufnahme, interner Flash, andere Druckermodelle |

Gemessene Raten:

| Medium | USB schreiben | USB lesen | Web-Upload |
|---|---|---|---|
| interner Flash (FAT12, 4-KiB-Cluster) | 47 KB/s | 753 KB/s | 46 KB/s |
| microSD 64 GB (FAT32) | 832 KB/s | – | 1335–1578 KB/s |
| microSD 64 GB, **verschlüsselt** (0.4.0) | 820 KB/s | 659 KB/s | 1,0 MB/s |

Bei der verschlüsselten Messung fehlt ein Vergleich mit unverschlüsselt unter denselben
Bedingungen (noch nicht geprüft). Für die Einrichtung der Verschlüsselung auf der
microSD-Karte misst das Gerät 51 s (64 GB).

Ohne Karte arbeitet der Dongle mit dem internen Flash (ca. 11 MB, eine 5-MB-Datei braucht
ca. 2 min). Empfohlen wird die microSD-Karte (FAT32, groß, schnell). Karte
nur im ausgesteckten Zustand wechseln. Ergebnisse in [docs/05_testprotokoll.md](docs/05_testprotokoll.md),
Befunde und offene Risiken in [docs/06_risiken.md](docs/06_risiken.md).

## Funktionen im Überblick

| Funktion | Version | Kurz erklärt | Anleitung |
|---|---|---|---|
| Dateien hochladen, laden, umbenennen, löschen | 0.2.0 | Im Browser, auch per Drag & Drop, beliebige Dateitypen | Schnellstart, Abschnitt 4 |
| Anmeldung, Benutzer und Rollen | 0.3.0 | `admin` darf alles, `user` verwaltet Dateien und wechselt die Betriebsart | [docs/09_api_v3.md](docs/09_api_v3.md) Abschnitt 2 |
| Ordner | 0.3.0 | Anlegen, öffnen, umbenennen, verschieben, kopieren, löschen; mehrere Dateien markieren | Weboberfläche |
| Mehrere WLANs | 0.3.0 | Bis zu 5 gespeicherte Netze; das stärkste bekannte wird genommen | Weboberfläche, Einstellungen → Netzwerk |
| Taste mit QR-Code | 0.3.0 | Klick zeigt die Adresse als QR-Code, Doppelklick wechselt die Betriebsart | Abschnitt „Bedienung an der Taste“ |
| App-Icon am Handy | 0.3.0 | Adresse im Browser als Verknüpfung auf den Home-Bildschirm legen | [docs/10_senden.md](docs/10_senden.md) |
| Senden vom PC (Windows) | 0.3.0 | Rechtsklick → „Senden an“ → WiFi DataDrive | [tools/sendto/README.md](tools/sendto/README.md) |
| Senden vom iPhone | 0.3.0 | Kurzbefehl über das Teilen-Menü (von Hand einzurichten) | [docs/10_senden.md](docs/10_senden.md) |
| Firmware-Update im Browser | 0.3.0 | Nur `admin`; Datei hochladen, Rückkehr zur alten Version bei Fehlstart | Einstellungen → System |
| Deutsch und Englisch | 0.3.0 | Weboberfläche folgt der Browsersprache, umstellbar | Einstellungen |
| Verschlüsselung des Laufwerks | 0.4.0 | AES-256-XTS, optional; nach jedem Einstecken mit Passwort entsperren | Abschnitt „Verschlüsselung einrichten und am Drucker benutzen“ |
| WLAN-Suche | 0.4.0 | Beim Hinzufügen eines Netzes die Netze in Reichweite auswählen | Einstellungen → Netzwerk |
| Animierte Anzeige „LIO“ | 0.4.0 | Zeigt den Zustand auf dem Display (bereit, Upload, gesperrt, Fehler …) | – |

## Schnellstart

Voraussetzung: ESP-IDF v5.5.6 unter `%USERPROFILE%\esp\esp-idf-v5.5.6`
(Installation siehe [docs/04_build_und_flash.md](docs/04_build_und_flash.md)).
Alle Befehle in PowerShell im Projektordner.

**0. Werks-Firmware** – Ein Vollbackup per `tools\backup_flash.ps1` war über
USB-Serial/JTAG unzuverlässig. Zum Wiederherstellen liegt das offizielle LILYGO-Abbild
`backup\T-Dongle-S3_factory_screen_251218.bin` bereit (SHA256 in `backup\SHA256SUMS.txt`,
Befehl in [docs/04_build_und_flash.md](docs/04_build_und_flash.md) Abschnitt 5).

**1. Smoke-Test bauen und flashen** (Werks-Firmware oder Smoke-Test läuft, Port COM4)

```powershell
.\tools\idf.ps1 smoke_test build
.\tools\idf.ps1 smoke_test -p COM4 flash monitor
```

Erwartet: Farbbalken rot/grün/blau/weiß auf dem LCD, im Log
`SMOKE: flash size 16777216 bytes`. Monitor beenden mit `Ctrl+]`.

**2. WLAN-Zugangsdaten (optional)**

```powershell
copy firmware\pd_secrets.h.example firmware\pd_secrets.h   # dann SSID/Passwort eintragen
```

Ohne diese Datei startet der Dongle ein Einrichtungs-WLAN `DataDrive-XXXX`
(Passwort steht auf dem LCD), WLAN wird dann über die Weboberfläche gesetzt
(Einstellungen → Netzwerk).

**3. Hauptfirmware bauen und flashen**

```powershell
.\tools\flash.ps1 -Project printdrive -Build -Monitor
```

`flash.ps1` findet den Port selbst und schaltet einen laufenden `printdrive` per
1200-Baud-Touch in den Download-Modus. Klappt das nicht: BOOT-Taste gedrückt halten,
Dongle einstecken, loslassen, Befehl wiederholen – danach einmal aus- und einstecken,
sonst bleibt der Dongle im Download-Modus.

Danach genügt für jedes Software-Update `.\tools\flash.ps1 -Project printdrive -Build`
(1200-Baud-Touch → Flashen → Start, ohne Handgriff am Dongle). Die Weboberfläche
(`main/web/index.html`) wird dabei automatisch gzip-komprimiert eingebettet.
Ab 0.3.0 kann dasselbe Update auch über die Weboberfläche kommen (noch nicht geprüft).

**4. Benutzen**

- Optional microSD-Karte einlegen (nur bei ausgestecktem Dongle). Ohne Karte wird der
  interne Flash genutzt. exFAT-Karten (meist > 32 GB) einmal im Web auf FAT32 formatieren
  (Einstellungen → Gefahrenbereich).
- Am PC/Drucker einstecken → Laufwerk erscheint (LCD: `USB`).
- Im Browser `http://datadrive.local` oder die IP vom LCD öffnen.
- **Erste Anmeldung:** Benutzer `admin`, Passwort `admin`. Die Oberfläche verlangt sofort
  ein neues Passwort; `admin` ist dabei nicht erlaubt. Ohne Passwortwechsel ist nichts
  anderes bedienbar.
- Im Bereich „Meine Dateien“ Ordner anlegen und Dateien hochladen („Hochladen“ oder
  Drag & Drop, beliebige Dateitypen). Ist das USB-Laufwerk aktiv, aktiviert die
  Weboberfläche zuerst die Dateiverwaltung (ggf. mit Rückfrage), speichert die Dateien und
  stellt das USB-Laufwerk danach wieder bereit.
- Weitere Benutzer legt `admin` im Bereich Benutzer der Weboberfläche an. Rolle `user` darf
  Dateien verwalten und die Betriebsart wechseln, aber keine Einstellungen, WLAN oder
  Benutzer. Ein Gerät kann sich als „vertrautes Gerät“ merken lassen (höchstens 4 je Benutzer).
- Von Hand umschalten: „Dateiverwaltung aktivieren“ bzw. „USB-Laufwerk bereitstellen“ in
  der Statusleiste, oder BOOT-Taste kurz drücken (Doppelklick, siehe unten).
- Passwort vergessen (Anmeldung): BOOT-Taste 10 Sekunden halten. Die Benutzer werden auf
  `admin` / `admin` zurückgesetzt; Dateien und WLAN bleiben, das Laufwerks-Passwort (siehe
  unten) wird nicht geändert.

**Bedienung an der Taste** (laut Quelltext, am Gerät noch nicht geprüft):

| Bedienung | Wirkung |
|---|---|
| einmal kurz drücken | QR-Code mit der Adresse für 20 Sekunden; nochmal drücken blendet ihn aus |
| zweimal schnell drücken | zwischen USB-Laufwerk und Dateiverwaltung wechseln; ist das USB-Gerät noch angeschlossen, zweiter Doppelklick innerhalb von 4 Sekunden |
| 5 bis 10 Sekunden halten, dann loslassen | Wartungsmodus (Download-Modus zum Flashen) |
| 10 Sekunden halten | Benutzer auf `admin` / `admin` zurücksetzen |

**5. API-Tests (optional, verändern den Laufwerksinhalt)**

```powershell
python tools\api_test.py 192.168.2.152 --user admin --password <Passwort> --drive E:
```

Die Tests legen Testdateien an und löschen sie wieder. Mit `--crypt-test` werden zusätzlich
Verschlüsselung und Laufwerks-Passwort geprüft; das **löscht das ganze Laufwerk** und braucht
`--drive-password` sowie `--i-know-this-erases-the-drive`. Die Optionen stehen in
[docs/04_build_und_flash.md](docs/04_build_und_flash.md) Abschnitt 10.

**6. Weboberfläche ohne Hardware entwickeln (optional)**

```powershell
python tools\mock_server.py --port 8090 --scenario files    # dann http://127.0.0.1:8090/
```

Der Testserver bildet die Geräte-API nach und liest `index.html` bei jedem Aufruf neu
(Details: [docs/04_build_und_flash.md](docs/04_build_und_flash.md) Abschnitt 11).

Browser-Test gegen den laufenden Testserver (einmalig `pip install playwright`, nutzt das
installierte Edge, ca. 19 min für PC- und Handy-Profil):

```powershell
python tools\ui_test.py --base http://localhost:8090
```

## Verschlüsselung einrichten und am Drucker benutzen

Die Verschlüsselung ist **optional** und in der Voreinstellung aus. Sie schützt die Dateien
auf der Speicherkarte oder dem Stick vor Leuten, die den Stick ohne Passwort in die Hand
bekommen. Was sie nicht schützt, steht in „Grenzen“ weiter unten.

**Vor dem Einrichten:** Alle Dateien auf der Karte werden gelöscht. Vorher auf den PC
kopieren.

### Einrichten

1. Im Browser anmelden. Einstellungen öffnen, Abschnitt **Speicher & USB**, Eintrag
   **Verschlüsselung**. (Die Bezeichnungen stammen aus der Weboberfläche; der Weg ist noch
   nicht von Hand am Gerät nachgeklickt.)
2. **Einrichten** antippen. Die Oberfläche fragt „Laufwerk verschlüsseln?“ und erklärt die
   Folgen. Mit **Löschen und verschlüsseln** bestätigen.
3. Ein **Laufwerks-Passwort** eingeben, mindestens 12 Zeichen. Es ist **nicht** das
   Anmelde-Passwort. Ein langer Satz oder ein zufälliges Passwort ist am besten.
4. Warten. Auf einer 64-GB-microSD dauerte die Einrichtung 51 s. Bei anderen Karten
   kann es länger dauern. Das Laufwerk antwortet währenddessen auf nichts anderes; den Stick
   nicht abziehen.
5. Am Ende erscheint **einmalig** der **Wiederherstellungsschlüssel** (8 Gruppen zu je 4
   Zeichen, z. B. `7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S`). **Sofort aufschreiben** und
   getrennt vom Stick aufbewahren. Er wird nie wieder angezeigt. Groß-/Kleinschreibung und
   Bindestriche sind beim Eingeben egal.
6. Das Laufwerk ist danach entsperrt und benutzbar.

### Am Drucker benutzen

1. Stick mit Karte am Drucker einstecken. **Nach jedem Einstecken und nach jedem
   Stromausfall ist das Laufwerk gesperrt.** Der Drucker sieht dann kein Medium.
2. Mit dem Handy im selben WLAN die Adresse `http://datadrive.local` oder die IP öffnen und
   anmelden.
3. Die Oberfläche zeigt „Laufwerk gesperrt“. Das **Laufwerks-Passwort** eingeben (nicht das
   Anmelde-Passwort) und entsperren. Ist der Drucker angeschlossen, bekommt er das Laufwerk
   danach.
4. Auf dem Drucker die Datei wählen und drucken.

Hinweis: Dass der X2D von einem verschlüsselten, per Handy entsperrten Stick druckt, ist
**noch nicht geprüft**. Bis das getestet ist, gilt das als Annahme.

Solange das Laufwerk entsperrt ist, sieht der Drucker die Dateien im Klartext. Die
Verschlüsselung schützt nur, solange der Stick gesperrt ist.

### Passwort vergessen

- **Wiederherstellungsschlüssel vorhanden:** Im Entsperr-Dialog „Passwort vergessen?“ den
  Schlüssel eintragen. Danach das Laufwerk entsperren und unter Verschlüsselung ein neues
  Laufwerks-Passwort festlegen. Als bisheriges Passwort dient dabei der Schlüssel.
- **Schlüssel verloren, aber Passwort bekannt:** Unter Verschlüsselung **Neuen
  Wiederherstellungsschlüssel erzeugen** (Laufwerk muss entsperrt sein). Der alte Schlüssel
  funktioniert danach nicht mehr. Der neue wird nur einmal angezeigt.
- **Beides verloren:** Die Daten sind **endgültig verloren**. Es bleibt nur, das Laufwerk
  zu löschen und neu zu formatieren (Verschlüsselung löschen, dann neu einrichten). Das
  Zurücksetzen per Taste hilft hier nicht.

### Verschlüsselung ausschalten

Unter Verschlüsselung **Ausschalten** und das Laufwerks-Passwort eingeben. **Alle Dateien
werden dabei gelöscht.** Danach ist der Speicher unverschlüsselt und an jedem Gerät lesbar.

### Grenzen (aus der unabhängigen Prüfung)

- **Die Stärke des Passworts ist der eigentliche Schutz.** Wer die Karte hat, kann Passwörter
  offline sehr schnell durchprobieren. Ein kurzes Passwort schützt nicht.
- Die Wartezeit nach falschen Eingaben bremst nur das Raten über das Netzwerk am laufenden
  Gerät. Ein Neustart setzt sie zurück.
- Drucker und PC sehen die Dateien im Klartext, solange das Laufwerk entsperrt ist.
- Das Verfahren erkennt keine gezielten Änderungen an den verschlüsselten Daten.
- Updates sind nicht signiert. Wer Administrator-Zugriff hat, könnte geänderte Firmware
  aufspielen, die beim nächsten Entsperren das Passwort mitliest.
- Passwörter gehen ohne HTTPS unverschlüsselt durchs WLAN.

Die vollständige Beschreibung steht in [docs/12_api_v4.md](docs/12_api_v4.md) Abschnitt 2.5.

## Ordnerstruktur

```
README.md
docs/                        Projektdokumentation (deutsch)
firmware/
  partitions.csv             gemeinsame Partitionstabelle (16 MB, OTA-fähig)
  pd_secrets.h.example       Vorlage für WLAN-Zugangsdaten (Kopie pd_secrets.h ist git-ignoriert)
  components/
    board/                   Pinbelegung, APA102-LED, BOOT-Taste, Download-Modus
    st7735/                  ST7735-Paneltreiber (von LILYGO, MIT)
    display/                 Framebuffer, 5x7-Font (Adafruit, BSD), Text/Rechtecke
    pd_wifi/                 WLAN STA/AP, mDNS, WLAN-Suche
    pd_crypt/                AES-256-XTS-Schicht, Kopfbereich, Selbsttest (tools/pd_crypt_check.py)
    applog/                  Log-Ringpuffer (Web/CDC)
  smoke_test/                Phase-0-Hardwaretest
  printdrive/                Hauptfirmware WiFi DataDrive (Ordnername historisch;
                             main/: drive, usb_dev, auth, web_*.c (Anmeldung, Dateien, WLAN,
                             Verschlüsselung, Update, System), ota, tls (HTTPS, aus), ui,
                             anim (Anzeige LIO), web/index.html, web/gzip_file.py)
tools/
  idf.ps1                    idf.py mit geladener ESP-IDF-Umgebung
  flash.ps1                  Flashen mit automatischer Port-/Download-Modus-Erkennung
  monitor.ps1 / monitor.py   Log-Leser (TinyUSB-CDC oder USB-Serial/JTAG)
  backup_flash.ps1 / .py     vollständiges Flash-Backup nach backup/ (über USB-Serial/JTAG unzuverlässig)
  api_test.py                End-to-End-Test der Web-API v4 gegen den echten Dongle
  mock_server.py             Testserver: Geräte-API ohne Hardware, zum Entwickeln/Testen der Weboberfläche
  ui_test.py                 Browser-Test der Weboberfläche gegen den Testserver (Playwright + Edge,
                             PC 1280x800 und Handy-Emulation 390x844, hell/dunkel)
  sendto/                    „Senden an“ für Windows (Install-SendTo.ps1, Send-DataDrive.ps1)
  make_icons.py, make_anim.py  Erzeugen der App-Icons bzw. der LIO-Animation
backup/                      LILYGO-Werksabbild, Flash-Abbilder (*.bin git-ignoriert) + SHA256SUMS.txt
```

## Versionsverlauf

| Version | Inhalt | Commits |
|---|---|---|
| 0.1.0 | „WiFi PrintDrive“: USB-Laufwerk, Umschalten, Upload per Web, microSD | Vorgeschichte vor `72109c6` |
| 0.2.0 | Umbenennung in WiFi DataDrive, neue Weboberfläche, API v2, Browser-Test 566/566 | `72109c6` … `0b52a5d` |
| 0.3.0 | Anmeldung und Benutzer, Ordner, mehrere WLANs, Taste mit QR-Code, App-Icon, Senden an (Windows, iPhone), Update über die Weboberfläche, Deutsch/Englisch; API-Test am Gerät 182/182 | `9ee80f4` … `9de5179` |
| 0.4.0 | Verschlüsselung des Laufwerks (AES-256-XTS), WLAN-Suche, Display-Animation „LIO“, HTTPS vorbereitet (aus); API-Test am Gerät 334/334; Browser-Tests 548/548 (PC) und 561/561 (Handy) | `24b12b9` … `0de2267` |

Die Versionen 0.3.0 und 0.4.0 sind mit ihren Plänen in [docs/08_plan_v03.md](docs/08_plan_v03.md)
und [docs/11_plan_v04.md](docs/11_plan_v04.md) beschrieben. Was als Nächstes kommen kann, steht in
[docs/14_vormerkliste.md](docs/14_vormerkliste.md).

## Dokumentation

| Datei | Inhalt |
|---|---|
| [docs/00_kern_usb_gadget.md](docs/00_kern_usb_gadget.md) | der Kern in Kurzform: Besitzer-Prinzip, Sicherungen, Eckdaten |
| [docs/01_bestandsaufnahme.md](docs/01_bestandsaufnahme.md) | Hardware, verifizierte Daten, Technologieentscheidung |
| [docs/02_architektur.md](docs/02_architektur.md) | Module, Besitzer-Zustandsautomat, Upload, Weboberfläche, LCD, USB, Anmeldung, Verschlüsselung |
| [docs/03_umsetzungsplan.md](docs/03_umsetzungsplan.md) | Phasen 0–8, Abnahmekriterien, Status, Backlog |
| [docs/04_build_und_flash.md](docs/04_build_und_flash.md) | ESP-IDF-Installation, Bauen, Flashen, Recovery, Konfiguration, Tests, Testserver |
| [docs/05_testprotokoll.md](docs/05_testprotokoll.md) | Testprozeduren mit erwarteten Ergebnissen, Ergebnisse je Version |
| [docs/06_risiken.md](docs/06_risiken.md) | offene Risiken, ungeprüfte Punkte und Annahmen |
| [docs/07_api.md](docs/07_api.md) | HTTP-API Version 2 (historisch; ersetzt durch Version 3 und 4) |
| [docs/08_plan_v03.md](docs/08_plan_v03.md) | Plan Version 0.3.0: Funktionen, Entscheidungen, Abnahme |
| [docs/09_api_v3.md](docs/09_api_v3.md) | HTTP-API Version 3 (0.3.0): Anmeldung, Rollen, Dateien, Ordner, WLAN, Taste |
| [docs/10_senden.md](docs/10_senden.md) | Senden vom iPhone (Kurzbefehl) und App-Icon am Handy |
| [docs/11_plan_v04.md](docs/11_plan_v04.md) | Plan Version 0.4.0: Verschlüsselung, WLAN-Suche, Restarbeiten |
| [docs/12_api_v4.md](docs/12_api_v4.md) | HTTP-API Version 4 (0.4.0): WLAN-Suche, Verschlüsselung, HTTPS-Option; Schutzwirkung in Abschnitt 2.5 |
| [docs/13_display_animation_briefing.md](docs/13_display_animation_briefing.md) | Vorgaben für die animierte Anzeige „LIO“ |
| [docs/14_vormerkliste.md](docs/14_vormerkliste.md) | Vorgemerkte Funktionen für die Zeit nach 0.4.0 (u. a. HTTPS mit Speicher-Umbau) |

## Lizenzen von Fremdcode

- `firmware/components/st7735/` – ST7735-Treiber aus dem LILYGO-Repository, MIT (`LICENSE.LILYGO`).
- `firmware/components/display/font5x7.c` – Adafruit-GFX-Font, BSD (`LICENSE.Adafruit_GFX`).
