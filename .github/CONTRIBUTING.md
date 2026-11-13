# Mitmachen / Contributing

*English summary below.*

Danke für das Interesse. Fehlerberichte, Testergebnisse von echter Hardware und Korrekturen sind
willkommen. Die Dokumentation ist auf Deutsch; Code-Kommentare und Logmeldungen sind auf Englisch.

## Regeln des Projekts

1. **Jede Aussage trägt eine Markierung:** „verifiziert“ heißt am echten Gerät geprüft, mit Ergebnis
   in [docs/05_testprotokoll.md](../docs/05_testprotokoll.md). Alles andere heißt „noch nicht
   geprüft“ (gebaut, im Quelltext vorhanden, nur am Testserver oder nur recherchiert). Das gilt für
   README, Doku, Pull Requests und Release-Texte.
2. **Keine Kompatibilitätsaussage ohne echten Test.** Ein Drucker, ein Betriebssystem, ein Browser
   oder ein WebDAV-Programm gilt erst als unterstützt, wenn es damit ausprobiert wurde, mit Angabe
   von Version, Speicher und Ergebnis. Recherche wird als Recherche gekennzeichnet.
3. **Die Ein-Besitzer-Regel des Laufwerks wird nie aufgeweicht.** Das FAT-Dateisystem hat zu jedem
   Zeitpunkt genau einen Besitzer: das USB-Gerät (Drucker/PC) oder die Firmware (Weboberfläche,
   WebDAV). Es gibt keinen gleichzeitigen Zugriff, kein „nur schnell lesen“ am Besitzer vorbei und
   keinen Wechsel ohne die Sicherungen in `drive.c` (Details:
   [docs/02_architektur.md](../docs/02_architektur.md), [docs/00_kern_usb_gadget.md](../docs/00_kern_usb_gadget.md)).
   Änderungen an diesen Stellen brauchen eine Begründung und Tests am Gerät.
4. **API-Änderungen** zuerst im API-Vertrag in `docs/` beschreiben, dann in der Firmware und im
   Testserver `tools/mock_server.py` umsetzen, dann `tools/api_test.py` erweitern.
5. **Keine Geheimnisse im Repository:** WLAN-Zugangsdaten nur in `firmware/pd_secrets.h`
   (git-ignoriert, Vorlage `firmware/pd_secrets.h.example`), keine Schlüssel, keine Flash-Abbilder,
   keine Build-Ordner.
6. **Fremdcode** nur mit einer Lizenz, die zur MIT-Lizenz passt; Herkunft und Lizenz in
   [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) eintragen, Lizenzkopf und Lizenzdatei behalten.

## Bauen

Benötigt: **ESP-IDF v5.5.6** (genau diese Version; die Komponenten sind in
`firmware/printdrive/dependencies.lock` festgelegt).

Linux/macOS (ESP-IDF-Umgebung geladen, `. $IDF_PATH/export.sh`):

```sh
idf.py -C firmware/printdrive build
idf.py -C firmware/smoke_test build      # Hardwaretest, nutzt dieselben Komponenten
```

Windows (PowerShell, ESP-IDF unter `%USERPROFILE%\esp\esp-idf-v5.5.6`):

```powershell
.\tools\idf.ps1 printdrive build
.\tools\flash.ps1 -Project printdrive -Build -Monitor
```

Installation, Flashen, Wiederherstellen: [docs/04_build_und_flash.md](../docs/04_build_und_flash.md).
Die Voreinstellungen stehen in `firmware/printdrive/sdkconfig.defaults`; die erzeugte `sdkconfig`
wird nicht eingecheckt. Jeder Push und jeder Pull Request wird zusätzlich von GitHub Actions mit dem
Container `espressif/idf:v5.5.6` gebaut (`.github/workflows/build.yml`).

## Testen

Python-Pakete: `python -m pip install -r requirements-dev.txt` (Python 3.10 oder neuer).

**API-Test gegen den Testserver** (läuft auch in GitHub Actions):

```sh
python tools/mock_server.py --port 8090 --upload-kbps 0
python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123
```

**Browser-Test der Weboberfläche** gegen den Testserver (Playwright mit dem installierten Edge oder
Chrome, etwa 19 Minuten für PC- und Handy-Profil), bei jeder Änderung an `index.html`:

```sh
python tools/mock_server.py --port 8090
python tools/ui_test.py --base http://localhost:8090            # --channel chrome ohne Edge
```

**Am echten Gerät** (wenn möglich, verändert den Laufwerksinhalt; zerstörende Tests nur mit den
ausdrücklichen Schaltern, siehe Kopf von `tools/api_test.py`):

```sh
python tools/api_test.py <IP des Sticks> --user admin --password <Passwort>
```

Ergebnisse mit Datum, Version, Board, Speicher und Host in den Pull Request schreiben; was nicht am
Gerät lief, als „noch nicht geprüft“ markieren.

## Pull Requests

- Ein Thema je Pull Request; die Vorlage füllt den Prüfstand ab.
- Bestehende Formatierung beibehalten (`.editorconfig`); keine reinen Umformatierungen ganzer Dateien.
- README, `docs/05_testprotokoll.md` und `docs/06_risiken.md` mitpflegen, wenn sich Verhalten oder
  Prüfstand ändern.

---

## English summary

Bug reports, results from real hardware and fixes are welcome. Documentation is German; code
comments and log messages are English.

**Project rules:** (1) every claim is marked *verified* (checked on a real device, result recorded
in `docs/05_testprotokoll.md`) or *untested*; (2) no compatibility claim without a real test
(printer, OS, browser, WebDAV client: name version, storage and result); (3) the single-owner rule
of the drive is never weakened: the FAT file system always has exactly one owner, the USB host or
the firmware; (4) API changes go into the API contract in `docs/`, the firmware, the mock server
and `tools/api_test.py`; (5) no secrets, keys, flash dumps or build output in the repository;
(6) third-party code only under an MIT-compatible licence, recorded in `THIRD_PARTY_NOTICES.md`.

**Build:** ESP-IDF v5.5.6, `idf.py -C firmware/printdrive build` (Windows: `.\tools\idf.ps1
printdrive build`). CI builds every push and pull request with `espressif/idf:v5.5.6`.

**Test:** `pip install -r requirements-dev.txt`; start `python tools/mock_server.py --port 8090`,
then run `python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123` and, for web UI
changes, `python tools/ui_test.py --base http://localhost:8090`. Report on-device results with
date, version, board, storage and host.
