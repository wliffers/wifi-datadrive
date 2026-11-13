# 18 – Veröffentlichung auf GitHub

Checkliste für den Besitzer. Stand: 2026-11-13. Nichts davon ist bisher ausgeführt: Es gibt noch
kein GitHub-Repository, keinen Push, keinen Workflow-Lauf und keine Pages-Seite. Die Workflows
unter `.github/workflows/` sind geschrieben, aber **noch nicht auf GitHub gelaufen** (noch nicht
geprüft). Lokal geprüft wurden nur: `tools/api_test.py` gegen `tools/mock_server.py` (373/373 am
2026-11-13 mit dem Stand dieses Tages) und `.github/scripts/merge_image.py` an einem vorhandenen
lokalen Build (Abbild 1.478.800 Byte, von esptool als ESP32-S3-Abbild erkannt).

## 1. Vor dem ersten Push

Diese Punkte sind offen und müssen vom Besitzer entschieden oder erledigt werden.

| Nr. | Punkt | Wo | Was tun |
|---|---|---|---|
| V1 | Name des Rechteinhabers fehlt | `LICENSE` Zeile 3: `Copyright (c) 2026 <NAME>` | Erledigt: Willi Liffers. |
| V2 | E-Mail-Adresse in allen 33 Commits (Autor und Committer) | `git log --format='%an <%ae> / %cn <%ce>'` | Wird mit dem Push öffentlich. Entweder hinnehmen oder vor dem ersten Push den Verlauf mit einer GitHub-noreply-Adresse neu schreiben (z. B. `git filter-repo --mailmap`). Für neue Commits `git config user.email <id>+<name>@users.noreply.github.com` setzen und in den GitHub-Einstellungen „Keep my email addresses private“ einschalten |
| V3 | Alle 33 Commit-Nachrichten enden mit einer Zeile Mitautor-Zeile mit fremdem Namen und fremder Adresse | `git log --all --format=%B` | Vor der Veröffentlichung entfernen (Verlauf neu schreiben, z. B. `git filter-repo --message-callback`). Geht nur zusammen mit V2 in einem Durchgang sinnvoll |
| V4 | Lokaler Pfad mit Benutzername | `docs/04_build_und_flash.md` Zeile 4 | Erledigt. |
| V5 | Fest eingetragener Pfad in den lokalen Temp-Ordner des Benutzers | `tools/ui_test.py` Zeilen 92–93 (`_SCRATCH_SHOTS`) | Erledigt. |
| V6 | Interne Arbeitsnotizen zur Arbeitsteilung | `docs/08_plan_v03.md` Zeilen 44–60 (Spalte „Modell“, „interne Notizen“), `docs/11_plan_v04.md` Zeilen 77–97 (ebenso), `docs/04_build_und_flash.md` Zeile 55, `docs/13_display_animation_briefing.md` Zeile 3 | Erledigt. |
| V7 | Lizenz der Maskottchen-Grafik „LIO“ ungeklärt | `firmware/printdrive/main/anim/*.png`, daraus erzeugt `anim_data.inc` | Erledigt: eigene Arbeit des Rechteinhabers, steht unter der MIT-Lizenz des Repositorys. |
| V8 | Platzhalter für Repository und Pages-Adresse | `README.md` Abschnitt „Flashen im Browser“, `README.en.md` Abschnitt „Flash in the browser“: `https://<OWNER>.github.io/wifi-datadrive/` | Nach dem Anlegen des Repositorys echte Adresse eintragen. Die Workflows und die Flasher-Seite brauchen keine Platzhalter (sie nehmen den Namen aus GitHub) |
| V9 | README verweist auf ein Werksabbild in `backup\`, das nicht veröffentlicht wird | `README.md` Schnellstart Punkt 0, `docs/04_build_und_flash.md` Abschnitt 5 | Für Fremde ergänzen, woher das LILYGO-Werksabbild kommt (Hersteller-Repository); das Abbild selbst **nicht** einchecken (`backup/*.bin` ist ignoriert) |
| V10 | Geräte-MAC-Adresse und daraus abgeleiteter Name des Einrichtungs-WLANs | `docs/01_bestandsaufnahme.md` Zeile 24, `docs/05_testprotokoll.md` Zeile 52; `PrintDrive-54D5` in `docs/03`, `docs/04`, `docs/05` | Geringes Risiko (identifiziert nur dieses Exemplar). Nach Wunsch kürzen, z. B. `e0:72:a1:xx:xx:xx` |
| V11 | Eintrag für einen lokalen Werkzeugordner | `.gitignore` Zeile 26 | Der Eintrag nennt den Ordnernamen eines Werkzeugs. Wer das nicht veröffentlichen will: Zeile löschen und denselben Eintrag in `.git/info/exclude` setzen (wirkt nur lokal) |

Prüfen, unmittelbar vor dem Push (alle Befehle im Projektordner):

```sh
git status                                   # sauber, nichts Unerwartetes
git ls-files | grep -i -E 'secret|\.bin$|\.key$|\.pem$|/build|sdkconfig$|managed_components'
git log --all --name-only --format= | sort -u | grep -i secret     # nur pd_secrets.h.example
git log --all --format='%an <%ae> | %cn <%ce>' | sort -u           # Adressen, die öffentlich werden
git grep -n -i -E 'C:\\Users|C:/Users'                              # lokale Pfade
```

Ergebnis der Durchsicht vom 2026-11-13: `firmware/pd_secrets.h` existiert nicht, ist ignoriert und
war nie im Verlauf (nur die Vorlage `pd_secrets.h.example` mit `MeinWLAN`/`MeinPasswort`). Keine
echten WLAN-Namen, keine Schlüssel oder Zertifikate, keine Build-Ausgaben, keine Flash-Abbilder und
keine Dateien über 300 KB im Verlauf. Die PNG-Dateien enthalten keine Metadaten.

Optional, in einem eigenen Commit, wenn niemand sonst offene Änderungen hat: Zeilenenden
vereinheitlichen (17 Dateien liegen mit CRLF im Repository). Vorgehen steht als Kommentar in
`.gitattributes`.

## 2. Repository anlegen

1. Auf GitHub ein neues **öffentliches** Repository anlegen, Vorschlag für den Namen:
   `wifi-datadrive`. **Ohne** README, Lizenz und `.gitignore` von GitHub (liegen schon vor).
2. Lokal: `git remote add origin https://github.com/<OWNER>/wifi-datadrive.git`, dann `git push -u origin main`.
3. Beschreibung und Themen setzen, z. B. `esp32-s3`, `esp-idf`, `usb-mass-storage`, `3d-printing`,
   `webdav`, `lilygo`.
4. Einstellungen:
   - **Security → Private vulnerability reporting** einschalten (darauf verweist `.github/SECURITY.md`).
   - **Actions → General:** Actions erlauben. Die Workflows setzen ihre Rechte selbst
     (`contents: read`, nur das Anlegen des Release-Entwurfs bekommt `contents: write`).
   - Optional **Branches:** Schutzregel für `main` mit dem Check „Build“.
   - Issues an, Wiki aus (die Doku liegt in `docs/`).
5. Nach dem ersten Push den Lauf „Build“ unter **Actions** ansehen. Erwartet: Firmware-Job grün,
   Artefakt `firmware` mit den `.bin`-Dateien, API-Test grün. Ergebnis hier eintragen.

## 3. Browser-Flasher (GitHub Pages)

1. **Settings → Pages → Source: „GitHub Actions“**.
2. Workflow „Web flasher (GitHub Pages)“ einmal von Hand starten (**Actions → Run workflow**).
   Ohne veröffentlichtes Release zeigt die Seite „Es ist noch keine Version veröffentlicht“.
3. Nach dem ersten veröffentlichten Release läuft der Workflow von selbst und legt das
   zusammengeführte Abbild neben die Seite.
4. Mit einem echten Dongle ausprobieren (Chrome oder Edge, Taste beim Einstecken halten) und das
   Ergebnis in `docs/05_testprotokoll.md` eintragen. Bis dahin gilt die Seite als **noch nicht geprüft**.

## 4. Release erstellen

1. Version in `firmware/printdrive/CMakeLists.txt` (`PROJECT_VER`) setzen, README-Stand,
   `docs/05_testprotokoll.md` und `docs/06_risiken.md` nachziehen, committen.
2. Tag setzen und pushen; der Tag muss zu `PROJECT_VER` passen (sonst bricht der Build ab):

   ```sh
   git tag -a v0.5.0 -m "WiFi DataDrive 0.5.0"
   git push origin v0.5.0
   ```

3. Der Workflow „Release“ baut, testet und legt einen **Entwurf** an mit
   `wifi-datadrive-vX.Y.Z-esp32s3-merged.bin` (Erstinstallation ab 0x0, Browser-Flasher),
   `wifi-datadrive-vX.Y.Z-app.bin` (Update über die Weboberfläche),
   `wifi-datadrive-vX.Y.Z-build-files.zip`, `licenses.zip`, `LICENSE`, `THIRD_PARTY_NOTICES.md`
   und `SHA256SUMS.txt`. Diese Dateien sind **nicht signiert**.
4. **Signierte Updates** (falls genutzt): Signiert wird **nur auf dem eigenen Rechner**, der private
   Schlüssel kommt nie zu GitHub (kein Secret, keine Variable, kein Upload). Den signierten Build
   nach der Anleitung zu den signierten Updates (`tools/signing/`, Doku unter `docs/`) erstellen,
   dann das zusammengeführte Abbild daraus bauen:

   ```sh
   python .github/scripts/merge_image.py firmware/printdrive/build_signed wifi-datadrive-vX.Y.Z-esp32s3-merged.bin
   ```

   Im Entwurf die beiden `.bin`-Dateien durch die signierten ersetzen (**gleiche Dateinamen**) und
   `SHA256SUMS.txt` neu erzeugen und ersetzen; der Pages-Workflow prüft das Abbild gegen diese
   Datei. Vor diesem Build sicherstellen, dass `firmware/pd_secrets.h` auf dem Rechner **nicht**
   existiert, sonst stehen die eigenen WLAN-Zugangsdaten im veröffentlichten Abbild.
5. Release-Text prüfen (Prüfstand der Version, bekannte Grenzen) und **veröffentlichen**.
   Danach aktualisiert sich der Browser-Flasher von selbst.

## 5. Was von Hand bleibt

- Signieren der Release-Dateien und Ersetzen im Entwurf (Abschnitt 4).
- Veröffentlichen jedes Releases (der Workflow legt nur Entwürfe an).
- Tests am echten Gerät und am Drucker; GitHub Actions testet nur gegen den Testserver.
  Der Browser-Test `tools/ui_test.py` läuft nicht in GitHub Actions.
- Einstellungen auf GitHub (Pages, Private vulnerability reporting, Schutzregeln).
- Antworten auf vertrauliche Sicherheitsmeldungen.
- Die offenen Punkte V1 bis V11 oben.
