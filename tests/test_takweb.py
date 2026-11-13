# -*- coding: utf-8 -*-
"""Tests für takweb.py mit einem nachgebauten Meshtastic-Gerät (Verhalten wie die Firmware).

    python -m unittest discover -s tests
    TAKWEB_ZIP=takweb-mesh-x.zip python -m unittest discover -s tests   # zusätzlich mit echtem Paket
"""
import gzip
import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import takweb  # noqa: E402
from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2, portnums_pb2, xmodem_pb2  # noqa: E402

X = xmodem_pb2.XModem


class FakeRadio:
    """Antwortet wie die Firmware: want_config, XModem (Dateien im Flash), Admin. capacity = Platz im Flash."""

    def __init__(self, deliver, capacity=1024 * 1024, node=0x1234ABCD):
        self.deliver = deliver
        self.capacity = capacity
        self.node = node
        self.fs = {"/static/index.html.gz": b"old", "/static/old.js.gz": b"x" * 10, "/prefs/config.proto": b"c"}
        self.net = config_pb2.Config.NetworkConfig(ntp_server="pool.ntp.org", wifi_ssid="alt")
        self.cur = None
        self.admin = []
        self.lost = 0           # so viele XModem-Antworten gehen verloren (Test: Wiederholung)

    def used(self):
        return sum(len(v) for v in self.fs.values())

    def out(self, **kw):
        self.deliver(mesh_pb2.FromRadio(**kw).SerializeToString())

    def ack(self, c):
        if self.lost:
            self.lost -= 1
            return
        self.out(xmodemPacket=X(control=c))

    def receive(self, data):
        m = mesh_pb2.ToRadio()
        m.ParseFromString(data)
        k = m.WhichOneof("payload_variant")
        if k == "want_config_id":
            self.out(my_info=mesh_pb2.MyNodeInfo(my_node_num=self.node))
            self.out(config=config_pb2.Config(network=self.net))
            self.out(config=config_pb2.Config(lora=config_pb2.Config.LoRaConfig(region=3, use_preset=True, hop_limit=5)))
            self.out(config=config_pb2.Config(position=config_pb2.Config.PositionConfig(gps_en_gpio=3, rx_gpio=44, fixed_position=False)))
            for n, v in self.fs.items():
                self.out(fileInfo=mesh_pb2.FileInfo(file_name=n, size_bytes=len(v)))
            self.out(config_complete_id=m.want_config_id)
        elif k == "xmodemPacket":
            x = m.xmodemPacket
            if x.control == X.SOH and x.seq == 0:
                self.cur = x.buffer.decode()
                self.fs.pop(self.cur, None)
                self.fs[self.cur] = b""
                self.ack(X.ACK)
            elif x.control in (X.SOH, X.STX):
                if takweb.crc16(x.buffer) != x.crc16:
                    return self.ack(X.NAK)
                room = max(0, self.capacity - self.used())
                self.fs[self.cur] += x.buffer[:room]       # Firmware bestätigt trotzdem
                self.ack(X.ACK)
            elif x.control == X.CAN:
                self.fs.pop(self.cur, None)
                self.ack(X.ACK)
            elif x.control == X.EOT:
                self.ack(X.ACK)
        elif k == "packet" and m.packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
            a = admin_pb2.AdminMessage()
            a.ParseFromString(m.packet.decoded.payload)
            self.admin.append((getattr(m.packet, "to"), a))


def make_zip(files, version="1.2.3"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, data in files.items():
            z.writestr(n + ".gz", gzip.compress(data))
        z.writestr("takweb-mesh.json", json.dumps({"version": version, "files": list(files)}))
    return b.getvalue()


def connect(capacity=1024 * 1024):
    box = {}
    inst = takweb.RadioInstaller(lambda b: box["r"].receive(b), log=lambda s: None)
    inst.xm_timeout = 0.3
    box["r"] = FakeRadio(inst.feed_frame, capacity)
    return inst, box["r"]


class TestTakweb(unittest.TestCase):
    def test_crc(self):
        self.assertEqual(takweb.crc16(b"123456789"), 0x31C3)

    def test_package(self):
        p = takweb.Package.from_zip(make_zip({"index.html": b"<html>", "eud.js": b"x" * 500}))
        self.assertEqual(p.version, "1.2.3")
        self.assertEqual([n for n, _ in p.files], ["eud.js", "index.html"])   # index.html zuletzt
        with self.assertRaises(takweb.TakwebError):
            takweb.Package.from_zip(b"kein zip")
        with self.assertRaises(takweb.TakwebError):
            takweb.Package.from_zip(make_zip({"../boese": b"x", "index.html": b""}))

    def test_install(self):
        inst, radio = connect()
        js = os.urandom(1000)                     # nicht komprimierbar -> mehrere Pakete
        pkg = takweb.Package.from_zip(make_zip({"index.html": b"<html>TAKweb</html>", "eud.js": js}))
        steps = []
        radio.lost = 1
        inst.install(pkg, ("TAKweb", "12345678"), lambda s, d, t, n="": steps.append(s))
        self.assertNotIn("/static/old.js.gz", radio.fs)
        self.assertIn("/prefs/config.proto", radio.fs)          # nur /static wird gelöscht
        self.assertEqual(gzip.decompress(radio.fs["/static/eud.js.gz"]), js)
        self.assertEqual(gzip.decompress(radio.fs["/static/index.html.gz"]), b"<html>TAKweb</html>")
        self.assertEqual(steps[-1], "done")
        self.assertIn("wifi", steps)
        to, cfg = [x for x in radio.admin if x[1].set_config.HasField("network")][0]
        self.assertEqual(to, radio.node)
        n = cfg.set_config.network
        self.assertEqual((n.wifi_enabled, n.wifi_ssid, n.wifi_psk, n.ntp_server),
                         (True, "TAKweb", "12345678", "pool.ntp.org"))   # NTP bleibt
        self.assertEqual([a for _, a in radio.admin if a.reboot_seconds][0].reboot_seconds, 3)

    def test_static_ip(self):
        self.assertEqual(takweb.ip_u32("172.20.10.14"), 172 | 20 << 8 | 10 << 16 | 14 << 24)   # wie ESP32 IPAddress
        self.assertEqual(takweb.static_net("172.20.10.14"), ("172.20.10.14", "172.20.10.1", "255.255.255.240", "172.20.10.1"))
        self.assertEqual(takweb.static_net("192.168.43.50")[1:3], ("192.168.43.1", "255.255.255.0"))
        self.assertTrue(takweb.check_wifi("TAKweb", "12345678", "300.1.1.1"))
        self.assertTrue(takweb.check_wifi("TAKweb", "12345678", "192.168.1.1"))      # Router-Adresse
        inst, radio = connect()
        inst.install(takweb.Package.from_zip(make_zip({"index.html": b"x"})), ("TAKweb", "12345678", takweb.IPHONE_IP))
        n = [a for _, a in radio.admin if a.set_config.HasField("network")][0].set_config.network
        self.assertEqual(n.address_mode, config_pb2.Config.NetworkConfig.STATIC)
        self.assertEqual(n.ipv4_config.ip, takweb.ip_u32("172.20.10.14"))
        self.assertEqual(n.ipv4_config.gateway, takweb.ip_u32("172.20.10.1"))
        self.assertEqual(n.ipv4_config.subnet, takweb.ip_u32("255.255.255.240"))
        self.assertEqual(n.ntp_server, "pool.ntp.org")
        # ohne feste Adresse wieder DHCP
        inst.install(takweb.Package.from_zip(make_zip({"index.html": b"x"})), ("TAKweb", "12345678"))
        self.assertEqual([a for _, a in radio.admin if a.set_config.HasField("network")][-1].set_config.network.address_mode,
                         config_pb2.Config.NetworkConfig.DHCP)

    def test_always_send(self):
        inst, radio = connect()
        inst.install(takweb.Package.from_zip(make_zip({"index.html": b"x"})), None)
        lora = [a for _, a in radio.admin if a.set_config.HasField("lora")][0].set_config.lora
        self.assertTrue(lora.override_duty_cycle)
        self.assertEqual((lora.region, lora.hop_limit, lora.use_preset), (3, 5, True))  # Rest bleibt

    def test_gps_mqtt_gateway(self):
        inst, radio = connect()
        opts = {"gps_secs": 900, "mqtt": {"address": "tak.example.org:8883", "username": "radio", "password": "pw-123456", "tls": True}}
        inst.install(takweb.Package.from_zip(make_zip({"index.html": b"x"})), ("HQ-WLAN", "hq-pass-123"), options=opts)
        pos = [a for _, a in radio.admin if a.set_config.HasField("position")][0].set_config.position
        self.assertEqual((pos.position_broadcast_secs, pos.position_broadcast_smart_enabled, pos.gps_en_gpio, pos.rx_gpio),
                         (900, True, 0, 0))                     # von Hand gesetzte GPS-Pins sind weg
        self.assertEqual(pos.gps_mode, config_pb2.Config.PositionConfig.GpsMode.ENABLED)
        mq = [a for _, a in radio.admin if a.set_module_config.HasField("mqtt")][0].set_module_config.mqtt
        self.assertEqual((mq.enabled, mq.address, mq.username, mq.encryption_enabled, mq.tls_enabled, mq.json_enabled, mq.root),
                         (True, "tak.example.org:8883", "radio", True, True, False, "opentakserver"))
        lora = [a for _, a in radio.admin if a.set_config.HasField("lora")][0].set_config.lora
        self.assertTrue(lora.config_ok_to_mqtt)
        self.assertEqual([a for _, a in radio.admin if a.set_config.HasField("network")][0].set_config.network.wifi_ssid, "HQ-WLAN")

    def test_without_wifi(self):
        inst, radio = connect()
        inst.install(takweb.Package.from_zip(make_zip({"index.html": b"x"})), None)
        self.assertFalse([a for _, a in radio.admin if a.set_config.HasField("network")])

    def test_flash_full(self):
        inst, radio = connect(capacity=600)
        pkg = takweb.Package.from_zip(make_zip({"index.html": b"x", "big.js": os.urandom(2000)}))
        with self.assertRaises(takweb.TakwebError) as e:
            inst.install(pkg, ("TAKweb", "12345678"))
        self.assertIn("big.js", str(e.exception))
        self.assertEqual(radio.admin, [])                   # WLAN nicht angefasst

    def test_wifi_check(self):
        self.assertTrue(takweb.check_wifi("TAKweb", "123456"))
        self.assertEqual(takweb.check_wifi("TAKweb", "12345678"), "")
        self.assertEqual(takweb.check_wifi("TAKweb", ""), "")   # offenes WLAN

    def test_framing(self):
        frames, text = [], []
        p = takweb.FrameParser(frames.append, text.append)
        a, b = b"\x08\x01", bytes(range(200))
        stream = b"INFO boot\r\n" + takweb.frame(a) + b"\x94" + takweb.frame(b) + b"log"
        for i in range(0, len(stream), 7):                    # in Stücken wie über USB
            p.push(stream[i:i + 7])
        self.assertEqual(frames, [a, b])
        self.assertIn("INFO boot", "".join(text))

    @unittest.skipUnless(os.environ.get("TAKWEB_ZIP"), "TAKWEB_ZIP nicht gesetzt")
    def test_real_package(self):
        pkg = takweb.Package.from_file(os.environ["TAKWEB_ZIP"])
        inst, radio = connect()
        # Rahmen auch über den seriellen Parser schicken (wie am USB-Port)
        parser = takweb.FrameParser(inst.feed_frame)
        radio.deliver = lambda b: parser.push(takweb.frame(b))
        inst.install(pkg, ("TAKweb", "12345678"))
        for n, gz in pkg.files:
            self.assertEqual(radio.fs[f"/static/{n}.gz"], gz)


if __name__ == "__main__":
    unittest.main()
