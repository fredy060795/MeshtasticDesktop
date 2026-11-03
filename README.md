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
- **Hop-Limit**: Es ist auf **1–7** begrenzt, weil höhere Werte den Node abstürzen lassen. Auch beim Wiederherstellen eines Backups wird der Wert begrenzt.
- **📜 Log**: App-Log und optional das Debug-Log des Geräts.

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
