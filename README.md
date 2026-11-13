# Meshtastic Desktop (offline)

Eine lokale PC-App für Meshtastic-Geräte, die ohne Internet läuft, inklusive Offline-Weltkarte. Chats und Einstellungen liegen in
`%APPDATA%\MeshtasticDesktop`.

## Verbindungsarten
| Art | Eingabe |
|---|---|
| **USB / Seriell** | COM-Port wählen (🔄 Suchen) oder leer lassen für automatische Erkennung |
| **WLAN / TCP** | IP oder Hostname, z. B. `192.168.1.50` oder `meshtastic.local:4403` |
| **Bluetooth LE** | 🔄 Suchen (ca. 10 s), dann Gerät wählen. Alternativ Adresse oder Gerätename eintippen (z. B. `AA:BB:CC:DD:EE:FF`, `BLE:Meshtastic_ab12`), Groß-/Kleinschreibung egal. „(erstes gefundenes Gerät)“ verbindet mit dem ersten Meshtastic-Gerät in Reichweite. Unter Windows das Gerät ggf. vorher in den Bluetooth-Einstellungen koppeln (PIN am Display, oft `123456`) |

## Funktionen
- **💬 Chats**: alle Kanäle (Gruppen) und Direktnachrichten. Zeigt Empfangsbestätigung (✓/✗), SNR/RSSI und ungelesene Nachrichten. Verlauf wird lokal gespeichert, CSV-Export.
- **📶 Knoten**: Liste mit Filter und Sortierung (Hardware, Rolle, SNR, Hops, Akku, zuletzt gehört, Position). Dazu Nachricht, Traceroute, Position anfordern, Favorit, Ignorieren, Entfernen, Kontakt-QR und Details.
- **🗺 Offline-Weltkarte** (unter *Knoten → Karte*): zeigt alle Knoten mit GPS-Position. Die Farbe zeigt, wann ein Knoten zuletzt gehört wurde (grün < 2 h, orange < 24 h, grau älter, blau = eigener Knoten).
  - Klick auf einen Knoten zeigt Infos: Name, Hardware, SNR, Hops, Akku, Höhe und die **Entfernung zu dir**.
  - Doppelklick öffnet eine Direktnachricht. Rechtsklick öffnet ein Menü mit Traceroute, Details, Koordinaten kopieren und „Feste Position hier setzen“.
  - Bedienung: Mausrad zoomt, Ziehen verschiebt. Dazu die Knöpfe „Alle Knoten“, „Mein Knoten“ und „Welt“, eine Maßstabsleiste und abschaltbare Ebenen.
  - **Eingebaute Vektorkarte** (Natural Earth, gemeinfrei): Länder, Bundesländer, Städte, Flüsse, Seen und Hauptstraßen. Funktioniert immer **komplett offline**, ohne Download.
  - **Mehr Details** über ⚙ neben „Hintergrund“:
    1. *MBTiles-Datei*: eine eigene Raster-Kartendatei (PNG/JPG), komplett offline, bis Straßenebene.
    2. *Kachel-Cache*: einmalig mit Internet einen Kartenausschnitt speichern, danach offline nutzen (max. 3000 Kacheln pro Vorgang; die Nutzungsregeln des Kachelservers beachten).
- **👥 Kanäle & QR**: Kanäle 0–7 anlegen, bearbeiten und löschen (Name, Rolle, PSK mit AES-128/256-Generator, MQTT Up-/Downlink, Stumm, Positionsgenauigkeit). QR-Code teilen (alle Kanäle oder nur Primär), als PNG speichern, URL kopieren. Kanal-URLs importieren (hinzufügen oder ersetzen) und Kontakt-URLs importieren.
- **⚙ Geräte-Einstellungen**: alle Felder aus Gerät, Position, Energie, Netzwerk/WLAN, Display, LoRa, Bluetooth und Sicherheit.
- **🧩 Modul-Einstellungen**: MQTT, Seriell, Ext. Benachrichtigung, Store & Forward, Reichweitentest, Telemetrie, Vorgefertigte Nachrichten, Audio, Remote-Hardware, Nachbar-Info, Umgebungslicht, Erkennungssensor, Paxcounter, Statusnachricht, Traffic-Management und TAK.
  Die Formulare entstehen automatisch aus den Meshtastic-Protobuf-Definitionen. Neue Firmware-Optionen erscheinen deshalb nach einem Update der `meshtastic`-Bibliothek von selbst.
- **🛠 Gerät**: Besitzername und Kurzname, feste Position, Uhrzeit setzen, vorgefertigte Nachrichten und Klingelton, Neustart, Herunterfahren, Knoten-DB zurücksetzen, Werksreset, DFU-Modus. Dazu **Backup und Wiederherstellung** der kompletten Konfiguration als JSON.
- **⚡ Firmware-Flasher** (Tab *Firmware* oder eigenständig `MeshtasticFlasher.exe`): flasht Meshtastic-Geräte **offline**, wie flasher.meshtastic.org.
  - **ESP32 / S3 / C3 / C6** (Heltec, T-Beam, Station G1/G2 …) über `esptool`:
    - *Update*: Die Einstellungen bleiben erhalten.
    - *Komplett neu installieren*: Der Flash wird gelöscht, dann werden factory.bin, OTA-App und LittleFS geschrieben, genau wie beim Web-Flasher.
  - **nRF52840 / RP2040 / RP2350** (RAK4631, T-Echo, Heltec T114, Pico …) über das UF2-Laufwerk:
    - Das Gerät wird automatisch in den Bootloader versetzt, per Meshtastic-Befehl oder 1200-Baud-Reset.
    - Bei Neuinstallation wird vorher ein *Factory Erase* ausgeführt.
  - **Verbundenes Gerät erkennen** wählt das passende Board automatisch aus. **Eigene Datei flashen** erkennt Chip, Image-Art und Version und schlägt die richtige Adresse vor.
  - **Heltec V2.0**: Die bereitgestellte Firmware 2.7.13 (`firmware_custom/heltec-v2_0`) ist fest eingebaut. Für den Heltec V2 gibt es keine offizielle Firmware mehr.
  - **Offline-Firmware**: Der Workflow *Firmware-Offline-Pakete* (Actions → Run workflow, außerdem jeden Montag) lädt die neueste stabile Version und die neueste Alpha herunter. Er legt sie unter *Releases* als ZIP je Chip-Familie ab. Im Flasher lassen sie sich über **Online laden…** abrufen oder als ZIP mit **Paket importieren…** einlesen. Danach ist kein Internet mehr nötig.
  - **📶 TAKweb-Firmware – das Gerät macht sein eigenes WLAN** (am einfachsten, ESP32: Heltec V2/V3, T-Beam, T-Beam S3, LilyGo T-LoRa/T3S3, XIAO ESP32-S3):
    1. Im Flasher **🌐 Online laden…** → Release *TAKweb-Firmware …* (Version endet auf `-takweb`).
    2. Gerät wählen (Name endet auf „– TAKweb-WLAN“) → **Komplett neu installieren** → **⚡ Firmware flashen**. TAKweb ist schon in der Firmware drin.
    3. Am Handy mit dem WLAN **TAKweb-xxxx** verbinden – jedes Gerät hat seinen eigenen Namen (letzte 4 Stellen der Gerätekennung), Passwort **12345678**. Im TAKweb-Admin lassen sich Name und Passwort beim Einrichten frei festlegen. Kommt „kein Internet“: trotzdem verbunden bleiben.
    4. Im Browser **https://192.168.4.1** öffnen (oder `https://takweb.local`) und das Zertifikat des Geräts einmal akzeptieren („Erweitert“ → „Weiter“). Als Lesezeichen oder auf dem Home-Bildschirm ablegen.

    Internet fürs Gerät (optional): in TAKweb unter Meshtastic → „Internet over WLAN“ ein WLAN mit Internet wählen (z. B. den Handy-Hotspot) – das Gerät verbindet sich zusätzlich damit, das eigene WLAN „TAKweb“ bleibt immer an. Bluetooth ist aus, solange das WLAN läuft (zu wenig Speicher für beides mit https).

    Kein Server, kein Hotspot nötig: Das Gerät hat immer die Adresse 192.168.4.1. Sein Namensdienst beantwortet nur „takweb“ – das Handy erkennt „WLAN ohne Internet“ und nutzt fürs Internet weiter die mobilen Daten. Über https gehen auch eigene Position, Kompass und Mikrofon (das Zertifikat ist selbst erstellt, weil es offline keine offizielle Beglaubigung geben kann). WLAN-Name und Passwort ändern: Abschnitt *4. TAKweb aufs Gerät* (Hotspot-Name/Passwort = Name/Passwort des Geräte-WLANs) oder in der Meshtastic-App unter Netzwerk. Gebaut wird die Firmware vom Workflow *TAKweb-Firmware* (Actions → Run workflow; Patch: `tools/takweb_firmware/`).
  - **🗺 TAKweb aufs Gerät** (Abschnitt *4. TAKweb aufs Gerät*, nur ESP32 – Heltec V2/V3, T-Beam, LilyGo, XIAO ESP32-S3 …): schreibt die Mesh-Ausgabe von [TAKweb](https://github.com/fredy060795/5036-TAKweb) in den Flash des Funkgeräts. Der Webserver der Meshtastic-Firmware liefert sie dann über WLAN aus. Ein Handy, das noch nie TAKweb geladen hat, bekommt so ohne Server und ohne Internet die Karte, Positionen, Chat, Marker und Sprachnachrichten über das Mesh.
    - **Paket**: `takweb-mesh-<version>.zip` aus dem TAKweb-Admin (*Meshtastic → TAKweb on a Meshtastic radio*, oder direkt `https://<server>/api/eud/meshsite.zip`) mit **📦 Paket (ZIP)…** einlesen, oder **🌐 Vom TAKweb-Server…** laden. Das Paket bleibt in der Bibliothek (`…\MeshtasticDesktop\takweb\`), danach geht es offline.
    - **Hotspot-Name / Passwort**: das WLAN, mit dem sich das Gerät verbindet – normalerweise der Handy-Hotspot. Vorgabe *TAKweb* / *12345678*. WPA2 braucht mindestens 8 Zeichen; leer = offenes WLAN. Am **iPhone** ist der Hotspot-Name der Gerätename (*Einstellungen → Allgemein → Info → Name*), also dort z. B. „TAKweb“ eintragen. Am **Android-Handy** lässt sich der Hotspot-Name frei wählen.
    - **Adresse**: Vorgabe ist die feste Adresse **172.20.10.14**. Ein iPhone-Hotspot vergibt immer Adressen aus 172.20.10.x, das Gerät ist dort also immer unter **https://172.20.10.14** erreichbar – am besten als Lesezeichen am Handy ablegen. Android-Hotspots wählen ihren Adressbereich meist bei jedem Einschalten neu: dort *automatisch (DHCP)* wählen und die IP vom Display des Geräts nehmen oder `https://meshtastic.local` versuchen (mDNS – je nach Handy). Eine andere feste IP lässt sich auch eintragen (Router = .1, /24).
      `127.0.0.1` geht nicht: Das ist immer das Handy selbst, TAKweb liegt aber auf dem Funkgerät.
    - **🗺 TAKweb aufspielen** über die normale USB-Verbindung (die Firmware muss schon laufen):
      1. Die alte Web-Oberfläche unter `/static` wird gelöscht. Einstellungen, Knoten und Schlüssel bleiben erhalten.
      2. Die TAKweb-Dateien (≈ 0,5 MB gzip) werden geschrieben und mit der Dateiliste des Geräts geprüft.
      3. Das WLAN wird gesetzt und das Gerät startet neu.

      Mit *nach dem Flashen automatisch aufspielen* passiert das direkt nach dem Flashen der Firmware.
    - **Am Handy**:
      1. Den Hotspot einschalten. Das Gerät verbindet sich.
      2. `https://172.20.10.14` öffnen (iPhone, feste Adresse) – sonst die IP vom Display des Geräts.
      3. Das Zertifikat einmal akzeptieren.

      TAKweb verbindet sich dann selbst mit „This radio (WLAN)“.
    - Solange WLAN an ist, ist Bluetooth am ESP32 aus (Firmware). Der Platz reicht ab 4 MB Flash (1 MB Dateisystem).
    - Ohne Oberfläche: `python takweb.py COM5 takweb-mesh.zip TAKweb 12345678 172.20.10.14`
- **Hop-Limit**: Es ist auf **1–7** begrenzt, weil höhere Werte den Node abstürzen lassen. Auch beim Wiederherstellen eines Backups wird der Wert begrenzt.
- **📜 Log**: App-Log und optional das Debug-Log des Geräts.

## Heltec Wireless Tracker: GPS geht nicht?
Vom Wireless Tracker gibt es drei Hardware-Versionen mit **unterschiedlicher GPS-Stromversorgung**:

| Aufdruck auf der Platine | Firmware wählen |
|---|---|
| `HTIT-Tracker V1.0` | **Heltec Wireless Tracker V1.0** (GPS-Strom über Pin 37) |
| `HTIT-Tracker V1.1` | Heltec Wireless Tracker V1.1 (über Vext, Pin 3) |
| V2 | Heltec Wireless Tracker V2 |

Der offizielle Web-Flasher bietet als „Wireless Tracker“ nur V1.1 an – auf einem **V1.0** bekommt das GPS damit nie Strom.
Die TAKweb-Firmware gibt es für alle drei Versionen. Außerdem: Das GPS braucht freien Himmel (der erste Fix dauert
draußen 1–5 Minuten) und muss in den Einstellungen an sein (Position → GPS-Modus „Aktiviert“).

## EXE und Installer bauen (Windows)
1. Python 3.10–3.13 von python.org installieren (Häkchen bei „Add to PATH“ setzen)
2. Optional für den Setup-Installer: [Inno Setup 6](https://jrsoftware.org/isdl.php) installieren
3. `build.bat` doppelklicken

Ergebnis:
- `Output\MeshtasticDesktop-Setup.exe`: Installer mit Startmenü, Desktop-Icon und Deinstallation
- `dist\MeshtasticDesktop-Portable.exe`: eine einzelne Datei, läuft ohne Installation
- `dist\MeshtasticFlasher.exe`: nur der Firmware-Flasher, als einzelne Datei

Internet braucht nur der Build-Schritt (pip). Die fertige EXE läuft komplett offline.

**Ohne Windows-PC**: Den Ordner in ein GitHub-Repo hochladen. Der Workflow `.github/workflows/build-windows.yml`
baut dann automatisch Setup und Portable-EXE (unter *Actions → Artifacts*).

## Direkt mit Python starten
```bash
pip install -r requirements.txt
python app.py
```

## Hinweise
- USB-Treiber: Manche Boards brauchen den CP210x- oder CH340-Treiber von Silabs bzw. WCH.
- Pro Gerät ist immer nur **eine** Verbindung gleichzeitig möglich. Handy-App oder Web-Client vorher trennen.
- Absturzprotokoll bei Fehlern in Treibern (z. B. Bluetooth): `%APPDATA%\MeshtasticDesktop\crash.log`, App-Log: `app.log`.
- Optional können QR-Codes aus Bilddateien eingelesen werden: `pip install opencv-python-headless`.

## Kartendaten
`worldmap.json.gz` wurde mit `tools_build_worldmap.py` aus [Natural Earth](https://www.naturalearthdata.com/) erzeugt (gemeinfrei).

## Treiber für den Flasher
- ESP32-Boards mit CP210x (Heltec V2, viele T-Beams) brauchen den Silabs-CP210x-Treiber, Boards mit CH340/CH9102 den WCH-Treiber.
- Lässt sich ein ESP32 nicht verbinden: Baudrate auf 115200 stellen, ein Datenkabel nutzen (kein reines Ladekabel), oder die BOOT/PRG-Taste halten und kurz RST drücken.
