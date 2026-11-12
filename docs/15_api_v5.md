# HTTP-API Version 5 (WiFi DataDrive 0.5.0)

Ergänzung zu [09_api_v3.md](09_api_v3.md) und [12_api_v4.md](12_api_v4.md). Dieses Dokument nennt nur, was Version 5 hinzufügt oder ändert; alles andere gilt unverändert aus Version 3 und 4 (Grundregeln, Anmeldung und Rollen, Pfade, Dateien, Verschlüsselung, Fehlerformat `{"ok":false,"code":"…","error":"…"}`). Hintergrund: [14_vormerkliste.md](14_vormerkliste.md), Punkte V1 bis V7.

Gemeinsam mit Version 3 und 4 gilt: `POST` braucht den Header `X-PrintDrive: 1` (sonst `403 FORBIDDEN`), Abfrageparameter stehen in der URL. Ohne Anmeldung `401 UNAUTHORIZED`, bei zu geringer Rolle `403 FORBIDDEN_ROLE`, beim Auslieferungspasswort `403 MUST_CHANGE_PASSWORD`, bei unlesbarer Benutzerdatenbank `503 AUTH_UNAVAILABLE`. Ausnahme ist nur das Netzlaufwerk unter `/dav/` (Abschnitt 6). Alle Einstellungen dieses Dokuments werden dauerhaft gespeichert und überstehen einen Neustart; den aktuellen Wert liefert immer `GET /api/status`.

## 1. Uhrzeit und Zeitzone

### 1.1 Status

`GET /api/status` enthält zusätzlich `"time":{"epoch":1794512045,"synced":true,"tz":"CET-1CEST,M3.5.0,M10.5.0/3"}`:

- `epoch`: Uhrzeit des Geräts als Unix-Sekunden (UTC); `0`, solange sie unbekannt ist.
- `synced`: `true`, wenn die Uhr in diesem Start per NTP aus dem Internet gestellt wurde. Nur im WLAN-Betrieb (nicht im Einrichtungsmodus).
- `tz`: Zeitzone als POSIX-TZ-Zeichenkette. Sie bestimmt, welche Ortszeit das Gerät auf das Laufwerk (FAT) schreibt und auf dem Display zeigt.

Nach einem Stromverlust ist die Uhr unbekannt (`epoch:0`), bis NTP sie stellt oder ein Browser sie mitteilt (`POST /api/time`).

### 1.2 Endpunkte

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| POST | `/api/settings/time` | admin | `tz` | `{"ok":true,…}`. Voreinstellung `CET-1CEST,M3.5.0,M10.5.0/3` (Deutschland). Fehlt oder ungültig: `400 BAD_REQUEST`. Ein `+` in der Zeichenkette muss als `%2B` kodiert werden (ein rohes `+` ist ein Leerzeichen). |
| POST | `/api/time` | user | `epoch` (Unix-Sekunden, UTC) | `{"ok":true,"set":true}`, wenn die Uhr gestellt wurde; `{"ok":true,"set":false}`, wenn sie bereits per NTP gestellt ist (dann wird nichts geändert). Nicht plausibel (vor dem Erstellungsdatum der Firmware oder ab 2100-01-01 = `4102444800`), keine Zahl oder fehlend: `400 BAD_REQUEST`. |

Ohne NTP stellt jedes `POST /api/time` die Uhr neu; `synced` bleibt dann `false`. Die Oberfläche schickt nach dem Anmelden die Uhrzeit des Browsers, wenn `synced` `false` ist.

### 1.3 Zeitstempel (Änderung gegenüber Version 2 bis 4)

**Ab Version 5 sind alle Zeitstempel echte Unix-Sekunden (UTC).** Bisher galt „Sekunden seit 1970 in Ortszeit des Schreibers, als UTC zu lesen“ ([07_api.md](07_api.md)). Das betrifft:

- `mtime` in `GET /api/files` (und `/api/zip`, WebDAV `getlastmodified`): Das Gerät rechnet die FAT-Ortszeit mit `tz` in UTC um. `0` = unbekannt (Datei wurde geschrieben, als die Uhr unbekannt war, oder trägt ein Datum vor 1981). **Ordner melden weiterhin `0`.**
- Upload-Parameter `mtime` (bei `/api/upload` und `/api/push`): Unix-Sekunden; das Gerät schreibt sie mit `tz` als Ortszeit auf das Laufwerk. Ohne `mtime` bekommt die Datei die Geräteuhr, bei unbekannter Uhr `0`.
- Upload-Parameter `now`: Unix-Sekunden; stellt wie bisher die Uhr nur, solange sie unbekannt ist.

Die Oberfläche zeigt `mtime` mit der Zeitzone des Browsers an (`new Date(mtime * 1000)`) und schickt `mtime = lastModified / 1000` ohne Umrechnung. Skripte, die bisher Ortszeit-Sekunden geschickt haben, liegen um den Zeitzonen-Versatz daneben.

## 2. Automatisches Sperren des verschlüsselten Laufwerks

`GET /api/status` enthält zusätzlich `"autolock":{"minutes":0,"on_detach":false}` (Voreinstellung: beides aus).

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| POST | `/api/settings/autolock` | admin | `minutes=0..1440`, `on_detach=0\|1\|true\|false`; mindestens einer der beiden | `{"ok":true,…}`. Ein fehlender Parameter behält seinen Wert. Keiner der beiden, `minutes` über 1440 oder keine Zahl, `on_detach` mit anderem Wert: `400 BAD_REQUEST`. |

Regeln:

- Wirkt nur auf ein verschlüsseltes, entsperrtes Laufwerk; die Einstellung lässt sich auch ohne Verschlüsselung speichern. `minutes=0` schaltet das Sperren nach Zeit aus.
- **Nach Zeit:** gesperrt wird, wenn `minutes` lang keine Datei-Anfrage kam (Web: Liste, Download, ZIP, Upload, Push, Ordner-, Lösch-, Verschiebe- und Kopieraufträge, Moduswechsel; WebDAV: jede Anfrage außer `OPTIONS`) **und** das USB-Gerät nichts gelesen oder geschrieben hat. Abfragen von Status, Sitzung und `/api/crypt` zählen nicht (die Oberfläche fragt den Status laufend ab). Die Zeit läuft ab dem Ende der letzten Anfrage. Ein untätiges USB-Gerät verliert dabei das Medium.
- **`on_detach`:** gesperrt wird 10 Sekunden, nachdem das USB-Gerät verschwunden ist, oder nach 60 Sekunden im USB-Ruhezustand (Suspend). Bloßes Auswerfen am USB-Gerät zählt nicht.
- **Nie, solange das USB-Gerät schreibt (Schreibschutzzeit `write_guard_ms`) oder ein Vorgang läuft** (Up- oder Download, ZIP, Kopieren, Formatieren); das Sperren folgt, sobald er beendet ist.
- Die Wirkung ist dieselbe wie bei `POST /api/crypt/lock` (Version 4, Abschnitt 2), einschließlich: ein angefangener Upload (Abschnitt 4) wird verworfen. Entsperren startet die Zeit neu.

## 3. Sprache des Displays

`GET /api/status` enthält zusätzlich `"lang":"de"` (Voreinstellung `de`).

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| POST | `/api/settings/lang` | user | `value=de\|en` | `{"ok":true,…}`. Anderer Wert (auch `DE`) oder fehlend: `400 BAD_REQUEST`. |

Die Einstellung gilt für das Display des Geräts (eine für alle). Die Weboberfläche bleibt je Browser einstellbar ([09_api_v3.md](09_api_v3.md), Abschnitt 8) und schickt `POST /api/settings/lang`, wenn der Benutzer dort die Sprache umstellt. Die Fehlertexte im Feld `error` bleiben deutsch.

## 4. Abgebrochene Uploads fortsetzen

### 4.1 Ablauf

1. `GET /api/upload/resume?path=…&size=…` fragt, wie viele Bytes dieser Datei das Gerät schon hat (`0`: von vorn).
2. `POST /api/upload?path=…&size=…&offset=N` schickt den Rest ab Byte `N` als rohen Body (`Content-Length` = `size − N`). Ein ganz neuer Upload, der fortsetzbar sein soll, beginnt mit `offset=0` und der ganzen Datei.
3. Reißt die Verbindung ab, behält das Gerät die bis dahin geschriebenen Bytes; weiter bei Schritt 1.

Ohne `offset` verhält sich `/api/upload` wie bisher (nicht fortsetzbar).

### 4.2 Endpunkte

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| GET | `/api/upload/resume` | user | `path`, `size` | `{"ok":true,"offset":262144}`. `0`, wenn für genau diesen `path` und genau diese `size` nichts vorliegt, auch wenn das Laufwerk gerade dem USB-Gerät gehört oder gesperrt ist. `path` oder `size` fehlt: `400 BAD_REQUEST`. |
| POST | `/api/upload` | user | wie bisher (`path`, `overwrite`, `mtime`, `now`), dazu `offset` und `size` | Wie bisher. Zusätzlich: `size` fehlt oder `Content-Length` ≠ `size − offset`: `400 BAD_REQUEST`. `offset` ≠ Stand des Geräts für diese Datei (auch `offset=0`, solange ein Teil dieser Datei vorliegt, und `offset` > `size`): `409 RESUME_MISMATCH` mit dem Stand des Geräts: `{"ok":false,"code":"RESUME_MISMATCH","error":"…","offset":262144}`. `offset=0`, wenn für diese Datei nichts vorliegt, beginnt neu. |

### 4.3 Regeln

- Es gibt höchstens **einen** angefangenen Upload. Sein Stand liegt nur im Arbeitsspeicher, die Daten in der temporären Datei im Hauptverzeichnis.
- **Verworfen wird er durch:** jeden neu beginnenden Upload (auch zum selben Ziel, ohne `offset`, per `/api/push` oder WebDAV-`PUT`), die Übergabe an das USB-Gerät, Sperren (auch automatisch), Formatieren, Einrichten oder Ausschalten der Verschlüsselung und jeden Neustart. **Nicht** durch `/api/upload/check` oder die Abfrage `/api/upload/resume`. Abgewiesene Fortsetzungen (`400`, `409`) lassen ihn unverändert.
- Er entsteht nur, wenn mindestens ein Teil geschrieben wurde und kein Schreibfehler auftrat. Das Gerät behält nicht unbedingt jedes empfangene Byte (es schreibt blockweise); maßgeblich ist immer das gemeldete `offset`.
- Die Prüfungen von `/api/upload/check` (Name, Typ, Zielordner, `EXISTS` ohne `overwrite`, Platz für die ganze `size`) laufen zu Beginn; Zielprüfung und 3MF-Prüfung beim Abschluss. Ausgewertet werden `overwrite` und `mtime` der Anfrage, die die Datei vollendet; jede Teilanfrage sollte dieselben Parameter tragen. Eine abgeschnittene 3MF-Datei bekommt auch nach dem Fortsetzen `422 INVALID_FILE`.
- Der Vergleich von `path` ist byte-genau (dieselbe Schreibweise wie beim ersten Teil verwenden).
- `/api/push` und WebDAV setzen nicht fort.
- `GET /api/status` meldet im Objekt `upload` zusätzlich `"resumable":{"path":"Projekte/Groß.3mf","size":524540,"offset":262144}` oder `"resumable":null`.

## 5. Ordner als ZIP herunterladen

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| GET | `/api/zip` | user | `path` (Ordner; leer = ganzes Laufwerk) | ZIP-Datei, siehe unten. Fehlt der Ordner oder ist `path` eine Datei: `404 NOT_FOUND`. Ungültiger Pfad: `400 BAD_NAME`. Mehr als 4096 Dateien im Ordnerbaum: `413 TOO_MANY_FILES`. Wie ein Download abgewiesen: `409 WRONG_OWNER` (Laufwerk beim USB-Gerät), `409 BUSY`, `423 DRIVE_LOCKED` / `423 CRYPT_DAMAGED`. Alle Fehler kommen, bevor Daten gesendet werden. |

- `Content-Type: application/zip`, `Transfer-Encoding: chunked` (keine Länge vorab), `Content-Disposition: attachment; filename="…"; filename*=UTF-8''…`. Dateiname `<Ordnername>.zip`, für das ganze Laufwerk `DataDrive.zip`; in `filename` sind Nicht-ASCII-Zeichen ersetzt, `filename*` trägt den echten Namen.
- Inhalt: alle Dateien und Unterordner, rekursiv. Jeder Eintrag beginnt mit dem Ordnernamen (`Projekte/Drachen/Kopf.3mf`), beim ganzen Laufwerk ohne Vorsilbe. Ordner sind eigene Einträge mit `/` am Ende, auch leere. Versteckte Namen (führender Punkt, `System Volume Information`) fehlen wie in der Liste.
- Format: Methode „stored“ (unkomprimiert, 3MF ist schon komprimiert), Namen in UTF-8 mit gesetztem Bit 11, CRC-32 und Größen in Daten-Deskriptoren (Bit 3) hinter jeder Datei, Datum aus dem FAT-Eintrag. ZIP64-Felder nur dort, wo die klassischen nicht reichen (Datei von 4 GiB − 1, Archiv über 4 GiB).
- Solange ein ZIP oder ein Download läuft, antworten `/api/mkdir` und Besitzerwechsel mit `409 BUSY`.

## 6. Netzlaufwerk (WebDAV)

Der Stick erscheint unter `http://<adresse>/dav/` als Netzlaufwerk; `/dav/` entspricht dem Hauptverzeichnis, `/dav/Projekte/Teil.3mf` dem Pfad `Projekte/Teil.3mf`. Pfade sind UTF-8, prozentkodiert; Ordner werden mit und ohne `/` am Ende gefunden.

### 6.1 Anmeldung und Regeln

- **Nur HTTP Basic** bei jeder Anfrage, auch bei `OPTIONS`. Ein Sitzungs-Cookie wird hier nicht angenommen, ein `X-PrintDrive`-Header nicht verlangt. Ohne oder mit falscher Anmeldung: `401` mit `WWW-Authenticate: Basic realm="DataDrive", charset="UTF-8"`. Anmeldesperre (`429 LOCKED` mit `Retry-After`), `403 MUST_CHANGE_PASSWORD` und `503 AUTH_UNAVAILABLE` wie bei der API.
- **Rollen und Laufwerksregeln wie bei der Datei-API:** Rolle `user` darf alles unter `/dav/`. Gehört das Laufwerk dem USB-Gerät: `503` mit `Retry-After: 30` und einem kurzen Text (so zeigen WebDAV-Programme „vorübergehend nicht verfügbar“); läuft ein Up- oder Download oder fehlt das Dateisystem: `503` mit `Retry-After: 10`; gesperrt: `423` (`DRIVE_LOCKED` / `CRYPT_DAMAGED`). Namensregeln, erlaubte Dateitypen (`415`), 3MF-Prüfung (`422`) und Platzprüfung (`507`) wie beim Upload. Es gibt keine automatische Übernahme des Laufwerks.
- Fehlerantworten tragen denselben JSON-Körper wie die API (für die Fehlersuche; WebDAV-Programme werten nur den Status aus).
- Versteckte Namen (führender Punkt in irgendeinem Abschnitt, `System Volume Information`) und die Hilfsdateien von Windows und macOS (`._*`, `.DS_Store`, `desktop.ini`, `Thumbs.db`, `ehthumbs.db`) erscheinen nie: Lesen antwortet `404`, Schreiben (`PUT`, `MKCOL`, `LOCK`, Ziel von `MOVE`/`COPY`) `403`.

### 6.2 Methoden

| Methode | Wirkung | Antworten |
|---|---|---|
| `OPTIONS` | Fähigkeiten; braucht das Laufwerk nicht | `200`, `DAV: 1, 2`, `Allow: …`, `MS-Author-Via: DAV` |
| `PROPFIND` | Eigenschaften, `Depth: 0` oder `1`; der Anfragekörper wird ignoriert, es kommen immer alle | `207` Multi-Status (`application/xml`): `displayname`, `resourcetype`, `getcontentlength`, `getlastmodified`, `creationdate`, `getcontenttype`, `getetag`, `supportedlock`; beim Hauptverzeichnis zusätzlich `quota-available-bytes`, `quota-used-bytes`. `Depth: infinity` oder fehlender `Depth`-Header (gilt laut RFC 4918 als infinity): `403` mit `propfind-finite-depth`. Pfad fehlt: `404` |
| `GET`, `HEAD` | Datei lesen | `200` mit `Last-Modified`, `ETag`; Ordner: `405`; fehlt: `404` |
| `PUT` | Datei anlegen oder ersetzen (immer überschreibend) | `201` neu, `204` ersetzt. `Content-Length` Pflicht (chunked: `411`). Zielordner fehlt: `409`; Ziel ist ein Ordner: `405`. **Leere Dateien sind erlaubt** (Windows legt Dateien erst leer an); die 3MF-Prüfung gilt nur für nicht leere Dateien |
| `DELETE` | Datei oder Ordner samt Inhalt löschen | `204`; Hauptverzeichnis: `403`; fehlt: `404` |
| `MKCOL` | Ordner anlegen | `201`; existiert: `405`; übergeordneter fehlt: `409`; mit Körper: `415` |
| `MOVE`, `COPY` | Header `Destination` (volle URL oder Pfad unter `/dav/`), `Overwrite: T\|F` (Standard `T`); Ordner immer mit Inhalt | `201` neu, `204` ersetzt; Ziel existiert und `Overwrite: F`: `412`; Zielordner fehlt: `409`; Quelle = Ziel, Ordner in sich selbst oder Ziel enthält die Quelle: `403`; `507`, `415` wie oben |
| `LOCK`, `UNLOCK` | Werden beantwortet, damit Windows und macOS schreiben, aber **nicht durchgesetzt** | `LOCK`: `200` mit `lockdiscovery` und Header `Lock-Token`; auf einen neuen Namen `201` (Platzhalter bis zum `PUT`; Zielordner fehlt: `409`); ohne Körper: Verlängerung mit dem Token aus `If`. `UNLOCK`: `204` |
| `PROPPATCH` | Wird angenommen und ignoriert (Windows setzt Zeitstempel) | `207`, jede genannte Eigenschaft mit `200` |

### 6.3 Schalter (falls vorhanden)

`GET /api/status` enthält `"webdav":true`, und `POST /api/settings/webdav?value=0|1` (Rolle `admin`; anderer Wert: `400 BAD_REQUEST`) schaltet das Netzlaufwerk ein oder aus. Ausgeschaltet antwortet `OPTIONS` ohne DAV-Kopfzeilen und jede andere Anfrage unter `/dav/` mit `403`. Voreinstellung: an. (Der Testserver antwortet ausgeschaltet mit `404`.)

### 6.4 Hinweise zu den Programmen

- **Windows** („Netzlaufwerk verbinden“, Adresse `http://<adresse>/dav/`): Der Windows-WebDAV-Client schickt Basic-Anmeldungen über unverschlüsseltes HTTP nur, wenn in der Registrierung `HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters\BasicAuthLevel` auf `2` steht (danach den Dienst „WebClient“ neu starten). Er begrenzt außerdem Dateien auf etwa 50 MB (`FileSizeLimitInBytes`, gleicher Schlüssel).
- **macOS** Finder: „Mit Server verbinden“, `http://<adresse>/dav/`. Der Finder schickt manche Uploads ohne `Content-Length` (chunked); solche `PUT` lehnt der Stick mit `411` ab.
- **iPhone/iPad:** Die App „Dateien“ verbindet sich nach heutigem Stand nur mit SMB-Servern; für WebDAV ist eine Zusatz-App nötig.
- Passwörter gehen ohne HTTPS unverschlüsselt durchs WLAN, wie bei HTTP Basic für Skripte.

## 7. Einrichtungsseite öffnet sich von selbst

Dafür gibt es keine Schnittstelle: Solange der Einrichtungs-Zugangspunkt aktiv ist, leitet das Gerät Anfragen, die an der Adresse des Zugangspunkts mit einem fremden Hostnamen ankommen (auch die Prüfadressen der Betriebssysteme), mit `302` auf `http://192.168.4.1/?captive=1` um, sodass Handy und PC die Einrichtungsseite von selbst öffnen. Unbekannte Pfade unter dem eigenen Namen des Sticks antworten weiterhin mit `404`.

## 8. HTTPS

Unverändert wie in Version 4, Abschnitt 3 beschrieben. Ab 0.5.0 ist HTTPS im Build enthalten (Build-Schalter `PD_HTTPS_OPTION`, Voreinstellung an); der Schalter in den Einstellungen steht auf aus, bis ihn ein Administrator einschaltet. In 0.4.0 und in Builds ohne die Option fehlen in `GET /api/status` die Felder `https`, `https_active`, `https_restart`, `cert_sha256`, `ca_sha256` und `cert_expires`, und `POST /api/settings/https` sowie `GET /api/https/ca.cer` gibt es nicht. Oberfläche und Skripte müssen das Fehlen der Felder vertragen (dann kein HTTPS-Bereich). Der Testserver meldet die Felder weiterhin wie in Version 4.

## 9. Ganze Ordner hochladen

Keine neue Schnittstelle: Die Oberfläche legt die Ordner mit `POST /api/mkdir` an (Eltern zuerst) und lädt die Dateien einzeln mit `/api/upload` hoch. `POST /api/mkdir` auf einen bereits vorhandenen Ordner antwortet `409 EXISTS`, ebenso, wenn eine Datei dieses Namens existiert; die Oberfläche prüft dann per `GET /api/files`, ob dort ein Ordner (`type:"dir"`) liegt, und lädt in diesen weiter.

## 10. Ergänzung zum gesperrten Zustand

Zur Liste in Version 4, Abschnitt 2.3 kommt `GET /api/zip` hinzu (`423 DRIVE_LOCKED` / `423 CRYPT_DAMAGED`), ebenso jede WebDAV-Anfrage außer `OPTIONS`. `GET /api/upload/resume` antwortet auch im gesperrten Zustand mit `offset:0`. Die neuen Einstellungen (`/api/settings/time`, `/api/time`, `/api/settings/autolock`, `/api/settings/lang`, `/api/settings/webdav`) bleiben erreichbar.

## 11. Neue Felder in `GET /api/status` (Überblick)

```json
{
  "time": {"epoch": 1794512045, "synced": true, "tz": "CET-1CEST,M3.5.0,M10.5.0/3"},
  "autolock": {"minutes": 0, "on_detach": false},
  "lang": "de",
  "webdav": true,
  "upload": {"active": false, "name": "", "received": 0, "total": 0, "resumable": null}
}
```

`webdav` nur, falls die Firmware den Schalter hat (6.3). Alle übrigen Felder wie in Version 3 und 4.

## 12. Neue Fehlercodes

| HTTP | code | Bedeutung |
|---|---|---|
| 409 | `RESUME_MISMATCH` | `offset` passt nicht zum Stand des Geräts; das Feld `offset` der Antwort nennt ihn |
| 413 | `TOO_MANY_FILES` | Ordner enthält zu viele Dateien für ein ZIP (mehr als 4096) |

Unverändert und hier ebenfalls verwendet: `400 BAD_REQUEST`, `400 BAD_NAME`, `404 NOT_FOUND`, `409 BUSY`, `409 WRONG_OWNER`, `409 EXISTS`, `422 INVALID_FILE`, `423 DRIVE_LOCKED`, `423 CRYPT_DAMAGED`.

## 13. Testserver und Test

- `tools/mock_server.py` bildet Version 5 nach. Zusätzliche Schalter unter `/mock/set`: `clock=synced|unsynced|manual|<unix s>` (Uhr), `resumable=PFAD|GRÖSSE|OFFSET` (angefangener Upload), `upload_drop_after=<bytes>` (nächste Upload-Verbindung bricht nach so vielen Bytes ab), `idle_s=<n>` (letzte Aktivität, Abziehen und Suspend liegen n Sekunden zurück; löst das automatische Sperren aus). Das Szenario `ap_setup` startet mit unbekannter Uhr.
- `tools/api_test.py` prüft Version 5 in den Abschnitten 11 bis 16; nur diese: `python tools/api_test.py HOST --password PW --only v5` (einzeln: `--only time,autolock,lang,resume,zip,webdav`). Abschnitt 16 wird übersprungen, wenn `/dav/` mit `404` antwortet oder `webdav:false` gemeldet wird.
