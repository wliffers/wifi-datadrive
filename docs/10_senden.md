# Dateien an das WiFi DataDrive senden

Es gibt drei Wege, Dateien auf das Laufwerk zu bringen: die Weboberfläche im Browser, den Windows-Eintrag „Senden an“ (siehe `tools/sendto/README.md`) und einen Kurzbefehl auf dem iPhone. Dieses Dokument beschreibt die Weboberfläche als App-Icon und den iPhone-Kurzbefehl.

## App-Icon auf dem iPhone

1. iPhone und DataDrive müssen im selben WLAN sein.
2. In Safari die Adresse des DataDrive öffnen, z. B. `http://datadrive.local` oder `http://192.168.2.152`.
3. Unten auf **Teilen** tippen.
4. **Zum Home-Bildschirm** wählen und mit **Hinzufügen** bestätigen.

Das Icon öffnet die Weboberfläche danach ohne Browser-Leiste. Das ist auf einem echten Handy
**noch nicht geprüft**; Stand 0.3.0 ist das Icon im Quelltext (Web-App-Manifest) vorhanden.

## iPhone-Kurzbefehl einrichten

Vorab: Die Adresse des Geräts brauchst du in Schritt 5e. Für das Passwort gilt Abschnitt „Hinweise“.

1. Die App **Kurzbefehle** öffnen, Reiter **Kurzbefehle**, oben rechts auf **+** tippen.
2. Oben auf den Namen tippen und `An DataDrive senden` eintragen.
3. Auf das Einstellungs-Symbol tippen (Schieberegler bzw. „i“, je nach iOS-Version), dann **Details** öffnen.
4. **Im Share-Sheet anzeigen** einschalten.
5. Bei **Eingabe-Typen** **Dateien** aktivieren. Optional zusätzlich **Bilder**.
6. Die Aktionen unten in dieser Reihenfolge hinzufügen. Über **Aktion hinzufügen** und das Suchfeld findest du jede Aktion.

   **a) Text**: `handy:geheim` eintragen, mit dem Benutzer und Passwort aus dem Benutzerkonto für das Handy (siehe „Hinweise“).

   **b) Base64-codieren**: Eingabe ist das Ergebnis aus a. Bei **Zeilenumbrüche** den Wert **Keine** wählen.

   **c) Name abrufen**: Eingabe ist die **Kurzbefehl-Eingabe** (die gesendete Datei).
   Danach **Details von Dateien abrufen**: Eingabe ist wieder die **Kurzbefehl-Eingabe**, Detail **Dateierweiterung**.
   Danach eine **Text**-Aktion, die aus den Variablen **Name**, einem Punkt und **Dateierweiterung** besteht, also `Name.Erweiterung`.
   Ergibt der Name bei deinem Test schon die Endung, entfallen die Erweiterungs-Schritte. Das musst du beim ersten Test prüfen.
   Soll die Datei in einen Ordner, schreibst du vor die Variablen den Ordnernamen mit Schrägstrich, also `Ordner/` gefolgt von `Name.Erweiterung`. Der Ordner muss auf dem Laufwerk bereits existieren, sonst schlägt der Upload fehl.

   **d) URL-codieren**: Eingabe ist das Ergebnis aus c.

   **e) Inhalte von URL abrufen**:
   - **URL**: Den Text `http://<Adresse>/api/push?path=` eintragen, `<Adresse>` durch die Adresse des Geräts ersetzen. Danach die Variable **URL-codiert** aus d einfügen.
   - **Methode**: **POST**.
   - **Header** (über **Neues Feld** / **Header**) zwei Felder anlegen:
     - `X-PrintDrive` = `1`
     - `Authorization` = `Basic ` (mit Leerzeichen am Ende), danach die Variable **Base64-codiertes Ergebnis** aus b einfügen.
   - **Anfrage-Text**: Typ **Datei**, Wert **Kurzbefehl-Eingabe**. So wird die Datei als roher Inhalt gesendet.

   **f) Wörterbuchwert abrufen**: Eingabe ist das Ergebnis aus e, Schlüssel `ok`.
   **Wenn** dieser Wert wahr ist, **Mitteilung anzeigen** mit dem Text `Gespeichert`.
   **Sonst**: Wörterbuchwert abrufen mit Schlüssel `error` aus e, danach **Mitteilung anzeigen** mit diesem Text.
   Statt **Mitteilung anzeigen** kannst du auch **Ergebnis einblenden** verwenden.

7. Oben auf **Fertig** tippen.

Die Adresse trägst du nur in Schritt 6e ein. Ändert sich die Adresse des Geräts (etwa bei DHCP), muss dort der Eintrag angepasst werden.

## Benutzen

1. In der Dateien-App oder einer anderen App die Datei antippen.
2. **Teilen** wählen.
3. **An DataDrive senden** antippen.

Ist alles richtig, erscheint „Gespeichert“. Sonst erscheint der Fehlertext aus der Tabelle unten.

## Wenn es nicht klappt

Die Meldungen stammen aus der Antwort des Geräts (`code` und `error`).

| Meldung / Code | Bedeutung | Was tun |
|---|---|---|
| `UNAUTHORIZED` / `BAD_LOGIN` | Benutzer oder Passwort falsch, oder der Header fehlt | Schritt 6a und 6b prüfen. Im Header `Authorization` muss `Basic ` (mit Leerzeichen) vor dem Base64-Text stehen. |
| `LOCKED` | Nach 5 Fehlversuchen in Folge gesperrt (Anmeldesperre je Adresse) | Die Antwort nennt die Wartezeit im Header `Retry-After`. Sie beginnt bei 30 Sekunden und verdoppelt sich bei jeder neuen Sperre, höchstens 15 Minuten. Während der Wartezeit prüft das Gerät gar nichts. Nach Ablauf setzt eine richtige Anmeldung Zähler und Dauer zurück. |
| `DRIVE_LOCKED` | Das Laufwerk ist verschlüsselt und gesperrt (seit 0.4.0) | Laufwerk im Browser mit dem Laufwerks-Passwort entsperren, dann erneut senden. Das Anmelde-Passwort reicht dafür nicht. |
| `MUST_CHANGE_PASSWORD` | Das Konto `admin` hat noch das Auslieferungspasswort | Im Browser einmal anmelden und das Passwort ändern. Ohne Änderung ist keine Anfrage erlaubt, auch nicht per Kurzbefehl. |
| `HOST_ACTIVE` | Das USB-Gerät nutzt das Laufwerk | Am USB-Gerät (z. B. Drucker) zuerst auswerfen bzw. die Übernahme dort bestätigen. Notfalls in der URL `&force=1` anhängen. Ein laufender Druck von dem Stick wird dann unterbrochen. |
| `HOST_WRITING` | Das USB-Gerät schreibt gerade auf das Laufwerk | Warten und später erneut senden. Diese Sperre lässt sich nicht erzwingen. |
| `NO_SPACE` | Nicht genug freier Speicher | Dateien auf dem Laufwerk löschen oder eine microSD verwenden. |
| `TOO_LARGE` | Datei ist 4 GB oder größer | Die Grenze gilt pro Datei (FAT32). Die Datei muss kleiner sein. |
| `INVALID_FILE` | Datei ist leer oder ungültig, z. B. eine `.3mf`, die kein ZIP ist | Datei auf dem iPhone prüfen. |
| `keine Verbindung` | iPhone und Gerät sind nicht im selben WLAN, oder die Adresse in Schritt 6e stimmt nicht | WLAN des iPhones prüfen. Adresse im Browser testen. |

Fehlt der Ordner im Zielpfad, kommt `NOT_FOUND`. Den Ordner dann zuerst auf dem Laufwerk anlegen.

## Hinweise

- **Passwort**: Das Passwort steht im Kurzbefehl im Klartext und ist lesbar, wenn man den Kurzbefehl öffnet. Die Verbindung läuft über HTTP, also unverschlüsselt im WLAN. Empfehlung: Für das iPhone einen eigenen Benutzer mit der Rolle `user` anlegen, nicht das Administrator-Konto verwenden. Anlegen geht in der Weboberfläche unter Benutzer. Die Rolle `user` darf Dateien senden, aber kein WLAN und keine Benutzer verwalten.
- **Kein fertiger Kurzbefehl als Datei**: Apple erlaubt es nicht, fertige Kurzbefehle als Datei weiterzugeben. Deshalb müssen die Schritte von Hand eingegeben werden.
- **Namen der Aktionen**: Die Aktionsnamen können sich zwischen iOS-Versionen leicht unterscheiden.
- **Nicht getestet**: Diese Schritte sind aus der API-Beschreibung (`docs/09_api_v3.md`, Abschnitte 2.2 und 3.2) abgeleitet. Sie wurden nicht auf einem echten iPhone getestet. Beim ersten Versuch ist mit Abweichungen zu rechnen.
