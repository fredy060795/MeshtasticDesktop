# -*- coding: utf-8 -*-
"""
TAKweb aufs Meshtastic-Gerät (ESP32) – ohne Server, ohne Internet.

Die Meshtastic-Firmware hat einen eigenen Webserver: Dateien aus /static im Flash werden über WLAN ausgeliefert
(gzip: name.gz), dazu das Funkgerät über HTTP (/api/v1/toradio, /api/v1/fromradio). Dieses Modul schreibt die
Mesh-Ausgabe von TAKweb dorthin – über die normale USB-Verbindung (seriell), genauso wie TAKweb selbst es im
Admin-Bereich macht (public/eud/meshinstall.js):

  1. Zustand lesen (want_config): eigene Knotennummer, Dateien im Flash, WLAN-Einstellungen
  2. alte Dateien unter /static löschen (XModem: SOH seq 0 + CAN)
  3. TAKweb-Dateien als /static/<name>.gz schreiben (XModem, 128-Byte-Pakete mit CRC-16)
  4. prüfen (Dateiliste des Geräts – die Firmware bestätigt auch, wenn der Flash voll ist)
  5. WLAN setzen (z. B. Handy-Hotspot) und Neustart

Danach: Handy-Hotspot einschalten, das Gerät verbindet sich, im Browser https://<Adresse des Geräts> öffnen.
Mit fester Adresse im iPhone-Hotspot immer https://172.20.10.14 (IPHONE_IP).

Das Paket („takweb-mesh-<version>.zip“) gibt es im TAKweb-Admin (Meshtastic → „TAKweb on a Meshtastic radio“)
oder direkt vom TAKweb-Server: https://<server>/api/eud/meshsite.zip
"""
import io
import ipaddress
import json
import os
import queue
import random
import threading
import time
import urllib.request
import zipfile

CHUNK = 128               # XModem-Puffer der Firmware
MAGIC = b"\x94\xc3"       # Rahmen der seriellen Client-API: 0x94 0xC3 <Länge 2 Byte> <Protobuf>
MAX_FRAME = 512
PACKAGE_INFO = "takweb-mesh.json"

# Feste Adresse im iPhone-Hotspot: das iPhone vergibt immer 172.20.10.0/28 (Router 172.20.10.1, Geräte ab .2).
# .14 ist die letzte Adresse – die bekommt per DHCP erst das 13. Gerät, also praktisch nie ein anderes.
IPHONE_IP = "172.20.10.14"


def static_net(ip):
    """feste IP -> (ip, gateway, subnet, dns) als Text; iPhone-Hotspot /28, sonst /24 mit Router .1"""
    a = ipaddress.IPv4Address(ip.strip())
    if a in ipaddress.IPv4Network("172.20.10.0/28"):
        return str(a), "172.20.10.1", "255.255.255.240", "172.20.10.1"
    gw = str(ipaddress.IPv4Network(f"{a}/24", strict=False).network_address + 1)
    if gw == str(a):
        raise ValueError("die Adresse ist die des Routers")
    return str(a), gw, "255.255.255.0", gw


def ip_u32(ip):
    """so wie die Firmware die Adresse speichert (ESP32 IPAddress: erstes Byte unten)"""
    return int.from_bytes(ipaddress.IPv4Address(ip).packed, "little")


# Partition für Dateien (LittleFS) je nach Flash-Größe – TAKweb braucht gut 0,5 MB
FS_HINT = "4 MB Flash: 1 MB, 8 MB: 1,5 MB, 16 MB: 3,4 MB"


class TakwebError(Exception):
    pass


# ======================================================================================
# Paket
# ======================================================================================
class Package:
    """TAKweb für das Funkgerät: version, files = [(name, gz-Bytes)] – index.html zuletzt."""

    def __init__(self, version, files):
        self.version = version
        self.files = sorted(files, key=lambda f: f[0] == "index.html")
        self.total = sum(len(gz) for _, gz in self.files)
        self.raw = None             # die ZIP-Datei selbst (zum Speichern in der Bibliothek)

    @classmethod
    def from_zip(cls, data):
        try:
            z = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
        except zipfile.BadZipFile:
            raise TakwebError("Keine ZIP-Datei – erwartet wird takweb-mesh-<version>.zip aus dem TAKweb-Admin.")
        with z:
            if PACKAGE_INFO not in z.namelist():
                raise TakwebError(f"Kein TAKweb-Paket ({PACKAGE_INFO} fehlt).")
            info = json.loads(z.read(PACKAGE_INFO).decode("utf-8"))
            files = []
            for name in info.get("files", []):
                if "/" in name or "\\" in name or name.startswith("."):
                    raise TakwebError(f"Ungültiger Dateiname im Paket: {name}")
                try:
                    gz = z.read(name + ".gz")
                except KeyError:
                    raise TakwebError(f"Datei fehlt im Paket: {name}.gz")
                if gz[:2] != b"\x1f\x8b":
                    raise TakwebError(f"{name}.gz ist nicht gzip-komprimiert.")
                files.append((name, gz))
        if not any(n == "index.html" for n, _ in files):
            raise TakwebError("index.html fehlt im Paket.")
        p = cls(str(info.get("version", "?")), files)
        p.raw = bytes(data) if isinstance(data, (bytes, bytearray)) else None
        return p

    @classmethod
    def from_file(cls, path):
        with open(path, "rb") as f:
            return cls.from_zip(f.read())

    @classmethod
    def from_server(cls, url, timeout=60):
        """url: Adresse des TAKweb-Servers (https://server:8443) oder direkt …/api/eud/meshsite.zip"""
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not url.endswith(".zip"):
            url += "/api/eud/meshsite.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "MeshtasticDesktop-Flasher"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        except Exception as e:
            raise TakwebError(f"Paket nicht geladen ({url}): {e}")
        return cls.from_zip(data)


def crc16(data):
    """CRC-16/XMODEM, wie in der Firmware (\"123456789\" -> 0x31C3)"""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ======================================================================================
# Serielle Verbindung (Rahmen der Meshtastic-Client-API)
# ======================================================================================
class FrameParser:
    """Zerlegt den seriellen Datenstrom: Rahmen -> on_frame(bytes), alles andere ist Log-Text -> on_text(str)."""

    def __init__(self, on_frame, on_text=None):
        self.on_frame = on_frame
        self.on_text = on_text or (lambda s: None)
        self.buf = b""

    def push(self, chunk):
        b = self.buf + bytes(chunk)
        while True:
            i = b.find(MAGIC)
            if i < 0:
                keep = 1 if b.endswith(MAGIC[:1]) else 0
                if len(b) > keep:
                    self.on_text(b[:len(b) - keep].decode("utf-8", "replace"))
                b = b[len(b) - keep:]
                break
            if i > 0:
                self.on_text(b[:i].decode("utf-8", "replace"))
                b = b[i:]
            if len(b) < 4:
                break
            n = (b[2] << 8) | b[3]
            if n > MAX_FRAME:          # kein Rahmen
                b = b[1:]
                continue
            if len(b) < 4 + n:
                break
            self.on_frame(b[4:4 + n])
            b = b[4 + n:]
        self.buf = b


def frame(payload):
    return MAGIC + bytes([len(payload) >> 8, len(payload) & 255]) + payload


class SerialLink:
    """Meshtastic über USB (pyserial). send(bytes) schickt ein ToRadio, empfangene FromRadio -> on_frame(bytes)."""

    def __init__(self, port, on_frame, on_text=None, baud=115200):
        import serial
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, baud, 0.2
        self.ser.dtr = False     # vor dem Öffnen: das Gerät nicht über DTR/RTS (Auto-Reset) neu starten
        self.ser.rts = False
        self.ser.open()
        self.parser = FrameParser(on_frame, on_text)
        self.alive = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.ser.write(b"\xc3" * 32)      # API des Geräts aufwecken
        time.sleep(0.1)

    def _read(self):
        while self.alive:
            try:
                data = self.ser.read(max(1, self.ser.in_waiting))   # nicht auf volle Puffer warten
            except Exception:
                break
            if data:
                self.parser.push(data)

    def send(self, payload):
        with self.lock:
            self.ser.write(frame(payload))
            self.ser.flush()

    def close(self):
        self.alive = False
        try:
            self.thread.join(1)
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


# ======================================================================================
# Installation
# ======================================================================================
class RadioInstaller:
    """send(bytes): ein ToRadio ans Gerät; feed_frame(bytes) bekommt jedes FromRadio (aus SerialLink oder Test)."""

    def __init__(self, send, log=print):
        from meshtastic.protobuf import mesh_pb2  # noqa: F401  (früh prüfen, ob das Paket da ist)
        self.send = send
        self.log = log
        self.msgs = queue.Queue()
        self.my_node = 0
        self.files = []
        self.network = None
        self.lora = None
        self.position = None
        self.xm_timeout = 6
        self.state_lock = threading.Lock()

    # -------------------------------------------------------------- Empfang
    def feed_frame(self, data):
        from meshtastic.protobuf import mesh_pb2
        m = mesh_pb2.FromRadio()
        try:
            m.ParseFromString(bytes(data))
        except Exception:
            return
        kind = m.WhichOneof("payload_variant")
        with self.state_lock:
            if kind == "my_info":
                self.my_node = m.my_info.my_node_num
            elif kind == "fileInfo":
                self.files.append((m.fileInfo.file_name, m.fileInfo.size_bytes))
            elif kind == "config" and m.config.WhichOneof("payload_variant") == "network":
                self.network = m.config.network
            elif kind == "config" and m.config.WhichOneof("payload_variant") == "lora":
                self.lora = m.config.lora
            elif kind == "config" and m.config.WhichOneof("payload_variant") == "position":
                self.position = m.config.position
        self.msgs.put((kind, m))

    def _wait(self, test, timeout, what):
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise TakwebError(f"{what}: keine Antwort vom Gerät")
            try:
                kind, m = self.msgs.get(timeout=left)
            except queue.Empty:
                continue
            if test(kind, m):
                return m

    def _drain(self):
        try:
            while True:
                self.msgs.get_nowait()
        except queue.Empty:
            pass

    # -------------------------------------------------------------- Zustand
    def read_state(self, timeout=30):
        from meshtastic.protobuf import mesh_pb2
        with self.state_lock:
            self.files = []
        nonce = random.randint(1, 0x7FFFFFF0)
        self._drain()
        self.send(mesh_pb2.ToRadio(want_config_id=nonce).SerializeToString())
        self._wait(lambda k, m: k == "config_complete_id" and m.config_complete_id == nonce, timeout,
                   "Gerät auslesen")
        if not self.my_node:
            raise TakwebError("Das Gerät hat seine Knotennummer nicht gemeldet.")
        with self.state_lock:
            return {"node": self.my_node, "files": list(self.files), "network": self.network}

    # -------------------------------------------------------------- XModem
    def xm(self, control, seq=0, buffer=b"", timeout=None):
        from meshtastic.protobuf import mesh_pb2, xmodem_pb2
        x = xmodem_pb2.XModem(control=control, seq=seq)
        if buffer:
            x.buffer = bytes(buffer)
            x.crc16 = crc16(buffer)
        self._drain()
        self.send(mesh_pb2.ToRadio(xmodemPacket=x).SerializeToString())
        return self._wait(lambda k, m: k == "xmodemPacket", timeout or self.xm_timeout, "Dateiübertragung").xmodemPacket.control

    def _open(self, name):
        """Datei im Flash zum Schreiben öffnen (eine vorhandene wird dabei geleert), mit Wiederholung"""
        from meshtastic.protobuf import xmodem_pb2 as X
        for _ in range(3):
            try:
                if self.xm(X.XModem.SOH, 0, name.encode("utf-8")) == X.XModem.ACK:
                    return
            except TakwebError:
                pass
        raise TakwebError(f"Das Gerät nimmt {name} nicht an")

    def remove(self, name):
        from meshtastic.protobuf import xmodem_pb2 as X
        self._open(name)
        try:
            self.xm(X.XModem.CAN)
        except TakwebError:
            self.xm(X.XModem.CAN)

    def put(self, name, data, on_bytes=lambda n: None):
        from meshtastic.protobuf import xmodem_pb2 as X
        C = X.XModem
        self._open(name)
        seq = 1
        for i in range(0, len(data), CHUNK):
            part = data[i:i + CHUNK]
            sent = False
            for _ in range(6):
                try:
                    c = self.xm(C.SOH, seq & 0xFFFF, part)
                except TakwebError:
                    c = None
                if c == C.ACK:
                    sent = True
                    break
                if c == C.CAN:
                    raise TakwebError(f"Das Gerät hat {name} abgebrochen")
                time.sleep(0.2)
            if not sent:
                raise TakwebError(f"Paket {seq} von {name} nicht angenommen")
            on_bytes(min(len(data), i + CHUNK))
            seq += 1
        if self.xm(C.EOT) != C.ACK:
            raise TakwebError(f"Ende von {name} nicht bestätigt")

    # -------------------------------------------------------------- Admin
    def _admin(self, admin):
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        p = mesh_pb2.MeshPacket()
        setattr(p, "to", self.my_node)
        p.id = random.randint(1, 0x7FFFFFFF)
        p.want_ack = True
        p.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        p.decoded.payload = admin.SerializeToString()
        p.decoded.want_response = True
        self.send(mesh_pb2.ToRadio(packet=p).SerializeToString())

    def set_wifi(self, ssid, psk, static_ip=None):
        from meshtastic.protobuf import admin_pb2, config_pb2
        net = config_pb2.Config.NetworkConfig()
        if self.network is not None:
            net.CopyFrom(self.network)          # NTP, feste IP usw. bleiben
        net.wifi_enabled = True
        net.wifi_ssid = ssid
        net.wifi_psk = psk or ""
        C = config_pb2.Config.NetworkConfig
        if static_ip:                           # feste Adresse – das Handy findet das Gerät immer unter derselben IP
            ip, gw, mask, dns = static_net(static_ip)
            net.address_mode = C.STATIC
            net.ipv4_config.ip, net.ipv4_config.gateway = ip_u32(ip), ip_u32(gw)
            net.ipv4_config.subnet, net.ipv4_config.dns = ip_u32(mask), ip_u32(dns)
        else:
            net.address_mode = C.DHCP
        a = admin_pb2.AdminMessage()
        a.set_config.network.CopyFrom(net)
        self._admin(a)

    def always_send(self):
        """Krisengeräte senden immer: Duty-Cycle-Override an (die übrigen LoRa-Einstellungen bleiben)."""
        from meshtastic.protobuf import admin_pb2, config_pb2
        lora = config_pb2.Config.LoRaConfig()
        if self.lora is not None:
            lora.CopyFrom(self.lora)
        if lora.override_duty_cycle and lora.config_ok_to_mqtt:
            return False
        lora.override_duty_cycle = True
        lora.config_ok_to_mqtt = True           # seine Nachrichten dürfen über MQTT zum Server
        a = admin_pb2.AdminMessage()
        a.set_config.lora.CopyFrom(lora)
        self._admin(a)
        return True

    def set_position(self, secs=120):
        """GPS an, Position alle secs Sekunden (bei Bewegung öfter), immer mit den GPS-Pins des Boards."""
        from meshtastic.protobuf import admin_pb2, config_pb2
        pos = config_pb2.Config.PositionConfig()
        if self.position is not None:
            pos.CopyFrom(self.position)
        for f in ("rx_gpio", "tx_gpio", "gps_en_gpio"):   # von Hand gesetzte Pins machen GPS stumm (Tracker V1.0)
            pos.ClearField(f)
        pos.position_broadcast_secs = int(secs)
        pos.position_broadcast_smart_enabled = True
        pos.gps_mode = config_pb2.Config.PositionConfig.GpsMode.ENABLED
        a = admin_pb2.AdminMessage()
        a.set_config.position.CopyFrom(pos)
        self._admin(a)

    def set_mqtt(self, address, username, password, root="opentakserver", tls=True):
        """MQTT zum TAK-Server (OpenTAKServer / WebTAK Server): Positionen und Nachrichten auch ohne Handy."""
        from meshtastic.protobuf import admin_pb2, module_config_pb2
        mq = module_config_pb2.ModuleConfig.MQTTConfig(
            enabled=True, address=address, username=username, password=password, encryption_enabled=True,
            json_enabled=False, tls_enabled=bool(tls), root=root or "opentakserver")
        a = admin_pb2.AdminMessage()
        a.set_module_config.mqtt.CopyFrom(mq)
        self._admin(a)

    def reboot(self, seconds=3):
        from meshtastic.protobuf import admin_pb2
        self._admin(admin_pb2.AdminMessage(reboot_seconds=seconds))

    # -------------------------------------------------------------- alles
    def install(self, package, wifi=None, progress=lambda step, done, total, name="": None, options=None):
        """package: Package; wifi: (ssid, psk[, feste IP]) oder None (WLAN bleibt wie es ist);
        options: {"gps_secs": 120, "mqtt": {"address", "username", "password", "root", "tls"}} oder None"""
        options = options or {}
        st = self.read_state()
        self.log(f"Gerät !{st['node']:08x}: {len(st['files'])} Dateien im Flash\n")
        old = [n for n, _ in st["files"] if n.lstrip("/").startswith("static/")]
        for i, n in enumerate(old):
            progress("remove", i, len(old), n)
            self.remove(n if n.startswith("/") else "/" + n)
        if old:
            self.log(f"{len(old)} alte Web-Dateien gelöscht\n")
        before = 0
        for name, gz in package.files:
            self.put(f"/static/{name}.gz", gz,
                     lambda b, before=before, name=name: progress("upload", before + b, package.total, name))
            before += len(gz)
            self.log(f"  {name}: {len(gz)} Bytes\n")
        # die Firmware bestätigt auch bei vollem Flash – mit ihrer Dateiliste vergleichen
        progress("check", 0, 1)
        after = {n.lstrip("/"): s for n, s in self.read_state()["files"]}
        bad = [name for name, gz in package.files if after.get(f"static/{name}.gz") != len(gz)]
        if bad:
            raise TakwebError("Nicht vollständig gespeichert (Flash voll?): " + ", ".join(bad) +
                              f"\nDateisystem je nach Flash-Größe: {FS_HINT}.")
        if self.always_send():
            self.log("Duty-Cycle-Override eingeschaltet (Gerät sendet immer)\n")
            time.sleep(1)
        if options.get("gps_secs"):
            self.set_position(options["gps_secs"])
            self.log(f"GPS an, Position alle {options['gps_secs']} s (Pins des Boards)\n")
            time.sleep(1)
        mq = options.get("mqtt")
        if mq and mq.get("address"):
            self.set_mqtt(mq["address"], mq.get("username", ""), mq.get("password", ""), mq.get("root") or "opentakserver",
                          mq.get("tls", True))
            self.log(f"MQTT zum Server: {mq['address']} ({mq.get('username', '')})\n")
            time.sleep(1)
        if not (wifi and wifi[0]) and (options.get("gps_secs") or mq):
            try:
                self.reboot(3)
            except Exception:
                pass
        if wifi and wifi[0]:
            progress("wifi", 0, 1)
            static_ip = wifi[2] if len(wifi) > 2 else None
            self.set_wifi(wifi[0], wifi[1], static_ip)
            time.sleep(1.5)
            try:
                self.reboot(3)
            except Exception:
                pass
            self.log(f"WLAN gesetzt: „{wifi[0]}“" + (f", feste Adresse {static_ip}" if static_ip else ", Adresse vom Hotspot (DHCP)")
                     + " – das Gerät startet neu (Bluetooth ist aus, solange WLAN an ist)\n")
        progress("done", 1, 1)


def check_wifi(ssid, psk, static_ip=None):
    """-> Fehlertext oder ''"""
    if static_ip:
        try:
            static_net(static_ip)
        except ValueError as e:
            return f"Feste Adresse ungültig: {static_ip} ({e})"
    if len(ssid.encode("utf-8")) > 32:
        return "Der WLAN-Name darf höchstens 32 Zeichen haben."
    if psk and not 8 <= len(psk) <= 63:
        return "Das WLAN-Passwort braucht 8 bis 63 Zeichen (oder leer lassen für ein offenes WLAN)."
    return ""


def install_serial(port, package, wifi=None, log=print, progress=lambda *a: None, baud=115200, options=None):
    """Komplett: Port öffnen, TAKweb schreiben, WLAN setzen, Port schließen."""
    inst = None
    link = None
    try:
        inst = RadioInstaller(lambda b: link.send(b), log)
        link = SerialLink(port, inst.feed_frame, baud=baud)
        inst.install(package, wifi, progress, options)
    finally:
        if link:
            link.close()


if __name__ == "__main__":       # python takweb.py COM5 takweb-mesh.zip [WLAN-Name] [Passwort] [feste IP]
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    pkg = Package.from_file(sys.argv[2]) if os.path.exists(sys.argv[2]) else Package.from_server(sys.argv[2])
    print(f"TAKweb {pkg.version}: {len(pkg.files)} Dateien, {pkg.total // 1024} KB")
    w = (sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "",
         sys.argv[5] if len(sys.argv) > 5 else None) if len(sys.argv) > 3 else None
    install_serial(sys.argv[1], pkg, w, log=lambda s: print(s, end=""),
                   progress=lambda step, d, t, n="": print(f"\r{step} {n} {100 * d // max(t, 1)}%   ", end=""))
    print("\nFertig.")
