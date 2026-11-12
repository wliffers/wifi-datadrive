# HTTP-API Version 4 (WiFi DataDrive 0.4.0)

Ergänzung zu [09_api_v3.md](09_api_v3.md). Dieses Dokument nennt nur, was Version 4 hinzufügt oder ändert; alles andere gilt unverändert aus Version 3 (Grundregeln, Anmeldung und Rollen, Pfade, Dateien, Fehlerformat `{"ok":false,"code":"…","error":"…"}`). Hintergrund und Entscheidungen: [11_plan_v04.md](11_plan_v04.md), Abschnitte F1 und F2.

Gemeinsam mit Version 3 gilt: `POST` braucht den Header `X-PrintDrive: 1` (sonst `403 FORBIDDEN`), Formularfelder gehen als URL-kodierter Body (`Content-Type: application/x-www-form-urlencoded`), Abfrageparameter in der URL. Ohne Anmeldung `401 UNAUTHORIZED`, bei zu geringer Rolle `403 FORBIDDEN_ROLE`, beim Auslieferungspasswort `403 MUST_CHANGE_PASSWORD`, bei unlesbarer Benutzerdatenbank `503 AUTH_UNAVAILABLE`.

## 1. WLAN-Suche (Rolle `admin`)

| Methode | Pfad | Parameter | Antwort / Fehler |
|---|---|---|---|
| GET | `/api/wifi/scan` | – | `{"ok":true,"networks":[{"ssid":"Zuhause","rssi":-52,"secure":true,"saved":true}]}`; `409 BUSY`, wenn gerade ein Up- oder Download läuft |

- `ssid`: Name als UTF-8-String (JSON-maskiert, kann Anführungszeichen und Umlaute enthalten). Netze ohne Namen (versteckte) fehlen; mehrere Zugangspunkte mit demselben Namen erscheinen einmal, mit der stärksten Signalstärke.
- `rssi`: Signalstärke in dBm (negativ, näher an 0 = stärker).
- `secure`: `true`, wenn das Netz verschlüsselt ist (Passwort nötig), `false` bei offenen Netzen.
- `saved`: `true`, wenn das Netz unter den gespeicherten WLANs (`/api/wifi/list`) steht.
- Sortiert nach `rssi` absteigend, höchstens 20 Einträge, eine leere Liste ist ein gültiges Ergebnis.
- Die Suche dauert 2 bis 3 Sekunden; die Anfrage blockiert so lange (wie jede andere, es läuft immer nur eine). Die Oberfläche setzt ein Zeitlimit von mindestens 10 Sekunden. Im Einrichtungsmodus (Handy am Zugangspunkt des Sticks) kann die Verbindung währenddessen kurz stocken; die Oberfläche versucht es dann erneut.
- Die Suche ändert nichts an den gespeicherten Netzen und nichts an der bestehenden Verbindung.

## 2. Verschlüsseltes Laufwerk

Verfahren und Grenzen des Schutzes: Plan F1 (AES-256-XTS je 512-Byte-Sektor, Hauptschlüssel im Kopfbereich, abgeleitet aus dem Laufwerks-Passwort). Das **Laufwerks-Passwort ist unabhängig vom Anmelde-Passwort**; der Benutzer-Reset per Taste öffnet das Laufwerk nicht.

### 2.1 Zustände

| Zustand | `enabled` | `unlocked` | Bedeutung |
|---|---|---|---|
| unverschlüsselt | `false` | `true` | wie Version 3 |
| entsperrt | `true` | `true` | Dateiverwaltung und USB-Laufwerk arbeiten wie in Version 3 |
| gesperrt | `true` | `false` | kein Zugriff auf Inhalt; für das USB-Gerät ist **kein Medium** eingelegt |
| beschädigt | `true` | `false` | wie gesperrt, aber der Schlüsselbereich ist unlesbar (`damaged:true`); Entsperren ist unmöglich, nur `erase` hilft |

- Nach jedem Neustart (Stromverlust, `/api/reboot`, Update) ist ein verschlüsseltes Laufwerk **gesperrt**; der Schlüssel liegt nur im Arbeitsspeicher.
- **Entsperren** meldet das Laufwerk an: Ist ein USB-Gerät angeschlossen, wird das Laufwerk ihm bereitgestellt (`drive.owner` = `usb`), sonst übernimmt die Dateiverwaltung (`app`), wie nach einem Moduswechsel.
- **Sperren** nimmt dem USB-Gerät das Medium weg und beendet den Zugriff der Dateiverwaltung.
- Der Zustand gilt für das Gerät, nicht für die Sitzung: wer angemeldet ist (Rolle `user` genügt zum Entsperren und Sperren), arbeitet auf demselben Laufwerk wie alle anderen.

### 2.2 Endpunkte

| Methode | Pfad | Rolle | Parameter (Body) | Antwort / Fehler |
|---|---|---|---|---|
| GET | `/api/crypt` | user | – | `{"ok":true,"enabled":true,"unlocked":false,"damaged":false,"retry_after":0}`. `retry_after`: Sekunden bis zum nächsten erlaubten Versuch mit einem Laufwerks-Passwort (siehe Regeln), sonst `0` |
| POST | `/api/crypt/setup` | admin | `password` | **Löscht zuerst das ganze Medium**, legt dann den Schlüsselbereich an, formatiert das Laufwerk (alle Dateien gehen verloren), schaltet die Verschlüsselung ein und lässt das Laufwerk **entsperrt**. Antwort: `{"ok":true,"code":"OK","error":"","recovery_key":"7K3M-Q9XD-4WTB-HN2C-5RYE-8FGA-ZP6V-JC3S"}`. `400 CRYPT_WEAK_PASSWORD`, `400 BAD_REQUEST`, `409 CRYPT_ALREADY_ENABLED`, `409 BUSY`, `409 HOST_WRITING`, `409 WRONG_OWNER`, `503 CRYPT_UNAVAILABLE` (Verschlüsselung nicht möglich: Selbsttest fehlgeschlagen, Medium lässt sich nicht löschen oder ist zu klein). Scheitert das Einrichten mit `500 IO`, **nachdem** der Schlüsselbereich geschrieben wurde, enthält die Fehlerantwort trotzdem `recovery_key` und das Laufwerk ist verschlüsselt (Zustand per `GET /api/crypt` prüfen). Dauer siehe Regeln |
| POST | `/api/crypt/unlock` | user | `password` **oder** `recovery_key` | `{"ok":true,"code":"OK","error":""}`. Falsch: `403 CRYPT_WRONG_PASSWORD`, nach wiederholten Fehlversuchen `429 LOCKED` mit `Retry-After`. `423 CRYPT_DAMAGED`, `409 CRYPT_NOT_ENABLED`, `409 BUSY`, `400 BAD_REQUEST` (keines der Felder). Schon entsperrt: `200`, ohne Prüfung |
| POST | `/api/crypt/lock` | user | – | `{"ok":true,"code":"OK","error":""}`. `409 CRYPT_NOT_ENABLED`, `409 BUSY`, `409 HOST_WRITING`. Schon gesperrt: `200` |
| POST | `/api/crypt/password` | admin | `old`, `new` | Packt den Hauptschlüssel mit dem neuen Passwort neu ein; die Daten bleiben unberührt, der Wiederherstellungsschlüssel bleibt gültig und wird nicht erneut ausgegeben. **Braucht ein entsperrtes Laufwerk** (gesperrt: `423 DRIVE_LOCKED`); ändert den Zustand nicht. `old` ist das bisherige Laufwerks-Passwort **oder der Wiederherstellungsschlüssel** (Passwort vergessen: erst mit dem Schlüssel entsperren, dann mit `old` = Schlüssel ein neues Passwort setzen). `403 CRYPT_WRONG_PASSWORD` / `429 LOCKED` (`old`), `400 CRYPT_WEAK_PASSWORD`, `409 CRYPT_NOT_ENABLED`, `423 DRIVE_LOCKED`, `423 CRYPT_DAMAGED` |
| POST | `/api/crypt/recovery` | admin | `password` | Erzeugt einen **neuen Wiederherstellungsschlüssel**; der bisherige funktioniert danach nicht mehr. `password` ist das Laufwerks-Passwort oder der aktuelle Wiederherstellungsschlüssel. Das Laufwerk muss entsperrt sein. Antwort: `{"ok":true,"recovery_key":"…"}` (Form und Einmaligkeit wie bei `setup`). `403 CRYPT_WRONG_PASSWORD` / `429 LOCKED`, `400 BAD_REQUEST`, `409 CRYPT_NOT_ENABLED`, `423 DRIVE_LOCKED`, `423 CRYPT_DAMAGED` |
| POST | `/api/crypt/erase` | admin | `confirm=ERASE` | Löscht den Schlüsselbereich und **formatiert das Laufwerk unverschlüsselt** (alle Dateien gehen unwiederbringlich verloren); danach `enabled:false`, Besitzer `app`. Für verlorenes Passwort **und** verlorenen Schlüssel. Ohne `confirm=ERASE`: `400 BAD_REQUEST`. **Nur im gesperrten oder beschädigten Zustand**: bei entsperrtem Laufwerk `409 CRYPT_ALREADY_ENABLED` (dort gilt `disable`), ohne Verschlüsselung `409 CRYPT_NOT_ENABLED`. Braucht kein Passwort (und wird nicht gedrosselt). `409 BUSY` |
| POST | `/api/crypt/disable` | admin | `password` | Schaltet die Verschlüsselung aus und **formatiert das Laufwerk unverschlüsselt** (alle Dateien gehen verloren); danach `enabled:false`, Besitzer `app`. `403 CRYPT_WRONG_PASSWORD` / `429 LOCKED`, `409 CRYPT_NOT_ENABLED`, `423 CRYPT_DAMAGED`, `409 BUSY`, `409 HOST_WRITING`, `409 WRONG_OWNER` |

Regeln:

- **Laufwerks-Passwort:** mindestens 12 Zeichen (Unicode-Zeichen), höchstens 64 Bytes (UTF-8). Zu kurz: `400 CRYPT_WEAK_PASSWORD`; zu lang: `400 BAD_REQUEST`.
- **Wiederherstellungsschlüssel:** Nur die Antwort von `setup` enthält ihn, **genau einmal**; er wird nie wieder ausgegeben und nicht gespeichert. Form: 8 Gruppen zu je 4 Zeichen aus `0-9 A-Z` ohne `I`, `L`, `O`, `U`, durch `-` getrennt (160 Bit). Beim Entsperren sind Groß-/Kleinschreibung, Bindestriche und Leerzeichen egal.
- **Drosselung falscher Laufwerks-Passwörter** (Entsperren, `old` bei `password`, `password` bei `recovery` und `disable`; auch der Wiederherstellungsschlüssel zählt): **ein** Zähler für das ganze Gerät, nicht je Adresse. Die ersten zwei falschen Eingaben sind frei, danach wird jede weitere falsche Eingabe mit einer Wartezeit belegt: 5 Sekunden, jede weitere doppelt, höchstens 300 Sekunden. Während der Wartezeit antwortet das Gerät `429 LOCKED` mit `Retry-After` (Sekunden) und prüft nichts; `retry_after` in `GET /api/crypt` zeigt die Restzeit. Ein richtiges Passwort setzt den Zähler zurück. Der Zähler liegt im Arbeitsspeicher; ein Neustart setzt ihn zurück. Er ist von dem der Anmeldung getrennt.
- **Dauer:** Entsperren, Passwortwechsel und neuer Wiederherstellungsschlüssel rechnen eine Schlüsselableitung von etwa 2 Sekunden. **`setup` löscht zuerst das ganze Medium und kann Minuten dauern** (auf einer microSD typisch 10 Sekunden bis 2 Minuten, auf dem internen Flash etwa 2 Minuten); das Gerät antwortet währenddessen auf nichts anderes. Die Oberfläche wartet entsprechend lange und warnt vorher. `disable` und `erase` dauern so lange wie das Formatieren.
- **Größe:** Das verschlüsselte Laufwerk ist etwa 8 bis 12 MiB kleiner als die Karte (Schlüsselbereich und Reserve); `drive.total` zeigt die nutzbare Größe.
- **`setup` und `disable` formatieren und brauchen daher das Laufwerk:** läuft ein Up- oder Download: `409 BUSY`; schreibt das USB-Gerät gerade (`host_write_idle_ms` kleiner als `write_guard_ms`): `409 HOST_WRITING`; gehört das Laufwerk dem USB-Gerät: `409 WRONG_OWNER` (vorher per `POST /api/mode?target=app` übernehmen, wie bei `POST /api/format`). Ein gesperrtes Laufwerk kann mit `disable` und dem richtigen Passwort ohne Besitzerwechsel ausgeschaltet werden; mit `erase` auch ganz ohne Passwort.
- **`lock`:** Läuft ein Up- oder Download: `409 BUSY`; schreibt das USB-Gerät gerade: `409 HOST_WRITING`. Sonst wird auch ein vom USB-Gerät benutztes Laufwerk gesperrt (das Gerät sieht „Medium entfernt“).
- `POST /api/format` auf einem verschlüsselten, entsperrten Laufwerk bleibt verschlüsselt (nur das Dateisystem wird neu angelegt, sofort). Das Gerät startet dabei **nicht** neu und das Laufwerk bleibt **entsperrt**; die Antwort lautet `{"ok":true,"restart":false}` (unverschlüsselt weiterhin `{"ok":true,"restart":true}` mit Neustart). Die Oberfläche richtet sich nach dem Feld `restart`.
- Das Laufwerks-Passwort geht ohne HTTPS unverschlüsselt durchs WLAN (Plan E4).

### 2.3 Wirkung im gesperrten und im beschädigten Zustand

Solange `crypt.enabled` und nicht `crypt.unlocked`:

- Jede dieser Anfragen antwortet mit **`423 DRIVE_LOCKED`** („Das Laufwerk ist gesperrt. Bitte zuerst entsperren.“), im beschädigten Zustand mit **`423 CRYPT_DAMAGED`**, ohne sonstige Prüfung des Inhalts: `GET /api/files`, `GET /api/download`, `POST /api/mode`, `/api/mkdir`, `/api/delete`, `/api/move`, `/api/copy`, `/api/upload/check`, `/api/upload`, `/api/push`, `/api/format`. Die Rollen- und Anmeldeprüfung kommt zuerst, dann `DRIVE_LOCKED`.
- Weiter erreichbar: Status, Sitzung, Benutzer, vertraute Geräte, WLAN, `POST /api/settings/host_writable`, Protokoll, Neustart, Update und alle `/api/crypt/*` (im beschädigten Zustand antworten `unlock`, `password`, `recovery` und `disable` mit `423 CRYPT_DAMAGED`; es bleiben `lock`, `erase` und die Abfrage).
- `GET /api/status` meldet in `drive`: `owner:"none"`, `fs_ok:false`, `needs_format:false`, `total`, `free` und `files` jeweils `0`, die übrigen Felder unverändert. Die Oberfläche erkennt „gesperrt“ und „beschädigt“ an `crypt` (siehe 2.4), nicht an `fs_ok`, und bietet dann nie „Formatieren“ an.
- Das USB-Gerät sieht kein Medium.

### 2.4 Status

`GET /api/status` enthält zusätzlich das Objekt `"crypt":{"enabled":false,"unlocked":true,"damaged":false}` (unverschlüsselt: `enabled:false`, `unlocked:true`; entsperrt: beide `true`; gesperrt: `enabled:true`, `unlocked:false`; beschädigt: `enabled:true`, `unlocked:false`, `damaged:true`; `damaged` ist sonst immer `false`). Alle übrigen Felder wie in Version 3.

### 2.5 Was die Verschlüsselung schützt

- **Gegen fremde Leser der Karte:** Wer die microSD-Karte (oder den Stick) in die Hand bekommt, sieht ohne Laufwerks-Passwort nur zufällig aussehende Daten.
- **Die Stärke des Passworts ist der eigentliche Schutz.** Wer die Karte hat, kann Passwörter offline und sehr schnell durchprobieren; kein Zähler des Geräts bremst ihn dabei. Ein langes, zufälliges Passwort (oder mehrere Wörter) schützt, ein kurzes nicht. Der Wiederherstellungsschlüssel ist dagegen praktisch nicht zu erraten.
- **Die Drosselung** (2.2) bremst nur das Raten **über das Netzwerk** am laufenden Gerät, nicht den Angriff auf eine entnommene Karte.
- **Wer Administratorzugriff hat, könnte geänderte Firmware aufspielen** (Updates sind noch nicht signiert); eine solche Firmware könnte das Passwort beim nächsten Entsperren mitlesen. Der Schutz gilt also gegen Diebstahl der Karte, nicht gegen jemanden, der das Gerät unbemerkt verändern kann.
- Ohne HTTPS geht das Passwort unverschlüsselt durchs WLAN (siehe oben).

## 3. Optionales HTTPS (experimentell, standardmäßig aus)

| Methode | Pfad | Rolle | Parameter (Query) | Antwort / Fehler |
|---|---|---|---|---|
| POST | `/api/settings/https` | admin | `value=0` oder `1`; optional `renew=1` (neues Serverzertifikat für die aktuelle IP, gleiche CA), `regenerate=1` (auch neue CA) | `{"ok":true,"https":true,"https_active":false,"https_restart":true,"cert_sha256":"AA:BB:…","ca_sha256":"AA:BB:…","cert_expires":"2028-02-10"}`. Ohne `value`, `renew` und `regenerate`: `400 BAD_REQUEST`. Zertifikat nicht erzeugbar: `500 IO`. Wirkt **erst nach einem Neustart** |
| GET | `/api/https/ca.cer` | user | – | Das Zertifikat der gerätereigenen Zertifizierungsstelle (CA) als DER, `Content-Type: application/x-x509-ca-cert`, als Download `DataDrive-CA.cer`. `404 NOT_FOUND`, solange noch nie eingeschaltet wurde (kein Zertifikat) |

`GET /api/status` enthält zusätzlich `https` (Einstellung, bool), `https_active` (läuft in diesem Start, bool), `https_restart` (Einstellung oder Zertifikat geändert, Neustart nötig, bool), `cert_sha256` und `ca_sha256` (Fingerabdrücke als `AA:BB:…`, 32 Bytes; leer vor dem ersten Einschalten) sowie `cert_expires` (`JJJJ-MM-TT`, leer vor dem ersten Einschalten). Beim Einschalten entstehen die Schlüssel sofort, die Fingerabdrücke sind also schon vor dem Neustart sichtbar.

Verhalten und Regeln:

- **Aktiv:** HTTPS auf Port 443; Port 80 antwortet auf jede Anfrage mit `307` auf die `https://`-Adresse (nicht zwischengespeichert, Ausschalten wirkt sofort). Das Sitzungs-Cookie bekommt `Secure`. Kein HSTS. Nur TLS 1.2. Scheitert der Start von HTTPS, läuft das Gerät wie bisher über HTTP weiter.
- **Eigene kleine CA:** Das Gerät stellt sein Serverzertifikat selbst aus. Die CA ist auf `*.local`, den bloßen Gerätenamen und private IP-Bereiche beschränkt (Name Constraints), taugt also nicht, um fremde Adressen zu beglaubigen. Das Serverzertifikat gilt 820 Tage und wird beim Start automatisch erneuert, wenn es bald abläuft; die CA bleibt dabei gleich. `regenerate=1` erzeugt auch eine neue CA, die dann auf allen Geräten neu installiert werden muss.
- **Browser warnen, bis die CA installiert ist:** Wer die Warnung loswerden will, lädt `/api/https/ca.cer` und installiert das Zertifikat als vertrauenswürdige Stammzertifizierungsstelle (iPhone: Profil laden, installieren und in den Zertifikatsvertrauenseinstellungen aktivieren; Windows: Zertifikat in „Vertrauenswürdige Stammzertifizierungsstellen“ importieren). Den Fingerabdruck der CA dabei mit `ca_sha256` vergleichen.
- **Skripte** über `http://` funktionieren nach dem Einschalten nicht mehr (nur noch die Weiterleitung) und müssen `https://` benutzen (mit der CA als Vertrauensanker).
- **Sitzungen und vertraute Geräte**, die über HTTP ausgestellt wurden, bleiben gültig. Nach dem Einschalten von HTTPS sollten daher die Passwörter geändert werden, falls sie vorher unverschlüsselt durchs WLAN gingen.

## 4. Neue Fehlercodes

| HTTP | code | Bedeutung |
|---|---|---|
| 400 | `CRYPT_WEAK_PASSWORD` | Laufwerks-Passwort kürzer als 12 Zeichen |
| 403 | `CRYPT_WRONG_PASSWORD` | Laufwerks-Passwort oder Wiederherstellungsschlüssel falsch |
| 409 | `CRYPT_NOT_ENABLED` | Verschlüsselung ist nicht eingeschaltet |
| 409 | `CRYPT_ALREADY_ENABLED` | Verschlüsselung ist bereits eingeschaltet |
| 423 | `DRIVE_LOCKED` | Laufwerk ist gesperrt, zuerst entsperren |
| 423 | `CRYPT_DAMAGED` | Schlüsselbereich unlesbar; nur `POST /api/crypt/erase` hilft |
| 503 | `CRYPT_UNAVAILABLE` | Verschlüsselung nicht möglich (Selbsttest fehlgeschlagen, Medium nicht löschbar oder zu klein) |

Unverändert aus Version 3 und hier ebenfalls verwendet: `429 LOCKED` (mit `Retry-After`, auch für zu viele falsche Laufwerks-Passwörter), `500 IO`, `409 BUSY`, `409 HOST_WRITING`, `409 WRONG_OWNER`, `400 BAD_REQUEST`, `503 AUTH_UNAVAILABLE`.
