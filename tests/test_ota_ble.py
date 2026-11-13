# -*- coding: utf-8 -*-
"""Tests für ota_ble.py mit einem nachgebauten OTA-Lader (Bluetooth über eine Attrappe von bleak)."""
import hashlib
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ota_ble  # noqa: E402


class FakeLoader:
    """Antwortet wie esp32-unified-ota: OTA <size> <hash> → ERASING, OK; Stücke → ACK; letztes → OK/ERR."""

    def __init__(self, nvs_hash):
        self.nvs_hash, self.notify, self.size, self.got, self.state = nvs_hash, None, 0, bytearray(), "idle"

    def say(self, text):
        self.notify(None, text.encode())

    def write(self, data):
        if self.state == "idle":
            _, size, h = data.decode().split()
            if h != self.nvs_hash:
                return self.say("ERR Hash Rejected (NVS Mismatch)\n")
            self.size, self.state = int(size), "down"
            self.say("ERASING\n")
            self.say("OK\n")
            return
        self.got += data
        if len(self.got) < self.size:
            return self.say("ACK\n")
        self.say("OK\n" if hashlib.sha256(self.got).hexdigest() == self.nvs_hash else "ERR Hash Mismatch\n")


def fake_bleak(loader, name="Meshtastic_8E1B"):
    dev = types.SimpleNamespace(name=name, address="AA:BB")
    adv = types.SimpleNamespace(service_uuids=[ota_ble.SVC.upper()], local_name=name)

    class Scanner:
        @staticmethod
        async def find_device_by_filter(fn, timeout=0):
            return dev if fn(dev, adv) else None

    class Client:
        mtu_size = 247

        def __init__(self, d, timeout=0):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def start_notify(self, uuid, cb):
            loader.notify = cb

        async def write_gatt_char(self, uuid, data, response=False):
            assert uuid == ota_ble.OTA
            loader.write(bytes(data))

    return types.SimpleNamespace(BleakScanner=Scanner, BleakClient=Client)


class TestOtaBle(unittest.TestCase):
    def run_with(self, loader, data, name=""):
        sys.modules["bleak"] = fake_bleak(loader)
        steps = []
        try:
            ota_ble.upload(data, log=lambda s: None, progress=lambda d, t: steps.append(d), name=name, find_timeout=1)
        finally:
            del sys.modules["bleak"]
        return steps

    def test_upload(self):
        data = b"\xe9" + os.urandom(5000)
        loader = FakeLoader(hashlib.sha256(data).hexdigest())
        steps = self.run_with(loader, data, name="Meshtastic_8E1B")
        self.assertEqual(bytes(loader.got), data)
        self.assertEqual(steps[-1], len(data))

    def test_hash_rejected(self):
        data = b"\xe9" + os.urandom(1000)
        with self.assertRaises(ota_ble.OTAError) as e:
            self.run_with(FakeLoader("00" * 32), data)
        self.assertIn("Hash Rejected", str(e.exception))

    def test_not_found(self):
        data = b"\xe9" + os.urandom(100)
        with self.assertRaises(ota_ble.OTAError):
            self.run_with(FakeLoader(hashlib.sha256(data).hexdigest()), data, name="Meshtastic_0000")

    def test_names_and_image(self):
        self.assertEqual(ota_ble.loader_name(0x6A0B8E1B), "Meshtastic_8E1B")
        self.assertTrue(ota_ble.check_image(b"PK\x03\x04"))
        self.assertEqual(ota_ble.check_image(b"\xe9\x06"), "")


if __name__ == "__main__":
    unittest.main()
