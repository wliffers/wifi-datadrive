# Sicherheit / Security

*English version below.*

## Sicherheitslücke melden

Bitte **nicht** als öffentliches Issue melden. Stattdessen vertraulich über GitHub:
im Repository **Security → Report a vulnerability** (private Meldung einer Sicherheitslücke).

Hilfreich sind: Firmware-Version, Board-Variante, Einstellungen (Verschlüsselung, HTTPS, WebDAV
an/aus), Schritte zum Nachstellen und was ein Angreifer dafür braucht (Zugang zum WLAN, ein
Benutzerkonto, die Rolle `admin`, den Stick in der Hand). Bitte keine echten Passwörter oder
Wiederherstellungsschlüssel mitschicken.

Das ist ein Freizeitprojekt mit einem Betreuer. Es gibt keine zugesagten Antwortzeiten; Meldungen
werden so bald wie möglich gelesen. Behoben wird nur in der jeweils neuesten Version.

## Geltungsbereich

Im Geltungsbereich:

- die Firmware unter `firmware/` (Weboberfläche und HTTP-API, Anmeldung, Sitzungen und Rollen,
  WebDAV, Firmware-Update, Laufwerks-Verschlüsselung, HTTPS, USB-Laufwerk und die Regel, dass das
  Laufwerk immer genau einen Besitzer hat),
- die Weboberfläche `firmware/printdrive/main/web/index.html`,
- die Skripte für Benutzer unter `tools/sendto/` und `tools/webdav/`.

Nicht im Geltungsbereich:

- Testserver und Testskripte (`tools/mock_server.py`, `tools/api_test.py`, `tools/ui_test.py`);
  sie laufen nur auf dem eigenen PC.
- Die unten genannten bekannten Grenzen, solange eine Meldung nicht zeigt, dass es schlimmer ist als
  beschrieben.
- Fehler in ESP-IDF, TinyUSB oder anderen Fremdkomponenten (bitte beim jeweiligen Projekt melden;
  ein Hinweis hier ist trotzdem willkommen, wenn die Firmware betroffen ist).

## Was die Laufwerks-Verschlüsselung schützt und was nicht

Quelle: [docs/12_api_v4.md](../docs/12_api_v4.md) Abschnitt 2.5 („Was die Verschlüsselung schützt“)
und [docs/06_risiken.md](../docs/06_risiken.md) Abschnitte 5 und 8. Die Verschlüsselung ist optional
und in der Voreinstellung aus (AES-256-XTS je 512-Byte-Sektor, Schlüssel aus dem Laufwerks-Passwort
abgeleitet).

Sie schützt:

- **gegen fremde Leser der Karte:** Wer die microSD-Karte oder den Stick ohne Laufwerks-Passwort in
  die Hand bekommt, sieht nur zufällig aussehende Daten.

Sie schützt nicht oder nur eingeschränkt:

- **Die Stärke des Passworts ist der eigentliche Schutz.** Wer die Karte hat, kann Passwörter offline
  und sehr schnell durchprobieren; kein Zähler des Geräts bremst dabei. Ein kurzes Passwort schützt
  nicht (Mindestlänge 12 Zeichen; der Wiederherstellungsschlüssel hat 160 Bit).
- Die **Wartezeit nach falschen Eingaben** bremst nur das Raten über das Netzwerk am laufenden Gerät;
  ein Neustart setzt sie zurück.
- **Solange das Laufwerk entsperrt ist**, sehen Drucker und PC die Dateien im Klartext.
- Das Verfahren **erkennt keine gezielten Änderungen** an den verschlüsselten Daten.
- **Wer Administratorzugriff hat, könnte geänderte Firmware aufspielen**, die das Passwort beim
  nächsten Entsperren mitliest (Stand 0.5.0: Updates sind nicht signiert). Der Schutz gilt gegen
  Diebstahl der Karte, nicht gegen jemanden, der das Gerät unbemerkt verändern kann.
- **Ohne HTTPS** gehen Laufwerks-Passwort, Anmelde-Passwörter und WebDAV-Zugangsdaten
  unverschlüsselt durchs WLAN. HTTPS ist eingebaut, aber in der Voreinstellung aus.

## Bekannte Grenzen (keine neue Sicherheitslücke)

- Gedacht für **vertrauenswürdige Netze**. In der Voreinstellung nur HTTP.
- **WLAN-Zugangsdaten und Einstellungen liegen unverschlüsselt im Flash** (NVS; keine
  NVS-Verschlüsselung, kein Flash Encryption, kein Secure Boot). Wer den Stick hat, kann sie per
  esptool auslesen oder eigene Firmware über USB aufspielen.
- **Zurücksetzen per Taste:** BOOT 10 Sekunden halten setzt die Benutzer auf `admin` / `admin`
  zurück (physischer Zugriff; das Laufwerks-Passwort öffnet das nicht).
- Das **Passwort des Einrichtungs-WLANs** steht auf dem Display.
- Der Stick nimmt **jeden Dateityp** an; eine Inhaltsprüfung gibt es nicht.
- **HTTPS** arbeitet mit einer eigenen CA des Geräts (Name Constraints auf `*.local`, den
  Gerätenamen und private IP-Bereiche); Browser warnen, bis sie installiert ist. Nur TLS 1.2, kein
  HSTS, eine TLS-Verbindung gleichzeitig.

---

## Reporting a vulnerability (English)

Please do **not** open a public issue. Report privately through GitHub instead: in this
repository go to **Security → Report a vulnerability**.

Useful details: firmware version, board variant, settings (encryption, HTTPS, WebDAV on/off),
steps to reproduce and what an attacker needs (Wi-Fi access, a user account, the `admin` role,
physical access). Please do not send real passwords or recovery keys.

This is a hobby project with a single maintainer. There are no guaranteed response times. Fixes
go into the latest version only.

**In scope:** the firmware in `firmware/` (web UI and HTTP API, login, sessions and roles, WebDAV,
firmware update, drive encryption, HTTPS, USB drive and the single-owner rule), the web UI, and
the user scripts in `tools/sendto/` and `tools/webdav/`. **Out of scope:** the mock server and
test scripts, the known limits listed above (unless it is worse than described), and bugs in
ESP-IDF, TinyUSB or other third-party components (report them upstream).

**What the drive encryption protects:** someone who gets the microSD card or the stick without
the drive password sees only random-looking data. **What it does not protect:** a weak password
(offline guessing against a removed card is not rate-limited); the wait time after wrong
passwords only slows guessing over the network and resets on reboot; while unlocked, printer and
PC see plain files; no detection of targeted modification of the ciphertext; an `admin` user
could install modified firmware that captures the password on the next unlock (as of 0.5.0
updates are not signed); without HTTPS, passwords cross the Wi-Fi in clear text (HTTPS is built
in but off by default). Sources: `docs/12_api_v4.md` section 2.5 and `docs/06_risiken.md`
sections 5 and 8 (German).
