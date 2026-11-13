# HTTP-API Version 6 (WiFi DataDrive 0.6.0)

Ergänzung zu [09_api_v3.md](09_api_v3.md), [12_api_v4.md](12_api_v4.md) und [15_api_v5.md](15_api_v5.md). Dieses Dokument nennt nur, was Version 6 hinzufügt oder ändert; alles andere gilt unverändert aus Version 3 bis 5 (Grundregeln, Anmeldung und Rollen, Pfade, Dateien, Verschlüsselung, Fehlerformat `{"ok":false,"code":"…","error":"…"}`).

Gemeinsam mit Version 3 bis 5 gilt: `POST` braucht den Header `X-PrintDrive: 1` (sonst `403 FORBIDDEN`). Ohne Anmeldung `401 UNAUTHORIZED`, bei zu geringer Rolle `403 FORBIDDEN_ROLE`, beim Auslieferungspasswort `403 MUST_CHANGE_PASSWORD`, bei unlesbarer Benutzerdatenbank `503 AUTH_UNAVAILABLE`. Die Einstellungen dieses Dokuments werden dauerhaft gespeichert und überstehen einen Neustart.

## 1. Restzeit bis zum automatischen Sperren

### 1.1 Status

Das Objekt `autolock` in `GET /api/status` (Version 5, Abschnitt 2) bekommt zwei Felder: `"autolock":{"minutes":30,"on_detach":false,"remaining_s":1712,"waiting":false}`.

- `remaining_s`: Sekunden, bis die Zeitregel (`minutes`) das Laufwerk sperrt, wenn bis dahin nichts geschieht. Auf ganze Sekunden aufgerundet, höchstens `minutes × 60`.
- **`-1`: es läuft kein Countdown**, nämlich wenn das Sperren nach Zeit aus ist (`minutes:0`), das Laufwerk nicht verschlüsselt, gesperrt oder beschädigt ist, oder gerade eine Übertragung oder ein langer Vorgang läuft (Up- oder Download, ZIP, Kopieren, rekursives Löschen, Formatieren; ein laufender Vorgang zählt als Aktivität, die Frist beginnt erst danach).
- **`0` mit `waiting:true`: die Zeit ist abgelaufen, das Sperren ist aber aufgeschoben.** Das ist der Fall, solange
  - ein USB-Gerät das Laufwerk hält und `minutes` zwischen 1 und 9 liegt (siehe unten), oder
  - das USB-Gerät gerade schreibt (Schreibschutzzeit `write_guard_ms`) oder das Laufwerk beschäftigt ist und der letzte Sperrversuch deshalb abgewiesen wurde.
  
  Sobald der Grund entfällt, wird gesperrt. `waiting` ist sonst immer `false`.
- **Kurze Zeiten nehmen dem USB-Gerät das Laufwerk nicht weg:** Ein USB-Gerät „hält“ das Laufwerk, solange es ihm bereitgestellt ist (`drive.owner` = `usb`), angeschlossen ist, es nicht ausgeworfen hat und nicht länger als die Frist für `on_detach` im Ruhezustand ist. Bei `minutes` von 1 bis 9 wartet die Zeitregel dann, bis das USB-Gerät das Laufwerk nicht mehr hält; ab 10 Minuten nimmt sie es ihm weg (wie in Version 5 beschrieben: ein untätiges USB-Gerät verliert das Medium). Die Regel `on_detach` bleibt davon unberührt.
- **Was die Frist neu startet:** jede Datei-Anfrage (Liste in Version 5, Abschnitt 2), jedes Lesen oder Schreiben des USB-Geräts, `POST /api/autolock/touch` (1.2), das Entsperren und jede Änderung von `minutes`. **Das Abfragen von `/api/status` zählt nicht**, ebenso wenig Sitzung und `/api/crypt`: eine offene Seite hält das Laufwerk nicht offen.
- Der Wert ist eine Momentaufnahme. Das Gerät prüft die Regel alle paar Sekunden; nach `remaining_s:0` (ohne `waiting`) kann es daher bis zu einigen Sekunden dauern, bis `crypt.unlocked` `false` meldet.

### 1.2 Endpunkt

| Methode | Pfad | Rolle | Parameter | Antwort / Fehler |
|---|---|---|---|---|
| POST | `/api/autolock/touch` | user | – | „Ich arbeite noch“: startet die Frist der Zeitregel neu wie eine Datei-Anfrage. `{"ok":true,"remaining_s":1800}` mit der neuen Restzeit; `-1`, wenn kein Countdown läuft (Fälle wie in 1.1; dann ändert sich nichts). Auch im gesperrten Zustand erreichbar (`200`, `-1`). |

Die Oberfläche zeigt die Restzeit an und bietet kurz vor Ablauf „Weiterarbeiten“ an, das `POST /api/autolock/touch` schickt. `on_detach` wird durch `touch` nicht aufgehalten.

## 2. Netzwerkeinstellungen

Feste IPv4-Adresse oder DHCP sowie der Gerätename. **Eine Einstellung für alle gespeicherten WLANs.** Der Einrichtungs-Zugangspunkt behält immer seine feste Adresse `192.168.4.1`.

### 2.1 Endpunkte

| Methode | Pfad | Rolle | Parameter | Antwort / Fehler |
|---|---|---|---|---|
| GET | `/api/network` | admin | – | `{"ok":true,"dhcp":false,"ip":"192.168.2.50","mask":"255.255.255.0","gateway":"192.168.2.1","dns":"192.168.2.1","hostname":"datadrive","current":{"ip":"192.168.2.50","mask":"255.255.255.0","gateway":"192.168.2.1","dns":"192.168.2.1"},"connected":true}` |
| POST | `/api/settings/network` | admin | `dhcp`, mit `dhcp=0` außerdem `ip`, `mask`, `gateway`, optional `dns`; optional `hostname` | `{"ok":true,"restart":false,"ip":"192.168.2.50","hostname":"datadrive"}`. Ungültig: `400 BAD_REQUEST` (siehe 2.2). Nicht speicherbar: `500 IO`. |

Felder von `GET /api/network`:

- `dhcp`, `ip`, `mask`, `gateway`, `dns`, `hostname`: die **gespeicherte** Einstellung. Die vier Adressen sind leere Zeichenketten, solange nie eine feste Adresse gespeichert wurde; mit `dhcp:true` bleiben früher gespeicherte feste Werte stehen (unbenutzt), damit die Oberfläche sie vorbelegen kann. `hostname` ist ohne Einstellung `datadrive`.
- `current`: was die WLAN-Schnittstelle **jetzt** hat (per DHCP bezogen oder fest eingestellt); alle vier leer, solange sie nicht verbunden ist. Die Oberfläche belegt damit das Formular für eine feste Adresse vor.
- `connected`: die Verbindung mit einem WLAN besteht (wie `wifi.sta_connected`).

Parameter von `POST /api/settings/network` (als Formular im Body oder, ohne Body, als Abfrageparameter in der URL):

- `dhcp`: `1` (oder `true`) = Adresse per DHCP, `0` (oder `false`) = feste Adresse. Pflicht.
- `ip`, `mask`, `gateway`: Pflicht mit `dhcp=0`, Form `a.b.c.d` (dezimal, ohne führende Nullen). Mit `dhcp=1` werden sie nicht ausgewertet und die gespeicherten festen Werte bleiben unverändert.
- `dns`: optional; fehlt oder leer, gilt das Gateway als DNS-Server (gespeichert und gemeldet wird dann die Adresse des Gateways).
- `hostname`: optional, 1 bis 31 Zeichen aus `a-z`, `0-9` und Bindestrich, kein Bindestrich am Anfang oder Ende. Großbuchstaben werden in Kleinbuchstaben umgewandelt. Fehlt er oder ist er leer, bleibt der gespeicherte Name. Voreinstellung `datadrive`.

### 2.2 Prüfungen

Jede Verletzung ergibt `400 BAD_REQUEST`; der Text in `error` nennt das betroffene Feld (IP-Adresse, Subnetzmaske, Gateway, DNS-Server, Gerätename bzw. „dhcp“). Ein eigenes JSON-Feld dafür gibt es nicht. Geprüft wird in dieser Reihenfolge, gemeldet wird der erste Fehler:

1. `dhcp` fehlt oder hat einen anderen Wert.
2. Mit `dhcp=0`: `ip`, `mask` oder `gateway` fehlt.
3. `hostname` verletzt die Regel.
4. Syntax von `ip`, `mask`, `gateway`, `dns` (keine IPv4-Adresse in der Form `a.b.c.d`).
5. `mask`: nicht zusammenhängend oder außerhalb von `255.0.0.0` (/8) bis `255.255.255.252` (/30).
6. `ip`: keine gewöhnliche Adresse (`0.x`, `127.x`, ab `224.x`), Netz- oder Broadcast-Adresse des Subnetzes, oder `192.168.4.1` (Adresse des Einrichtungs-Zugangspunkts).
7. `gateway`: keine gewöhnliche Adresse, nicht im selben Subnetz wie `ip`, gleich `ip`, oder Netz- bzw. Broadcast-Adresse.
8. `dns`: keine gewöhnliche Adresse.

Eine abgewiesene Anfrage ändert nichts.

### 2.3 Ablauf einer Änderung

- **Die Antwort kommt, bevor die Änderung wirkt.** `ip` nennt die Adresse, die das Gerät danach hat (leer mit DHCP), `hostname` den Namen danach; `restart` ist immer `false` (kein Neustart). Etwa 1 bis 2 Sekunden später übernimmt das Gerät die Einstellung.
- **Ändert sich die Adresse, reißt die Verbindung ab.** Die Oberfläche zeigt vor dem Speichern die neue Adresse (bei DHCP den Gerätenamen `http://<hostname>.local`) und wechselt danach dorthin. Mit DHCP kennt nur der Router die neue Adresse; meist bekommt das Gerät dieselbe wie zuvor.
- Bleibt die Adresse gleich, kann die Verbindung trotzdem kurz unterbrochen sein; Sitzungen bleiben gültig.
- Ein neuer `hostname` gilt sofort für mDNS (`http://<hostname>.local`) und für die nächste DHCP-Anfrage (Name beim Router). Ist HTTPS eingerichtet, stellt das Gerät nach einer neuen Adresse ein neues Serverzertifikat aus, nach einem neuen Gerätenamen auch eine neue CA (sie muss auf den Geräten neu installiert werden); beides wirkt nach dem nächsten Neustart (`https_restart:true`).
- **Taste 10 Sekunden halten** setzt die Benutzer **und** die Netzwerkeinstellungen zurück (DHCP, Gerätename `datadrive`, gespeicherte feste Werte gelöscht). Das ist der Weg zurück, wenn eine feste Adresse das Gerät unerreichbar gemacht hat.

### 2.4 Status

`GET /api/status` enthält im Objekt `wifi` zusätzlich `"dhcp":true` (gespeicherte Einstellung). `wifi.ip` ist die aktuelle Adresse (wie `current.ip`), `wifi.hostname` der Gerätename in Kraft.

## 3. Signierte Updates

### 3.1 Status

Das Objekt `ota` in `GET /api/status` bekommt `"signed_required":true` und `"key_id":"3fa1c07e"`:

- `signed_required`: Diese Firmware installiert über `POST /api/ota` nur Abbilder, die mit ihrem eigenen Schlüssel signiert sind. `false` in Builds ohne signierte Updates (dann wird nichts geprüft, wie in Version 5).
- `key_id`: die ersten 8 Hex-Ziffern (Kleinbuchstaben) des SHA-256 des öffentlichen Schlüssels in der laufenden Firmware; leer, wenn `signed_required` `false` ist. Damit lässt sich prüfen, ob eine Update-Datei zum Gerät passt. Schlüsselverwaltung und Signieren: [17_signierte_updates.md](17_signierte_updates.md).

### 3.2 `POST /api/ota`

Zusätzlich zu den bisherigen Antworten, nur mit `signed_required:true`:

| HTTP | code | Wann |
|---|---|---|
| 422 | `UNSIGNED` | Das Abbild trägt keine Signatur. Eine Datei, deren Größe kein Vielfaches von 4096 ist, kann keine tragen und wird schon nach dem Kopf abgewiesen, bevor etwas geschrieben wird. |
| 422 | `BAD_SIGNATURE` | Das Abbild trägt eine Signatur, die nicht mit dem Schlüssel des Geräts übereinstimmt (anderer Schlüssel, oder die Datei wurde nach dem Signieren verändert). |

- **In beiden Fällen bleibt die laufende Firmware unberührt:** kein Neustart, `ota.partition` unverändert, die Startpartition wird nicht umgestellt.
- Reihenfolge: Ein Kopf, der kein ESP32-S3-Abbild ist, ergibt wie bisher `422 INVALID_FILE`. Danach entscheidet die Prüfung am Ende über das ganze Abbild; scheitert sie, meldet das Gerät `UNSIGNED` oder `BAD_SIGNATURE`, je nachdem, ob ein Signaturblock vorhanden ist (auch wenn das Abbild zusätzlich beschädigt ist).
- Eine signierte Datei ist das auf 4 KiB aufgefüllte Abbild plus ein 4-KiB-Sektor mit dem Signaturblock (Secure Boot V2, RSA-3072), wie ihn `espsecure.py sign_data` anhängt.

## 4. Ergänzung zum gesperrten Zustand

`POST /api/autolock/touch`, `GET /api/network` und `POST /api/settings/network` bleiben im gesperrten und im beschädigten Zustand erreichbar (Liste in Version 4, Abschnitt 2.3).

## 5. Neue Felder in `GET /api/status` (Überblick)

```json
{
  "autolock": {"minutes": 30, "on_detach": false, "remaining_s": 1712, "waiting": false},
  "wifi": {"…": "…", "dhcp": true},
  "ota": {"partition": "ota_0", "pending_verify": false, "signed_required": true, "key_id": "3fa1c07e"}
}
```

Alle übrigen Felder wie in Version 3 bis 5.

## 6. Neue Fehlercodes

| HTTP | code | Bedeutung |
|---|---|---|
| 422 | `UNSIGNED` | Update ohne Signatur, obwohl das Gerät signierte Updates verlangt |
| 422 | `BAD_SIGNATURE` | Update mit einer Signatur, die nicht zum Schlüssel des Geräts passt |

Unverändert und hier ebenfalls verwendet: `400 BAD_REQUEST`, `422 INVALID_FILE`, `500 IO`.

## 7. Testserver und Test

- `tools/mock_server.py` bildet Version 6 nach (Prüfungen und Texte wie die Firmware). Zusätzliche Schalter unter `/mock/set`:
  - `autolock_remaining=<s>`: verschiebt die letzte Aktivität so, dass der Countdown `s` Sekunden zeigt (braucht `minutes` > 0; `0` sperrt sofort, wenn nichts aufschiebt). Derselbe Mechanismus wie `idle_s`, nur von der anderen Seite.
  - `autolock_waiting=0|1`: `1` schiebt das Sperren nach Zeit auf; ist die Zeit um, meldet der Status `remaining_s:0`, `waiting:true`. Die Regel „1 bis 9 Minuten nehmen dem USB-Gerät das Laufwerk nicht weg“ gilt im Testserver ohnehin.
  - `net_dhcp=0|1`: gespeicherte Netzwerk-Einstellung (`0` ohne gespeicherte feste Werte setzt `192.0.2.152/24`, Gateway und DNS `192.0.2.1`).
  - `net_fail=1` (oder Sekunden): Nach der nächsten angenommenen Netzwerkänderung setzt der Testserver 3 Sekunden (bzw. so viele) lang jede Verbindung zurück, wie bei einem Adresswechsel. Ohne den Schalter bleibt er erreichbar; er lauscht immer auf seiner eigenen Adresse, `current` und `wifi.ip` zeigen aber die eingestellte.
  - `ota_signed=0|1`: Updates müssen signiert sein (`key_id` des Testservers `4d6f636b`).
  - `ota_sig=ok|bad|none|clear`: Ergebnis der Signaturprüfung für das nächste Update, das sie erreicht (`ok` angenommen, `bad` `BAD_SIGNATURE`, `none` `UNSIGNED`). Ohne den Schalter: kein gültiger Signaturblock → `UNSIGNED`, Signaturblock mit anderem Schlüssel → `BAD_SIGNATURE` (der Testserver kann RSA nicht prüfen; echt signierte Dateien bekommen daher `BAD_SIGNATURE`).
  
  Der Gerätename des Testservers ist `datadrive-mock`, seine Adresse mit DHCP `192.0.2.152`.
- `tools/api_test.py` prüft Version 6 in den Abschnitten 17 bis 19; nur diese: `python tools/api_test.py HOST --password PW --only v6` (einzeln: `--only countdown,network,otasig`).
  - Abschnitt 17 setzt die Zeit vorübergehend auf 30 Minuten (stellt sie wieder her) und prüft Restzeit, Abfragen ohne Wirkung, `touch` und Datei-Anfragen. Den Zustand `waiting` prüft er nicht (dauert mindestens eine Minute).
  - Abschnitt 18 liest die Einstellungen und schickt am Gerät nur Anfragen, die schon durch ihre Form ungültig sind (fehlende oder unlesbare Werte, ungültige Gerätenamen bei unverändertem Modus und unveränderten Adressen); nimmt das Gerät eine davon trotzdem an, stellt der Test sofort den vorigen Stand her. Die Prüfungen von Maske, Subnetz, Netz- und Broadcast-Adresse sowie das Umstellen (DHCP → feste aktuelle Adresse → DHCP, bzw. bei fester Adresse nur ein vorübergehender Gerätename) laufen am Gerät nur mit `--network-test`; der Test wartet nach jeder Änderung, bis das Gerät wieder antwortet, und stellt am Ende den vorigen Stand her. Am Testserver läuft alles ohne Schalter.
  - Abschnitt 19 prüft die Statusfelder; mit `--ota-probe` und `signed_required:true` außerdem drei nie startfähige Abbilder (Größe kein Vielfaches von 4096, unsigniert, Signaturblock mit fremdem Schlüssel), die mit `422` abgewiesen werden müssen, ohne dass sich die Partition ändert.
