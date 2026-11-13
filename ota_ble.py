# -*- coding: utf-8 -*-
"""
Firmware-Update ohne USB (z. B. eingegossene Geräte): über den Meshtastic-OTA-Lader (esp32-unified-ota, ab Firmware 2.7.18).

Ablauf:
  1. Über die bestehende Verbindung (Bluetooth oder WLAN/TCP) bekommt das Gerät eine Admin-Nachricht
     ota_request {reboot_ota_mode = OTA_BLE, ota_hash = SHA-256 der neuen Firmware}. Es merkt sich den Hash und startet in
     den OTA-Lader.
  2. Der Lader meldet sich per Bluetooth als „Meshtastic_XXXX“ (letzte 4 Stellen der MAC) mit dem OTA-Dienst.
  3. Hierher: „OTA <größe> <sha256>\\n“ → „ERASING“ … „OK“ → Firmware in Stücken, jedes wird mit „ACK“ bestätigt →
     „OK“: Der Lader prüft den Hash, macht die neue Firmware startbar und startet neu.

Nur das App-Abbild (firmware-*.bin) wird erneuert – Einstellungen, Knoten und die Dateien (z. B. TAKweb) bleiben.
"""
import asyncio
import hashlib

SVC = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
TX = "62ec0272-3ec5-11eb-b378-0242ac130003"    # Gerät → hier (Notify): OK, ERR, ACK, ERASING
OTA = "62ec0272-3ec5-11eb-b378-0242ac130005"   # hier → Gerät: Befehle und Firmware


class OTAError(Exception):
    pass


def check_image(data):
    """ESP32-App-Abbild? (beginnt mit 0xE9) – sonst Fehlertext"""
    if not data or data[0] != 0xE9:
        return "Das ist kein ESP32-Firmware-Abbild (firmware-*.bin)."
    return ""


def loader_name(node_num):
    """Bluetooth-Name des OTA-Laders: Meshtastic_<letzte 4 Stellen der MAC> = untere 16 Bit der Knotennummer"""
    return "Meshtastic_%04X" % (node_num & 0xFFFF) if node_num else ""


def request_ota(iface, data):
    """Schickt dem verbundenen Gerät den OTA-Befehl (Bluetooth-Modus) mit dem Hash der neuen Firmware."""
    from meshtastic.protobuf import admin_pb2
    node = iface.localNode
    if not hasattr(node, "startOTA"):
        raise OTAError("Die meshtastic-Bibliothek ist zu alt (pip install -U meshtastic).")
    node.startOTA(ota_mode=admin_pb2.OTAMode.OTA_BLE, ota_file_hash=hashlib.sha256(data).digest())


async def _upload(data, log, progress, name, find_timeout):
    from bleak import BleakClient, BleakScanner

    def fits(dev, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        return SVC in uuids and (not name or (dev.name or adv.local_name or "") == name)

    log(f"Suche den OTA-Lader per Bluetooth{f' ({name})' if name else ''} …\n")
    dev = await BleakScanner.find_device_by_filter(fits, timeout=find_timeout)
    if dev is None:
        raise OTAError("Der OTA-Lader wurde per Bluetooth nicht gefunden. Ist Bluetooth am PC an und das Gerät in der Nähe?")
    log(f"Gefunden: {dev.name or dev.address}\n")
    lines = asyncio.Queue()
    buf = bytearray()

    def on_notify(_, chunk):
        buf.extend(chunk)
        while b"\n" in buf:
            i = buf.index(b"\n")
            line = bytes(buf[:i]).decode("utf-8", "replace").strip()
            del buf[:i + 1]
            if line:
                lines.put_nowait(line)

    async def answer(timeout, wanted=("OK",)):
        while True:
            line = await asyncio.wait_for(lines.get(), timeout)
            if line.startswith("ERR"):
                raise OTAError(f"Das Gerät meldet: {line}")
            if line.split(" ")[0] in wanted:
                return line

    async with BleakClient(dev, timeout=30) as client:
        await client.start_notify(TX, on_notify)
        sha = hashlib.sha256(data).hexdigest()
        await client.write_gatt_char(OTA, f"OTA {len(data)} {sha}\n".encode(), response=True)
        log("Gerät löscht den Speicher …\n")
        await answer(120)                                  # ERASING … OK
        size = max(20, min(512, (getattr(client, "mtu_size", 23) or 23) - 3))
        log(f"Übertrage {len(data)} Bytes in Stücken zu {size} Bytes …\n")
        for i in range(0, len(data), size):
            await client.write_gatt_char(OTA, data[i:i + size], response=False)
            last = i + size >= len(data)
            if not last:
                await answer(20, ("ACK",))
            progress(min(len(data), i + size), len(data))
        log("Prüfe die Firmware …\n")
        await answer(90)                                   # nach dem letzten Stück: OK (Hash geprüft) oder ERR
    log("Firmware übernommen – das Gerät startet neu.\n")


def upload(data, log=print, progress=lambda done, total: None, name="", find_timeout=90):
    """Lädt die Firmware per Bluetooth in den OTA-Lader (blockiert, z. B. in einem Thread aufrufen)."""
    try:
        asyncio.run(_upload(data, log, progress, name, find_timeout))
    except asyncio.TimeoutError:
        raise OTAError("Zeitüberschreitung bei der Übertragung – bitte nochmal versuchen (der Lader wartet weiter).")
