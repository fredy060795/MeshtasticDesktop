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
- **📜 Log**: App-Log und optional das Debug-Log des Geräts.

## EXE und Installer bauen (Windows)
1. Python 3.10–3.13 von python.org installieren (Häkchen bei „Add to PATH“ setzen)
2. Optional für den Setup-Installer: [Inno Setup 6](https://jrsoftware.org/isdl.php) installieren
3. `build.bat` doppelklicken

Ergebnis:
- `Output\MeshtasticDesktop-Setup.exe`: Installer mit Startmenü, Desktop-Icon und Deinstallation
- `dist\MeshtasticDesktop-Portable.exe`: eine einzelne Datei, läuft ohne Installation

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
