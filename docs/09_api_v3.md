# HTTP-API Version 3 (WiFi DataDrive 0.3.0)

Verbindlicher Vertrag für Firmware, Weboberfläche, Testserver, Tests und Werkzeuge. Er ersetzt [07_api.md](07_api.md) (Version 2), sobald 0.3.0 fertig ist. Was hier nicht erwähnt ist, gilt unverändert aus Version 2 (Status-Felder, Zeitstempel in Ortszeit, Ablauf eines Uploads).

## 1. Grundregeln

- Port 80, nur HTTP, eine Anfrage nach der anderen.
- Ändernde Anfragen sind `POST` und brauchen den Header `X-PrintDrive: 1` (sonst `403 FORBIDDEN`).
- Antworten sind JSON; Fehler immer `{"ok":false,"code":"…","error":"deutscher Klartext"}`, Erfolg mindestens `{"ok":true}`.
- **Anmeldung ist Pflicht** für alles außer den unter 2.1 genannten Ausnahmen.

## 2. Anmeldung und Rollen

### 2.1 Ohne Anmeldung erreichbar

`GET /`, `GET /index.html`, `GET /manifest.webmanifest`, `GET /icon-180.png`, `GET /icon-192.png`, `GET /icon-512.png`, `GET /api/session`, `POST /api/login`.

Alles andere antwortet ohne gültige Anmeldung mit `401` und `code: "UNAUTHORIZED"`.

**Benutzerdatenbank nicht lesbar:** Ist die gespeicherte Benutzerdatenbank vorhanden, aber nicht lesbar oder beschädigt, verweigert das Gerät sicherheitshalber jede Anmeldung: `GET /api/session`, `POST /api/login` und jede geschützte Anfrage antworten mit `503` und `code: "AUTH_UNAVAILABLE"` („Die Benutzerverwaltung ist nicht lesbar. Bitte die Taste am Gerät 10 Sekunden halten, um die Anmeldung zurückzusetzen.“). Abhilfe nur der Benutzer-Reset per Taste (Abschnitt 9). Eine fehlende Datenbank (erster Start) ist dagegen kein Fehler: es entsteht `admin` / `admin`.

### 2.2 Zwei Wege der Anmeldung

1. **Sitzung (Browser):** `POST /api/login` setzt das Cookie `dd_session=<64 Hex-Zeichen>; Path=/; HttpOnly; SameSite=Strict`. Sitzungen liegen im Arbeitsspeicher (höchstens 8, die älteste wird verdrängt), laufen nach 12 Stunden ohne Anfrage ab und enden mit einem Neustart.
2. **HTTP Basic (Skripte, Kurzbefehle):** Header `Authorization: Basic base64(benutzer:passwort)` bei jeder Anfrage. Es wird kein Cookie gesetzt.

Fehlversuche (Anmeldesperre, je Adresse des Anfragenden):

- Nach 5 falschen Passwörtern in Folge wird jede Passwortprüfung von dieser Adresse mit `429` und `code: "LOCKED"` abgewiesen, zuerst 30 Sekunden lang. Jede weitere Sperre derselben Adresse dauert doppelt so lang (30, 60, 120, 240, 480 Sekunden, dann höchstens 900 Sekunden = 15 Minuten). Ein bestätigtes Passwort von dieser Adresse setzt Zähler und Dauer zurück.
- Die Antwort trägt den Header `Retry-After: <Sekunden>` (aufgerundet, mindestens 1) bis zum frühesten neuen Versuch. Während der Sperre wird gar nicht geprüft, auch ein richtiges Passwort nicht. Sie gilt für `POST /api/login`, für HTTP Basic (auch bei gespeichertem Prüfwert) und für das alte Passwort in `POST /api/users/password`; Anfragen mit gültigem Cookie sind nicht betroffen.
- Die Sperre betrifft nur die Adresse, von der die Fehlversuche kamen (höchstens 16 Adressen werden verfolgt, die am längsten unbenutzte wird ersetzt). Dazu gibt es eine Gesamtgrenze gegen verteiltes Raten: Mehr als 30 Fehlversuche innerhalb von 60 Sekunden von allen Adressen zusammen sperren die Passwortprüfung für Adressen ohne bestätigtes Passwort in den letzten 12 Stunden für 30 Sekunden (`429 LOCKED` mit `Retry-After`).
- Text: „Zu viele Fehlversuche. Bitte etwas warten und dann erneut versuchen.“ Die Dauer steht nur im Header, nicht im Text.
- Zähler und Sperren liegen im Arbeitsspeicher und enden mit einem Neustart.

### 2.2a Vertraute Geräte

- `POST /api/login` nimmt zusätzlich `remember=1` und `device=<Bezeichnung>` (höchstens 32 Bytes, z. B. „iPhone“, „Windows-PC“; von der Oberfläche aus dem Browser abgeleitet). Dann wird die Sitzung als **vertrautes Gerät** dauerhaft gespeichert: Das Cookie bekommt `Max-Age=31536000`, und im Einstellungsspeicher liegt der SHA-256-Wert des Sitzungsschlüssels samt Benutzer und Bezeichnung (nie der Schlüssel selbst). Vertraute Geräte überstehen einen Neustart und haben keinen Zeitablauf (das Gerät hat keine Uhr).
- Höchstens 8 vertraute Geräte insgesamt und **höchstens 4 je Benutzer**. Meldet sich derselbe Benutzer mit derselben Bezeichnung (`device`) erneut an, ersetzt der neue Eintrag den alten (alter Schlüssel ungültig). Hat der Benutzer schon 4 andere, wird sein ältester ersetzt. Geräte anderer Benutzer werden nie verdrängt; ist das Gerät mit 8 Einträgen voll und der Benutzer hat keinen eigenen zum Ersetzen, bekommt die Anmeldung nur eine flüchtige Sitzung (Cookie ohne `Max-Age`, Antwort sonst wie gewohnt). Dasselbe gilt, wenn zu oft gespeichert wird (Schonung des Flash-Speichers: mindestens 10 Sekunden Abstand, höchstens 20 neue Einträge pro Stunde) oder das Speichern fehlschlägt.
- Die Bezeichnung wird bereinigt (nur druckbare Zeichen und vollständiges UTF-8, Leerzeichen am Rand entfernt, gekürzt auf 32 Bytes); ist sie leer, lautet sie „Unbekanntes Gerät“.
- Ohne `remember` gilt die flüchtige Sitzung aus 2.2 (12 Stunden, endet mit dem Neustart).
- Vertraute Geräte eines Benutzers werden entfernt, wenn sein Passwort geändert, seine Rolle geändert oder er gelöscht wird, ebenso alle beim Benutzer-Reset per Taste. Ausnahme: Ändert ein Benutzer sein eigenes Passwort, bleibt genau die Sitzung bzw. das Gerät gültig, von dem die Änderung kam. `POST /api/logout` entfernt das eigene.
- `GET /api/devices` (Rolle `user`): `{"ok":true,"devices":[{"id":"3f9a1c2e","user":"admin","label":"iPhone","current":true}],"max":8}`. `user` sieht nur die eigenen, `admin` alle. `id` sind die ersten 8 Hex-Zeichen des gespeicherten Hash-Werts.
- `POST /api/devices/delete?id=…` (Rolle `user`): eigenes Gerät, als `admin` jedes. `404 NOT_FOUND`, `403 FORBIDDEN_ROLE` bei fremdem Gerät.
- Der erzwungene Passwortwechsel (2.4) gilt auch für vertraute Geräte; `/api/devices` gehört nicht zu den dort erlaubten Anfragen.

### 2.3 Rollen

| Rolle | Darf |
|---|---|
| `user` | Status lesen, Dateien und Ordner verwalten, Betriebsart wechseln, `push`, eigenes Passwort ändern, abmelden |
| `admin` | alles von `user`, dazu WLAN, USB-Schreibrecht, Benutzerverwaltung, Formatieren, Neustart, Protokoll, Update |

Reicht die Rolle nicht: `403` mit `code: "FORBIDDEN_ROLE"`.

### 2.4 Benutzer

- Höchstens 8 Benutzer. Name: 1 bis 24 Zeichen aus `a–z A–Z 0–9 . _ -`, Vergleich ohne Groß-/Kleinschreibung. Passwort: 5 bis 64 Bytes.
- Gespeichert wird je Benutzer: Name, Rolle, 16 Byte Salz, PBKDF2-HMAC-SHA256 (2000 Runden, 32 Byte). Nie das Klartext-Passwort.
- Gibt es keinen Benutzer (erster Start, Reset per Taste), wird `admin` / `admin` mit Rolle `admin` angelegt. Solange dieses Passwort gilt, meldet die Sitzung `default_password: true`.
- Der letzte Administrator kann weder gelöscht noch zur Rolle `user` herabgestuft werden (`409 LAST_ADMIN`).
- **Erzwungener Passwortwechsel:** Solange ein Konto das Auslieferungspasswort hat (`default_password: true`, also `admin` mit Passwort `admin`), sind für dieses Konto nur `GET /api/session`, `POST /api/login`, `POST /api/logout` und `POST /api/users/password` (eigenes Konto) erlaubt. Jede andere geschützte Anfrage antwortet mit `403` und `code: "MUST_CHANGE_PASSWORD"`, auch bei Anmeldung per HTTP Basic. Das neue Passwort darf nicht `admin` sein (`400 BAD_PASSWORD`).

### 2.5 Endpunkte

| Methode | Pfad | Rolle | Parameter | Antwort / Fehler |
|---|---|---|---|---|
| GET | `/api/session` | – | – | angemeldet: `{"ok":true,"user":"admin","role":"admin","default_password":true}`; sonst `401 UNAUTHORIZED` |
| POST | `/api/login` | – | Body `user=…&pass=…` (URL-kodiert) | wie `/api/session` plus Cookie; `401 BAD_LOGIN`, `429 LOCKED` (mit `Retry-After`), `503 AUTH_UNAVAILABLE` |
| POST | `/api/logout` | user | – | `{"ok":true}`, Cookie wird gelöscht |
| GET | `/api/users` | admin | – | `{"ok":true,"users":[{"name":"admin","role":"admin"}],"max":8}` |
| POST | `/api/users/add` | admin | Body `user`, `pass`, `role` | `409 EXISTS`, `409 USER_LIMIT`, `400 BAD_USER`, `400 BAD_PASSWORD` |
| POST | `/api/users/delete` | admin | Query `user` | `404 NOT_FOUND`, `409 LAST_ADMIN`; Sitzungen dieses Benutzers enden |
| POST | `/api/users/role` | admin | Query `user`, `role=admin\|user` | `404 NOT_FOUND`, `409 LAST_ADMIN` |
| POST | `/api/users/password` | user | Body `user`, `pass`, bei eigenem Konto zusätzlich `old` | Eigenes Konto: `old` muss stimmen (`401 BAD_LOGIN`). Fremdes Konto nur als `admin` (sonst `403 FORBIDDEN_ROLE`). `400 BAD_PASSWORD`. Andere Sitzungen dieses Benutzers enden. |

## 3. Dateien und Ordner

### 3.1 Pfade

- Ein Pfad ist relativ zum Hauptverzeichnis, mit `/` getrennt, ohne führenden oder abschließenden `/`. Der leere Pfad ist das Hauptverzeichnis.
- Jeder Abschnitt gehorcht den bisherigen Namensregeln: 1 bis 120 Bytes UTF-8, nicht mit Punkt oder Leerzeichen beginnend, nicht mit Punkt oder Leerzeichen endend, keines der Zeichen `/ \ : * ? " < > |`, keine Steuerzeichen. Damit sind `.` und `..` ausgeschlossen.
- Höchstens 8 Ebenen und 240 Bytes Gesamtlänge. Verstöße: `400 BAD_NAME`.
- Bereits vorhandene Namen (etwa vom USB-Gerät angelegt) dürfen bis 255 Bytes lang sein und werden beim Lesen, Löschen und als Quelle akzeptiert, solange sie keinen `/`, `\` und keinen führenden Punkt enthalten.
- Alle Dateioperationen verlangen, dass die Dateiverwaltung das Laufwerk besitzt (`409 WRONG_OWNER`), und sind bei laufendem Up- oder Download gesperrt (`409 BUSY`).

### 3.2 Endpunkte (alle Rolle `user`)

| Methode | Pfad | Parameter | Wirkung und Fehler |
|---|---|---|---|
| GET | `/api/files` | `dir` (Ordnerpfad, leer = Hauptverzeichnis) | `{"ok":true,"dir":"Projekte","entries":[{"name":"Alt","type":"dir","size":0,"mtime":0},{"name":"Würfel.3mf","type":"file","size":524288,"mtime":1794430000}],"truncated":false}`. Ordner zuerst ist Sache der Oberfläche. Namen mit führendem Punkt und `System Volume Information` fehlen. `404 NOT_FOUND`, wenn `dir` nicht existiert. |
| GET | `/api/download` | `path` | Dateiinhalt. `404 NOT_FOUND`, `400 IS_DIR` |
| POST | `/api/mkdir` | `path` | Legt genau einen Ordner an (der übergeordnete muss existieren, sonst `404 NOT_FOUND`). `409 EXISTS` |
| POST | `/api/delete` | `path`, `recursive=0\|1` | Datei oder Ordner löschen. Nicht leerer Ordner ohne `recursive=1`: `409 DIR_NOT_EMPTY`. `404 NOT_FOUND` |
| POST | `/api/move` | `from`, `to` (vollständiger Zielpfad einschließlich Name) | Umbenennen und/oder verschieben, für Dateien und Ordner. `409 EXISTS` (nur Groß-/Kleinschreibung ändern ist erlaubt), `404 NOT_FOUND` (Quelle oder Zielordner fehlt), `400 BAD_NAME` (auch: Ordner in sich selbst) |
| POST | `/api/copy` | `from`, `to` | Datei oder Ordner (rekursiv) kopieren. `409 EXISTS`, `404 NOT_FOUND`, `507 NO_SPACE` (vorab geprüft: Summe der Dateigrößen + 64 KiB), `400 BAD_NAME` (Ordner in sich selbst). Bei einem Fehler mittendrin wird das unvollständige Ziel wieder entfernt. |
| POST | `/api/upload/check` | `path` (Zielpfad mit Dateiname), `size`, `overwrite=0\|1` | wie Version 2; zusätzlich `404 NOT_FOUND`, wenn der Zielordner fehlt, `400 IS_DIR`, wenn das Ziel ein Ordner ist |
| POST | `/api/upload` | `path`, `overwrite`, `mtime`, `now` | wie Version 2, Inhalt als roher Body |
| POST | `/api/push` | `path`, `overwrite=0\|1` (Standard 1), `force=0\|1` (Standard 0), `mtime`, `now` | Ein-Schritt-Upload für Skripte, Inhalt als roher Body. Gehört das Laufwerk dem USB-Gerät, wird es zuerst übernommen (Regeln wie `/api/mode?target=app`; `force` nur für `HOST_ACTIVE`, nie für `HOST_WRITING`), nach dem Speichern wieder bereitgestellt. Antwort: `{"ok":true,"code":"OK","error":"","took_over":true,"provided":true}`. Schlägt das Speichern fehl, wird trotzdem wieder bereitgestellt, wenn zuvor übernommen wurde. |

`/api/rename` aus Version 2 entfällt (durch `/api/move` ersetzt). Der Parameter `name` der Version 2 entfällt zugunsten von `path`.

Die temporären Dateien `.pd_upload.tmp` und `.pd_old.tmp` bleiben im Hauptverzeichnis.

## 4. WLAN (Rolle `admin`)

| Methode | Pfad | Parameter | Wirkung |
|---|---|---|---|
| GET | `/api/wifi/list` | – | `{"ok":true,"networks":[{"ssid":"Zuhause","active":true},{"ssid":"Werkstatt","active":false}],"max":5}`; Passwörter werden nie ausgegeben |
| POST | `/api/wifi/add` | Body `ssid=…&pass=…` | Netz speichern oder Passwort eines gespeicherten ersetzen, danach neu verbinden. `409 WIFI_LIMIT` bei 5 Netzen. `400 BAD_REQUEST` bei ungültiger Länge (SSID 1–32, Passwort leer oder 8–63) |
| POST | `/api/wifi/delete` | Query `ssid` | `404 NOT_FOUND` |
| POST | `/api/wifi` | Body `ssid=…&pass=…` | Alias für `/api/wifi/add` (Version 2) |

Verhalten: Beim Start und nach Verbindungsverlust sucht das Gerät und verbindet sich mit dem gespeicherten Netz mit dem stärksten Signal; schlägt das fehl, kommt das nächste an die Reihe. Ohne Erfolg startet wie bisher das Einrichtungs-WLAN. Die Zugangsdaten aus Version 2 werden beim ersten Start übernommen.

## 5. System

| Methode | Pfad | Rolle | Parameter | Wirkung |
|---|---|---|---|---|
| GET | `/api/status` | user | – | wie Version 2, zusätzlich `"ota":{"partition":"ota_0","pending_verify":false}` |
| POST | `/api/mode` | user | wie Version 2 | – |
| POST | `/api/settings/host_writable` | admin | wie Version 2 | – |
| POST | `/api/format` | admin | wie Version 2 | – |
| POST | `/api/reboot` | admin | wie Version 2 | – |
| GET | `/api/log` | admin | – | – |
| POST | `/api/ota` | admin | roher Body = Firmware-Abbild (`printdrive.bin`), `Content-Length` Pflicht | Schreibt in die jeweils andere Firmware-Partition, prüft das Abbild, stellt den Start darauf um und startet neu. Antwort vor dem Neustart: `{"ok":true,"restart":true}`. `409 BUSY` (Datei-Upload läuft), `409 HOST_WRITING`, `422 INVALID_FILE` (kein gültiges Abbild für diesen Chip), `413 TOO_LARGE`, `500 IO`. |

Rückkehr zur alten Version: Die neue Firmware gilt erst als gut, wenn sie 15 Sekunden gelaufen ist und der Webserver gestartet wurde. Startet sie nicht oder stürzt vorher ab, bootet das Gerät beim nächsten Start wieder die vorherige.

## 6. Statische Dateien

`GET /manifest.webmanifest` (`application/manifest+json`), `GET /icon-180.png`, `/icon-192.png`, `/icon-512.png` (`image/png`, `Cache-Control: max-age=86400`).

## 7. Neue Fehlercodes

| HTTP | code | Bedeutung |
|---|---|---|
| 400 | `BAD_USER` | ungültiger Benutzername |
| 400 | `BAD_PASSWORD` | Passwort zu kurz oder zu lang |
| 400 | `IS_DIR` | Ziel ist ein Ordner, erwartet wurde eine Datei |
| 401 | `UNAUTHORIZED` | nicht angemeldet |
| 401 | `BAD_LOGIN` | Benutzername oder Passwort falsch |
| 403 | `FORBIDDEN_ROLE` | Rolle reicht nicht |
| 403 | `MUST_CHANGE_PASSWORD` | Auslieferungspasswort muss zuerst geändert werden |
| 409 | `DIR_NOT_EMPTY` | Ordner ist nicht leer |
| 409 | `LAST_ADMIN` | letzter Administrator |
| 409 | `USER_LIMIT` | 8 Benutzer vorhanden |
| 409 | `WIFI_LIMIT` | 5 WLANs gespeichert |
| 429 | `LOCKED` | zu viele Fehlversuche; gesperrt, Dauer im Header `Retry-After` (30 Sekunden, bei jeder weiteren Sperre doppelt, höchstens 15 Minuten), siehe 2.2 |
| 503 | `AUTH_UNAVAILABLE` | Benutzerdatenbank nicht lesbar, Anmeldung gesperrt bis zum Reset per Taste |

## 8. Sprache

Die Weboberfläche ist zweisprachig (Deutsch und Englisch): Sie wählt beim ersten Aufruf nach der Browsersprache und lässt sich in den Einstellungen umstellen (gespeichert je Browser). Fehlertexte übersetzt die Oberfläche anhand des Feldes `code`; das Feld `error` des Geräts bleibt deutsch und dient als Rückfall für unbekannte Codes. Skripte sollen sich nur auf `code` verlassen. Die Texte auf dem Display des Geräts bleiben deutsch.

## 9. Taste am Gerät (zur Einordnung, keine HTTP-Schnittstelle)

| Bedienung | Wirkung |
|---|---|
| Einfacher Klick | QR-Code mit der Adresse für 20 Sekunden (im Einrichtungsmodus: QR-Code zum WLAN-Beitritt); ein weiterer Klick blendet ihn aus |
| Doppelklick | Betriebsart wechseln; nutzt das USB-Gerät das Laufwerk noch, verlangt das Display einen zweiten Doppelklick innerhalb von 4 Sekunden; schreibt das Gerät gerade, wird nicht gewechselt |
| 5 bis 10 Sekunden halten, dann loslassen | Wartungsmodus |
| 10 Sekunden halten | Benutzer auf `admin` / `admin` zurücksetzen |

## 10. Ergänzungen

Version 4 (WiFi-Suche, Laufwerksverschlüsselung) steht in [12_api_v4.md](12_api_v4.md) und baut auf diesem Vertrag auf.
