# Ein Release veröffentlichen

So kommt eine neue Fassung von WiFi DataDrive zu den Anwendern. Die einmaligen Schritte vor dem ersten
Push (Repository anlegen, Einstellungen, GitHub Pages) stehen in
[18_veroeffentlichung.md](18_veroeffentlichung.md). Was signierte Updates schützen und was nicht, steht in
[17_signierte_updates.md](17_signierte_updates.md).

Stand 2026-11-13: Dieser Ablauf ist geschrieben, aber noch nie vollständig durchlaufen. Die Workflows unter
`.github/workflows/` sind noch nicht auf GitHub gelaufen (noch nicht geprüft). Das erste Release wird
**0.6.0**; die Versionen 0.1.0 bis 0.5.0 gibt es nur im Verlauf des Repositorys, nicht als Release. 0.6.0
läuft bereits am Gerät des Besitzers (signiert, über die Weboberfläche von 0.5.0 aus installiert,
API-Test 415/415, [05_testprotokoll.md](05_testprotokoll.md) Phase 10).

## 0. Einmalig: Signierschlüssel

Nur nötig, wenn die Releases signierte Updates verlangen sollen.

```powershell
.\tools\signing\new_key.ps1
```

Das Skript legt `%USERPROFILE%\.datadrive\ota_signing_key.pem` (privat) und `ota_signing_key.pub.pem`
(öffentlich) an, zeigt die Schlüssel-ID und überschreibt nie einen vorhandenen Schlüssel.

- **Den privaten Schlüssel sichern**, an einem zweiten Ort und offline (USB-Stick im Schrank,
  Passwort-Manager). Nie ins Repository, in eine Cloud-Freigabe, in ein Issue, einen Chat oder zu GitHub
  (kein Secret, keine Variable, kein Upload).
- Die Schlüssel-ID notieren; der öffentliche Schlüssel darf veröffentlicht werden.
- Geht der Schlüssel verloren, nehmen die Geräte keine Web-Updates mehr an und müssen per USB neu geflasht
  werden: [17_signierte_updates.md](17_signierte_updates.md) Abschnitt 8.

## 1. Nummern und Texte nachziehen

- `firmware/printdrive/CMakeLists.txt`: `PROJECT_VER` setzen (z. B. `0.6.0`). Der Tag muss später genau
  dazu passen, sonst bricht der Build ab.
- `CHANGELOG.md`: Abschnitt der Version mit Datum statt „in Arbeit“; beim Veröffentlichen den Vermerk
  „noch nicht veröffentlicht“ entfernen (für 0.6.0 steht er heute im Abschnitt `[0.6.0] – 2026-11-13`).
  Der Satz „Bisher ist keine Version als Release veröffentlicht“ am Anfang fällt mit dem ersten Release weg.
- `README.md` und `README.en.md`: Versions-Abzeichen oben („unveröffentlicht“ bzw. „unreleased“ durch die
  bloße Nummer ersetzen), Kasten „Stand“ (ohne „Ein Release ist noch nicht veröffentlicht“), Tabelle
  „Geprüft und nicht geprüft“ und Fußzeile (Nummer und Datum). Mit dem ersten Release außerdem den Satz
  „solange kein Release veröffentlicht ist, gibt es dort nichts zu installieren“ im Abschnitt Installation.
- `docs/05_testprotokoll.md` und `docs/06_risiken.md`: Ergebnisse und offene Punkte der Version.
- Optional: Platzhalter der Version in `.github/ISSUE_TEMPLATE/bug_report.yml`.

## 2. Prüfen

```sh
python tools/mock_server.py --port 8090 --upload-kbps 0
python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123
python tools/ui_test.py --base http://localhost:8090      # läuft nicht in GitHub Actions
```

Am echten Gerät `tools/api_test.py` gegen den Stick laufen lassen und das Ergebnis in
`docs/05_testprotokoll.md` eintragen (für 0.6.0 geschehen: 415/415, Phase 10; ändert sich der Quelltext
bis zum Tag, den Lauf wiederholen). Was nicht am Gerät lief, bleibt in README und Release-Text als
„noch nicht geprüft“ stehen.

Vor dem Einchecken:

```sh
git status                                                  # nichts Unerwartetes
git grep -n -i -E 'C:\\Users|C:/Users'                      # keine lokalen Pfade
git ls-files | grep -i -E 'secret|\.bin$|\.key$|\.pem$|/build|sdkconfig$|managed_components'
```

`firmware/pd_secrets.h` darf auf dem Rechner, auf dem du baust, **nicht** existieren. Sonst stehen deine
WLAN-Zugangsdaten im veröffentlichten Abbild.

## 3. Einchecken und taggen

```sh
git commit -am "Release 0.6.0"
git tag -a v0.6.0 -m "WiFi DataDrive 0.6.0"
git push origin main
git push origin v0.6.0
```

## 4. Entwurf ansehen

Der Workflow „Release“ baut mit `espressif/idf:v5.5.6`, testet gegen den Testserver und legt einen
**Entwurf** an mit:

| Datei | Zweck |
|---|---|
| `wifi-datadrive-vX.Y.Z-esp32s3-merged.bin` | Erstinstallation ab 0x0, Browser-Flasher und esptool; setzt Benutzer, WLANs und Einstellungen zurück |
| `wifi-datadrive-vX.Y.Z-app.bin` | Update über die Weboberfläche (Einstellungen › System); Dateien und Einstellungen bleiben |
| `wifi-datadrive-vX.Y.Z-build-files.zip` | Bauergebnisse zum Nachvollziehen |
| `licenses.zip`, `LICENSE`, `THIRD_PARTY_NOTICES.md` | Lizenzen, die mit den Firmware-Dateien weitergegeben werden müssen |
| `SHA256SUMS.txt` | Prüfsummen; der Pages-Workflow prüft das Abbild dagegen |

Diese Dateien sind **nicht signiert**. Ohne signierte Updates kannst du direkt bei Schritt 6 weitermachen.

## 5. Signierte Dateien einsetzen

Signiert wird **nur auf dem eigenen Rechner**.

1. Signierter Build (baut von Grund auf in `firmware\printdrive\build_signed`, signiert beim Bauen und
   prüft die Signatur):

   ```powershell
   .\tools\signing\build_signed.ps1
   ```

   Ausgabe: `build_signed\printdrive-<Version>-signed.bin` mit Größe, SHA-256 und Schlüssel-ID.
2. Optional auf einem anderen Rechner mit dem öffentlichen Schlüssel prüfen:

   ```powershell
   .\tools\signing\verify.ps1 -File firmware\printdrive\build_signed\printdrive-0.6.0-signed.bin
   ```

3. Gesamtabbild aus dem signierten Build (in der ESP-IDF-Umgebung):

   ```sh
   python .github/scripts/merge_image.py firmware/printdrive/build_signed wifi-datadrive-v0.6.0-esp32s3-merged.bin
   ```

4. Im Entwurf ersetzen, **unter denselben Dateinamen**:
   - `…-app.bin` durch `printdrive-<Version>-signed.bin`,
   - `…-esp32s3-merged.bin` durch das Gesamtabbild aus Schritt 3.
5. `SHA256SUMS.txt` neu erzeugen und ersetzen, im Ordner mit allen Dateien des Entwurfs:

   ```sh
   sha256sum -- *.bin *.zip > SHA256SUMS.txt
   ```

6. Die Prüfsummen im Release-Text an die neue `SHA256SUMS.txt` angleichen und die Schlüssel-ID dazuschreiben.

Erst danach veröffentlichen. Ein Gerät mit 0.5.0 oder einer unsignierten Firmware nimmt die signierte Datei
über die Weboberfläche an und verlangt ab dann selbst signierte Updates
([17_signierte_updates.md](17_signierte_updates.md) Abschnitt 7; am Gerät geprüft am 2026-11-13 für
0.5.0 → 0.6.0 mit einer lokal signierten Datei, nicht mit einer Datei aus einem Release-Entwurf).

## 6. Veröffentlichen

1. Release-Text prüfen: Prüfstand der Version, bekannte Grenzen, signiert oder nicht. Als Grundlage dient
   der Abschnitt aus `CHANGELOG.md`.
2. **Publish release.** Der Workflow „Web flasher (GitHub Pages)“ läuft danach von selbst und legt das
   Gesamtabbild neben die Flasher-Seite. Die Links „Download“ und „Im Browser flashen“ in der README zeigen
   dann auf die neue Version.
3. Mit einem echten Dongle über den Browser-Flasher installieren und das Ergebnis in
   `docs/05_testprotokoll.md` eintragen. Bis dahin gilt der Flasher als **noch nicht geprüft**.

## Was von Hand bleibt

- Signieren und Ersetzen der Dateien im Entwurf (Abschnitt 5).
- Veröffentlichen jedes Releases; der Workflow legt nur Entwürfe an.
- Tests am echten Gerät und am Drucker. GitHub Actions testet nur gegen den Testserver.

## Was nie ins Repository gehört

- `firmware/pd_secrets.h`, private Schlüssel (`*.pem`), Flash-Abbilder und Werks-Firmware (`backup/*.bin`).
- Build-Ordner (`build/`, `build_signed/`), `sdkconfig`, `managed_components/`.
- Lokale Pfade, Benutzernamen, private Adressen.
