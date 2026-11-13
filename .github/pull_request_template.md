## Was und warum / What and why

<!-- Kurz: was ändert sich, welches Problem wird gelöst, Bezug zu Issue oder docs/14_vormerkliste.md.
     Briefly: what changes, which problem it solves, related issue or docs/14_vormerkliste.md entry. -->

## Prüfstand / Test status

<!-- Jede Aussage markieren: "verifiziert" (am echten Gerät geprüft, mit Ergebnis) oder "noch nicht geprüft".
     Mark every claim: "verified" (checked on a real device, with result) or "untested". -->

| Prüfung / Check | Ergebnis / Result |
|---|---|
| Firmware gebaut mit ESP-IDF v5.5.6 / firmware builds with ESP-IDF v5.5.6 | |
| `python tools/api_test.py 127.0.0.1:PORT --user admin --password admin123` gegen / against `tools/mock_server.py` | |
| `python tools/ui_test.py` gegen / against `tools/mock_server.py` (bei Änderungen an der Weboberfläche / for web UI changes) | |
| Am echten Gerät (Board, Speicher, Host) / on a real device (board, storage, host) | |

## Checkliste / Checklist

- [ ] Die Ein-Besitzer-Regel des Laufwerks bleibt unangetastet (genau ein Besitzer: USB-Gerät oder Firmware). / The single-owner rule of the drive is unchanged.
- [ ] Keine Kompatibilitätsaussage ohne echten Test (USB-Geräte wie Drucker, PC oder Fernseher, Programme, Browser). / No compatibility claim without a real test (USB hosts such as printers, PCs or TVs, programs, browsers).
- [ ] API-Änderungen sind im API-Vertrag in `docs/` und in `tools/mock_server.py` nachgezogen. / API changes are reflected in the API contract in `docs/` and in `tools/mock_server.py`.
- [ ] README, `docs/05_testprotokoll.md` und `docs/06_risiken.md` sind angepasst, wo nötig. / README and docs are updated where needed.
- [ ] Keine Zugangsdaten, Schlüssel, persönlichen Daten oder Build-Ausgaben im Commit. / No credentials, keys, personal data or build output in the commit.
- [ ] Fremdcode nur mit verträglicher Lizenz, eingetragen in `THIRD_PARTY_NOTICES.md`. / Third-party code only with a compatible licence, listed in `THIRD_PARTY_NOTICES.md`.
