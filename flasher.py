# -*- coding: utf-8 -*-
"""
Offline-Firmware-Flasher für Meshtastic-Geräte (wie flasher.meshtastic.org, aber ohne Internet).

* ESP32 / ESP32-S3 / ESP32-C3 / ESP32-C6: über esptool (USB-Seriell)
    - Update:           App-Image an app0 (Einstellungen bleiben erhalten)
    - Neuinstallation:  Flash löschen + factory.bin @0x0 + OTA-App @app1 + LittleFS @spiffs
* nRF52840 / RP2040 / RP2350: über UF2-Laufwerk (Bootloader)
    - Gerät wird per Meshtastic-Befehl oder 1200-Baud-Reset in den Bootloader versetzt,
      danach wird die .uf2 auf das Laufwerk kopiert (optional vorher "Factory Erase").
* Eigene Dateien (.bin/.uf2), z. B. Spezial-Firmware für Heltec V2.
* TAKweb aufs Gerät (ESP32): Mesh-Ausgabe von TAKweb in den Flash, WLAN setzen (takweb.py).

Firmware-Bibliothek: %APPDATA%\\MeshtasticDesktop\\firmware\\<version>\\  (offline)
Pakete: ZIP-Dateien aus den Releases des Repos (Workflow "Firmware-Offline-Pakete").

Eigenständig startbar:  python flasher.py
"""
import contextlib
import io
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

import takweb

APP_TITLE = "Meshtastic Flasher (offline)"
DEFAULT_RELEASE_REPO = "fredy060795/MeshtasticDesktop"
CUSTOM_KEY = "★ Eigene / Spezial-Firmware"

ESP_CHIP_IDS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6",
                16: "esp32h2", 18: "esp32p4", 23: "esp32c5"}
UF2_FAMILIES = {0xADA52840: "nrf52840", 0xE48BFF56: "rp2040", 0xE48BFF59: "rp2350",
                0xE48BFF5A: "rp2350", 0xE48BFF5B: "rp2350"}
SUPPORT = {1: "offiziell", 2: "Community", 3: "Community/Alt"}


# ======================================================================================
# Hilfen
# ======================================================================================
def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def default_data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "MeshtasticDesktop")
    os.makedirs(d, exist_ok=True)
    return d


def vkey(v):
    parts = re.findall(r"\d+", str(v))[:3]
    return [int(p) for p in parts] + [0] * (3 - len(parts))


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024


def chip_of(m):
    return (m.get("mcu") or m.get("architecture") or "").replace("-", "").lower()


def is_esp(chip):
    return chip.startswith("esp32")


def is_uf2_chip(chip):
    return chip.startswith(("nrf52", "rp2"))


def parse_off(v, default=None):
    if v is None:
        return default
    if isinstance(v, int):
        return v
    try:
        return int(str(v), 0)
    except ValueError:
        return default


def analyze_esp_image(path):
    """Erkennt Chip, Image-Art (factory/app) und Version einer ESP32-.bin-Datei."""
    with open(path, "rb") as f:
        data = f.read()
    info = {"size": len(data), "kind": None, "chip": None, "offset": None, "version": None, "target": None}
    # Factory-Image: Bootloader bei 0x0 (S2/S3/C3/C6...) oder 0x1000 (ESP32), Partitionstabelle bei 0x8000
    has_pt = len(data) > 0x8002 and data[0x8000:0x8002] == b"\xaa\x50"
    for boff in (0x0, 0x1000):
        if len(data) > boff + 16 and data[boff] == 0xE9 and has_pt:
            cid = int.from_bytes(data[boff + 12:boff + 14], "little")
            info.update(kind="factory", chip=ESP_CHIP_IDS.get(cid, f"chip{cid}"), offset=0)
            break
    if info["kind"] is None and len(data) > 16 and data[0] == 0xE9:
        cid = int.from_bytes(data[12:14], "little")
        info.update(kind="app", chip=ESP_CHIP_IDS.get(cid, f"chip{cid}"), offset=0x10000)
    head = data[:4 * 1024 * 1024]
    m = re.search(rb"\b(2\.\d+\.\d+\.[0-9a-f]{7,10})\b", head)
    if m:
        info["version"] = m.group(1).decode()
    m = re.search(rb"/\.pio/(?:libdeps|build)/([A-Za-z0-9_\-.]+)/", head)
    if m:
        info["target"] = m.group(1).decode()
    if has_pt:
        parts = []
        for i in range(32):
            e = data[0x8000 + i * 32:0x8000 + i * 32 + 32]
            if e[:2] != b"\xaa\x50":
                break
            parts.append({"name": e[12:28].rstrip(b"\0").decode(errors="replace"), "type": e[2], "subtype": e[3],
                          "offset": int.from_bytes(e[4:8], "little"), "size": int.from_bytes(e[8:12], "little")})
        info["partitions"] = parts
    return info


def uf2_family(path):
    with open(path, "rb") as f:
        blk = f.read(512)
    if len(blk) < 512 or blk[:8] != b"UF2\nWQ]\x9e":
        return None
    flags = int.from_bytes(blk[8:12], "little")
    if flags & 0x2000:
        return UF2_FAMILIES.get(int.from_bytes(blk[28:32], "little"), "unbekannt")
    return "unbekannt"


# ======================================================================================
# Firmware-Bibliothek
# ======================================================================================
class FwLibrary:
    def __init__(self, base_dir):
        self.base = base_dir
        os.makedirs(self.base, exist_ok=True)
        self.custom_dirs = [resource_path("firmware_custom"), os.path.join(self.base, "_custom")]

    def _load_dir(self, d):
        devs = []
        meta = {}
        mp = os.path.join(d, "meshdesk-firmware.json")
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp, encoding="utf-8"))
            except Exception:
                meta = {}
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".mt.json"):
                try:
                    m = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                except Exception:
                    continue
                m["_dir"] = d
                m["_manifest"] = fn
                m.setdefault("version", meta.get("version", "?"))
                devs.append(m)
        return meta, devs

    def versions(self):
        """Liste: [{key, version, channel, devices}] – neueste zuerst, eigene Firmware zuletzt."""
        res = []
        for name in os.listdir(self.base):
            d = os.path.join(self.base, name)
            if name.startswith("_") or not os.path.isdir(d):
                continue
            meta, devs = self._load_dir(d)
            if devs:
                res.append({"key": name, "version": meta.get("version", name), "channel": meta.get("channel", ""),
                            "devices": devs, "dir": d})
        res.sort(key=lambda r: vkey(r["version"]), reverse=True)
        cdevs = []
        for cd in self.custom_dirs:
            if os.path.isdir(cd):
                for sub in sorted(os.listdir(cd)):
                    p = os.path.join(cd, sub)
                    if os.path.isdir(p):
                        cdevs += self._load_dir(p)[1]
        if cdevs:
            res.append({"key": CUSTOM_KEY, "version": CUSTOM_KEY, "channel": "", "devices": cdevs, "dir": None})
        return res

    def import_zip(self, path, log=print):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            meta = {}
            if "meshdesk-firmware.json" in names:
                meta = json.loads(z.read("meshdesk-firmware.json"))
            version = meta.get("version")
            if not version:
                for n in names:
                    if n.endswith(".mt.json"):
                        version = json.loads(z.read(n)).get("version")
                        break
            if not version:
                raise ValueError("Keine Meshtastic-Firmware im ZIP gefunden (keine .mt.json).")
            dest = os.path.join(self.base, re.sub(r"[^A-Za-z0-9._-]", "_", version))
            os.makedirs(dest, exist_ok=True)
            n = 0
            for name in names:
                base = os.path.basename(name)
                if not base or base.endswith(".elf") or name.endswith("/"):
                    continue
                with z.open(name) as src, open(os.path.join(dest, base), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
            mp = os.path.join(dest, "meshdesk-firmware.json")
            old = {}
            if os.path.exists(mp):
                try:
                    old = json.load(open(mp, encoding="utf-8"))
                except Exception:
                    pass
            old.update({k: v for k, v in meta.items() if k != "chip"})
            old.setdefault("version", version)
            old["chips"] = sorted(set(old.get("chips", [])) | ({meta["chip"]} if meta.get("chip") else set()))
            json.dump(old, open(mp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        log(f"Importiert: {os.path.basename(path)} → Version {version} ({n} Dateien)")
        return version

    def add_custom(self, file_path, info, display_name):
        """Eigene Datei dauerhaft in die Bibliothek übernehmen."""
        name = re.sub(r"[^A-Za-z0-9._-]", "_", display_name)[:40] or "eigene"
        d = os.path.join(self.base, "_custom", name)
        os.makedirs(d, exist_ok=True)
        fn = os.path.basename(file_path)
        shutil.copyfile(file_path, os.path.join(d, fn))
        m = {"version": info.get("version") or "?", "platformioTarget": info.get("target") or name,
             "mcu": info.get("chip"), "architecture": info.get("chip"), "displayName": display_name,
             "custom": True, "files": [{"name": fn, "bytes": os.path.getsize(file_path)}], "supportLevel": 3}
        if info.get("partitions"):
            m["part"] = [{"name": p["name"], "offset": hex(p["offset"]), "size": hex(p["size"]),
                          "subtype": "ota_1" if (p["type"] == 0 and p["subtype"] == 17) else p["subtype"]}
                         for p in info["partitions"]]
        json.dump(m, open(os.path.join(d, name + ".mt.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        return d

    def delete(self, key):
        d = os.path.join(self.base, key)
        if key != CUSTOM_KEY and os.path.isdir(d):
            shutil.rmtree(d)


def device_files(m):
    """Ermittelt die Dateien eines Geräts: dict(factory, app, ota, littlefs, uf2, offsets...)."""
    d = m["_dir"]
    files = [f["name"] for f in m.get("files", []) if os.path.exists(os.path.join(d, f["name"]))]
    parts = {p.get("name"): p for p in m.get("part", []) or []}
    by_part = {f.get("part_name"): f["name"] for f in m.get("files", []) if f.get("part_name")}
    r = {"dir": d, "factory": None, "app": None, "ota": None, "littlefs": None, "uf2": None, "erase_uf2": []}
    for n in files:
        ln = n.lower()
        if ln.endswith(".factory.bin"):
            r["factory"] = n
        elif ln.endswith(".uf2"):
            r["uf2"] = n
        elif ln.startswith("littlefs-"):
            r["littlefs"] = n
        elif ln.startswith("mt-") and ln.endswith("-ota.bin"):
            r["ota"] = n
        elif ln.endswith(".bin") and ln.startswith("firmware-"):
            r["app"] = n
    if by_part.get("app0") and os.path.exists(os.path.join(d, by_part["app0"])):
        r["app"] = by_part["app0"]
    if not r["ota"]:   # OTA-App liegt evtl. nur im Ordner, nicht in der Manifest-Liste
        chip = chip_of(m)
        cand = os.path.join(d, f"mt-{chip}-ota.bin")
        if os.path.exists(cand):
            r["ota"] = os.path.basename(cand)

    def off(names, default):
        for nm in names:
            if nm in parts:
                return parse_off(parts[nm].get("offset"), default)
        for p in parts.values():
            if names[0] == "app1" and str(p.get("subtype")) in ("ota_1", "17"):
                return parse_off(p.get("offset"), default)
        return default
    r["app_off"] = off(["app0", "app"], 0x10000)
    r["ota_off"] = off(["app1", "flashApp"], None)
    r["fs_off"] = off(["spiffs", "littlefs"], None)
    if os.path.isdir(d):
        r["erase_uf2"] = sorted(f for f in os.listdir(d) if "factory_erase" in f.lower() and f.endswith(".uf2"))
    return r


# ======================================================================================
# UF2-Laufwerke / Bootloader
# ======================================================================================
def find_uf2_drives():
    found = []
    if sys.platform.startswith("win"):
        try:
            import ctypes
            k = ctypes.windll.kernel32
            old = k.SetErrorMode(1)
            mask = k.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    root = f"{chr(65 + i)}:\\"
                    if k.GetDriveTypeW(root) == 2 and os.path.exists(os.path.join(root, "INFO_UF2.TXT")):
                        found.append(root)
            k.SetErrorMode(old)
        except Exception:
            pass
    else:
        import glob
        for pat in ("/media/*/*", "/media/*", "/run/media/*/*", "/Volumes/*", "/mnt/*"):
            for p in glob.glob(pat):
                if os.path.exists(os.path.join(p, "INFO_UF2.TXT")):
                    found.append(p)
    return found


def read_uf2_info(drive):
    try:
        return open(os.path.join(drive, "INFO_UF2.TXT"), encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def touch_1200(port):
    import serial
    s = serial.Serial()
    s.port = port
    s.baudrate = 1200
    s.dtr = False
    s.open()
    time.sleep(0.1)
    s.dtr = False
    s.close()


def list_serial_ports():
    try:
        import serial.tools.list_ports as lp
        return [(p.device, p.description or "") for p in lp.comports()]
    except Exception:
        return []


class _QueueWriter(io.TextIOBase):
    def __init__(self, fn):
        self.fn = fn

    def write(self, s):
        if s:
            self.fn(s)
        return len(s)

    def flush(self):
        pass


# ======================================================================================
# GUI
# ======================================================================================
class FlasherFrame(ttk.Frame):
    TW_IPHONE = f"{takweb.IPHONE_IP}  (fest – iPhone-Hotspot)"
    TW_DHCP = "automatisch (vom Hotspot, DHCP)"

    def __init__(self, parent, data_dir=None, settings=None, save_settings=None,
                 get_iface=None, disconnect_app=None):
        super().__init__(parent, padding=6)
        self.data_dir = data_dir or default_data_dir()
        self.settings = settings if settings is not None else {}
        self.save_settings = save_settings or (lambda s: None)
        self.get_iface = get_iface or (lambda: None)
        self.disconnect_app = disconnect_app or (lambda: None)
        self.lib = FwLibrary(os.path.join(self.data_dir, "firmware"))
        self.q = queue.Queue()
        self.busy = False
        self.versions = []
        self.cur_devices = []
        self._build()
        self.reload_library()
        self.refresh_ports()
        self._takweb_load_saved()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ Aufbau
    def _build(self):
        top = ttk.LabelFrame(self, text="1. Firmware-Version (offline-Bibliothek)", padding=6)
        top.pack(fill="x")
        self.v_version = tk.StringVar()
        self.cb_version = ttk.Combobox(top, textvariable=self.v_version, state="readonly", width=44)
        self.cb_version.pack(side="left")
        self.cb_version.bind("<<ComboboxSelected>>", lambda e: self.show_devices())
        for txt, cmd in (("📥 Paket importieren…", self.import_packages),
                         ("🌐 Online laden…", self.online_dialog),
                         ("📁 Ordner", self.open_folder), ("🗑 Version löschen", self.delete_version)):
            ttk.Button(top, text=txt, command=cmd).pack(side="left", padx=2)
        self.lib_info = ttk.Label(top, text="", foreground="gray")
        self.lib_info.pack(side="left", padx=8)

        mid = ttk.LabelFrame(self, text="2. Gerät auswählen", padding=6)
        mid.pack(fill="both", expand=True, pady=6)
        bar = ttk.Frame(mid)
        bar.pack(fill="x")
        ttk.Label(bar, text="Suche:").pack(side="left")
        self.v_filter = tk.StringVar()
        ttk.Entry(bar, textvariable=self.v_filter, width=24).pack(side="left", padx=4)
        self.v_filter.trace_add("write", lambda *a: self.show_devices())
        self.v_chipf = tk.StringVar(value="alle Chips")
        self.cb_chipf = ttk.Combobox(bar, textvariable=self.v_chipf, state="readonly", width=12)
        self.cb_chipf.pack(side="left", padx=4)
        self.cb_chipf.bind("<<ComboboxSelected>>", lambda e: self.show_devices())
        ttk.Button(bar, text="🔎 Verbundenes Gerät erkennen", command=self.detect_device).pack(side="left", padx=4)
        ttk.Button(bar, text="🧪 Chip am Port prüfen (ESP32)", command=self.probe_chip).pack(side="left")
        tf = ttk.Frame(mid)
        tf.pack(fill="both", expand=True, pady=4)
        cols = (("name", "Gerät", 260), ("target", "Target", 170), ("chip", "Chip", 80),
                ("support", "Support", 90), ("method", "Methode", 110), ("size", "Größe", 70))
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in cols], show="headings", height=8, selectmode="browse")
        for k, t, w in cols:
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._dev_selected())
        self.dev_info = ttk.Label(mid, text="", foreground="#335", wraplength=1000, justify="left")
        self.dev_info.pack(fill="x")

        opt = ttk.LabelFrame(self, text="3. Anschluss & Modus", padding=6)
        opt.pack(fill="x")
        ttk.Label(opt, text="Port:").grid(row=0, column=0, sticky="w")
        self.v_port = tk.StringVar(value=self.settings.get("flash_port", ""))
        self.cb_port = ttk.Combobox(opt, textvariable=self.v_port, width=40)
        self.cb_port.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(opt, text="🔄", width=3, command=self.refresh_ports).grid(row=0, column=2)
        ttk.Label(opt, text="Baudrate:").grid(row=0, column=3, sticky="e", padx=(12, 2))
        self.v_baud = tk.StringVar(value=self.settings.get("flash_baud", "921600"))
        ttk.Combobox(opt, textvariable=self.v_baud, width=9, state="readonly",
                     values=["921600", "460800", "230400", "115200"]).grid(row=0, column=4, sticky="w")
        self.v_mode = tk.StringVar(value="update")
        ttk.Radiobutton(opt, text="Update (Einstellungen & Knoten bleiben erhalten)", value="update",
                        variable=self.v_mode).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Radiobutton(opt, text="Komplett neu installieren (Flash löschen – alle Einstellungen weg)",
                        value="full", variable=self.v_mode).grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(opt, text="Tipp: Bei Board-Problemen, Bootschleifen oder Wechsel ESP32-Version → "
                            "„Komplett neu installieren“. Bei 921600 Baud Fehlern → 115200 wählen.",
                  foreground="gray").grid(row=3, column=0, columnspan=6, sticky="w")
        ttk.Button(opt, text="🔁 Bootmodus (1200-bps-Reset)", command=self.reset_1200).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="Gerät wird nicht erkannt? USB-Kabel mit Daten prüfen. ESP32-S3 (XIAO, Heltec V3): BOOT/USR-Taste "
                            "beim Einstecken festhalten – oder diesen 1200-bps-Reset versuchen, dann neu flashen.",
                  foreground="gray", wraplength=760, justify="left").grid(row=4, column=2, columnspan=4, sticky="w",
                                                                            pady=(6, 0))

        tw = ttk.LabelFrame(self, text="4. TAKweb aufs Gerät (eigene WLAN-Webseite, nur ESP32)", padding=6)
        tw.pack(fill="x", pady=(6, 0))
        ttk.Label(tw, text="Paket:").grid(row=0, column=0, sticky="w")
        self.lbl_tw = ttk.Label(tw, text="– keins geladen –", foreground="gray")
        self.lbl_tw.grid(row=0, column=1, columnspan=3, sticky="w", padx=4)
        ttk.Button(tw, text="📦 Paket (ZIP)…", command=self.takweb_import).grid(row=0, column=4, padx=2)
        ttk.Button(tw, text="🌐 Vom TAKweb-Server…", command=self.takweb_download).grid(row=0, column=5, padx=2)
        ttk.Label(tw, text="Hotspot-Name:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.v_tw_ssid = tk.StringVar(value=self.settings.get("takweb_ssid", "TAKweb"))
        ttk.Entry(tw, textvariable=self.v_tw_ssid, width=22).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(tw, text="Passwort:").grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.v_tw_psk = tk.StringVar(value=self.settings.get("takweb_psk", "12345678"))
        ttk.Entry(tw, textvariable=self.v_tw_psk, width=18).grid(row=1, column=3, sticky="w", padx=4, pady=(4, 0))
        self.v_tw_wifi = tk.BooleanVar(value=self.settings.get("takweb_wifi", True))
        ttk.Checkbutton(tw, text="WLAN am Gerät setzen", variable=self.v_tw_wifi).grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(tw, text="Adresse:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.v_tw_ip = tk.StringVar(value=self.settings.get("takweb_ip", self.TW_IPHONE))
        ttk.Combobox(tw, textvariable=self.v_tw_ip, width=44, values=[self.TW_IPHONE, self.TW_DHCP]).grid(
            row=2, column=1, columnspan=3, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(tw, text="(oder eigene feste IP eintragen)", foreground="gray").grid(
            row=2, column=4, columnspan=2, sticky="w", pady=(4, 0))
        self.v_tw_after = tk.BooleanVar(value=self.settings.get("takweb_after", False))
        ttk.Checkbutton(tw, text="nach dem Flashen (ESP32) automatisch aufspielen", variable=self.v_tw_after).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.btn_tw = ttk.Button(tw, text="🗺 TAKweb aufspielen", command=self.takweb_install)
        self.btn_tw.grid(row=3, column=4, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Label(tw, text="Am einfachsten: die TAKweb-Firmware (Version „…-takweb“, „Online laden…“) mit „Komplett neu "
                           "installieren“ flashen – TAKweb ist dann schon drauf und das Gerät öffnet IMMER ein eigenes "
                           "WLAN „TAKweb“ (Passwort 12345678): Handy verbinden, https://takweb.local öffnen, Zertifikat "
                           "einmal akzeptieren. Der Hotspot-Name hier ist dann ein ZUSÄTZLICHES WLAN mit Internet "
                           "(z. B. Handy-Hotspot), mit dem sich das Gerät außerdem verbindet – geht auch in TAKweb unter "
                           "Meshtastic → „Internet over WLAN“. Auf ESP32-S3 (XIAO, Heltec V3) bleibt Bluetooth an. "
                           "Mit normaler Meshtastic-Firmware verbindet sich das Gerät dagegen mit einem Handy-Hotspot "
                           f"dieses Namens (iPhone: feste Adresse https://{takweb.IPHONE_IP}). "
                           "Solange WLAN an ist, ist Bluetooth am Gerät aus.",
                  foreground="gray", wraplength=1000, justify="left").grid(row=4, column=0, columnspan=6, sticky="w",
                                                                           pady=(4, 0))
        self.tw_pkg = None

        act = ttk.Frame(self)
        act.pack(fill="x", pady=6)
        self.btn_flash = ttk.Button(act, text="⚡ Firmware flashen", command=self.flash_selected)
        self.btn_flash.pack(side="left")
        ttk.Button(act, text="📄 Eigene Datei flashen…", command=self.flash_custom_file).pack(side="left", padx=4)
        ttk.Button(act, text="🧹 Nur Flash löschen (ESP32)", command=self.erase_only).pack(side="left")
        self.progress = ttk.Progressbar(act, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.lbl_state = ttk.Label(act, text="Bereit", width=28)
        self.lbl_state.pack(side="left")

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=9, font=("Consolas", 9), wrap="word")
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # ------------------------------------------------------------------ Log / Queue
    def logmsg(self, s):
        self.q.put(("log", s))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log_ui(val)
                elif kind == "prog":
                    self.progress["value"] = val
                elif kind == "state":
                    self.lbl_state.configure(text=val)
                elif kind == "done":
                    self.busy = False
                    self.btn_flash.configure(state="normal")
                    self.btn_tw.configure(state="normal")
                    ok, msg = val
                    self.progress["value"] = 100 if ok else self.progress["value"]
                    self.lbl_state.configure(text="✔ Fertig" if ok else "✖ Fehler")
                    (messagebox.showinfo if ok else messagebox.showerror)(APP_TITLE, msg, parent=self)
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _log_ui(self, s):
        # esptool nutzt \r für Fortschrittsbalken -> letzte Zeile ersetzen
        for chunk in re.split(r"(\r|\n)", s):
            if chunk == "\n":
                self.log.insert("end", "\n")
            elif chunk == "\r":
                self.log.delete("end-1c linestart", "end-1c")
            elif chunk:
                self.log.insert("end", chunk)
                m = re.findall(r"(\d{1,3}(?:\.\d+)?)\s?%", chunk)
                if m:
                    try:
                        self.progress["value"] = min(100.0, float(m[-1]))
                    except ValueError:
                        pass
        self.log.see("end")

    # ------------------------------------------------------------------ Bibliothek
    def reload_library(self, select=None):
        self.versions = self.lib.versions()
        labels = []
        for v in self.versions:
            if v["key"] == CUSTOM_KEY:
                labels.append(f"{CUSTOM_KEY}  ({len(v['devices'])})")
            else:
                ch = f" – {v['channel']}" if v.get("channel") else ""
                labels.append(f"{v['version']}{ch}  ({len(v['devices'])} Geräte)")
        self.cb_version["values"] = labels
        if not labels:
            self.v_version.set("")
            self.lib_info.configure(text="Noch keine Firmware – „Paket importieren“ oder „Online laden“.")
        else:
            idx = 0
            if select:
                for i, v in enumerate(self.versions):
                    if v["version"] == select or v["key"] == select:
                        idx = i
            self.cb_version.current(idx)
            n = sum(len(v["devices"]) for v in self.versions if v["key"] != CUSTOM_KEY)
            self.lib_info.configure(text=f"{len(self.versions)} Version(en), {n} Geräte-Images offline")
        self.show_devices()

    def cur_version(self):
        i = self.cb_version.current()
        return self.versions[i] if 0 <= i < len(self.versions) else None

    def show_devices(self):
        v = self.cur_version()
        self.tree.delete(*self.tree.get_children())
        self.cur_devices = []
        if not v:
            return
        chips = sorted({chip_of(m) for m in v["devices"]})
        self.cb_chipf["values"] = ["alle Chips"] + chips
        flt = self.v_filter.get().lower().strip()
        cf = self.v_chipf.get()
        devs = sorted(v["devices"], key=lambda m: ((m.get("supportLevel") or 9), (m.get("displayName") or "").lower()))
        for i, m in enumerate(devs):
            chip = chip_of(m)
            text = " ".join(str(m.get(k, "")) for k in ("displayName", "platformioTarget", "hwModelSlug")).lower()
            if flt and flt not in text:
                continue
            if cf not in ("", "alle Chips") and chip != cf:
                continue
            size = sum(f.get("bytes", 0) for f in m.get("files", []) if not str(f.get("name")).endswith(".elf"))
            method = "esptool" if is_esp(chip) else ("UF2-Laufwerk" if is_uf2_chip(chip) else "nicht unterstützt")
            sup = SUPPORT.get(m.get("supportLevel"), "")
            if m.get("custom"):
                sup = "eigene Datei"
            iid = str(len(self.cur_devices))
            self.cur_devices.append(m)
            self.tree.insert("", "end", iid=iid, values=(m.get("displayName") or m.get("platformioTarget"),
                                                         m.get("platformioTarget"), chip, sup, method, human(size)))

    def sel_device(self):
        s = self.tree.selection()
        return self.cur_devices[int(s[0])] if s else None

    def _dev_selected(self):
        m = self.sel_device()
        if not m:
            return
        f = device_files(m)
        parts = [f"Version {m.get('version')}", f"Chip {chip_of(m)}", f"HW {m.get('hwModelSlug', '-')}"]
        if is_esp(chip_of(m)):
            parts.append("Dateien: " + ", ".join(x for x in (f["factory"], f["app"], f["ota"], f["littlefs"]) if x))
        else:
            parts.append("Datei: " + (f["uf2"] or "– keine .uf2 –"))
        if m.get("note"):
            parts.append(m["note"])
        if m.get("requiresDfu"):
            parts.append("Gerät muss in den Bootloader (wird automatisch versucht).")
        self.dev_info.configure(text="  ·  ".join(parts))

    def import_packages(self):
        fns = filedialog.askopenfilenames(parent=self, title="Firmware-Paket(e) importieren",
                                          filetypes=[("Firmware-Pakete", "*.zip"), ("Alle Dateien", "*.*")])
        if not fns:
            return
        last = None
        for fn in fns:
            try:
                last = self.lib.import_zip(fn, self.logmsg)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"{os.path.basename(fn)}:\n{e}", parent=self)
        self.reload_library(select=last)

    def open_folder(self):
        d = self.lib.base
        v = self.cur_version()
        if v and v.get("dir"):
            d = v["dir"]
        try:
            if sys.platform.startswith("win"):
                os.startfile(d)  # noqa
            else:
                import subprocess
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", d])
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e), parent=self)

    def delete_version(self):
        v = self.cur_version()
        if not v or v["key"] == CUSTOM_KEY:
            return
        if messagebox.askyesno(APP_TITLE, f"Firmware {v['version']} aus der Offline-Bibliothek löschen?", parent=self):
            self.lib.delete(v["key"])
            self.reload_library()

    # ------------------------------------------------------------------ Ports / Erkennung
    def refresh_ports(self):
        ports = list_serial_ports()
        vals = [f"{d}  –  {desc}" if desc else d for d, desc in ports]
        self.cb_port["values"] = vals
        cur = self.v_port.get().split("  –  ")[0]
        if vals and cur not in [d for d, _ in ports]:
            self.v_port.set(vals[0])

    def port(self):
        return self.v_port.get().split("  –  ")[0].strip()

    def detect_device(self):
        iface = self.get_iface()
        if not iface:
            messagebox.showinfo(APP_TITLE, "Kein Meshtastic-Gerät in der App verbunden.\n"
                                           "Verbinde das Gerät zuerst (oben: Verbindung) – dann wird das passende "
                                           "Board automatisch ausgewählt.\nOder: „Chip am Port prüfen“.", parent=self)
            return
        info = iface.getMyNodeInfo() or {}
        hw = (info.get("user") or {}).get("hwModel", "")
        fw = ""
        try:
            fw = iface.metadata.firmware_version
        except Exception:
            pass
        dev_path = getattr(iface, "devPath", None)
        if dev_path:
            self.v_port.set(dev_path)
        self.logmsg(f"Verbundenes Gerät: Hardware {hw}, Firmware {fw or '?'}, Port {dev_path or '-'}\n")
        self.v_filter.set("")
        self.v_chipf.set("alle Chips")
        self.show_devices()
        for iid, m in enumerate(self.cur_devices):
            if hw and (m.get("hwModelSlug") == hw):
                self.tree.selection_set(str(iid))
                self.tree.see(str(iid))
                return
        messagebox.showinfo(APP_TITLE, f"Hardware „{hw}“ ist in der gewählten Firmware-Version nicht enthalten.\n"
                                       "Andere Version wählen oder eigene Datei flashen.", parent=self)

    def reset_1200(self):
        """1200-bps-Reset (wie im Meshtastic-Web-Flasher): bringt viele Geräte in den Boot-/Download-Modus."""
        port = self.port()
        if not port or self.busy:
            if not port:
                messagebox.showwarning(APP_TITLE, "Bitte zuerst den USB-Port wählen.", parent=self)
            return
        self._prepare_port(port)

        def work():
            try:
                touch_1200(port)
                time.sleep(2)
                self.q.put(("call", self.refresh_ports))
                self.q.put(("done", (True, "1200-bps-Reset gesendet. Das Gerät sollte jetzt im Bootmodus sein "
                                           "(der Port kann sich dabei ändern – ggf. neu wählen), dann flashen.\n\n"
                                           "Klappt es nicht: BOOT/USR-Taste beim Einstecken des USB-Kabels festhalten.")))
            except Exception as e:
                self.q.put(("done", (False, f"1200-bps-Reset nicht möglich: {e}")))
        self._start(work, "Bootmodus …")

    def probe_chip(self):
        port = self.port()
        if not port or self.busy:
            return
        self._prepare_port(port)

        def work():
            try:
                self._esptool(["--port", port, "--baud", "115200", "flash-id"])
                self.q.put(("done", (True, "Chip-Informationen siehe Protokoll.")))
            except Exception as e:
                self.q.put(("done", (False, f"Kein ESP32 erkannt: {e}\n\nBei nRF52/RP2040 ist das normal.")))
        self._start(work, "Prüfe Chip …")

    # ------------------------------------------------------------------ Flashen
    def _start(self, fn, state):
        wait = getattr(self, "_port_wait", 0)
        self._port_wait = 0
        if wait:
            inner = fn

            def fn():
                time.sleep(wait)   # Port nach dem Trennen kurz freigeben lassen
                inner()
        self.busy = True
        self.btn_flash.configure(state="disabled")
        self.btn_tw.configure(state="disabled")
        self.progress["value"] = 0
        self.lbl_state.configure(text=state)
        threading.Thread(target=fn, daemon=True).start()

    def _prepare_port(self, port):
        """Gibt den Port frei, falls die App gerade darüber verbunden ist."""
        iface = self.get_iface()
        if iface is not None:
            self.logmsg("Trenne die App-Verbindung, damit der Port frei ist …\n")
            try:
                self.disconnect_app()
            except Exception:
                pass
            self._port_wait = 1.5
        self._baud = self.v_baud.get()          # Tk-Werte nur im Haupt-Thread lesen
        self.settings["flash_port"] = self.v_port.get()
        self.settings["flash_baud"] = self._baud
        self.save_settings(self.settings)

    def _esptool(self, args):
        import esptool
        w = _QueueWriter(self.logmsg)
        self.logmsg("\n$ esptool " + " ".join(args) + "\n")
        with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
            try:
                esptool.main(args)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise RuntimeError(f"esptool beendet mit Code {e.code}")

    def flash_selected(self):
        if self.busy:
            return
        m = self.sel_device()
        if not m:
            messagebox.showinfo(APP_TITLE, "Bitte zuerst ein Gerät auswählen.", parent=self)
            return
        chip = chip_of(m)
        f = device_files(m)
        mode = self.v_mode.get()
        name = m.get("displayName") or m.get("platformioTarget")
        if is_esp(chip):
            port = self.port()
            if not port:
                messagebox.showwarning(APP_TITLE, "Bitte den USB-Port des Geräts wählen.", parent=self)
                return
            if not (f["factory"] or f["app"]):
                messagebox.showerror(APP_TITLE, "Für dieses Gerät fehlen die .bin-Dateien.", parent=self)
                return
            txt = (f"{name}\nVersion {m.get('version')}  ·  Chip {chip}  ·  Port {port}\n\n"
                   + ("KOMPLETT NEU INSTALLIEREN – der gesamte Flash wird gelöscht (Einstellungen, Knoten, "
                      "Schlüssel gehen verloren)." if mode == "full" else "Update – Einstellungen bleiben erhalten."))
            if not messagebox.askyesno(APP_TITLE, txt + "\n\nJetzt flashen?", parent=self):
                return
            self._prepare_port(port)
            self._tw_after = self._takweb_params(quiet=True) if self.v_tw_after.get() else None
            self._start(lambda: self._flash_esp(port, chip, f, mode, name), "Flashe …")
        elif not is_uf2_chip(chip):
            messagebox.showinfo(APP_TITLE, f"{name} ({chip}) kann hier nicht geflasht werden.\n\n"
                                           "STM32-Geräte (z. B. Wio-E5) brauchen einen ST-Link bzw. STM32CubeProgrammer. "
                                           "Die Firmware-Dateien liegen im Ordner (Knopf „Ordner“).", parent=self)
            return
        else:
            if not f["uf2"]:
                messagebox.showerror(APP_TITLE, "Für dieses Gerät gibt es keine .uf2-Datei.", parent=self)
                return
            txt = (f"{name}\nVersion {m.get('version')}  ·  Chip {chip}\n\nDas Gerät wird in den Bootloader "
                   "versetzt und die Firmware auf das UF2-Laufwerk kopiert."
                   + ("\n\nKOMPLETT NEU: vorher wird der Speicher gelöscht (Factory Erase)." if mode == "full" else ""))
            if not messagebox.askyesno(APP_TITLE, txt + "\n\nFortfahren?", parent=self):
                return
            uf2s = []
            if mode == "full" and chip.startswith("nrf52") and f["erase_uf2"]:
                uf2s.append(("erase", f["erase_uf2"]))
            uf2s.append(("fw", os.path.join(f["dir"], f["uf2"])))
            self._start_uf2(uf2s, chip, name)

    def _flash_esp(self, port, chip, f, mode, name, custom_offset=None, custom_file=None):
        try:
            d = f["dir"] if f else None
            base = ["--chip", chip or "auto", "--port", port, "--baud", self._baud,
                    "--before", "default-reset", "--after", "hard-reset"]
            tmp = None
            if custom_file:
                if mode == "full":
                    self.q.put(("state", "Lösche Flash …"))
                    self._esptool(base[:-2] + ["--after", "no-reset", "erase-flash"])
                pairs = [(custom_offset, custom_file)]
            elif mode == "full":
                if not f["factory"]:
                    raise RuntimeError("Keine factory.bin für die Neuinstallation vorhanden.")
                self.q.put(("state", "Lösche Flash …"))
                self._esptool(base[:-2] + ["--after", "no-reset", "erase-flash"])
                pairs = [(0x0, os.path.join(d, f["factory"]))]
                if f["ota"] and f["ota_off"]:
                    pairs.append((f["ota_off"], os.path.join(d, f["ota"])))
                if f["littlefs"] and f["fs_off"]:
                    pairs.append((f["fs_off"], os.path.join(d, f["littlefs"])))
            else:
                if f["app"]:
                    app = os.path.join(d, f["app"])
                else:   # nur factory.bin vorhanden -> App-Teil herausschneiden
                    data = open(os.path.join(d, f["factory"]), "rb").read()
                    off = f["app_off"] or 0x10000
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
                    tmp.write(data[off:])
                    tmp.close()
                    app = tmp.name
                    self.logmsg(f"App-Teil ab 0x{off:x} aus factory.bin extrahiert.\n")
                pairs = [(f["app_off"] or 0x10000, app)]
            args = base + ["write-flash"]
            for off, fn in pairs:
                args += [hex(off), fn]
            self.q.put(("state", "Schreibe Firmware …"))
            self._esptool(args)
            if tmp:
                os.unlink(tmp.name)
            after = getattr(self, "_tw_after", None) if not custom_file else None
            self._tw_after = None
            if after:
                self.logmsg("\nFirmware geschrieben – warte auf den Neustart des Geräts, dann kommt TAKweb …\n")
                self._takweb_work(port, *after, first_wait=8)
                return
            self.q.put(("done", (True, f"{name} wurde erfolgreich geflasht.\n\nDas Gerät startet neu. "
                                       "Bei Neuinstallation: Region (LoRa) wieder einstellen!")))
        except Exception as e:
            msg = str(e)
            hint = ""
            if "busy" in msg.lower() or "could not open" in msg.lower() or "permission" in msg.lower():
                hint = "\n\nPort belegt? Andere Programme (Meshtastic-App, Browser, Serial-Monitor) schließen."
            elif "wrong chip" in msg.lower() or "is not" in msg.lower() and "chip" in msg.lower():
                hint = "\n\nDie Firmware passt nicht zum Chip des Geräts – anderes Board wählen."
            elif "failed to connect" in msg.lower() or "timed out" in msg.lower():
                hint = ("\n\nVerbindung zum Chip fehlgeschlagen: Baudrate 115200 versuchen, anderes USB-Kabel "
                        "(Datenkabel!) nutzen, „🔁 Bootmodus (1200-bps-Reset)“ versuchen, oder BOOT/PRG-Taste gedrückt "
                        "halten und kurz RST drücken (ESP32-S3: BOOT beim Einstecken des Kabels festhalten).")
            self.logmsg(f"\nFEHLER: {msg}\n")
            self.q.put(("done", (False, f"Flashen fehlgeschlagen:\n{msg}{hint}")))

    # ------------------------------------------------------------------ TAKweb (takweb.py)
    def _takweb_dir(self):
        d = os.path.join(self.data_dir, "takweb")
        os.makedirs(d, exist_ok=True)
        return d

    def _takweb_set(self, pkg, path=None):
        self.tw_pkg = pkg
        if pkg.raw and not path:            # in der Bibliothek ablegen -> später offline da
            path = os.path.join(self._takweb_dir(), f"takweb-mesh-{re.sub(r'[^\w.-]', '_', pkg.version)}.zip")
            with open(path, "wb") as fh:
                fh.write(pkg.raw)
        if path:
            self.settings["takweb_pkg"] = path
            self.save_settings(self.settings)
        self.lbl_tw.configure(text=f"TAKweb {pkg.version} – {len(pkg.files)} Dateien, {human(pkg.total)} im Flash",
                              foreground="#335")

    def _takweb_load_saved(self):
        p = self.settings.get("takweb_pkg")
        if p and os.path.exists(p):
            try:
                self._takweb_set(takweb.Package.from_file(p), p)
            except Exception as e:
                self.lbl_tw.configure(text=f"Paket nicht lesbar: {e}", foreground="#a00")

    def takweb_import(self):
        p = filedialog.askopenfilename(parent=self, title="TAKweb-Paket wählen",
                                       filetypes=[("TAKweb-Paket", "takweb-mesh*.zip"), ("ZIP", "*.zip")])
        if not p:
            return
        try:
            self._takweb_set(takweb.Package.from_file(p))
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e), parent=self)

    def takweb_download(self):
        url = simpledialog.askstring(APP_TITLE, "Adresse des TAKweb-Servers (z. B. https://tak.example.org:8443):",
                                     initialvalue=self.settings.get("takweb_server", ""), parent=self)
        if not url:
            return
        self.settings["takweb_server"] = url.strip()
        self.lbl_tw.configure(text="Lade Paket …", foreground="gray")

        def work():
            try:
                pkg = takweb.Package.from_server(url)
                self.q.put(("call", lambda: self._takweb_set(pkg)))
            except Exception as e:
                err = str(e)
                self.q.put(("call", lambda: (self.lbl_tw.configure(text="Nicht geladen", foreground="#a00"),
                                             messagebox.showerror(APP_TITLE, err, parent=self))))
        threading.Thread(target=work, daemon=True).start()

    def _takweb_params(self, quiet=False):
        """(Paket, WLAN) aus der Oberfläche – im Haupt-Thread aufrufen; None bei Fehler"""
        def fail(msg):
            if quiet:
                self.logmsg(f"TAKweb wird nicht aufgespielt: {msg}\n")
            else:
                messagebox.showwarning(APP_TITLE, msg, parent=self)
            return None
        if not self.tw_pkg:
            return fail("Zuerst ein TAKweb-Paket laden (ZIP aus dem TAKweb-Admin oder „Vom TAKweb-Server…“).")
        wifi = None
        ssid, psk = self.v_tw_ssid.get().strip(), self.v_tw_psk.get()
        ip_txt = self.v_tw_ip.get().strip()
        static_ip = None if ip_txt in ("", self.TW_DHCP) or ip_txt.startswith("auto") else ip_txt.split()[0]
        if self.v_tw_wifi.get():
            if not ssid:
                return fail("Bitte den Hotspot-Namen eintragen (oder „WLAN am Gerät setzen“ abwählen).")
            err = takweb.check_wifi(ssid, psk, static_ip)
            if err:
                return fail(err)
            wifi = (ssid, psk, static_ip)
        self.settings.update(takweb_ssid=ssid, takweb_psk=psk, takweb_wifi=self.v_tw_wifi.get(), takweb_ip=ip_txt,
                             takweb_after=self.v_tw_after.get())
        self.save_settings(self.settings)
        return self.tw_pkg, wifi

    def takweb_install(self):
        if self.busy:
            return
        port = self.port()
        if not port:
            messagebox.showwarning(APP_TITLE, "Bitte den USB-Port des Geräts wählen (Abschnitt 3).", parent=self)
            return
        params = self._takweb_params()
        if not params:
            return
        pkg, wifi = params
        txt = (f"TAKweb {pkg.version} ({human(pkg.total)}) auf das Gerät an {port} schreiben?\n\n"
               "Die bisherige Web-Oberfläche des Geräts (/static) wird ersetzt, Einstellungen und Knoten bleiben."
               + (f"\n\nWLAN danach: „{wifi[0]}“, Adresse " + (wifi[2] or "automatisch (DHCP)")
                  + " – das Gerät startet neu, Bluetooth ist dann aus." if wifi else ""))
        if not messagebox.askyesno(APP_TITLE, txt, parent=self):
            return
        self._prepare_port(port)
        self._start(lambda: self._takweb_work(port, pkg, wifi), "TAKweb …")

    def _takweb_work(self, port, pkg, wifi, first_wait=0):
        labels = {"remove": "Alte Web-Dateien löschen", "upload": "Schreibe TAKweb", "check": "Prüfe",
                  "wifi": "Setze WLAN", "done": "Fertig"}

        def progress(step, done, total, name=""):
            self.q.put(("state", labels.get(step, step)))
            if step == "upload":
                self.q.put(("prog", 100.0 * done / max(total, 1)))

        try:
            if first_wait:
                time.sleep(first_wait)
            last = None
            # nach dem Flashen braucht das Gerät etwas, bis die API antwortet (Port kann kurz weg sein)
            for attempt in range(4 if first_wait else 1):
                try:
                    takweb.install_serial(port, pkg, wifi, log=self.logmsg, progress=progress)
                    last = None
                    break
                except Exception as e:
                    last = e
                    if attempt < (3 if first_wait else 0):
                        self.logmsg(f"Gerät noch nicht bereit ({e}) – neuer Versuch …\n")
                        time.sleep(6)
            if last:
                raise last
            msg = f"TAKweb {pkg.version} ist auf dem Gerät."
            if wifi:
                where = (f"https://{wifi[2]}" if wifi[2] else
                         "https://<IP des Geräts> (Display) oder https://meshtastic.local")
                msg += (f"\n\nDas Gerät startet neu und verbindet sich mit dem WLAN „{wifi[0]}“.\n"
                        f"Handy: Hotspot einschalten, dann im Browser {where} öffnen (Lesezeichen anlegen) und das "
                        "Zertifikat einmal akzeptieren.")
            self.q.put(("done", (True, msg)))
        except Exception as e:
            hint = ""
            if isinstance(e, ImportError):
                hint = "\n\nPython-Paket fehlt: pip install meshtastic pyserial"
            elif "busy" in str(e).lower() or "could not open" in str(e).lower() or "permission" in str(e).lower():
                hint = "\n\nPort belegt? Andere Programme (Meshtastic-App, Browser, Serial-Monitor) schließen."
            elif "keine Antwort" in str(e):
                hint = "\n\nLäuft Meshtastic-Firmware auf dem Gerät? Richtiger Port? Kabel mit Daten?"
            self.logmsg(f"\nFEHLER: {e}\n")
            self.q.put(("done", (False, f"TAKweb nicht aufgespielt:\n{e}{hint}")))

    def erase_only(self):
        port = self.port()
        if self.busy or not port:
            return
        if not messagebox.askyesno(APP_TITLE, f"Den gesamten Flash des ESP32 an {port} löschen?\n"
                                              "Danach muss eine Firmware neu installiert werden.", parent=self):
            return
        self._prepare_port(port)

        def work():
            try:
                self._esptool(["--chip", "auto", "--port", port, "--baud", self._baud, "erase-flash"])
                self.q.put(("done", (True, "Flash gelöscht.")))
            except Exception as e:
                self.q.put(("done", (False, f"Löschen fehlgeschlagen: {e}")))
        self._start(work, "Lösche Flash …")

    # ------------------------------------------------------------------ UF2
    def _start_uf2(self, steps, chip, name):
        port = self.port()
        iface = self.get_iface()
        if iface is not None:
            # Meshtastic-Befehl "in den Bootloader neu starten" (zuverlässigste Methode)
            try:
                self.logmsg("Sende Meshtastic-Befehl: Neustart in den Bootloader (DFU) …\n")
                iface.localNode.enterDFUMode()
                time.sleep(1.0)
            except Exception as e:
                self.logmsg(f"DFU-Befehl nicht möglich: {e}\n")
            try:
                self.disconnect_app()
            except Exception:
                pass
        self._start(lambda: self._uf2_worker(steps, chip, name, port), "Warte auf Bootloader …")

    def _wait_drive(self, timeout, want_present=True):
        t0 = time.time()
        while time.time() - t0 < timeout:
            drives = find_uf2_drives()
            if want_present and drives:
                return drives[0]
            if not want_present and not drives:
                return True
            time.sleep(0.5)
        return None

    def _ask_bootloader(self, name):
        ev = threading.Event()
        res = {}

        def ask():
            res["ok"] = messagebox.askokcancel(
                APP_TITLE, f"Bitte {name} in den Bootloader versetzen:\n\n"
                           "• nRF52 (RAK, T-Echo, Heltec T114 …): RESET-Taste zweimal schnell drücken\n"
                           "• RP2040/RP2350: BOOTSEL gedrückt halten und USB einstecken / RESET drücken\n\n"
                           "Es erscheint ein USB-Laufwerk (z. B. „RAK4631“, „TECHOBOOT“, „RPI-RP2“).\n"
                           "Dann OK klicken.", parent=self)
            ev.set()
        self.q.put(("call", ask))
        ev.wait()
        return res.get("ok")

    def _uf2_worker(self, steps, chip, name, port):
        try:
            drive = self._wait_drive(4)
            if not drive and port:
                self.logmsg(f"Versuche 1200-Baud-Reset an {port} …\n")
                try:
                    touch_1200(port)
                except Exception as e:
                    self.logmsg(f"1200-Baud-Reset nicht möglich: {e}\n")
                drive = self._wait_drive(15)
            while not drive:
                if not self._ask_bootloader(name):
                    raise RuntimeError("Abgebrochen.")
                drive = self._wait_drive(20)
            for i, (kind, item) in enumerate(steps):
                info = read_uf2_info(drive)
                self.logmsg(f"UF2-Laufwerk: {drive}\n{info.strip()}\n")
                if kind == "erase":
                    sd = "7.3.0" if re.search(r"S140\s*7", info) else "6.1.0"
                    cand = [x for x in item if sd in x] or item
                    path = os.path.join(os.path.dirname(steps[-1][1]), cand[0])
                else:
                    path = item
                fam = uf2_family(path)
                if fam and fam != "unbekannt" and chip and not chip.startswith(fam[:5]) and not fam.startswith(chip[:5]):
                    raise RuntimeError(f"Die Datei ist für {fam}, das Gerät/Board ist {chip}.")
                if "RP2" in info and fam == "nrf52840" or "nRF52" in info and fam and fam.startswith("rp"):
                    raise RuntimeError(f"Die UF2-Datei ({fam}) passt nicht zum Bootloader:\n{info.strip()}")
                self.q.put(("state", "Lösche Speicher …" if kind == "erase" else "Kopiere Firmware …"))
                self._copy_uf2(path, drive)
                self.logmsg("Warte auf Neustart des Geräts …\n")
                self._wait_drive(30, want_present=False)
                if kind == "erase" and i < len(steps) - 1:
                    self.logmsg("Speicher gelöscht. Gerät muss erneut in den Bootloader.\n")
                    drive = self._wait_drive(20)
                    while not drive:
                        if not self._ask_bootloader(name):
                            raise RuntimeError("Abgebrochen nach dem Löschen.")
                        drive = self._wait_drive(20)
            self.q.put(("done", (True, f"{name}: Firmware kopiert. Das Gerät startet neu.\n"
                                       "Bei Neuinstallation: Region (LoRa) wieder einstellen!")))
        except Exception as e:
            self.logmsg(f"\nFEHLER: {e}\n")
            self.q.put(("done", (False, f"UF2-Flashen fehlgeschlagen:\n{e}")))

    def _copy_uf2(self, src, drive):
        total = os.path.getsize(src)
        dst = os.path.join(drive, os.path.basename(src))
        self.logmsg(f"Kopiere {os.path.basename(src)} ({human(total)}) → {drive}\n")
        done = 0
        try:
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    b = fi.read(64 * 1024)
                    if not b:
                        break
                    fo.write(b)
                    done += len(b)
                    self.q.put(("prog", done * 100.0 / total))
                fo.flush()
                try:
                    os.fsync(fo.fileno())
                except OSError:
                    pass
        except OSError as e:
            # Gerät startet nach dem letzten Block sofort neu -> Schreibfehler am Ende ist normal
            if done < total:
                raise
            self.logmsg(f"(Hinweis beim Schließen: {e} – normal, Gerät startet neu)\n")

    # ------------------------------------------------------------------ Eigene Datei
    def flash_custom_file(self):
        if self.busy:
            return
        fn = filedialog.askopenfilename(parent=self, title="Firmware-Datei wählen",
                                        filetypes=[("Firmware", "*.bin *.uf2"), ("Alle Dateien", "*.*")])
        if not fn:
            return
        if fn.lower().endswith(".uf2"):
            fam = uf2_family(fn) or "unbekannt"
            if not messagebox.askyesno(APP_TITLE, f"UF2-Datei für {fam}:\n{os.path.basename(fn)}\n\n"
                                                  "Auf das Gerät kopieren?", parent=self):
                return
            self._start_uf2([("fw", fn)], fam if fam != "unbekannt" else "", os.path.basename(fn))
            return
        if fn.lower().endswith(".zip"):
            messagebox.showinfo(APP_TITLE, "OTA-ZIP-Dateien (nRF52-DFU) werden nicht unterstützt – bitte die .uf2 "
                                           "verwenden.", parent=self)
            return
        try:
            info = analyze_esp_image(fn)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Datei nicht lesbar: {e}", parent=self)
            return
        if not info["kind"]:
            messagebox.showerror(APP_TITLE, "Das ist kein ESP32-Firmware-Image (falsche Kennung).", parent=self)
            return
        CustomDialog(self, fn, info)

    def run_custom(self, fn, info, offset, full, save_name=None):
        if save_name:
            try:
                self.lib.add_custom(fn, info, save_name)
                self.reload_library(select=CUSTOM_KEY)
            except Exception as e:
                self.logmsg(f"Übernahme in Bibliothek fehlgeschlagen: {e}\n")
        port = self.port()
        if not port:
            messagebox.showwarning(APP_TITLE, "Bitte den USB-Port wählen.", parent=self)
            return
        self._prepare_port(port)
        self._start(lambda: self._flash_esp(port, info["chip"], None, "full" if full else "update",
                                            os.path.basename(fn), custom_offset=offset, custom_file=fn),
                    "Flashe eigene Datei …")

    # ------------------------------------------------------------------ Online
    def online_dialog(self):
        OnlineDialog(self)


class CustomDialog(tk.Toplevel):
    def __init__(self, fl, fn, info):
        super().__init__(fl)
        self.fl, self.fn, self.info = fl, fn, info
        self.title("Eigene Firmware-Datei flashen")
        self.resizable(False, False)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both")
        lines = [f"Datei: {os.path.basename(fn)} ({human(info['size'])})",
                 f"Art: {'Factory-Image (Bootloader + Partitionen + App)' if info['kind'] == 'factory' else 'App-Image'}",
                 f"Chip: {info['chip']}", f"Version: {info.get('version') or 'unbekannt'}",
                 f"Board/Target: {info.get('target') or 'unbekannt'}"]
        for p in info.get("partitions", [])[:8]:
            lines.append(f"   Partition {p['name']:<10} @0x{p['offset']:06x}  {human(p['size'])}")
        ttk.Label(f, text="\n".join(lines), font=("Consolas", 9), justify="left").grid(row=0, column=0, columnspan=3,
                                                                                    sticky="w")
        ttk.Label(f, text="Adresse:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.v_off = tk.StringVar(value=hex(info["offset"] or 0))
        ttk.Entry(f, textvariable=self.v_off, width=12).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f, text="(Factory: 0x0, App-Update: 0x10000)", foreground="gray").grid(row=1, column=2, sticky="w",
                                                                                     pady=(8, 0))
        self.v_full = tk.BooleanVar(value=info["kind"] == "factory")
        ttk.Checkbutton(f, text="Vorher kompletten Flash löschen (empfohlen bei Factory-Image / Problem-Boards)",
                        variable=self.v_full).grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
        self.v_save = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="In die Offline-Bibliothek übernehmen als:", variable=self.v_save).grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.v_name = tk.StringVar(value=f"{info.get('target') or 'Eigene Firmware'} {info.get('version') or ''}".strip())
        ttk.Entry(f, textvariable=self.v_name, width=34).grid(row=3, column=2, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=3, pady=(10, 0), sticky="e")
        ttk.Button(b, text="⚡ Flashen", command=self.ok).pack(side="left")
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)
        self.transient(fl.winfo_toplevel())
        self.grab_set()

    def ok(self):
        off = parse_off(self.v_off.get().strip(), None)
        if off is None or off < 0:
            messagebox.showerror(APP_TITLE, "Ungültige Adresse.", parent=self)
            return
        if self.info["kind"] == "factory" and off != 0:
            if not messagebox.askyesno(APP_TITLE, "Factory-Images gehören an Adresse 0x0. Trotzdem fortfahren?",
                                       parent=self):
                return
        port = self.fl.port()
        if not messagebox.askyesno(APP_TITLE, f"{os.path.basename(self.fn)} an {hex(off)} auf {port} flashen?"
                                              + ("\n\nDer gesamte Flash wird vorher gelöscht!" if self.v_full.get() else ""),
                                   parent=self):
            return
        save = self.v_name.get().strip() if self.v_save.get() else None
        self.destroy()
        self.fl.run_custom(self.fn, self.info, off, self.v_full.get(), save)


class OnlineDialog(tk.Toplevel):
    """Lädt Offline-Pakete aus den GitHub-Releases (einmalig online, danach offline)."""

    def __init__(self, fl):
        super().__init__(fl)
        self.fl = fl
        self.title("Firmware online laden (für Offline-Nutzung)")
        self.geometry("720x500")
        self.releases = []
        self.vars = []
        f = ttk.Frame(self, padding=8)
        f.pack(fill="both", expand=True)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Quelle (GitHub-Repo):").pack(side="left")
        self.v_repo = tk.StringVar(value=fl.settings.get("fw_repo", DEFAULT_RELEASE_REPO))
        ttk.Entry(top, textvariable=self.v_repo, width=34).pack(side="left", padx=4)
        ttk.Button(top, text="🔄 Releases abrufen", command=self.fetch).pack(side="left")
        self.v_rel = tk.StringVar()
        self.cb_rel = ttk.Combobox(f, textvariable=self.v_rel, state="readonly", width=80)
        self.cb_rel.pack(fill="x", pady=6)
        self.cb_rel.bind("<<ComboboxSelected>>", lambda e: self.show_assets())
        self.box = ttk.Frame(f)
        self.box.pack(fill="both", expand=True)
        self.status = ttk.Label(f, text="Releases abrufen, Chip-Familien auswählen, „Herunterladen“.",
                                foreground="gray", wraplength=680)
        self.status.pack(fill="x")
        self.pb = ttk.Progressbar(f, maximum=100)
        self.pb.pack(fill="x", pady=4)
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="⬇ Herunterladen & importieren", command=self.download).pack(side="left")
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="right")
        ttk.Label(f, text="Pakete entstehen über den Workflow „Firmware-Offline-Pakete“ im Repo (Actions → Run "
                          "workflow). Alternativ ZIP im Browser laden und „Paket importieren…“ nutzen.",
                  foreground="gray", wraplength=680).pack(fill="x", pady=(6, 0))
        self.after(200, self.fetch)

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "MeshtasticDesktop-Flasher"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def fetch(self):
        repo = self.v_repo.get().strip()
        self.status.configure(text="Rufe Releases ab …")

        def work():
            try:
                rels = self._get(f"https://api.github.com/repos/{repo}/releases?per_page=30")
                rels = [r for r in rels if r.get("tag_name", "").startswith("fw-") and r.get("assets")]
                self.fl.q.put(("call", lambda: self._set_releases(rels)))
            except Exception as e:
                msg = f"Abruf fehlgeschlagen (kein Internet?): {e}"
                self.fl.q.put(("call", lambda: self.status.configure(text=msg)))
        threading.Thread(target=work, daemon=True).start()

    def _set_releases(self, rels):
        if not self.winfo_exists():
            return
        self.releases = sorted(rels, key=lambda r: vkey(r["tag_name"]), reverse=True)
        self.cb_rel["values"] = [r.get("name") or r["tag_name"] for r in self.releases]
        if self.releases:
            self.cb_rel.current(0)
            self.show_assets()
            self.status.configure(text=f"{len(self.releases)} Firmware-Release(s) gefunden.")
        else:
            self.status.configure(text="Keine Firmware-Pakete gefunden. Im Repo unter Actions den Workflow "
                                       "„Firmware-Offline-Pakete“ starten.")
        self.fl.settings["fw_repo"] = self.v_repo.get().strip()
        self.fl.save_settings(self.fl.settings)

    def show_assets(self):
        for w in self.box.winfo_children():
            w.destroy()
        self.vars = []
        i = self.cb_rel.current()
        if i < 0:
            return
        rel = self.releases[i]
        assets = [a for a in rel["assets"] if a["name"].endswith(".zip")]
        common = ("esp32s3", "esp32", "nrf52840")
        for a in sorted(assets, key=lambda a: a["name"]):
            chip = a["name"].rsplit("-", 1)[-1].replace(".zip", "")
            v = tk.BooleanVar(value=chip in common)
            hint = {"esp32": "T-Beam, T-LoRa, Station G1 …", "esp32s3": "Heltec V3/V4, T-Beam Supreme, T-Deck, "
                    "Station G2 …", "esp32c3": "Heltec HT62 …", "esp32c6": "C6-Boards",
                    "nrf52840": "RAK4631, T-Echo, Heltec T114, Seeed …", "rp2040": "RP2040/Pico-Boards",
                    "rp2350": "Pico 2 …"}.get(chip, "")
            ttk.Checkbutton(self.box, text=f"{chip:<10} {human(a['size']):>9}   {hint}", variable=v).pack(anchor="w")
            self.vars.append((v, a))

    def download(self):
        sel = [a for v, a in self.vars if v.get()]
        if not sel:
            return

        def work():
            last = None
            total = sum(a["size"] for a in sel) or 1
            done = 0
            try:
                for a in sel:
                    self.fl.q.put(("call", lambda a=a: self.status.configure(text=f"Lade {a['name']} …")))
                    tmp = os.path.join(tempfile.gettempdir(), a["name"])
                    req = urllib.request.Request(a["browser_download_url"],
                                                 headers={"User-Agent": "MeshtasticDesktop-Flasher"})
                    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fo:
                        while True:
                            b = r.read(256 * 1024)
                            if not b:
                                break
                            fo.write(b)
                            done += len(b)
                            pct = done * 100.0 / total
                            self.fl.q.put(("call", lambda p=pct: self.winfo_exists() and self.pb.configure(value=p)))
                    last = self.fl.lib.import_zip(tmp, self.fl.logmsg)
                    os.unlink(tmp)
                self.fl.q.put(("call", lambda: (self.fl.reload_library(select=last),
                                                self.winfo_exists() and self.status.configure(
                                                    text="Fertig – die Firmware ist jetzt offline verfügbar."))))
            except Exception as e:
                msg = f"Download fehlgeschlagen: {e}"
                self.fl.q.put(("call", lambda: self.winfo_exists() and self.status.configure(text=msg)))
        threading.Thread(target=work, daemon=True).start()


# ======================================================================================
def main():
    if sys.stdout is None:
        sys.stdout = _QueueWriter(lambda s: None)
    if sys.stderr is None:
        sys.stderr = _QueueWriter(lambda s: None)
    if "--selftest" in sys.argv:
        try:   # Windows-Konsole (cp1252) kann nicht alle Zeichen darstellen
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        import esptool
        lib = FwLibrary(os.path.join(default_data_dir(), "firmware"))
        print(f"{APP_TITLE} OK – esptool {esptool.__version__}, Versionen offline: "
              f"{[v['version'] for v in lib.versions()]}")
        return
    sp = os.path.join(default_data_dir(), "flasher.json")
    try:
        settings = json.load(open(sp, encoding="utf-8"))
    except Exception:
        settings = {}

    def save(s):
        try:
            json.dump(s, open(sp, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1100x800")
    try:
        ico = resource_path("icon.ico")
        if os.path.exists(ico) and sys.platform.startswith("win"):
            root.iconbitmap(ico)
    except Exception:
        pass
    st = ttk.Style(root)
    for t in ("vista", "clam"):
        if t in st.theme_names():
            st.theme_use(t)
            break
    FlasherFrame(root, settings=settings, save_settings=save).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
