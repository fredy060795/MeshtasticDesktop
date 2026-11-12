#!/usr/bin/env python3
"""
Packt die gebauten TAKweb-Firmwares (Ordner mit firmware-*.mt.json, *.factory.bin, *.bin, littlefs-*.bin)
als Offline-Pakete für den Flasher – im selben Format wie tools/fetch_firmware.py (ZIP je Chip-Familie).

    python tools/takweb_firmware/pack.py --src build --version 2.7.15.567b8ea --out out
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_firmware  # noqa: E402

NOTES = """**TAKweb-Firmware** für Meshtastic-Geräte mit ESP32 – das Gerät öffnet ein **eigenes WLAN** mit TAKweb.

- Firmware: Meshtastic `{fw}` + TAKweb `{tw}`
- Geräte: {devices}

**Einfach so:**
1. Im Flasher (Meshtastic Desktop): *Online laden…* → dieses Release, oder ZIP herunterladen und *Paket importieren…*
2. Gerät wählen (Version „{version}“) → **Komplett neu installieren** → *Firmware flashen*.
3. Am Handy mit dem WLAN **TAKweb** verbinden (Passwort **12345678**). TAKweb öffnet sich von selbst
   (Anmeldeseite) – sonst im Browser **http://takweb/** oder **http://192.168.4.1** öffnen.

Kein Internet, kein Server, kein Handy-Hotspot nötig. Bluetooth ist am Gerät aus, solange das WLAN an ist.
WLAN-Name/Passwort lassen sich im Flasher (Abschnitt 4) oder in der Meshtastic-App ändern (Netzwerk).
Region (LoRa) nach der Neuinstallation einstellen!
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--version", required=True, help="Meshtastic-Version, z. B. 2.7.15.567b8ea")
    ap.add_argument("--takweb", default="?")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    version = a.version.lstrip("v") + "-takweb"
    names = []
    for fn in sorted(os.listdir(a.src)):
        if fn.endswith(".mt.json"):
            p = os.path.join(a.src, fn)
            m = json.load(open(p, encoding="utf-8"))
            if "TAKweb" not in (m.get("displayName") or ""):
                m["displayName"] = f"{m.get('displayName') or m.get('platformioTarget')} – TAKweb-WLAN"
            m["version"] = version
            json.dump(m, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
            names.append(m["displayName"])
    if not names:
        sys.exit("Keine gebaute Firmware gefunden.")
    dest = fetch_firmware.pack(a.src, version, "TAKweb", f"Meshtastic {a.version} mit TAKweb-WLAN", a.out)
    meta_p = os.path.join(dest, "index.json")
    meta = json.load(open(meta_p, encoding="utf-8"))
    meta["source"] = "https://github.com/meshtastic/firmware + tools/takweb_firmware"
    json.dump(meta, open(meta_p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    with open(os.path.join(dest, "release.md"), "w", encoding="utf-8") as f:
        f.write(NOTES.format(fw=a.version, tw=a.takweb, version=version, devices=", ".join(sorted(names))))
    with open(os.path.join(a.out, "version.txt"), "w") as f:
        f.write(version)
    print("Fertig:", dest)


if __name__ == "__main__":
    main()
