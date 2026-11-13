# Haftungsausschluss und Hinweise zur Nutzung

*English version below.*

## Privates Projekt

WiFi DataDrive ist ein privates Freizeitprojekt einer Einzelperson. Es ist **kein Produkt** von LILYGO,
Espressif Systems oder Bambu Lab und steht mit diesen in keiner Verbindung. Es gibt keinen
Support-Anspruch, keine zugesicherten Reaktionszeiten und keine Pflicht zur Pflege oder Weiterentwicklung.

## Keine Gewährleistung

Die Software wird **so bereitgestellt, wie sie ist** („as is“), ohne jede ausdrückliche oder
stillschweigende Gewährleistung – insbesondere ohne Zusicherung der Fehlerfreiheit, der Eignung für einen
bestimmten Zweck, der Verträglichkeit mit einem bestimmten USB-Gerät (etwa einem Drucker, Rechner oder
Fernseher) oder Programm oder mit künftigen Versionen von ESP-IDF oder der Firmware des angeschlossenen
Geräts. Getestet ist nur, was in der README und in
[docs/05_testprotokoll.md](docs/05_testprotokoll.md) als geprüft steht. Maßgeblich ist der Lizenztext in
[LICENSE](LICENSE).

## Keine Haftung

Soweit gesetzlich zulässig, haftet der Urheber nicht für Schäden, die aus der Nutzung oder der
Unmöglichkeit der Nutzung entstehen – weder für Datenverlust, Fehlfunktionen oder abgebrochene Vorgänge
am angeschlossenen Gerät (etwa missglückte oder abgebrochene Drucke), Materialverbrauch, Schäden am
angeschlossenen Gerät, an Rechner, Speicherkarte oder Dongle noch für Folgeschäden jeder Art.
Unberührt bleibt eine Haftung, die sich nach zwingendem Recht nicht ausschließen lässt (etwa bei Vorsatz).

## Benutzung auf eigene Gefahr

- **Sichere deine Dateien.** Der Stick ist kein Backup. Formatieren, das Einrichten und das Ausschalten der
  Verschlüsselung **löschen alle Dateien** auf der Karte. Ein Stromausfall während eines Schreibvorgangs
  kann Dateien oder das Dateisystem beschädigen. Bewahre wichtige Dateien zusätzlich an anderer Stelle auf.
- **Passwort und Wiederherstellungsschlüssel.** Gehen bei einem verschlüsselten Laufwerk das
  Laufwerks-Passwort **und** der Wiederherstellungsschlüssel verloren, sind die Daten **endgültig
  verloren**. Niemand, auch nicht der Urheber, kann sie wiederherstellen.
- **Prüfe, was das angeschlossene Gerät mit den Dateien macht.** Der Stick steckt in einem anderen Gerät,
  das die Dateien liest und verarbeitet. Kontrolliere die Datei und das Ergebnis am Gerät, an einem
  3D-Drucker zum Beispiel Datei, Druckerprofil und den Beginn des Drucks. Wer auf die Dateiverwaltung
  umschaltet oder das Laufwerk sperren lässt, nimmt dem Gerät das Laufwerk weg; liest es gerade davon,
  bricht der Vorgang ab, am Drucker etwa der Druck. Für den Betrieb des Geräts gelten die Hinweise seines
  Herstellers. Geprüft sind nur die Geräte, die in der README als geprüft stehen.
- **Sicherheitsfunktionen ohne Zusicherung.** Anmeldung, Verschlüsselung, HTTPS und signierte Updates sind
  nach bestem Wissen gebaut, aber von keiner unabhängigen Stelle geprüft oder zertifiziert. Ihre Grenzen
  stehen in der README, in [docs/06_risiken.md](docs/06_risiken.md) und in
  [.github/SECURITY.md](.github/SECURITY.md). Verlass dich nicht allein darauf, wenn es um vertrauliche
  Daten geht.
- **Netzwerk.** In der Voreinstellung spricht der Stick unverschlüsseltes HTTP. Betreibe ihn nur in
  Netzen, denen du vertraust.
- **Firmware flashen.** Beim Flashen wird der Speicher des Dongles überschrieben, auch die Werks-Firmware
  von LILYGO. Das geschieht auf eigene Gefahr.
- **Funkanlage.** Der Dongle sendet im 2,4-GHz-Band. Für den rechtmäßigen Betrieb an deinem Ort, auch mit
  selbst gebauter oder veränderter Firmware, bist du selbst verantwortlich.

## Daten

WiFi DataDrive nutzt keine Cloud, kein Konto und keine Telemetrie. Die einzige Verbindung ins Internet
ist das Stellen der Uhr per NTP. Auf dem Stick liegen deine Dateien; im Einstellungsspeicher des Sticks
liegen Benutzer (als gesalzener Passwort-Hash), Einstellungen und die WLAN-Zugangsdaten, diese
**unverschlüsselt**. Wer den Stick in der Hand hat, kann diesen Speicher per USB auslesen. Wer den Stick
oder die Karte weitergibt, gibt deren Inhalt weiter.

## Marken

LILYGO ist eine Marke ihres Inhabers. Espressif und ESP32 sind Marken von Espressif Systems. Bambu Lab ist
eine Marke von Bambu Lab. Alle weiteren Namen gehören ihren jeweiligen Inhabern und werden nur zur
Beschreibung der Zusammenarbeit genannt.

## Kein Rechtsrat

Dieser Text ist ein Hinweis des Urhebers, keine Rechtsberatung. Verbindlich ist die Lizenz in
[LICENSE](LICENSE); für das Maskottchen LIO gilt [LICENSE-LIO.md](LICENSE-LIO.md).

---

# Disclaimer and Notes on Use (English)

## Private project

WiFi DataDrive is a private spare-time project of an individual. It is **not a product** of LILYGO,
Espressif Systems or Bambu Lab and is not affiliated with any of them. There is no entitlement to support,
no promised response time and no obligation to maintain or extend it.

## No warranty

The software is provided **"as is"**, without warranty of any kind, express or implied – in particular
without any assurance of being free of defects, fit for a particular purpose, compatible with a particular
USB host (such as a printer, computer or TV) or program, or with future versions of ESP-IDF or of the
connected device's firmware. Only what the README
and [docs/05_testprotokoll.md](docs/05_testprotokoll.md) list as tested has been tested. The licence text
in [LICENSE](LICENSE) governs.

## No liability

To the extent permitted by law, the author is not liable for any damage arising from the use or the
inability to use the software – including loss of data, malfunctions or aborted operations on the
connected device (such as failed or aborted prints), material consumption, damage to the connected device,
computer, memory card or dongle, or consequential damage of any kind. Liability
that cannot be excluded under mandatory law (for example for intent) remains unaffected.

## Use at your own risk

- **Back up your files.** The stick is not a backup. Formatting, setting up and switching off encryption
  **erase all files** on the card. A power loss during a write can damage files or the file system. Keep
  important files elsewhere as well.
- **Password and recovery key.** If both the drive password **and** the recovery key of an encrypted drive
  are lost, the data is **gone for good**. Nobody, including the author, can recover it.
- **Check what the connected device does with the files.** The stick sits in another device that reads
  and processes the files. Check the file and the result on the device, on a 3D printer for example
  the file, the printer profile and the start of the print. Switching to the file manager or letting the
  drive lock takes the drive away from the device; if it is reading, the operation stops, on a printer the
  print, for example. The device manufacturer's instructions apply to operating the device. Only the
  devices the README lists as tested have been tested.
- **Security features without guarantee.** Login, encryption, HTTPS and signed updates are built to the
  best of the author's knowledge but have not been reviewed or certified by any independent party. Their
  limits are described in the README, in [docs/06_risiken.md](docs/06_risiken.md) and in
  [.github/SECURITY.md](.github/SECURITY.md). Do not rely on them alone for confidential data.
- **Network.** By default the stick speaks unencrypted HTTP. Use it only on networks you trust.
- **Flashing firmware.** Flashing overwrites the dongle's memory, including LILYGO's factory firmware. You
  do this at your own risk.
- **Radio equipment.** The dongle transmits in the 2.4 GHz band. You are responsible for operating it
  lawfully where you are, also with self-built or modified firmware.

## Data

WiFi DataDrive uses no cloud, no account and no telemetry. The only internet connection is setting the
clock via NTP. Your files are on the stick; the stick's settings storage holds the users (as salted
password hashes), the settings and the Wi-Fi credentials, the latter **unencrypted**. Anyone holding the
stick can read this storage over USB. Whoever passes the stick or the card on passes its content on.

## Trademarks

LILYGO is a trademark of its owner. Espressif and ESP32 are trademarks of Espressif Systems. Bambu Lab is
a trademark of Bambu Lab. All other names belong to their respective owners and are mentioned only to
describe interoperability.

## Not legal advice

This text is a notice by the author, not legal advice. The licence in [LICENSE](LICENSE) is binding; the
mascot LIO is covered by [LICENSE-LIO.md](LICENSE-LIO.md).
