# Mitmachen

*English summary below.*

Danke fürs Interesse. WiFi DataDrive ist ein privates Freizeitprojekt – Rückmeldungen sind willkommen,
eine Antwort oder Behebung kann ich aber nicht zusichern. Die Dokumentation ist deutsch; Code-Kommentare
und Logmeldungen sind englisch.

## Fehler melden

Bitte als Issue (die Vorlage fragt die Punkte ab) mit:

1. **Firmware-Version** (Einstellungen › System oder `version` in `/api/status`) und woher die Firmware
   stammt: Release, Browser-Flasher oder selbst gebaut.
2. **Board-Variante.** Getestet ist nur der LILYGO T-Dongle-S3 mit LCD.
3. **Speicher:** microSD (Größe, FAT32 oder exFAT) oder interner Flash; verschlüsselt ja oder nein.
4. **Das USB-Gerät**, an dem der Stick steckt (zum Beispiel Druckermodell, PC, Fernseher), möglichst mit
   Firmware- bzw. Betriebssystem-Version.
5. **Was du getan hast** (Schritte) und **was stattdessen passiert ist.**
6. Die letzten Zeilen aus dem Protokoll (Einstellungen › System). Bitte vorher Passwörter,
   Wiederherstellungsschlüssel, WLAN-Namen und alles entfernen, was du nicht veröffentlichen willst.

**Sicherheitslücken bitte nicht als Issue**, sondern vertraulich melden: [.github/SECURITY.md](.github/SECURITY.md).

## Testergebnisse von echter Hardware

Besonders hilfreich, denn vieles ist nur am Testserver geprüft und als USB-Gerät sind nur ein Windows-PC
und ein 3D-Drucker geprüft: Ergebnisse mit anderen USB-Geräten (Drucker, Fernseher, Mediaplayer, andere
Rechner und so weiter), Karten, Handys oder WebDAV-Programmen. Bitte mit Datum, Firmware-Version, Board, Speicher, Gerät und Ergebnis –
auch wenn etwas nicht geklappt hat. Was noch offen ist, steht in [docs/14_vormerkliste.md](docs/14_vormerkliste.md)
und in der Tabelle „Geprüft und nicht geprüft“ der [README](README.md).

## Ideen

Gern als Issue mit dem Anwendungsfall: Was willst du erreichen, und was fehlt dir dafür?

## Regeln des Projekts

1. **Nichts Ungeprüftes behaupten.** „Geprüft“ heißt: am echten Gerät ausprobiert, mit Ergebnis in
   [docs/05_testprotokoll.md](docs/05_testprotokoll.md). Alles andere heißt „noch nicht geprüft“ – gebaut,
   im Quelltext vorhanden, nur am Testserver gelaufen oder nur recherchiert. Das gilt für README, Doku,
   Pull Requests und Release-Texte.
2. **Keine Kompatibilitätsaussage ohne echten Test.** Ein USB-Gerät (Drucker, Fernseher, Rechner …), ein
   Betriebssystem, ein Browser oder ein WebDAV-Programm gilt erst als unterstützt, wenn es damit ausprobiert wurde, mit Version, Speicher und
   Ergebnis. Recherche wird als Recherche gekennzeichnet.
3. **Die Ein-Besitzer-Regel des Laufwerks bleibt.** Das Dateisystem hat zu jedem Zeitpunkt genau einen
   Besitzer: das USB-Gerät, an dem der Stick steckt, oder die Firmware (Weboberfläche, WebDAV). Kein gleichzeitiger
   Zugriff, kein „nur schnell lesen“ am Besitzer vorbei, kein Wechsel ohne die Sicherungen in `drive.c`
   ([docs/00_kern_usb_gadget.md](docs/00_kern_usb_gadget.md), [docs/02_architektur.md](docs/02_architektur.md)).
   Änderungen an diesen Stellen brauchen eine Begründung und Tests am Gerät.
4. **API-Änderungen** zuerst im API-Vertrag in `docs/` beschreiben (zuletzt
   [docs/16_api_v6.md](docs/16_api_v6.md)), dann in der Firmware und im Testserver `tools/mock_server.py`
   umsetzen, dann `tools/api_test.py` erweitern.
5. **Keine Geheimnisse im Repository:** WLAN-Zugangsdaten nur in `firmware/pd_secrets.h` (git-ignoriert,
   Vorlage `firmware/pd_secrets.h.example`); keine Schlüssel, keine Flash-Abbilder, keine Build-Ordner.
6. **Fremdcode** nur mit einer Lizenz, die zur MIT-Lizenz passt. Herkunft und Lizenz kommen in
   [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), Lizenzkopf und Lizenzdatei bleiben erhalten.
7. **Das Maskottchen LIO** steht nicht unter MIT ([LICENSE-LIO.md](LICENSE-LIO.md)). Bitte keine
   veränderten Fassungen der Grafiken beisteuern.

## Bauen

Benötigt: **ESP-IDF 5.5.6** (genau diese Version; die Komponenten sind in
`firmware/printdrive/dependencies.lock` festgelegt).

Linux und macOS (ESP-IDF-Umgebung geladen, `. $IDF_PATH/export.sh`):

```sh
idf.py -C firmware/printdrive build
idf.py -C firmware/smoke_test build      # Hardwaretest, nutzt dieselben Komponenten
```

Windows (PowerShell, ESP-IDF unter `%USERPROFILE%\esp\esp-idf-v5.5.6`):

```powershell
.\tools\idf.ps1 printdrive build
.\tools\flash.ps1 -Project printdrive -Build -Monitor
```

Immer nur einen Build gleichzeitig laufen lassen. Installation, Flashen und Wiederherstellen:
[docs/04_build_und_flash.md](docs/04_build_und_flash.md). Die Voreinstellungen stehen in
`firmware/printdrive/sdkconfig.defaults`; die erzeugte `sdkconfig` wird nicht eingecheckt. GitHub Actions
baut jeden Push und jeden Pull Request zusätzlich im Container `espressif/idf:v5.5.6`
(`.github/workflows/build.yml`). Signierte Builds sind Sache des Besitzers:
[docs/17_signierte_updates.md](docs/17_signierte_updates.md).

## Testen

Python-Pakete (Python 3.10 oder neuer):

```sh
python -m pip install -r requirements-dev.txt
```

**API-Test gegen den Testserver** (läuft auch in GitHub Actions):

```sh
python tools/mock_server.py --port 8090 --upload-kbps 0
python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123
```

**Browser-Test der Weboberfläche** gegen den Testserver, bei jeder Änderung an `index.html` (Playwright
mit dem installierten Edge oder Chrome, dauert eine Weile):

```sh
python tools/mock_server.py --port 8090
python tools/ui_test.py --base http://localhost:8090            # --channel chrome ohne Edge
```

**Am echten Gerät**, wenn möglich. Der Test verändert den Laufwerksinhalt; zerstörende Prüfungen laufen
nur mit den ausdrücklichen Schaltern (siehe Kopf von `tools/api_test.py`):

```sh
python tools/api_test.py <Adresse des Sticks> --user admin --password <Passwort>
```

Ergebnisse mit Datum, Version, Board, Speicher und Gerät in den Pull Request schreiben; was nicht am Gerät
lief, als „noch nicht geprüft“ markieren.

## Pull Requests

- Fork, Branch, Pull Request – ein Thema je Pull Request, mit kurzer Beschreibung, was sich ändert und
  warum. Die Vorlage fragt den Prüfstand ab.
- Bestehende Formatierung beibehalten (`.editorconfig`); keine reinen Umformatierungen ganzer Dateien.
- README, [CHANGELOG.md](CHANGELOG.md), `docs/05_testprotokoll.md` und `docs/06_risiken.md` mitpflegen,
  wenn sich Verhalten oder Prüfstand ändern.
- Neue Texte der Weboberfläche brauchen eine deutsche und eine englische Fassung.

## Prüfung vor jedem Commit

`tools/publish_check.py` sucht nach Dingen, die nicht ins Repository gehören: fremde E-Mail-Adressen,
lokale Benutzerpfade, Schlüssel und Zugangsdaten, Build-Ausgaben, große Dateien, kaputte Links. GitHub
Actions führt die Prüfung bei jedem Push und Pull Request aus (`.github/workflows/publish-check.yml`).
Damit sie schon vor jedem Commit läuft, einmal je Klon die Git-Hooks einschalten:

```sh
git config core.hooksPath tools/git-hooks      # Windows auch: .\tools\install-hooks.ps1
```

Die Hooks brauchen Python 3.10 oder neuer und eine Datei mit privaten Suchbegriffen außerhalb des
Repositorys (`%USERPROFILE%\.datadrive\publish_secrets.txt` bzw. `~/.datadrive/publish_secrets.txt`;
eine leere Datei genügt). Von Hand: `python tools/publish_check.py` (Stand und ganze Historie) oder
`python tools/publish_check.py --tree-only` (nur die vorgemerkten Dateien). Einzelheiten:
[docs/19_sicherer_veroeffentlichungsprozess.md](docs/19_sicherer_veroeffentlichungsprozess.md).

---

## English summary

Thanks for your interest. WiFi DataDrive is a private spare-time project: feedback is welcome, but answers
or fixes cannot be promised. Documentation is German; code comments and log messages are English.

**Bug reports** (as an issue, the template asks for this): firmware version and source (release, web
flasher, own build), board variant, storage (microSD size and file system or internal flash, encrypted or
not), the USB host (for example printer model, PC, TV), steps and what happened instead, the last lines of the log without
passwords, recovery keys or Wi-Fi names. **Security vulnerabilities** go privately through
[.github/SECURITY.md](.github/SECURITY.md), not into an issue.

**Project rules:** (1) nothing untested is claimed: *tested* means checked on a real device with the result
in `docs/05_testprotokoll.md`, everything else is *untested*; (2) no compatibility claim without a real test
(USB host such as a printer, TV or computer, OS, browser, WebDAV client: name version, storage and
result); (3) the single-owner rule of the
drive stays: the file system always has exactly one owner, the USB host or the firmware; (4) API changes go
into the API contract in `docs/`, then the firmware, the mock server and `tools/api_test.py`; (5) no
secrets, keys, flash dumps or build output in the repository; (6) third-party code only under an
MIT-compatible licence, recorded in `THIRD_PARTY_NOTICES.md`; (7) the mascot LIO is not MIT-licensed,
please do not contribute modified artwork.

**Build:** ESP-IDF 5.5.6, `idf.py -C firmware/printdrive build` (Windows: `.\tools\idf.ps1 printdrive
build`). CI builds every push and pull request with `espressif/idf:v5.5.6`.

**Test:** `pip install -r requirements-dev.txt`; start `python tools/mock_server.py --port 8090`, then run
`python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123` and, for web UI changes,
`python tools/ui_test.py --base http://localhost:8090`. Report on-device results with date, version, board,
storage and host.
