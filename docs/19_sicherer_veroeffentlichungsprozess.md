# 19 – Sicherer Veröffentlichungsprozess

Anleitung für den Besitzer. Stand: 2026-11-13. Sie beschreibt den vereinbarten Weg vom lokalen
Repository zum öffentlichen Repository `wliffers/wifi-datadrive` auf GitHub, die automatischen
Wächter und den Notfallplan. Die Checkliste mit den bereits erledigten Punkten steht in
[18_veroeffentlichung.md](18_veroeffentlichung.md); der wiederkehrende Release-Ablauf in
[RELEASING.md](RELEASING.md).

Der Grundgedanke: **Was einmal öffentlich war, lässt sich nicht zurückholen.** Deshalb wird
mehrfach geprüft, zuerst lokal, dann in einem privaten Repository, und erst nach der Freigabe
durch den Besitzer wird das Repository öffentlich.

## Überblick

| Schritt | Was | Wer |
|---|---|---|
| 1 | Einfrieren: keine Änderungen mehr, alles eingecheckt | Besitzer |
| 2 | Prüfprogramm über den aktuellen Stand **und** jede Dateiversion der ganzen Historie | Prüfprogramm |
| 3 | Test eines frischen Klons: bauen, Testserver, API-Test, Browser-Test | Besitzer |
| 4 | Repository auf GitHub **privat** anlegen, Einstellungen setzen, pushen, erneut prüfen | Besitzer |
| 5 | Freigabe durch den Besitzer, dann öffentlich schalten, erstes Release | Besitzer |

Dazu kommen zwei Wächter, die danach dauerhaft laufen: eine Prüfung vor jedem Commit auf dem
eigenen Rechner und dieselbe Prüfung in GitHub Actions bei jedem Push und Pull Request.

## Schritt 1: Einfrieren

Ab jetzt ändert niemand mehr etwas, bis die Veröffentlichung durch ist oder abgebrochen wird.

1. Offene Arbeiten einchecken oder verwerfen. Im Projektordner:

   ```powershell
   git status                 # muss "nothing to commit, working tree clean" melden
   git stash list             # muss leer sein
   git branch -a              # nur main
   git tag                    # v0.4.0, v0.5.0, v0.6.0
   ```

2. Den Commit-Wächter einschalten (einmalig je Klon, siehe [Die Wächter](#die-wächter)):

   ```powershell
   .\tools\install-hooks.ps1
   ```

Das Prüfprogramm prüft den letzten Commit (`HEAD`) und die Historie, **nicht** ungespeicherte oder
nicht eingecheckte Änderungen. Darum zuerst einchecken, dann prüfen.

## Schritt 2: Prüfprogramm

`tools/publish_check.py` braucht nur Python 3.10 oder neuer, keine weiteren Pakete.

### Liste der privaten Begriffe füllen

Die Datei `%USERPROFILE%\.datadrive\publish_secrets.txt` liegt **außerhalb** des Repositorys und
kommt nie zu GitHub. Sie ist angelegt, enthält aber noch keine Begriffe. Mit einem Editor öffnen
und je Zeile einen Begriff eintragen, den niemand im Repository finden soll, zum Beispiel:

- der echte Name und das Passwort des eigenen WLANs,
- Passwörter, die am Stick benutzt werden,
- der Windows-Benutzername, die Wohnadresse, Telefonnummern, private E-Mail-Adressen,
- Namen von Familienmitgliedern, die MAC-Adresse des eigenen Sticks, Seriennummern.

Zeilen mit `#` am Anfang sind Kommentare. Groß- und Kleinschreibung spielt keine Rolle, und ein
Begriff wird auch mitten in einem Wort gefunden. Das Prüfprogramm zeigt einen Begriff **nie** an,
nur seine Nummer: „private term #3“ ist der dritte Begriff in der Datei.

### Prüfen

Im Projektordner:

```powershell
python tools/publish_check.py
```

Das dauert einige Sekunden. Geprüft werden:

| Bereich | Was | Stufe |
|---|---|---|
| Identität | Jeder Commit aller Branches und Tags hat als Autor und Committer genau `Willi Liffers <wliffers@users.noreply.github.com>`; keine Mitautor-Zeilen in Commit-Nachrichten | Fehler |
| Werkzeugnamen | Namen von Code-Assistenten, ihren Anbietern und Modellfamilien in jeder Dateiversion, in Dateinamen und in Commit-Nachrichten. Die Liste steht verschleiert (Base64) im Prüfprogramm, damit es sich nicht selbst findet | Fehler |
| Verdächtige Wörter | Wörter, die oft Reste solcher Werkzeuge sind, aber auch harmlos sein können; „User-Agent“ zählt nicht | Warnung |
| Persönliche Daten | E-Mail-Adressen (außer noreply-Adressen und Beispiel-Domains), absolute Benutzerpfade (`C:\Users\<Name>`, `/home/<Name>`, `/Users/<Name>`), die privaten Begriffe | Fehler |
| Geheimnisse | private Schlüssel im PEM-Format, Token-Muster (GitHub, AWS und andere), `pd_secrets.h` irgendwo in der Historie, Dateien `*.pem`, `*.key`, `*.p12`, `*.pfx`; WLAN-Passwörter in Konfigurationsdateien | Fehler, WLAN: Warnung |
| Dateien | Build-Ordner, `managed_components/`, erzeugte `sdkconfig` (die eingecheckten `sdkconfig.defaults`, `.signed`, `.diag` sind in Ordnung), Werkzeug-Ordner, `backup/`, Dateiversionen über 1 MB, Binärdateien außer Bildern und Schriften | Fehler (`backup/SHA256SUMS.txt`: Warnung) |
| Rechtliches | `LICENSE` (MIT mit echtem Namen), `LICENSE-LIO.md`, `THIRD_PARTY_NOTICES.md`, `HAFTUNGSAUSSCHLUSS.md` vorhanden; Lizenzköpfe in `firmware/components/st7735/esp_lcd_st7735.c` und `firmware/components/display/font5x7.c` samt Lizenzdateien | Fehler |
| Platzhalter | `<NAME>`, `<OWNER>`, `<REPO>`, TODO und Ähnliches in README-Dateien | Warnung |
| Links | Jeder relative Link in allen `*.md`-Dateien zeigt auf eine vorhandene Datei, Sprungmarken auf eine vorhandene Überschrift | Fehler |

### Ergebnis lesen

Jede Zeile ist ein Fund:

```text
ERROR   [email] docs/beispiel.md:12 [first in 1a2b3c4d; in current tree (line 14)]: e-mail address j***@g***.com
WARNING [file] backup/SHA256SUMS.txt [first in 982839fd; in current tree]: file in backup/ ...
```

- `ERROR` (Fehler) muss behoben werden. `WARNING` (Warnung) ansehen und entscheiden; mit
  `--strict` zählen auch Warnungen als Fehler.
- `first in …` ist der Commit, mit dem der Fund in die Historie kam.
- `in current tree` heißt: steht noch im aktuellen Stand. `history only, not in current tree`
  heißt: steht nur noch in alten Commits. Auch das wird mit veröffentlicht. Beheben geht dann nur
  durch Neuschreiben der Historie.
- Gefundene Werte werden maskiert (`j***@g***.com`), Geheimnisse und private Begriffe gar nicht
  gezeigt. Die Ausgabe darf also in ein Issue oder ein öffentliches Log.
- Am Ende steht `publish_check: OK` (Rückgabewert 0) oder `publish_check: FAILED` (Rückgabewert 1).

Weiter erst, wenn kein Fehler mehr da ist und jede Warnung bewusst entschieden ist.

Gut zu wissen: Nach dem Neuschreiben der Historie liegen alte Commits noch eine Weile im lokalen
`.git`-Ordner. Sie werden nicht geprüft und auch **nicht gepusht**, weil ein Push nur überträgt,
was von einem Branch oder Tag aus erreichbar ist. Niemals `git push --mirror` benutzen. Ob wirklich
nur das Geprüfte übertragen wird, zeigt Schritt 3.

## Schritt 3: Test eines frischen Klons

Ein Klon enthält genau das, was auch GitHub bekommt. `--no-local` sorgt dafür, dass git so
klont wie über das Netz (nur erreichbare Commits, keine alten Reste). In PowerShell, im
Projektordner beginnen:

```powershell
$t = Join-Path $env:TEMP 'datadrive-klontest'
if (Test-Path $t) { Remove-Item -Recurse -Force $t }
git clone --no-local . $t
Set-Location $t

git log --all --format='%an <%ae> | %cn <%ce>' | Sort-Object -Unique   # nur eine Zeile
python tools/publish_check.py                                          # wie in Schritt 2
.\tools\idf.ps1 printdrive build                                       # unsigniert, mit sdkconfig.defaults
python -m pip install -r requirements-dev.txt
```

Der Build muss ohne `firmware/pd_secrets.h` und ohne Signierschlüssel durchlaufen. Immer nur einen
Build gleichzeitig laufen lassen.

Testserver und Tests, jeweils in einem zweiten PowerShell-Fenster im selben Ordner `$t`:

```powershell
# Fenster 1: Testserver ohne Bremse für den API-Test
python tools/mock_server.py --port 8090 --upload-kbps 0
# Fenster 2
python tools/api_test.py 127.0.0.1:8090 --user admin --password admin123
```

Danach den Testserver in Fenster 1 mit Strg+C beenden und für den Browser-Test neu starten:

```powershell
# Fenster 1
python tools/mock_server.py --port 8090
# Fenster 2 (Edge oder Chrome nötig, dauert eine Weile)
python tools/ui_test.py --base http://localhost:8090
```

Erwartet: Build ohne Fehler, API-Test und Browser-Test ohne Fehlschlag. Ergebnis mit Datum in
[05_testprotokoll.md](05_testprotokoll.md) eintragen. Den Ordner `$t` danach löschen.

## Schritt 4: Privates Repository auf GitHub

### 4a. Konto absichern (einmalig)

Auf github.com rechts oben das Profilbild, dann **Settings**:

1. **Password and authentication → Two-factor authentication** einschalten (App oder
   Sicherheitsschlüssel). Die Wiederherstellungscodes offline aufbewahren.
2. **Emails:** „Keep my email addresses private“ einschalten **und** „Block command line pushes
   that expose my email“ einschalten. Dann lehnt GitHub jeden Push ab, in dem die private Adresse
   als Autor steht.
3. Zugangsdaten für `git push` nur über den Git Credential Manager (bei Git für Windows dabei),
   nie ein Token in eine Datei im Projektordner schreiben.

### 4b. Repository privat anlegen

**New repository:** Besitzer `wliffers`, Name `wifi-datadrive`, Sichtbarkeit **Private**.
**Ohne** README, Lizenz und `.gitignore` von GitHub (liegen schon vor).

### 4c. Einstellungen vor dem ersten Push

Im neuen Repository unter **Settings**:

1. **Advanced Security** (je nach Oberfläche „Code security“): **Secret scanning** und **Push
   protection** einschalten. Push protection weist Pushes mit erkannten Zugangsdaten ab.
2. **Advanced Security:** **Private vulnerability reporting** einschalten (darauf verweist
   `.github/SECURITY.md`).
3. **Actions → General → Workflow permissions:** „Read repository contents and packages
   permissions“ wählen (nur lesen) und „Allow GitHub Actions to create and approve pull requests“
   ausschalten. Die Workflows holen sich die wenigen Schreibrechte selbst, die sie brauchen.
4. **Rules → Rulesets** (oder **Branches → Branch protection rule**) für `main`: Löschen
   verhindern („Restrict deletions“) und Force-Push verhindern („Block force pushes“). Optional
   die Checks „Publish check“ und „Build“ als Pflicht.

Hinweis: Mit einem kostenlosen Konto bietet GitHub manche dieser Schalter nur für **öffentliche**
Repositorys an (vor allem Secret scanning, Push protection und Schutzregeln für Branches). Was hier
fehlt, gleich nach dem Umschalten in Schritt 5 einschalten. Bis dahin schützen das Prüfprogramm
und der Commit-Wächter.

### 4d. Pushen

Im Projektordner:

```powershell
git remote add origin https://github.com/wliffers/wifi-datadrive.git
git push -u origin main
```

Die Tags zunächst **nicht** pushen: Ein Tag `v…` startet den Release-Workflow. Das erste Tag
(`v0.6.0`) kommt in Schritt 5 nach [RELEASING.md](RELEASING.md). Ob die alten Tags `v0.4.0` und
`v0.5.0` überhaupt zu GitHub sollen, entscheidet der Besitzer; der Release-Workflow würde für sie
scheitern, weil die Versionsnummer im Quelltext nicht mehr passt.

### 4e. GitHub Actions grün

Unter **Actions** müssen „Publish check“ und „Build“ grün sein. Bei Rot das Log ansehen, Ursache
lokal beheben, einchecken, erneut pushen.

### 4f. Prüfen gegen einen frischen Klon von GitHub

```powershell
$g = Join-Path $env:TEMP 'datadrive-github'
if (Test-Path $g) { Remove-Item -Recurse -Force $g }
git clone https://github.com/wliffers/wifi-datadrive.git $g
Set-Location $g
git rev-parse HEAD             # muss derselbe Commit sein wie lokal
python tools/publish_check.py
```

Erwartet: dasselbe Ergebnis wie in Schritt 2. Den Ordner danach löschen.

## Schritt 5: Freigabe und öffentlich schalten

1. Der Besitzer liest auf GitHub selbst, wie ein Fremder es sehen würde: [README.md](../README.md),
   [README.en.md](../README.en.md), [LICENSE](../LICENSE), [LICENSE-LIO.md](../LICENSE-LIO.md),
   [HAFTUNGSAUSSCHLUSS.md](../HAFTUNGSAUSSCHLUSS.md), dazu
   [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
2. Die Bilder unter `docs/images/` und `assets/` mit eigenen Augen ansehen: Bildschirmfotos
   können WLAN-Namen, Dateinamen oder Adressen zeigen, und das findet kein Prüfprogramm.
3. Die Logs der Actions-Läufe überfliegen. Sie werden mit dem Repository öffentlich.
4. Erst wenn alles passt: **Settings → General → Danger Zone → Change repository visibility →
   Public**.
5. Sofort die Einstellungen aus 4c nachholen, die im privaten Repository nicht angeboten wurden.
6. Erstes Release (0.6.0) nach [RELEASING.md](RELEASING.md), dazu die Hinweise in
   [18_veroeffentlichung.md](18_veroeffentlichung.md) Abschnitt 4.

## Die Wächter

### Vor jedem Commit (eigener Rechner)

`tools/git-hooks/pre-commit` prüft vor jedem Commit die vorgemerkten Dateien (`git add`) und die
Identität des nächsten Commits; `tools/git-hooks/commit-msg` prüft die Commit-Nachricht. Beide
rufen `python tools/publish_check.py --tree-only` auf (dauert wenige Sekunden). Ein Fehler hält
den Commit an.

Einschalten (einmalig je Klon):

```powershell
.\tools\install-hooks.ps1           # oder: git config core.hooksPath tools/git-hooks
.\tools\install-hooks.ps1 -Check    # zeigt, ob er an ist
```

`git commit --no-verify` umgeht die Wächter. Das bitte nicht tun, sondern den Fund beheben.
Die Wächter brauchen die Datei mit den privaten Begriffen; fehlt sie, halten sie jeden Commit an
(eine leere Datei genügt, dann prüfen sie alles andere).

### In GitHub Actions

`.github/workflows/publish-check.yml` führt bei jedem Push und jedem Pull Request
`python tools/publish_check.py --ci` über den Stand **und** die ganze Historie aus (alle Branches
und Tags), nur mit Leserechten. Die Datei mit den privaten Begriffen gibt es dort nicht. Wer will,
legt die Begriffe als Repository-Secret `PUBLISH_PRIVATE_TERMS` an (**Settings → Secrets and
variables → Actions**, ein Begriff je Zeile). Pull Requests aus Forks bekommen keine Secrets; dort
laufen nur die allgemeinen Prüfungen.

Wichtig: Die Actions-Prüfung läuft **nach** dem Push. Bei einem öffentlichen Repository meldet sie
einen Fund, verhindert ihn aber nicht. Verhindern kann nur die Prüfung auf dem eigenen Rechner.

Hinweis für später: Die Identitätsprüfung erwartet in jedem Commit den Besitzer. Commits, die
GitHub selbst erzeugt (Merge im Browser), sind in `--ci` erlaubt. Werden Beiträge anderer
übernommen, tragen deren Commits deren Namen; dann im Workflow `--allow-identity "Name <Adresse>"`
ergänzen oder die Beiträge selbst neu einchecken.

## Notfallplan

### Fund, solange das Repository privat ist

1. Ruhe bewahren, es ist noch nichts öffentlich.
2. Ursache in den Dateien beheben und einchecken.
3. Steht der Fund auch in alten Commits (`history only` oder `first in` ein alter Commit): die
   Historie neu schreiben (zum Beispiel mit `git filter-repo`), dann Schritt 2 und 3 wiederholen.
4. Das private Repository auf GitHub löschen (**Settings → General → Danger Zone → Delete this
   repository**) und neu anlegen (Schritt 4b, 4c). Ein Force-Push allein reicht nicht: GitHub hebt
   alte Commits noch eine Zeit lang auf.
5. Neu pushen und weiter mit 4e.
6. War es ein echtes Geheimnis (Passwort, Schlüssel), es trotzdem ändern: Es lag auf einem fremden
   Server.

### Fund, nachdem das Repository öffentlich war

1. **Sofort auf privat schalten** (**Settings → General → Danger Zone → Change repository
   visibility → Private**). Das stoppt weitere Zugriffe, holt aber nichts zurück: Kopien,
   Forks und automatische Sammler (die GitHub binnen Minuten nach Zugangsdaten absuchen) behalten
   den Stand.
2. **Das Geheimnis gilt als verbrannt.** Nicht hoffen, dass es niemand gesehen hat, sondern ändern:
   - WLAN-Passwort: am Router ändern und am Stick neu eintragen (Einstellungen › WLAN).
   - Passwörter am Stick (Admin, weitere Benutzer) und das Passwort der Laufwerks-Verschlüsselung
     ändern.
   - Weitere betroffene Zugänge (E-Mail, GitHub-Token) ändern bzw. widerrufen.
3. **Signierschlüssel verraten** (`ota_signing_key.pem`): Wer ihn hat, kann Updates bauen, die
   der Stick über das Web annimmt. Neuen Schlüssel erzeugen und jedes Gerät **per USB** mit einem
   damit signierten Build flashen, dazu sofort die Admin-Passwörter ändern
   ([17_signierte_updates.md Abschnitt 8](17_signierte_updates.md#8-schlüssel-verloren-gewechselt-oder-verraten)).
4. Danach wie oben: Ursache beheben, Historie neu schreiben, Repository löschen und neu anlegen,
   vollständig neu prüfen (Schritt 2 bis 5). Für zwischengespeicherte Ansichten alter Commits
   kann der GitHub-Support um Entfernung gebeten werden.

### Signierschlüssel verloren (keine Sicherung)

Kein Datenleck, aber die Geräte nehmen keine Web-Updates mehr an. Neuen Schlüssel erzeugen und
per USB flashen: `.\tools\flash.ps1 printdrive -Signed -Build` behält Einstellungen und Dateien.
Das Gesamtabbild des Browser-Flashers setzt die Einstellungen dagegen zurück. Einzelheiten in
[17_signierte_updates.md Abschnitt 8](17_signierte_updates.md#8-schlüssel-verloren-gewechselt-oder-verraten).

## Was trotzdem bleibt (Restrisiken)

Das Prüfprogramm senkt das Risiko deutlich, es macht es nicht null:

- **Es findet nur, was es kennt.** Persönliche Angaben, die weder ein Muster noch ein privater
  Begriff sind (ein Ortsname, der Name eines Bekannten, eine Formulierung), rutschen durch. Die
  Liste der privaten Begriffe ist so gut wie ihr Inhalt.
- **Bilder werden nicht angesehen.** In Bildern und anderen Binärdateien sucht es nur nach lesbarem
  Text; was im Bild zu sehen ist, prüft nur das Auge (Schritt 5).
- **Die Werkzeugliste ist endlich.** Neue Namen oder Umschreibungen erkennt sie nicht. Warnungen
  brauchen ein menschliches Urteil.
- **Metadaten werden öffentlich.** Jeder Commit trägt Datum, Uhrzeit und Zeitzone. Daraus lässt
  sich ablesen, wann und in welcher Zeitzone gearbeitet wurde.
- **Die Wächter lassen sich umgehen** (`--no-verify`) und wirken nur in Klonen, in denen sie
  eingeschaltet sind. Die Actions-Prüfung kommt erst nach dem Push.
- **Öffentlich ist endgültig.** Umschalten auf privat, Löschen und Neuschreiben entfernen nichts
  aus fremden Kopien. Deshalb die Reihenfolge privat, prüfen, freigeben, öffentlich.
- **Die Datei mit den privaten Begriffen liegt nur auf dem eigenen Rechner.** Geht sie verloren
  oder fehlt sie auf einem anderen Rechner, ist dieser Teil der Prüfung blind.
