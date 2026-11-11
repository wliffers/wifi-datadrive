# HTTP-API (Version 2, WiFi DataDrive)

Vertrag zwischen Firmware (`firmware/printdrive/main/web_server.c`), Weboberfläche (`main/web/index.html`), Testserver (`tools/mock_server.py`) und API-Test (`tools/api_test.py`).

## Grundregeln

- Alles läuft über Port 80, ohne Anmeldung, nur im lokalen Netz.
- Ändernde Anfragen sind `POST` und brauchen den Header `X-PrintDrive: 1`, sonst `403` mit `code: "FORBIDDEN"`.
- Der Server bearbeitet **eine Anfrage nach der anderen**. Während eines Uploads bleibt jede andere Anfrage hängen, bis er fertig ist.
- Dateinamen stehen URL-kodiert (UTF-8) im Query-Parameter `name`. Es gibt nur das Hauptverzeichnis, keine Ordner.
- Antworten sind JSON. Fehler haben immer die Form `{"ok":false,"code":"…","error":"deutscher Klartext"}`.
- Zeitstempel (`mtime`) sind Sekunden seit 1970 in **Ortszeit des Schreibers** (FAT kennt keine Zeitzone). Zur Anzeige als UTC interpretieren. `0` = unbekannt.

## Fehlercodes

| HTTP | code | Bedeutung |
|---|---|---|
| 400 | `BAD_NAME` | ungültiger oder fehlender Dateiname (auch fehlendes `size`/`to`) |
| 400 | `BAD_REQUEST` | Parameter fehlt oder falsch |
| 400 | `INCOMPLETE` | weniger Daten als angekündigt |
| 403 | `FORBIDDEN` | Header `X-PrintDrive` fehlt |
| 404 | `NOT_FOUND` | Datei nicht vorhanden |
| 409 | `BUSY` | Upload oder Download läuft |
| 409 | `HOST_ACTIVE` | USB-Gerät nutzt das Laufwerk; mit `force=1` wiederholen |
| 409 | `HOST_WRITING` | USB-Gerät hat gerade geschrieben; nie erzwingbar, später erneut versuchen |
| 409 | `WRONG_OWNER` | Aktion nur möglich, wenn die Dateiverwaltung das Laufwerk besitzt |
| 409 | `NO_FS` | kein lesbares Dateisystem |
| 409 | `EXISTS` | Datei existiert bereits |
| 413 | `TOO_LARGE` | Datei ≥ 4 GB |
| 415 | `BAD_TYPE` | Dateityp nicht erlaubt (nur wenn `types` ≠ `"all"`) |
| 422 | `INVALID_FILE` | Inhalt ungültig (z. B. `.3mf` ist kein ZIP) oder Datei ist leer |
| 500 | `IO` | Lese-/Schreibfehler |
| 507 | `NO_SPACE` | nicht genug freier Speicher |

## Lesen

### `GET /api/status`

```json
{
  "version": "0.2.0", "uptime_s": 123, "heap": 140000,
  "types": "all",
  "drive": {
    "owner": "usb", "fs_ok": true, "needs_format": false,
    "usb_connected": true, "usb_suspended": false, "host_ejected": false,
    "host_writable": true, "host_idle_ms": 5200, "host_write_idle_ms": -1,
    "write_guard_ms": 3000,
    "total": 62525505536, "free": 62525308928, "files": 2, "sector": 512
  },
  "medium": { "sd": true, "name": "SDABC" },
  "upload": { "active": false, "name": "", "received": 0, "total": 0 },
  "wifi": {
    "sta_configured": true, "sta_connected": true, "ssid": "MeinWLAN",
    "ip": "192.168.2.152", "rssi": -61,
    "ap_active": false, "ap_ssid": "DataDrive-54D5", "ap_ip": "", "hostname": "datadrive"
  }
}
```

- `drive.owner`: `"usb"` (USB-Gerät besitzt das Laufwerk), `"app"` (Dateiverwaltung), `"none"` (Fehler).
- `host_idle_ms` / `host_write_idle_ms`: Millisekunden seit dem letzten Zugriff bzw. Schreibzugriff des USB-Geräts, `-1` = noch keiner.
- `types`: `"all"` oder eine Liste erlaubter Endungen wie `".3mf,.gcode"`.
- `total`, `free`, `files` sind die zuletzt bekannten Werte; im USB-Betrieb können sie veraltet sein.

### `GET /api/files`

Nur wenn `owner == "app"`, sonst `409 WRONG_OWNER`.

```json
{ "ok": true, "files": [ { "name": "Würfel.3mf", "size": 524288, "mtime": 1794430000 } ],
  "dirs": 1, "truncated": false }
```

- Nur normale Dateien im Hauptverzeichnis, ohne Namen mit führendem Punkt.
- `dirs`: Anzahl ausgeblendeter Ordner. `truncated`: Liste war zu lang und wurde gekürzt.

### `GET /api/download?name=`

Dateiinhalt als `application/octet-stream`. Fehler: `404 NOT_FOUND`, `409 WRONG_OWNER`.

### `GET /api/log`

Die letzten 8 KB Geräteprotokoll als Text.

## Ändern (alle `POST`, Header `X-PrintDrive: 1`)

| Pfad | Parameter | Wirkung |
|---|---|---|
| `/api/mode` | `target=app\|usb`, `force=0\|1` | Besitzer wechseln. Nach `app` kann `HOST_ACTIVE` (dann mit `force=1` bestätigen) oder `HOST_WRITING` kommen; `BUSY` bei laufendem Up-/Download. |
| `/api/upload/check` | `name`, `size`, `overwrite=0\|1` | Prüft Name, Typ, Vorhandensein und Platz, ohne Daten zu senden. |
| `/api/upload` | `name`, `overwrite=0\|1`, `mtime`, `now` (beide optional) | Dateiinhalt als roher Body, `Content-Length` Pflicht. Die Datei erscheint erst nach vollständiger Prüfung. `mtime` = Änderungsdatum der Datei, `now` = aktuelle Uhrzeit des Browsers (das Gerät hat keine eigene Uhr); beide als Ortszeit-Sekunden. |
| `/api/delete` | `name` | Datei löschen. |
| `/api/rename` | `name`, `to` | Umbenennen. `EXISTS`, wenn das Ziel vorhanden ist. |
| `/api/settings/host_writable` | `value=0\|1` | Darf das USB-Gerät schreiben? Wirkt beim nächsten Bereitstellen. |
| `/api/wifi` | Body `ssid=…&pass=…` (URL-kodiert) | WLAN-Zugangsdaten speichern und verbinden. Passwort leer oder 8–63 Zeichen. |
| `/api/format` | `confirm=FORMAT` | Aktuelles Medium formatieren; das Gerät startet neu. Antwort `{"ok":true,"restart":true}`. |
| `/api/reboot` | `mode=normal\|download`, `force=0\|1` | Neustart bzw. Wartungsmodus zum Flashen. `BUSY`/`HOST_WRITING` ohne `force`. |

Erfolg ist `{"ok":true,"code":"OK","error":""}` (bei `/api/wifi` und `/api/reboot` nur `{"ok":true}`).

## Ablauf eines Uploads aus Sicht der Oberfläche

1. `GET /api/status`. Ist `owner == "usb"`: `POST /api/mode?target=app`, bei `HOST_ACTIVE` den Benutzer fragen und mit `force=1` wiederholen.
2. `POST /api/upload/check` je Datei. Bei `EXISTS` nachfragen, dann mit `overwrite=1` erneut prüfen.
3. `POST /api/upload` mit Fortschritt. Währenddessen keine weiteren Anfragen senden.
4. Erst nach `ok:true` als gespeichert melden.
5. Optional `POST /api/mode?target=usb`, um das Laufwerk wieder bereitzustellen.
