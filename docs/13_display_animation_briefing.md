# Briefing: Animationen für das Display des WiFi DataDrive

Dieses Dokument richtet sich an die Person, die die Animationen entwirft. Es beschreibt das Gerät, das Display, die verfügbare Fläche und das Lieferformat. Vorwissen zum Projekt ist nicht nötig.

## Das Gerät

WiFi DataDrive ist ein USB-Stick mit WLAN. Er steckt in einem 3D-Drucker oder PC und erscheint dort als normales Laufwerk. Über eine Weboberfläche lädt man vom Handy oder Laptop Dateien darauf. Am Stick sitzt ein kleines Farbdisplay, das den Zustand zeigt. Man sieht es aus 30 bis 100 cm Entfernung, oft schräg und nur mit einem kurzen Blick.

## Hardware und System

| Punkt | Wert |
|---|---|
| Board | LILYGO T-Dongle-S3 |
| Prozessor | ESP32-S3, kein Grafikchip, kein Zusatzspeicher |
| Display | IPS-LCD, 0,96 Zoll, Ansteuerung ST7735 |
| Auflösung | 160 x 80 Pixel, Querformat |
| Sichtbare Fläche | ca. 21,7 x 10,8 mm, ein Pixel ist etwa 0,135 mm groß |
| Farben | RGB565 (65 536 Farben: 5 Bit Rot, 6 Bit Grün, 5 Bit Blau) |
| Transparenz | keine; es gibt nur deckende Pixel |
| Grafiksystem | eigener einfacher Bildspeicher, keine Grafikbibliothek. Vorhanden sind gefüllte Rechtecke und eine 5x7-Pixelschrift. Bilder werden als Pixelraster einkopiert. |
| Bildaufbau | Das ganze Bild wird jedes Mal neu übertragen; Tempo siehe unten |

Es gibt keine Kantenglättung, keine Drehung, keine Skalierung und keine Überblendung zur Laufzeit. Alles, was sich bewegt, muss als fertige Einzelbilder geliefert werden.

## Aufbau des Bildschirms

Der Hintergrund ist schwarz. Oben liegt eine 16 Pixel hohe Titelleiste in der Zustandsfarbe, darunter stehen Textzeilen in 8 Pixel hoher Schrift.

```
x: 0                                   108            156 160
   +--------------------------------------------------------+ y 0
   | USB                                             WLAN   |      Titelleiste, 16 px
   +--------------------------------------------------------+ y 16
   | 192.168.2.152                      +----------------+  | y 18
   | datadrive.local                    |                |  |
   |                                    |   ANIMATION    |  |
   | SD 58.2/59.5GB                     |    48 x 40     |  |
   | [=====-------------]               |                |  |
   |                                    +----------------+  | y 58
   | Gerät liest...                                         |      Statuszeile, volle Breite
   +--------------------------------------------------------+ y 80
```

## Die Fläche für die Animation

| Punkt | Wert |
|---|---|
| Größe | **48 x 40 Pixel** (Breite x Höhe) |
| Position | linke obere Ecke bei x = 108, y = 18 |
| Hintergrund | reines Schwarz (0, 0, 0), randlos |
| Rand | Das Motiv hält innen 1 Pixel Abstand zu allen Kanten |

Die Fläche ist fest. Größere Motive sind nicht möglich, weil links die Adresse und unten die Statuszeile lesbar bleiben müssen.

## Zustände

Für jeden Zustand eine eigene Animation als Endlosschleife. Die Farbe gehört zum Zustand und ist dieselbe wie die der Titelleiste.

| Nr. | Zustand | Idee für das Motiv | Hauptfarbe (R, G, B) |
|---|---|---|---|
| 1 | Bereit für Upload | ruhiges Symbol, langsames Pulsieren | Blau (30, 90, 220) |
| 2 | Laufwerk am Drucker/PC bereitgestellt | USB-Symbol, langsames Pulsieren | Grün (30, 170, 60) |
| 3 | Upload läuft | Pfeile laufen in den Stick hinein | Orange (240, 140, 0) |
| 4 | Drucker/PC liest oder schreibt | Pfeile laufen aus dem Stick heraus bzw. hinein | Grün (30, 170, 60) |
| 5 | Lange Aufgabe (Kopieren, Formatieren, Update) | drehender Ring | Orange (240, 140, 0) |
| 6 | Laufwerk gesperrt (Verschlüsselung) | geschlossenes Schloss, ruhig | Gelb (240, 220, 0) |
| 7 | Entsperrt (einmalig, keine Schleife) | Schloss springt auf, danach Wechsel zu 1 oder 2 | Grün (30, 170, 60) |
| 8 | WLAN wird gesucht oder Einrichtung | wandernde Funkwellen | Cyan (0, 190, 210) |
| 9 | Fehler | Warnsymbol, langsames Blinken | Rot (220, 30, 30) |

Weitere feste Farben des Geräts: Weiß (255, 255, 255) und Grau (110, 110, 110). Sie dürfen als Nebenfarben vorkommen.

## Gestaltungsregeln

- **Pixelgenau entwerfen**, in Originalgröße 48 x 40. Nicht größer zeichnen und verkleinern.
- **Harte Kanten, keine Kantenglättung.** Halbtöne an Kanten werden auf dem kleinen Display zu Schmutz.
- **Linien mindestens 2 Pixel stark.** 1-Pixel-Linien sind aus Armlänge kaum zu sehen.
- **Höchstens 4 Farben je Animation** einschließlich Schwarz. Keine Verläufe: RGB565 zeigt sie mit Stufen.
- **Ein Motiv, eine Bewegung.** Die Animation muss auch als Standbild sofort lesbar sein.
- **Ruhig bleiben.** Der Stick steckt oft dauerhaft im Blickfeld. Kein Flackern, kein schnelles Blinken; Fehler höchstens einmal pro Sekunde.
- **Schleifen nahtlos:** Das letzte Bild geht ohne Sprung in das erste über.
- Keine Schrift in der Animation, der Text steht daneben.

## Technische Grenzen

| Punkt | Vorgabe |
|---|---|
| Bildrate | 10 Bilder pro Sekunde (100 ms je Bild). Technisch möglich sind höchstens 20. |
| Bilder je Animation | 4 bis 12 |
| Dauer einer Schleife | 0,8 bis 2 Sekunden |
| Speicher | zusammen für alle Animationen höchstens 100 kB unkomprimiert. Ein Bild mit 48 x 40 Pixeln belegt 3,8 kB; das reicht für rund 25 Vollfarb-Bilder oder ein Vielfaches davon mit Farbpalette. |

## Lieferformat

- Je Animation **eine PNG-Datei als Filmstreifen**: alle Bilder nebeneinander, jedes genau 48 x 40 Pixel, ohne Abstand. Eine Animation mit 8 Bildern ist also 384 x 40 Pixel groß.
- PNG ohne Transparenz, Hintergrund reines Schwarz, 8 Bit je Farbkanal, keine Farbprofile.
- Dateiname: `anim_<nr>_<name>.png`, zum Beispiel `anim_3_upload.png`.
- Dazu eine kurze Liste: Dateiname, Anzahl Bilder, Dauer je Bild in Millisekunden, Schleife ja/nein.
- Zur Kontrolle je Animation eine Vorschau als GIF in 6-facher Vergrößerung **ohne Glättung** (jeder Pixel wird ein 6 x 6 Quadrat).

## Was nicht gebraucht wird

Titelleiste, Texte, Fortschrittsbalken und der QR-Code-Bildschirm existieren bereits und sind nicht Teil des Auftrags.
