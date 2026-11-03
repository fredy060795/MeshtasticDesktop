#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meshtastic Desktop – komplett offline lauffähige PC-App für Meshtastic-Geräte.

Verbindung:   USB/Seriell, WLAN/TCP (Netzwerk), Bluetooth LE
Funktionen:   Chats (Kanäle + Direktnachrichten, lokal gespeichert), Knotenliste,
              Kanäle/Gruppen bearbeiten, QR-Code teilen/importieren, Kontakt-QR,
              alle Geräte- und Moduleinstellungen bearbeiten, Besitzer, feste Position,
              Traceroute, Favoriten/Ignorieren, Neustart/Herunterfahren/Werksreset,
              Konfigurations-Backup/-Wiederherstellung (JSON), Vorgefertigte Nachrichten,
              Klingelton, Log-Ansicht.

Benötigt keine Internetverbindung. Abhängigkeiten: meshtastic, qrcode, pillow (optional
für PNG-Export). Start:  python app.py
"""
import base64
import datetime as _dt
import faulthandler
import io
import re
import json
import logging
import os
import queue
import secrets
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Meshtastic Desktop"
APP_VERSION = "1.2.0"


class _NullStream(io.TextIOBase):
    """Ersatz für sys.stdout/stderr in der EXE ohne Konsole (PyInstaller --windowed).

    Dort sind sys.stdout/sys.stderr None. Die Meshtastic-BLE-Schnittstelle ruft bei jeder
    Bluetooth-Operation sys.stdout.flush() auf – ohne diesen Ersatz scheitert jede
    BLE-Verbindung mit "'NoneType' object has no attribute 'flush'".
    """

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()


def _mt_version():
    try:
        from importlib.metadata import version
        return version("meshtastic")
    except Exception:
        return "?"

# --------------------------------------------------------------------------------------
# Abhängigkeiten
# --------------------------------------------------------------------------------------
try:
    import meshtastic
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    import meshtastic.util
    from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2, portnums_pb2
    from google.protobuf import json_format
    from google.protobuf.descriptor import FieldDescriptor
    from pubsub import pub
except Exception as exc:  # pragma: no cover
    print("Fehler: Python-Paket 'meshtastic' fehlt. Bitte installieren:\n"
          "  pip install -r requirements.txt\n", exc)
    raise

# Bluetooth LE wird erst bei Bedarf geladen (wie im WinTAK-Gateway): bleak lädt unter Windows
# native WinRT-Module, die bei manchen Python-/Treiber-Kombinationen den Prozess abstürzen
# lassen können. Ohne BLE-Nutzung soll das die App nicht treffen.
try:
    import importlib.util as _ilu
    HAS_BLE = _ilu.find_spec("bleak") is not None
except Exception:
    HAS_BLE = False
_ble = None
_ble_error = ""


def load_ble():
    """Lädt meshtastic.ble_interface beim ersten Gebrauch. Gibt das Modul oder None zurück."""
    global _ble, _ble_error, HAS_BLE
    if _ble is None and HAS_BLE:
        try:
            import meshtastic.ble_interface as mod
            _ble = mod
        except Exception as e:  # bleak/WinRT defekt
            _ble_error = f"{e.__class__.__name__}: {e}"
            log.exception("BLE-Modul konnte nicht geladen werden")
            HAS_BLE = False
    return _ble


BLE_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")


def norm_ble_id(value):
    return str(value or "").replace("-", "").replace("_", "").replace(":", "").strip().lower()


def parse_ble_target(text, known=None):
    """Akzeptiert 'Name  –  Adresse', 'BLE:Adresse', MAC, UUID oder Gerätenamen. Leer = erstes Gerät."""
    text = (text or "").strip()
    if known and text in known:
        return known[text]
    if not text or text.startswith("("):
        return None
    if re.match(r"^(COM\d+|/dev/)", text, re.I):   # alter serieller Eintrag
        return None
    if "  –  " in text:
        text = text.split("  –  ")[-1].strip()
    for prefix in ("ble://", "ble:", "bt://", "bt:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if text.lower() in ("ble", "bt", "bluetooth", ""):
        return None
    return text

try:
    import qrcode
    HAS_QR = True
except Exception:
    qrcode = None
    HAS_QR = False

try:
    from flasher import FlasherFrame
    HAS_FLASHER = True
except Exception:  # pragma: no cover
    FlasherFrame = None
    HAS_FLASHER = False

try:
    from mapview import OfflineMap
    HAS_MAP = True
except Exception:  # pragma: no cover
    OfflineMap = None
    HAS_MAP = False

try:
    import serial.tools.list_ports as _list_ports
except Exception:
    _list_ports = None

# --------------------------------------------------------------------------------------
# Pfade / Persistenz
# --------------------------------------------------------------------------------------
def data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "MeshtasticDesktop")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = data_dir()
DB_PATH = os.path.join(DATA_DIR, "messages.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("meshdesk")

# Absturzprotokoll für Fehler in nativem Code (z. B. Bluetooth-Treiber, 0xC0000005)
try:
    _crash_file = open(os.path.join(DATA_DIR, "crash.log"), "a", encoding="utf-8")
    _crash_file.write(f"\n--- Start {time.strftime('%Y-%m-%d %H:%M:%S')} (Python {sys.version.split()[0]}) ---\n")
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)
except Exception:
    pass

BROADCAST = "^all"


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log.warning("Einstellungen nicht gespeichert: %s", e)


class MessageStore:
    """Lokale SQLite-Datenbank für alle Chats (thread-sicher)."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 device TEXT, conv TEXT, ts REAL, from_id TEXT, from_name TEXT,
                 to_id TEXT, channel INTEGER, text TEXT, outgoing INTEGER,
                 status TEXT, packet_id INTEGER, snr REAL, rssi INTEGER)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_conv ON messages(device, conv, ts)")
        self.db.commit()

    def add(self, **kw):
        cols = ",".join(kw.keys())
        q = ",".join("?" for _ in kw)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO messages({cols}) VALUES({q})", list(kw.values()))
            self.db.commit()
            return cur.lastrowid

    def set_status(self, device, packet_id, status):
        with self.lock:
            self.db.execute("UPDATE messages SET status=? WHERE device=? AND packet_id=?",
                            (status, device, packet_id))
            self.db.commit()

    def conversation(self, device, conv, limit=1000):
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, from_id, from_name, text, outgoing, status, snr, rssi FROM messages "
                "WHERE device=? AND conv=? ORDER BY ts DESC LIMIT ?", (device, conv, limit)).fetchall()
        return list(reversed(rows))

    def conversations(self, device):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT conv FROM messages WHERE device=? GROUP BY conv ORDER BY MAX(ts) DESC",
                (device,)).fetchall()]

    def delete_conv(self, device, conv):
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE device=? AND conv=?", (device, conv))
            self.db.commit()

    def export(self, device):
        with self.lock:
            return self.db.execute(
                "SELECT ts, conv, from_id, from_name, to_id, channel, text, outgoing, status "
                "FROM messages WHERE device=? ORDER BY ts", (device,)).fetchall()


# --------------------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------------------
SECTION_DE = {
    "device": "Gerät", "position": "Position", "power": "Energie", "network": "Netzwerk/WLAN",
    "display": "Display", "lora": "LoRa/Funk", "bluetooth": "Bluetooth", "security": "Sicherheit",
    "mqtt": "MQTT", "serial": "Seriell", "external_notification": "Ext. Benachrichtigung",
    "store_forward": "Store & Forward", "range_test": "Reichweitentest", "telemetry": "Telemetrie",
    "canned_message": "Vorgef. Nachrichten", "audio": "Audio", "remote_hardware": "Remote-Hardware",
    "neighbor_info": "Nachbar-Info", "ambient_lighting": "Umgebungslicht",
    "detection_sensor": "Erkennungssensor", "paxcounter": "Paxcounter",
    "statusmessage": "Statusnachricht", "traffic_management": "Traffic-Management", "tak": "TAK",
}


def pretty(name):
    return name.replace("_", " ").strip().capitalize()


def is_repeated(fd):
    r = getattr(fd, "is_repeated", None)
    if r is not None:
        return bool(r)
    return getattr(fd, "label", 0) == 3


def fmt_time(ts):
    if not ts:
        return "-"
    try:
        d = _dt.datetime.fromtimestamp(ts)
        if d.date() == _dt.date.today():
            return d.strftime("%H:%M:%S")
        return d.strftime("%d.%m.%y %H:%M")
    except Exception:
        return "-"


def ago(ts):
    if not ts:
        return "-"
    s = int(time.time() - ts)
    if s < 0:
        return "jetzt"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


class ScrollFrame(ttk.Frame):
    """Vertikal scrollbarer Container."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.inner.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _bind_wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


# --------------------------------------------------------------------------------------
# Generisches Protobuf-Formular (für ALLE Einstellungen)
# --------------------------------------------------------------------------------------
# Feste Grenzen für einzelne Felder: (Abschnitt, Feld) -> (min, max, Hinweis)
FIELD_LIMITS = {
    ("lora", "hop_limit"): (1, 7, "1–7 · höhere Werte bringen den Node zum Absturz"),
}
HOP_LIMIT_MAX = 7


class ProtoForm:
    """Erzeugt aus einer Protobuf-Nachricht automatisch ein Eingabeformular."""

    def __init__(self, parent, msg, section=""):
        self.section = section
        self.fields = []  # (path, fd, var, kind)
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(1, weight=1)
        self._row = 0
        self._build(self.frame, msg, [])

    def _build(self, parent, msg, path):
        for fd in msg.DESCRIPTOR.fields:
            p = path + [fd.name]
            label = pretty(fd.name)
            if fd.type == FieldDescriptor.TYPE_MESSAGE and not is_repeated(fd):
                lf = ttk.LabelFrame(parent, text=label, padding=6)
                lf.grid(row=self._next(), column=0, columnspan=3, sticky="ew", padx=4, pady=4)
                lf.columnconfigure(1, weight=1)
                saved, self._row = self._row, 0
                self._build(lf, getattr(msg, fd.name), p)
                self._row = saved
                continue
            r = self._next()
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=2)
            val = getattr(msg, fd.name)
            if is_repeated(fd):
                if fd.type == FieldDescriptor.TYPE_MESSAGE:
                    ttk.Label(parent, text=f"({len(val)} Einträge – nur Anzeige)").grid(
                        row=r, column=1, sticky="w")
                    continue
                if fd.type == FieldDescriptor.TYPE_BYTES:
                    txt = ", ".join(base64.b64encode(v).decode() for v in val)
                    kind = "rbytes"
                elif fd.enum_type is not None:
                    txt = ", ".join(fd.enum_type.values_by_number[v].name
                                    if v in fd.enum_type.values_by_number else str(v) for v in val)
                    kind = "renum"
                else:
                    txt = ", ".join(str(v) for v in val)
                    kind = "rscalar"
                var = tk.StringVar(value=txt)
                ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4)
                ttk.Label(parent, text="(Komma-getrennt)", foreground="gray").grid(row=r, column=2, sticky="w")
            elif fd.type == FieldDescriptor.TYPE_BOOL:
                var = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(parent, variable=var).grid(row=r, column=1, sticky="w", padx=4)
                kind = "bool"
            elif fd.enum_type is not None:
                names = [v.name for v in fd.enum_type.values]
                cur = fd.enum_type.values_by_number.get(val)
                var = tk.StringVar(value=cur.name if cur else str(val))
                ttk.Combobox(parent, textvariable=var, values=names, state="readonly").grid(
                    row=r, column=1, sticky="ew", padx=4)
                kind = "enum"
            elif fd.type == FieldDescriptor.TYPE_BYTES:
                var = tk.StringVar(value=base64.b64encode(val).decode())
                ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4)
                ttk.Label(parent, text="(Base64)", foreground="gray").grid(row=r, column=2, sticky="w")
                kind = "bytes"
            elif fd.type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
                var = tk.StringVar(value=str(val))
                ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4)
                kind = "float"
            elif fd.type == FieldDescriptor.TYPE_STRING:
                var = tk.StringVar(value=val)
                show = "•" if ("psk" in fd.name or "password" in fd.name) else ""
                e = ttk.Entry(parent, textvariable=var, show=show)
                e.grid(row=r, column=1, sticky="ew", padx=4)
                if show:
                    ttk.Button(parent, text="👁", width=3,
                               command=lambda e=e: e.configure(show="" if e.cget("show") else "•")).grid(
                        row=r, column=2, sticky="w")
                kind = "str"
            elif (self.section, ".".join(p)) in FIELD_LIMITS:
                lo, hi, hint = FIELD_LIMITS[(self.section, ".".join(p))]
                var = tk.StringVar(value=str(val))
                ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=6, state="readonly"
                            if lo <= val <= hi else "normal").grid(row=r, column=1, sticky="w", padx=4)
                ttk.Label(parent, text=f"({hint})", foreground="#a33" if not lo <= val <= hi else "gray").grid(
                    row=r, column=2, sticky="w")
                kind = "int"
            else:
                var = tk.StringVar(value=str(val))
                ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4)
                kind = "int"
            self.fields.append((p, fd, var, kind))

    def _next(self):
        r = self._row
        self._row += 1
        return r

    def apply_to(self, msg):
        """Überträgt Formularwerte in msg. Wirft ValueError bei ungültigen Eingaben."""
        for path, fd, var, kind in self.fields:
            target = msg
            for name in path[:-1]:
                target = getattr(target, name)
            name = path[-1]
            label = ".".join(path)
            try:
                v = var.get()
                if kind == "bool":
                    setattr(target, name, bool(v))
                elif kind == "enum":
                    setattr(target, name, fd.enum_type.values_by_name[v].number if v in
                            fd.enum_type.values_by_name else int(v))
                elif kind == "int":
                    iv = int(str(v).strip() or 0, 0)
                    lim = FIELD_LIMITS.get((self.section, ".".join(path)))
                    if lim and not lim[0] <= iv <= lim[1]:
                        raise ValueError(f"erlaubt ist {lim[0]} bis {lim[1]} ({lim[2]})")
                    setattr(target, name, iv)
                elif kind == "float":
                    setattr(target, name, float(str(v).replace(",", ".").strip() or 0))
                elif kind == "str":
                    setattr(target, name, v)
                elif kind == "bytes":
                    setattr(target, name, base64.b64decode(v.strip()) if v.strip() else b"")
                else:
                    items = [x.strip() for x in v.split(",") if x.strip()]
                    rep = getattr(target, name)
                    del rep[:]
                    if kind == "rbytes":
                        rep.extend(base64.b64decode(x) for x in items)
                    elif kind == "renum":
                        rep.extend(fd.enum_type.values_by_name[x].number if x in fd.enum_type.values_by_name
                                   else int(x) for x in items)
                    elif fd.type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
                        rep.extend(float(x) for x in items)
                    elif fd.type == FieldDescriptor.TYPE_STRING:
                        rep.extend(items)
                    else:
                        rep.extend(int(x, 0) for x in items)
            except Exception as e:
                raise ValueError(f"Ungültiger Wert für '{label}': {e}") from e


# --------------------------------------------------------------------------------------
# Hauptanwendung
# --------------------------------------------------------------------------------------
class MeshApp(tk.Tk):
    CONN_TYPES = ("USB / Seriell", "WLAN / TCP", "Bluetooth")

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1200x780")
        self.minsize(900, 600)
        try:
            ico = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
            if os.path.exists(ico) and sys.platform.startswith("win"):
                self.iconbitmap(ico)
        except Exception:
            pass

        self.settings = load_settings()
        self.store = MessageStore(DB_PATH)
        self.iface = None
        self.device_id = None          # eigene Node-ID, z.B. !a1b2c3d4
        self.q = queue.Queue()
        self.current_conv = None
        self.unread = {}
        self.conv_keys = []
        self.ble_devices = {}
        self._connecting = False
        self._manual_disconnect = False

        style = ttk.Style(self)
        for t in ("vista", "clam"):
            if t in style.theme_names():
                style.theme_use(t)
                break
        style.configure("Status.TLabel", padding=(6, 2))
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))

        self._build_topbar()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_chat_tab()
        self._build_nodes_tab()
        self._build_channels_tab()
        self._build_config_tab("config", "Geräte-Einstellungen")
        self._build_config_tab("module", "Modul-Einstellungen")
        self._build_device_tab()
        self._build_flasher_tab()
        self._build_log_tab()

        self.statusbar = ttk.Label(self, text="Nicht verbunden", style="Status.TLabel", relief="sunken")
        self.statusbar.pack(fill="x", side="bottom")

        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_connected, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")
        pub.subscribe(self._on_log_line, "meshtastic.log.line")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)
        self.after(5000, self._periodic)
        self.refresh_ports()
        self.log_msg(f"{APP_NAME} gestartet. Daten: {DATA_DIR}")
        if not HAS_BLE:
            self.log_msg("Hinweis: Bluetooth nicht verfügbar (Paket 'bleak' fehlt).")
        if self.settings.get("autoconnect") and self.settings.get("last_target"):
            self.after(500, self.connect)

    # ------------------------------------------------------------------ Topbar
    def _build_topbar(self):
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text="Verbindung:").pack(side="left")
        self.conn_type = tk.StringVar(value=self.settings.get("last_type", self.CONN_TYPES[0]))
        cb = ttk.Combobox(bar, textvariable=self.conn_type, values=self.CONN_TYPES, state="readonly", width=15)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_ports())
        self.target = tk.StringVar(value=self.settings.get("last_target", ""))
        self.target_cb = ttk.Combobox(bar, textvariable=self.target, width=42)
        self.target_cb.pack(side="left", padx=4)
        self.scan_btn = ttk.Button(bar, text="🔄 Suchen", command=self.refresh_ports)
        self.scan_btn.pack(side="left", padx=2)
        self.conn_btn = ttk.Button(bar, text="Verbinden", command=self.toggle_connect)
        self.conn_btn.pack(side="left", padx=2)
        self.autoconnect = tk.BooleanVar(value=bool(self.settings.get("autoconnect")))
        ttk.Checkbutton(bar, text="Auto-Verbinden beim Start", variable=self.autoconnect,
                        command=self._save_conn_settings).pack(side="left", padx=8)
        self.me_label = ttk.Label(bar, text="", style="Big.TLabel")
        self.me_label.pack(side="right")

    def _save_conn_settings(self):
        self.settings["last_type"] = self.conn_type.get()
        self.settings["last_target"] = self.target.get()
        self.settings["autoconnect"] = bool(self.autoconnect.get())
        save_settings(self.settings)

    def refresh_ports(self):
        t = self.conn_type.get()
        if t == self.CONN_TYPES[0]:
            ports = []
            try:
                ports = list(meshtastic.util.findPorts(True))
            except Exception:
                pass
            if _list_ports:
                for p in _list_ports.comports():
                    if p.device not in ports:
                        ports.append(p.device)
            labels = []
            for p in ports:
                desc = ""
                if _list_ports:
                    for cp in _list_ports.comports():
                        if cp.device == p:
                            desc = cp.description
                labels.append(f"{p}  –  {desc}" if desc else p)
            self.target_cb["values"] = labels or ["(kein Gerät gefunden)"]
            if labels and not self.target.get().split("  –  ")[0] in ports:
                self.target.set(labels[0])
            self.set_status(f"{len(ports)} serielle(r) Port(s) gefunden")
        elif t == self.CONN_TYPES[1]:
            hist = self.settings.get("tcp_hosts", ["meshtastic.local", "192.168.4.1"])
            self.target_cb["values"] = hist
            if not self.target.get() or "COM" in self.target.get() or "/dev/" in self.target.get():
                self.target.set(hist[0] if hist else "")
            self.set_status("IP-Adresse oder Hostname eingeben (optional :Port, Standard 4403)")
        else:
            if not HAS_BLE:
                messagebox.showerror(APP_NAME, "Bluetooth-Unterstützung fehlt (pip install bleak).\n" + _ble_error)
                return
            self.scan_btn.configure(state="disabled")
            self.set_status("Suche Meshtastic-Bluetooth-Geräte (ca. 10 s)…")
            self.log_msg("BLE-Suche gestartet …")

            def work():
                try:
                    mod = load_ble()
                    if mod is None:
                        raise RuntimeError("Bluetooth-Modul nicht verfügbar: " + _ble_error)
                    devs = mod.BLEInterface.scan()
                    self.q.put(("ble_scan", [(d.name or "?", d.address) for d in devs if d.address]))
                except BaseException as e:
                    log.exception("BLE-Scan")
                    self.q.put(("error", f"BLE-Suche fehlgeschlagen: {e.__class__.__name__}: {e}\n\n"
                                         "Ist Bluetooth am PC eingeschaltet?"))
                    self.q.put(("ble_scan", []))
            threading.Thread(target=work, daemon=True).start()

    def toggle_connect(self):
        if self.iface or self._connecting:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        t = self.conn_type.get()
        target = self.target.get().strip()
        if t == self.CONN_TYPES[0]:
            target = target.split("  –  ")[0].strip()
            if target.startswith("("):
                target = ""
        elif t == self.CONN_TYPES[2]:
            if not HAS_BLE:
                messagebox.showerror(APP_NAME, "Bluetooth-Unterstützung fehlt.\n" + _ble_error)
                return
            target = parse_ble_target(target, self.ble_devices) or ""
        if t == self.CONN_TYPES[1] and not target:
            messagebox.showwarning(APP_NAME, "Bitte IP-Adresse / Hostname eingeben.")
            return
        self._save_conn_settings()
        if t == self.CONN_TYPES[1]:
            hist = [h for h in self.settings.get("tcp_hosts", []) if h != target]
            self.settings["tcp_hosts"] = [target] + hist[:9]
            save_settings(self.settings)
        self._connecting = True
        self._manual_disconnect = False
        self.conn_btn.configure(text="Abbrechen")
        self.set_status(f"Verbinde über {t} mit {target or 'automatisch erkanntem Gerät'} …")
        self.log_msg(f"Verbinde: {t} {target}")

        def work():
            try:
                if t == self.CONN_TYPES[0]:
                    iface = meshtastic.serial_interface.SerialInterface(devPath=target or None)
                elif t == self.CONN_TYPES[1]:
                    host, port = target, 4403
                    if ":" in target and target.count(":") == 1:
                        host, p = target.split(":")
                        port = int(p)
                    iface = meshtastic.tcp_interface.TCPInterface(hostname=host, portNumber=port)
                else:
                    iface = self._open_ble(target or None)
                self.q.put(("connected_iface", iface))
            except BaseException as e:
                log.exception("Verbindung fehlgeschlagen")
                msg = str(e) or e.__class__.__name__
                if t == self.CONN_TYPES[2]:
                    msg += ("\n\nTipps für Bluetooth:\n"
                            "• Gerät in Reichweite und eingeschaltet, Bluetooth am PC an\n"
                            "• Handy-App / anderen Client vorher trennen (nur eine BLE-Verbindung möglich)\n"
                            "• Unter Windows das Gerät in den Bluetooth-Einstellungen koppeln (PIN am Display, "
                            "oft 123456) – bei Problemen dort entfernen und neu koppeln\n"
                            "• Feld leer lassen = erstes gefundenes Gerät verwenden")
                self.q.put(("connect_failed", msg))
        threading.Thread(target=work, daemon=True).start()

    def _open_ble(self, target):
        """BLE-Verbindung wie im WinTAK-Gateway: direkt versuchen; wird das Gerät nicht gefunden,
        tolerant (Groß-/Kleinschreibung, Trennzeichen, Name oder Adresse) suchen und exakt neu verbinden."""
        mod = load_ble()
        if mod is None:
            raise RuntimeError("Bluetooth-Modul nicht verfügbar: " + _ble_error)
        cls = mod.BLEInterface
        self.q.put(("info", f"Suche Bluetooth-Gerät {target or '(erstes gefundenes)'} – Scan dauert ca. 10 s …"))
        try:
            return cls(target)
        except cls.BLEError as exc:
            if not target:
                raise
            wanted = norm_ble_id(target)
            candidates = []
            for dev in cls.scan():
                addr = str(getattr(dev, "address", "") or "")
                name = str(getattr(dev, "name", "") or "")
                if wanted in (norm_ble_id(addr), norm_ble_id(name)):
                    candidates.append(addr)
            if len(candidates) != 1 or candidates[0] == target:
                raise exc
            self.q.put(("info", f"Bluetooth-Gerät '{target}' als {candidates[0]} gefunden, verbinde erneut …"))
            return cls(candidates[0])

    def disconnect(self):
        self._manual_disconnect = True
        iface, self.iface = self.iface, None
        self._connecting = False
        if iface:
            threading.Thread(target=lambda: self._safe_close(iface), daemon=True).start()
        self.conn_btn.configure(text="Verbinden")
        self.me_label.configure(text="")
        self.set_status("Getrennt")
        self.log_msg("Verbindung getrennt.")

    def _disconnect_sync(self):
        """Trennt sofort und schließt den Port (für den Firmware-Flasher)."""
        iface = self.iface
        self.disconnect()
        if iface is not None:
            self._safe_close(iface)

    @staticmethod
    def _safe_close(iface):
        try:
            iface.close()
        except BaseException:
            pass

    # ------------------------------------------------------------------ pubsub (Fremd-Threads!)
    def _on_receive(self, packet, interface):
        self.q.put(("packet", packet, interface))

    def _on_connected(self, interface, topic=pub.AUTO_TOPIC):
        self.q.put(("established", interface))

    def _on_lost(self, interface, topic=pub.AUTO_TOPIC):
        self.q.put(("lost", interface))

    def _on_node_updated(self, node, interface):
        self.q.put(("node", node))

    def _on_log_line(self, line, interface):
        self.q.put(("logline", line))

    # ------------------------------------------------------------------ Event-Loop
    def _poll_queue(self):
        try:
            for _ in range(200):
                ev = self.q.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        except Exception as e:
            log.exception("Fehler im Event-Handler")
            self.log_msg(f"Fehler: {e}")
        self.after(100, self._poll_queue)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "connected_iface":
            if not self._connecting:      # abgebrochen
                self._safe_close(ev[1])
                return
            self.iface = ev[1]
            self._connecting = False
            self.conn_btn.configure(text="Trennen")
            self._after_connect()
        elif kind == "connect_failed":
            self._connecting = False
            self.conn_btn.configure(text="Verbinden")
            self.set_status("Verbindung fehlgeschlagen")
            self.log_msg(f"Verbindung fehlgeschlagen: {ev[1]}")
            messagebox.showerror(APP_NAME, f"Verbindung fehlgeschlagen:\n{ev[1]}")
        elif kind == "established":
            pass  # wird in _after_connect behandelt
        elif kind == "lost":
            if ev[1] is self.iface and not self._manual_disconnect:
                self.log_msg("Verbindung verloren!")
                self.set_status("Verbindung verloren – bitte neu verbinden")
                self.iface = None
                self.conn_btn.configure(text="Verbinden")
        elif kind == "packet":
            if ev[2] is self.iface:
                self._handle_packet(ev[1])
        elif kind == "node":
            self._nodes_dirty = True
        elif kind == "logline":
            self._append_log(ev[1])
        elif kind == "ble_scan":
            self.scan_btn.configure(state="normal")
            self.ble_devices = {f"{n}  –  {a}": a for n, a in ev[1]}
            self.target_cb["values"] = list(self.ble_devices) + ["(erstes gefundenes Gerät)"]
            if self.ble_devices:
                self.target.set(list(self.ble_devices)[0])
            else:
                self.target.set("(erstes gefundenes Gerät)")
            for k in self.ble_devices:
                self.log_msg(f"BLE gefunden: {k}")
            self.set_status(f"{len(self.ble_devices)} Bluetooth-Gerät(e) gefunden")
        elif kind == "ack":
            _, pid, status = ev
            if self.device_id:
                self.store.set_status(self.device_id, pid, status)
            self.refresh_chat()
        elif kind == "info":
            self.log_msg(ev[1])
            self.set_status(ev[1])
        elif kind == "popup":
            messagebox.showinfo(APP_NAME, ev[1])
        elif kind == "error":
            self.log_msg("FEHLER: " + ev[1])
            messagebox.showerror(APP_NAME, ev[1])
        elif kind == "call":
            ev[1]()

    def _after_connect(self):
        iface = self.iface
        info = iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        self.device_id = user.get("id") or (f"!{iface.myInfo.my_node_num:08x}" if iface.myInfo else "unbekannt")
        self.me_label.configure(text=f"📡 {user.get('longName', '?')} ({user.get('shortName', '')})  {self.device_id}")
        fw = ""
        try:
            fw = iface.metadata.firmware_version if iface.metadata else ""
        except Exception:
            pass
        self.set_status(f"Verbunden mit {user.get('longName', '?')} {self.device_id}  Firmware {fw or '?'}")
        self.log_msg(f"Verbunden: {self.device_id} {user.get('longName', '')} FW {fw}")
        self._nodes_dirty = True
        self.refresh_all()

    def refresh_all(self):
        self.refresh_conv_list()
        self.refresh_nodes()
        self.refresh_channels()
        self.reload_config("config")
        self.reload_config("module")
        self.refresh_device_tab()

    def _periodic(self):
        if getattr(self, "_nodes_dirty", False) and self.iface:
            self._nodes_dirty = False
            self.refresh_nodes()
            self.refresh_conv_list()
        self.after(3000, self._periodic)

    def need_iface(self):
        if not self.iface:
            messagebox.showwarning(APP_NAME, "Nicht mit einem Gerät verbunden.")
            return False
        return True

    def run_bg(self, func, ok_msg=None, popup=False):
        """Führt blockierende Gerätebefehle im Hintergrund aus."""
        def work():
            try:
                r = func()
                if ok_msg:
                    self.q.put(("popup" if popup else "info", ok_msg if not callable(ok_msg) else ok_msg(r)))
            except SystemExit as e:
                self.q.put(("error", f"Befehl abgebrochen: {e}"))
            except BaseException as e:
                log.exception("Hintergrundbefehl")
                self.q.put(("error", f"{e.__class__.__name__}: {e}"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ Hilfen Knoten
    def node_name(self, node_id, short=False):
        if not self.iface or not node_id:
            return node_id or "?"
        n = (self.iface.nodes or {}).get(node_id)
        if n and n.get("user"):
            return n["user"].get("shortName" if short else "longName") or node_id
        return node_id

    def channel_name(self, idx):
        try:
            ch = self.iface.localNode.channels[idx]
            if ch.settings.name:
                return ch.settings.name
            if ch.role == channel_pb2.Channel.Role.PRIMARY:
                try:
                    preset = self.iface.localNode.localConfig.lora.modem_preset
                    from meshtastic.protobuf import config_pb2
                    return config_pb2.Config.LoRaConfig.ModemPreset.Name(preset).replace("_", "").title()
                except Exception:
                    return "Primär"
            return f"Kanal {idx}"
        except Exception:
            return f"Kanal {idx}"

    # ==================================================================== CHAT
    def _build_chat_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="💬 Chats")
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)

        left = ttk.Frame(pw, padding=4)
        ttk.Label(left, text="Unterhaltungen", style="Big.TLabel").pack(anchor="w")
        self.conv_list = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10), exportselection=False)
        self.conv_list.pack(fill="both", expand=True, pady=4)
        self.conv_list.bind("<<ListboxSelect>>", self._conv_selected)
        bf = ttk.Frame(left)
        bf.pack(fill="x")
        ttk.Button(bf, text="Neue DM…", command=self.new_dm).pack(side="left")
        ttk.Button(bf, text="Verlauf löschen", command=self.delete_conv).pack(side="left", padx=2)
        ttk.Button(bf, text="Export…", command=self.export_chats).pack(side="left")
        pw.add(left, weight=1)

        right = ttk.Frame(pw, padding=4)
        self.chat_title = ttk.Label(right, text="Keine Unterhaltung gewählt", style="Big.TLabel")
        self.chat_title.pack(anchor="w")
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=4)
        self.chat_text = tk.Text(tf, wrap="word", state="disabled", font=("Segoe UI", 10), padx=8, pady=6)
        sb = ttk.Scrollbar(tf, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=sb.set)
        self.chat_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.chat_text.tag_configure("meta", foreground="#888888", font=("Segoe UI", 8))
        self.chat_text.tag_configure("out", foreground="#0b5394", lmargin1=80, lmargin2=80)
        self.chat_text.tag_configure("in", foreground="#000000")
        self.chat_text.tag_configure("name", font=("Segoe UI", 10, "bold"))

        inp = ttk.Frame(right)
        inp.pack(fill="x")
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(inp, textvariable=self.msg_var, font=("Segoe UI", 11))
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", lambda e: self.send_message())
        self.msg_var.trace_add("write", self._update_counter)
        self.counter = ttk.Label(inp, text="0/200", width=8)
        self.counter.pack(side="left", padx=4)
        ttk.Button(inp, text="Senden ➤", command=self.send_message).pack(side="left")
        pw.add(right, weight=3)

    def _update_counter(self, *a):
        n = len(self.msg_var.get().encode("utf-8"))
        self.counter.configure(text=f"{n}/200", foreground="red" if n > 200 else "")

    def refresh_conv_list(self):
        if not self.iface:
            return
        keys = []
        for ch in self.iface.localNode.channels or []:
            if ch.role != channel_pb2.Channel.Role.DISABLED:
                keys.append(f"ch:{ch.index}")
        for k in self.store.conversations(self.device_id):
            if k not in keys:
                keys.append(k)
        if self.current_conv and self.current_conv not in keys:
            keys.append(self.current_conv)
        self.conv_keys = keys
        sel = self.current_conv
        self.conv_list.delete(0, "end")
        for k in keys:
            u = self.unread.get(k, 0)
            badge = f"  ({u})" if u else ""
            if k.startswith("ch:"):
                idx = int(k[3:])
                label = f"#  {idx}: {self.channel_name(idx)}{badge}"
            else:
                nid = k[3:]
                label = f"@  {self.node_name(nid)} [{self.node_name(nid, True)}]{badge}"
            self.conv_list.insert("end", label)
            if u:
                self.conv_list.itemconfigure("end", foreground="#c00000")
        if sel in keys:
            i = keys.index(sel)
            self.conv_list.selection_set(i)
            self.conv_list.see(i)
        elif keys and not self.current_conv:
            self.conv_list.selection_set(0)
            self._conv_selected()

    def _conv_selected(self, e=None):
        s = self.conv_list.curselection()
        if not s:
            return
        self.open_conv(self.conv_keys[s[0]])

    def open_conv(self, key):
        self.current_conv = key
        self.unread[key] = 0
        self.nb.select(0)
        if key.startswith("ch:"):
            idx = int(key[3:])
            self.chat_title.configure(text=f"Kanal {idx}: {self.channel_name(idx)}  (Gruppe / Broadcast)")
        else:
            nid = key[3:]
            self.chat_title.configure(text=f"Direktnachricht an {self.node_name(nid)} ({nid})")
        self.refresh_conv_list()
        self.refresh_chat()
        self.msg_entry.focus_set()

    def refresh_chat(self):
        if not self.current_conv or not self.device_id:
            return
        rows = self.store.conversation(self.device_id, self.current_conv)
        t = self.chat_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for ts, fid, fname, text, outgoing, status, snr, rssi in rows:
            tag = "out" if outgoing else "in"
            meta = fmt_time(ts)
            if outgoing:
                st = {"pending": "⏳ gesendet", "ack": "✓ bestätigt", "implicit": "✓ weitergeleitet",
                      "failed": "✗ fehlgeschlagen"}.get(status or "", status or "")
                t.insert("end", f"Ich  ·  {meta}  ·  {st}\n", ("meta", tag))
            else:
                extra = f"  ·  SNR {snr:.1f} / RSSI {rssi}" if snr is not None and rssi else ""
                t.insert("end", f"{fname or fid}  ({fid})  ·  {meta}{extra}\n", ("meta", tag))
            t.insert("end", text + "\n\n", (tag,))
        t.configure(state="disabled")
        t.see("end")

    def send_message(self):
        txt = self.msg_var.get().strip()
        if not txt:
            return
        if not self.need_iface():
            return
        if not self.current_conv:
            messagebox.showinfo(APP_NAME, "Bitte zuerst eine Unterhaltung wählen.")
            return
        if len(txt.encode("utf-8")) > 200:
            if not messagebox.askyesno(APP_NAME, "Nachricht ist länger als 200 Bytes und wird evtl. "
                                                 "abgeschnitten/abgelehnt. Trotzdem senden?"):
                return
        if self.current_conv.startswith("ch:"):
            dest, ch = BROADCAST, int(self.current_conv[3:])
        else:
            dest = self.current_conv[3:]
            n = (self.iface.nodes or {}).get(dest, {})
            ch = n.get("channel", 0) if isinstance(n, dict) else 0
        self.msg_var.set("")
        device = self.device_id
        conv = self.current_conv
        holder = {}

        def on_resp(p):
            try:
                d = p.get("decoded", {})
                err = d.get("routing", {}).get("errorReason", "NONE")
                status = "ack" if err == "NONE" else "failed"
                if err == "NONE" and p.get("from") == self.iface.myInfo.my_node_num and dest == BROADCAST:
                    status = "implicit"
                self.q.put(("ack", holder.get("id"), status))
            except Exception:
                pass

        def work():
            try:
                pkt = self.iface.sendText(txt, destinationId=dest, wantAck=True, channelIndex=ch,
                                          onResponse=on_resp)
                holder["id"] = pkt.id
                self.store.add(device=device, conv=conv, ts=time.time(), from_id=device, from_name="Ich",
                               to_id=dest, channel=ch, text=txt, outgoing=1, status="pending",
                               packet_id=pkt.id, snr=None, rssi=None)
                self.q.put(("call", self.refresh_chat))
            except BaseException as e:
                self.q.put(("error", f"Senden fehlgeschlagen: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _handle_packet(self, p):
        d = p.get("decoded") or {}
        port = d.get("portnum")
        if port == "TEXT_MESSAGE_APP" and "text" in d:
            fid = p.get("fromId") or f"!{p.get('from', 0):08x}"
            tid = p.get("toId") or ""
            ch = p.get("channel", 0) or 0
            if fid == self.device_id:
                return
            if tid and tid != BROADCAST and tid == self.device_id:
                conv = f"dm:{fid}"
            else:
                conv = f"ch:{ch}"
            self.store.add(device=self.device_id, conv=conv, ts=p.get("rxTime") or time.time(),
                           from_id=fid, from_name=self.node_name(fid), to_id=tid, channel=ch,
                           text=d["text"], outgoing=0, status="", packet_id=p.get("id"),
                           snr=p.get("rxSnr"), rssi=p.get("rxRssi"))
            if conv == self.current_conv and str(self.nb.select()) == str(self.nb.tabs()[0]):
                self.refresh_chat()
            else:
                self.unread[conv] = self.unread.get(conv, 0) + 1
            self.refresh_conv_list()
            muted = False
            try:
                muted = conv.startswith("ch:") and self.iface.localNode.channels[ch].settings.module_settings.is_muted
            except Exception:
                pass
            if not muted:
                self.bell()
            self.log_msg(f"Nachricht von {self.node_name(fid)} [{conv}]: {d['text']}")
        elif port in ("POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP"):
            self._nodes_dirty = True

    def new_dm(self):
        if not self.need_iface():
            return
        nodes = [(nid, n) for nid, n in (self.iface.nodes or {}).items() if nid != self.device_id]
        if not nodes:
            messagebox.showinfo(APP_NAME, "Keine anderen Knoten bekannt.")
            return
        win = tk.Toplevel(self)
        win.title("Direktnachricht an …")
        win.geometry("420x420")
        lb = tk.Listbox(win)
        lb.pack(fill="both", expand=True, padx=6, pady=6)
        ids = []
        for nid, n in sorted(nodes, key=lambda x: (x[1].get("user", {}).get("longName") or x[0]).lower()):
            u = n.get("user", {})
            lb.insert("end", f"{u.get('longName', nid)} [{u.get('shortName', '')}]  {nid}")
            ids.append(nid)

        def ok(e=None):
            s = lb.curselection()
            if s:
                win.destroy()
                self.open_conv(f"dm:{ids[s[0]]}")
        lb.bind("<Double-1>", ok)
        ttk.Button(win, text="Öffnen", command=ok).pack(pady=4)

    def delete_conv(self):
        if not self.current_conv or not self.device_id:
            return
        if messagebox.askyesno(APP_NAME, "Lokalen Verlauf dieser Unterhaltung löschen?"):
            self.store.delete_conv(self.device_id, self.current_conv)
            self.refresh_chat()
            self.refresh_conv_list()

    def export_chats(self):
        if not self.device_id:
            return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                          initialfile=f"chats_{self.device_id.strip('!')}.csv")
        if not fn:
            return
        import csv
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Zeit", "Unterhaltung", "Von-ID", "Von", "An", "Kanal", "Text", "Ausgehend", "Status"])
            for r in self.store.export(self.device_id):
                w.writerow([_dt.datetime.fromtimestamp(r[0]).isoformat(sep=" ", timespec="seconds")] + list(r[1:]))
        self.set_status(f"Chats exportiert: {fn}")

    # ==================================================================== KNOTEN
    NODE_COLS = (("name", "Name", 180), ("short", "Kurz", 55), ("id", "ID", 95), ("hw", "Hardware", 120),
                 ("role", "Rolle", 90), ("snr", "SNR", 50), ("hops", "Hops", 45), ("batt", "Akku", 55),
                 ("heard", "Zuletzt", 70), ("pos", "Position", 170), ("fav", "★", 30))

    def _build_nodes_tab(self):
        f = ttk.Frame(self.nb, padding=4)
        self.nb.add(f, text="📶 Knoten")
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.node_filter = tk.StringVar()
        ttk.Label(top, text="Filter:").pack(side="left")
        e = ttk.Entry(top, textvariable=self.node_filter, width=25)
        e.pack(side="left", padx=4)
        self.node_filter.trace_add("write", lambda *a: self.refresh_nodes())
        self.node_count = ttk.Label(top, text="")
        self.node_count.pack(side="left", padx=10)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(4, 0))
        for txt, cmd in (("💬 Nachricht", self.node_dm), ("🛰 Traceroute", self.node_traceroute),
                         ("📍 Position anfordern", self.node_request_pos), ("★ Favorit", self.node_fav),
                         ("🚫 Ignorieren", self.node_ignore), ("🗑 Entfernen", self.node_remove),
                         ("🔳 Kontakt-QR", self.node_contact_qr), ("ℹ Details", self.node_details),
                         ("🗺 Auf Karte", self.node_on_map)):
            ttk.Button(top, text=txt, command=cmd).pack(side="left", padx=1)
        self.node_nb = ttk.Notebook(f)
        self.node_nb.pack(fill="both", expand=True, pady=4)
        tf = ttk.Frame(self.node_nb)
        self.node_nb.add(tf, text="📋 Liste")
        self.node_map = None
        if HAS_MAP:
            self.node_map = OfflineMap(
                self.node_nb, DATA_DIR, self.settings, save_settings,
                get_nodes=lambda: (self.iface.nodes if self.iface else {}) or {},
                my_id_func=lambda: self.device_id,
                on_node_action=self._map_node_action, on_set_position=self._map_set_position)
            self.node_nb.add(self.node_map, text="🗺 Karte (offline)")
        self.node_tree = ttk.Treeview(tf, columns=[c[0] for c in self.NODE_COLS], show="headings",
                                      selectmode="browse")
        for key, title, w in self.NODE_COLS:
            self.node_tree.heading(key, text=title, command=lambda k=key: self._sort_nodes(k))
            self.node_tree.column(key, width=w, anchor="w")
        vsb = ttk.Scrollbar(tf, command=self.node_tree.yview)
        self.node_tree.configure(yscrollcommand=vsb.set)
        self.node_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.node_tree.bind("<Double-1>", lambda e: self.node_dm())
        self._node_sort = ("heard", True)

    def _sort_nodes(self, key):
        k, rev = self._node_sort
        self._node_sort = (key, not rev if k == key else False)
        self.refresh_nodes()

    def refresh_nodes(self):
        if not self.iface:
            return
        flt = self.node_filter.get().lower()
        rows = []
        for nid, n in (self.iface.nodes or {}).items():
            u = n.get("user", {})
            pos = n.get("position", {})
            dm = n.get("deviceMetrics", {})
            lat, lon = pos.get("latitude"), pos.get("longitude")
            row = {
                "name": u.get("longName", "") + (" (ich)" if nid == self.device_id else ""),
                "short": u.get("shortName", ""), "id": nid, "hw": u.get("hwModel", ""),
                "role": u.get("role", "CLIENT"),
                "snr": n.get("snr", ""), "hops": n.get("hopsAway", 0 if nid == self.device_id else ""),
                "batt": (f"{dm['batteryLevel']}%" if dm.get("batteryLevel", 0) <= 100 else "🔌")
                if "batteryLevel" in dm else "",
                "heard": n.get("lastHeard") or 0,
                "pos": f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else "",
                "fav": "★" if n.get("isFavorite") else ("🚫" if n.get("isIgnored") else ""),
            }
            if flt and flt not in " ".join(str(v) for v in row.values()).lower():
                continue
            rows.append(row)
        key, rev = self._node_sort

        def sk(r):
            v = r[key]
            return (0, v) if isinstance(v, (int, float)) else (1, str(v).lower())
        rows.sort(key=sk, reverse=rev)
        sel = self.node_tree.selection()
        self.node_tree.delete(*self.node_tree.get_children())
        for r in rows:
            vals = [r[c[0]] if c[0] != "heard" else ago(r["heard"]) for c in self.NODE_COLS]
            self.node_tree.insert("", "end", iid=r["id"], values=vals)
        if sel and self.node_tree.exists(sel[0]):
            self.node_tree.selection_set(sel[0])
        self.node_count.configure(text=f"{len(rows)} Knoten")
        if self.node_map is not None:
            self.node_map.draw_nodes()

    def node_on_map(self):
        nid = self.sel_node()
        if nid and self.node_map is not None:
            self.node_nb.select(1)
            self.update_idletasks()
            self.node_map.center_node(nid)

    def _map_node_action(self, action, nid):
        if not self.iface:
            return
        if self.node_tree.exists(nid):
            self.node_tree.selection_set(nid)
            self.node_tree.see(nid)
        if action == "dm":
            if nid != self.device_id:
                self.open_conv(f"dm:{nid}")
        elif action == "traceroute":
            self.node_traceroute()
        elif action == "details":
            self.node_details()
        elif action == "select":
            self.node_nb.select(0)

    def _map_set_position(self, lat, lon):
        if not self.need_iface():
            return
        if messagebox.askyesno(APP_NAME, f"Feste Position meines Knotens setzen auf\n{lat:.6f}, {lon:.6f} ?"):
            self.fix_lat.set(f"{lat:.6f}")
            self.fix_lon.set(f"{lon:.6f}")
            self.set_fixed_pos()

    def sel_node(self):
        if not self.need_iface():
            return None
        s = self.node_tree.selection()
        if not s:
            messagebox.showinfo(APP_NAME, "Bitte einen Knoten auswählen.")
            return None
        return s[0]

    def node_dm(self):
        nid = self.sel_node()
        if nid and nid != self.device_id:
            self.open_conv(f"dm:{nid}")

    def node_traceroute(self):
        nid = self.sel_node()
        if not nid:
            return
        hop = min(max(self.iface.localNode.localConfig.lora.hop_limit or 3, 1), HOP_LIMIT_MAX)
        self.set_status(f"Traceroute zu {self.node_name(nid)} gesendet … (kann bis zu 1 min dauern)")

        def fmt(num):
            return self.node_name(self.iface._nodeNumToId(num, False) or f"!{num:08x}")

        def on_resp(p):
            d = p.get("decoded", {})
            if d.get("portnum") == "ROUTING_APP":
                err = d.get("routing", {}).get("errorReason")
                if err and err != "NONE":
                    self.q.put(("popup", f"Traceroute fehlgeschlagen: {err}"))
                return
            rd = mesh_pb2.RouteDiscovery()
            rd.ParseFromString(d["payload"])

            def snr(lst, i):
                return f"{lst[i] / 4:.1f} dB" if i < len(lst) and lst[i] != -128 else "? dB"
            hops = [p["to"]] + list(rd.route) + [p["from"]]
            s = "Hinweg:\n  " + fmt(hops[0])
            for i, h in enumerate(hops[1:]):
                s += f"\n   → {fmt(h)}  ({snr(rd.snr_towards, i)})"
            if rd.snr_back:
                back = [p["from"]] + list(rd.route_back) + [p["to"]]
                s += "\n\nRückweg:\n  " + fmt(back[0])
                for i, h in enumerate(back[1:]):
                    s += f"\n   → {fmt(h)}  ({snr(rd.snr_back, i)})"
            self.q.put(("popup", "Traceroute-Ergebnis\n\n" + s))
            self.q.put(("info", "Traceroute abgeschlossen"))

        self.run_bg(lambda: self.iface.sendData(mesh_pb2.RouteDiscovery(), destinationId=nid,
                                                portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True,
                                                onResponse=on_resp, hopLimit=hop))

    def node_request_pos(self):
        nid = self.sel_node()
        if not nid:
            return

        def on_resp(p):
            self.q.put(("info", f"Positionsantwort von {self.node_name(p.get('fromId'))} erhalten"))
            self._nodes_dirty = True
        self.run_bg(lambda: self.iface.sendData(mesh_pb2.Position(), destinationId=nid,
                                                portNum=portnums_pb2.PortNum.POSITION_APP, wantResponse=True,
                                                onResponse=on_resp),
                    ok_msg=f"Positionsanfrage an {self.node_name(nid)} gesendet")

    def node_fav(self):
        nid = self.sel_node()
        if not nid:
            return
        is_fav = (self.iface.nodes or {}).get(nid, {}).get("isFavorite")
        ln = self.iface.localNode

        def do():
            (ln.removeFavorite if is_fav else ln.setFavorite)(nid)
            n = self.iface.nodes.get(nid)
            if n is not None:
                n["isFavorite"] = not is_fav
            self._nodes_dirty = True
        self.run_bg(do, ok_msg="Favorit entfernt" if is_fav else "Als Favorit markiert")

    def node_ignore(self):
        nid = self.sel_node()
        if not nid:
            return
        ign = (self.iface.nodes or {}).get(nid, {}).get("isIgnored")
        ln = self.iface.localNode

        def do():
            (ln.removeIgnored if ign else ln.setIgnored)(nid)
            n = self.iface.nodes.get(nid)
            if n is not None:
                n["isIgnored"] = not ign
            self._nodes_dirty = True
        self.run_bg(do, ok_msg="Nicht mehr ignoriert" if ign else "Knoten wird ignoriert")

    def node_remove(self):
        nid = self.sel_node()
        if not nid or not messagebox.askyesno(APP_NAME, f"{self.node_name(nid)} aus der Knoten-DB entfernen?"):
            return

        def do():
            self.iface.localNode.removeNode(nid)
            self.iface.nodes.pop(nid, None)
            self._nodes_dirty = True
        self.run_bg(do, ok_msg="Knoten entfernt")

    def node_contact_qr(self):
        nid = self.sel_node()
        if not nid:
            return
        try:
            url = self.iface.localNode.getContactURL(nid)
        except BaseException as e:
            messagebox.showerror(APP_NAME, f"Kontakt-URL nicht möglich: {e}")
            return
        QRWindow(self, f"Kontakt: {self.node_name(nid)}", url)

    def node_details(self):
        nid = self.sel_node()
        if not nid:
            return
        n = (self.iface.nodes or {}).get(nid, {})
        win = tk.Toplevel(self)
        win.title(f"Knoten {self.node_name(nid)}")
        win.geometry("560x560")
        t = tk.Text(win, wrap="word", font=("Consolas", 10))
        t.pack(fill="both", expand=True)
        t.insert("1.0", json.dumps(n, indent=2, ensure_ascii=False, default=str))
        pos = n.get("position", {})
        if "latitude" in pos:
            t.insert("end", f"\n\nKoordinaten (kopierbar): {pos['latitude']}, {pos['longitude']}")
        t.configure(state="disabled")

    # ==================================================================== KANÄLE & QR
    def _build_channels_tab(self):
        f = ttk.Frame(self.nb, padding=6)
        self.nb.add(f, text="👥 Kanäle & QR")
        f.columnconfigure(1, weight=1)
        f.columnconfigure(2, weight=0, minsize=330)
        f.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(f, text="Kanäle (Gruppen)", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        self.ch_list = tk.Listbox(left, width=32, exportselection=False, font=("Segoe UI", 10))
        self.ch_list.pack(fill="both", expand=True)
        self.ch_list.bind("<<ListboxSelect>>", self._ch_selected)
        ttk.Button(left, text="➕ Neuer Kanal", command=self.ch_new).pack(fill="x", pady=(6, 1))
        ttk.Button(left, text="🗑 Kanal löschen", command=self.ch_delete).pack(fill="x", pady=1)
        ttk.Button(left, text="🔄 Neu laden", command=self.refresh_channels).pack(fill="x", pady=1)

        ed = ttk.LabelFrame(f, text="Kanal bearbeiten", padding=8)
        ed.grid(row=0, column=1, sticky="nsew", padx=4)
        ed.columnconfigure(1, weight=1)
        self.ch_idx = None
        self.ch_vars = {
            "name": tk.StringVar(), "role": tk.StringVar(), "psk": tk.StringVar(),
            "uplink": tk.BooleanVar(), "downlink": tk.BooleanVar(), "precision": tk.StringVar(),
            "muted": tk.BooleanVar(),
        }
        r = 0
        ttk.Label(ed, text="Index:").grid(row=r, column=0, sticky="w")
        self.ch_idx_lbl = ttk.Label(ed, text="-")
        self.ch_idx_lbl.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(ed, text="Name (max. 11 Zeichen):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(ed, textvariable=self.ch_vars["name"]).grid(row=r, column=1, columnspan=3, sticky="ew")
        r += 1
        ttk.Label(ed, text="Rolle:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(ed, textvariable=self.ch_vars["role"], state="readonly",
                     values=["PRIMARY", "SECONDARY", "DISABLED"]).grid(row=r, column=1, sticky="ew")
        r += 1
        ttk.Label(ed, text="Schlüssel (PSK, Base64):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(ed, textvariable=self.ch_vars["psk"]).grid(row=r, column=1, columnspan=3, sticky="ew")
        r += 1
        pb = ttk.Frame(ed)
        pb.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Button(pb, text="Zufällig AES-256", command=lambda: self._psk(32)).grid(row=0, column=0, sticky="ew")
        ttk.Button(pb, text="Zufällig AES-128", command=lambda: self._psk(16)).grid(row=0, column=1, sticky="ew")
        ttk.Button(pb, text="Standard (AQ==)", command=lambda: self.ch_vars["psk"].set("AQ==")).grid(
            row=1, column=0, sticky="ew")
        ttk.Button(pb, text="Keine", command=lambda: self.ch_vars["psk"].set("")).grid(row=1, column=1, sticky="ew")
        r += 1
        self.psk_info = ttk.Label(ed, text="", foreground="gray")
        self.psk_info.grid(row=r, column=1, columnspan=3, sticky="w")
        self.ch_vars["psk"].trace_add("write", self._psk_info)
        r += 1
        ttk.Checkbutton(ed, text="MQTT Uplink aktiviert", variable=self.ch_vars["uplink"]).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Checkbutton(ed, text="MQTT Downlink aktiviert", variable=self.ch_vars["downlink"]).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Checkbutton(ed, text="Stumm (keine Töne)", variable=self.ch_vars["muted"]).grid(
            row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Label(ed, text="Positionsgenauigkeit (0–32):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(ed, from_=0, to=32, textvariable=self.ch_vars["precision"], width=6).grid(
            row=r, column=1, sticky="w")
        r += 1
        ttk.Button(ed, text="💾 Kanal auf Gerät speichern", command=self.ch_save).grid(
            row=r, column=0, columnspan=4, sticky="ew", pady=12)

        qf = ttk.LabelFrame(f, text="QR-Code teilen / importieren", padding=8)
        qf.grid(row=0, column=2, sticky="nsew", padx=4)
        qf.columnconfigure(0, weight=1)
        self.qr_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(qf, text="Alle Kanäle im QR (sonst nur Primär)", variable=self.qr_all,
                        command=self.update_qr).grid(row=0, column=0, sticky="w")
        self.qr_canvas = QRCanvas(qf, size=260)
        self.qr_canvas.grid(row=1, column=0, pady=6)
        self.qr_url = tk.StringVar()
        ttk.Entry(qf, textvariable=self.qr_url, state="readonly").grid(row=2, column=0, sticky="ew")
        bb = ttk.Frame(qf)
        bb.grid(row=3, column=0, pady=4)
        ttk.Button(bb, text="📋 URL kopieren", command=lambda: self.copy(self.qr_url.get())).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(bb, text="🖼 Als PNG speichern", command=lambda: self.qr_canvas.save_png(self)).grid(
            row=0, column=1, sticky="ew")
        ttk.Button(bb, text="🔳 Mein Kontakt-QR", command=self.my_contact_qr).grid(
            row=1, column=0, columnspan=2, sticky="ew")
        ttk.Separator(qf).grid(row=4, column=0, sticky="ew", pady=8)
        ttk.Label(qf, text="Kanal-URL (…/e/#…) oder Kontakt-URL (…/v/#…) importieren:", wraplength=360).grid(
            row=5, column=0, sticky="w")
        self.import_url = tk.StringVar()
        ttk.Entry(qf, textvariable=self.import_url).grid(row=6, column=0, sticky="ew", pady=2)
        ib = ttk.Frame(qf)
        ib.grid(row=7, column=0)
        ttk.Button(ib, text="➕ Kanäle hinzufügen", command=lambda: self.import_channels(True)).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(ib, text="♻ Alle ersetzen", command=lambda: self.import_channels(False)).grid(
            row=0, column=1, sticky="ew")
        ttk.Button(ib, text="👤 Kontakt hinzufügen", command=self.import_contact).grid(row=1, column=0, sticky="ew")
        ttk.Button(ib, text="📂 QR-Bild laden…", command=self.import_qr_image).grid(row=1, column=1, sticky="ew")

    def _psk(self, n):
        self.ch_vars["psk"].set(base64.b64encode(secrets.token_bytes(n)).decode())

    def _psk_info(self, *a):
        v = self.ch_vars["psk"].get().strip()
        try:
            b = base64.b64decode(v) if v else b""
            txt = {0: "Keine Verschlüsselung!", 1: "Standard-/Einfacher Schlüssel (öffentlich bekannt)",
                   16: "AES-128", 32: "AES-256"}.get(len(b), f"Ungültige Länge ({len(b)} Bytes)")
        except Exception:
            txt = "Ungültiges Base64"
        self.psk_info.configure(text=txt)

    def refresh_channels(self):
        if not self.iface:
            return
        self.ch_list.delete(0, "end")
        for ch in self.iface.localNode.channels or []:
            role = channel_pb2.Channel.Role.Name(ch.role)
            label = f"{ch.index}: {self.channel_name(ch.index) if ch.role else '—'}  [{role}]"
            self.ch_list.insert("end", label)
            if ch.role == channel_pb2.Channel.Role.DISABLED:
                self.ch_list.itemconfigure("end", foreground="gray")
        if self.ch_idx is not None:
            self.ch_list.selection_set(self.ch_idx)
            self._load_channel(self.ch_idx)
        self.update_qr()

    def _ch_selected(self, e=None):
        s = self.ch_list.curselection()
        if s:
            self._load_channel(s[0])

    def _load_channel(self, idx):
        try:
            ch = self.iface.localNode.channels[idx]
        except Exception:
            return
        self.ch_idx = idx
        self.ch_idx_lbl.configure(text=str(idx))
        s = ch.settings
        self.ch_vars["name"].set(s.name)
        self.ch_vars["role"].set(channel_pb2.Channel.Role.Name(ch.role))
        self.ch_vars["psk"].set(base64.b64encode(s.psk).decode())
        self.ch_vars["uplink"].set(s.uplink_enabled)
        self.ch_vars["downlink"].set(s.downlink_enabled)
        self.ch_vars["precision"].set(str(s.module_settings.position_precision))
        self.ch_vars["muted"].set(s.module_settings.is_muted)

    def ch_new(self):
        if not self.need_iface():
            return
        for ch in self.iface.localNode.channels:
            if ch.role == channel_pb2.Channel.Role.DISABLED and ch.index > 0:
                self.ch_list.selection_clear(0, "end")
                self.ch_list.selection_set(ch.index)
                self._load_channel(ch.index)
                self.ch_vars["role"].set("SECONDARY")
                self.ch_vars["name"].set("")
                self._psk(32)
                self.ch_vars["precision"].set("0")
                messagebox.showinfo(APP_NAME, f"Freier Slot {ch.index} gewählt. Namen eingeben und "
                                              "'Kanal auf Gerät speichern' klicken.")
                return
        messagebox.showwarning(APP_NAME, "Alle 8 Kanalplätze sind belegt.")

    def ch_save(self):
        if not self.need_iface() or self.ch_idx is None:
            return
        name = self.ch_vars["name"].get().strip()
        if len(name.encode("utf-8")) > 11:
            messagebox.showerror(APP_NAME, "Kanalname darf max. 11 Bytes lang sein.")
            return
        try:
            psk = base64.b64decode(self.ch_vars["psk"].get().strip() or "")
            if len(psk) not in (0, 1, 16, 32):
                raise ValueError("PSK muss 0, 1, 16 oder 32 Bytes haben")
            prec = int(self.ch_vars["precision"].get() or 0)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Ungültige Eingabe: {e}")
            return
        role = channel_pb2.Channel.Role.Value(self.ch_vars["role"].get())
        if self.ch_idx == 0 and role != channel_pb2.Channel.Role.PRIMARY:
            messagebox.showerror(APP_NAME, "Kanal 0 muss PRIMARY sein.")
            return
        if self.ch_idx != 0 and role == channel_pb2.Channel.Role.PRIMARY:
            messagebox.showerror(APP_NAME, "Nur Kanal 0 kann PRIMARY sein.")
            return
        if role == channel_pb2.Channel.Role.DISABLED:
            return self.ch_delete()
        ch = self.iface.localNode.channels[self.ch_idx]
        ch.role = role
        ch.settings.name = name
        ch.settings.psk = psk
        ch.settings.uplink_enabled = self.ch_vars["uplink"].get()
        ch.settings.downlink_enabled = self.ch_vars["downlink"].get()
        ch.settings.module_settings.position_precision = prec
        ch.settings.module_settings.is_muted = self.ch_vars["muted"].get()
        idx = self.ch_idx

        def done(_):
            self.q.put(("call", self.refresh_channels))
            self.q.put(("call", self.refresh_conv_list))
            return f"Kanal {idx} gespeichert"
        self.run_bg(lambda: self.iface.localNode.writeChannel(idx), ok_msg=done)

    def ch_delete(self):
        if not self.need_iface() or self.ch_idx is None:
            return
        if self.ch_idx == 0:
            messagebox.showerror(APP_NAME, "Der Primärkanal kann nicht gelöscht werden.")
            return
        if not messagebox.askyesno(APP_NAME, f"Kanal {self.ch_idx} ({self.channel_name(self.ch_idx)}) löschen?"):
            return
        idx = self.ch_idx

        def done(_):
            self.q.put(("call", self.refresh_channels))
            self.q.put(("call", self.refresh_conv_list))
            return f"Kanal {idx} gelöscht"
        self.run_bg(lambda: self.iface.localNode.deleteChannel(idx), ok_msg=done)

    def update_qr(self):
        if not self.iface:
            return
        try:
            url = self.iface.localNode.getURL(includeAll=self.qr_all.get())
        except BaseException as e:
            url = ""
            self.log_msg(f"QR-URL Fehler: {e}")
        self.qr_url.set(url)
        self.qr_canvas.show(url)

    def my_contact_qr(self):
        if not self.need_iface():
            return
        try:
            url = self.iface.localNode.getContactURL(self.device_id)
            QRWindow(self, f"Mein Kontakt: {self.node_name(self.device_id)}", url)
        except BaseException as e:
            messagebox.showerror(APP_NAME, f"Kontakt-URL nicht möglich: {e}")

    def import_channels(self, add_only):
        if not self.need_iface():
            return
        url = self.import_url.get().strip()
        if "/e/" not in url and "#" not in url:
            messagebox.showerror(APP_NAME, "Das ist keine gültige Meshtastic-Kanal-URL.")
            return
        if not add_only and not messagebox.askyesno(
                APP_NAME, "ALLE Kanäle und die LoRa-Einstellungen werden durch die URL ersetzt. Fortfahren?"):
            return

        def done(_):
            self.q.put(("call", self.refresh_channels))
            self.q.put(("call", self.refresh_conv_list))
            return "Kanäle importiert"
        self.run_bg(lambda: self.iface.localNode.setURL(url, addOnly=add_only), ok_msg=done, popup=True)

    def import_contact(self):
        if not self.need_iface():
            return
        url = self.import_url.get().strip()
        if "/v/" not in url:
            messagebox.showerror(APP_NAME, "Das ist keine Kontakt-URL (https://meshtastic.org/v/#…).")
            return
        self.run_bg(lambda: self.iface.localNode.addContactURL(url), ok_msg="Kontakt hinzugefügt", popup=True)

    def import_qr_image(self):
        fn = filedialog.askopenfilename(filetypes=[("Bilder", "*.png *.jpg *.jpeg *.bmp"), ("Alle", "*.*")])
        if not fn:
            return
        try:
            from pyzbar.pyzbar import decode  # optional
            from PIL import Image
            res = decode(Image.open(fn))
            if not res:
                raise ValueError("Kein QR-Code erkannt")
            self.import_url.set(res[0].data.decode())
        except ImportError:
            try:
                import cv2  # optional
                img = cv2.imread(fn)
                data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
                if not data:
                    raise ValueError("Kein QR-Code erkannt")
                self.import_url.set(data)
            except ImportError:
                messagebox.showinfo(APP_NAME, "Zum Einlesen von QR-Bildern wird 'opencv-python-headless' "
                                              "oder 'pyzbar' benötigt. Bitte URL direkt einfügen.")
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def copy(self, txt):
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.set_status("In Zwischenablage kopiert")

    # ==================================================================== EINSTELLUNGEN
    def _build_config_tab(self, kind, title):
        f = ttk.Frame(self.nb, padding=4)
        self.nb.add(f, text=("⚙ " if kind == "config" else "🧩 ") + title)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="🔄 Vom Gerät neu laden", command=lambda: self.reload_config(kind, True)).pack(side="left")
        ttk.Button(top, text="💾 Diesen Abschnitt speichern", command=lambda: self.save_config(kind, False)).pack(
            side="left", padx=4)
        ttk.Button(top, text="💾 Alle Abschnitte speichern", command=lambda: self.save_config(kind, True)).pack(
            side="left")
        ttk.Label(top, text="  Hinweis: Viele Änderungen führen zu einem Neustart des Geräts.",
                  foreground="gray").pack(side="left")
        sub = ttk.Notebook(f)
        sub.pack(fill="both", expand=True, pady=4)
        setattr(self, f"{kind}_nb", sub)
        setattr(self, f"{kind}_forms", {})
        ttk.Label(sub, text="Bitte zuerst mit einem Gerät verbinden.").pack()

    def _cfg_root(self, kind):
        ln = self.iface.localNode
        return ln.localConfig if kind == "config" else ln.moduleConfig

    def reload_config(self, kind, from_device=False):
        if not self.iface:
            return
        if from_device:
            ln = self.iface.localNode

            def work():
                root = self._cfg_root(kind)
                for fd in root.DESCRIPTOR.fields:
                    if fd.name == "version":
                        continue
                    try:
                        p = admin_pb2.AdminMessage()
                        if kind == "config":
                            p.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
                                fd.name.upper() + "_CONFIG")
                        else:
                            p.get_module_config_request = fd.index
                        ln._sendAdmin(p, wantResponse=True, onResponse=ln.onResponseRequestSettings)
                        time.sleep(0.4)
                    except BaseException as e:
                        log.warning("requestConfig %s: %s", fd.name, e)
                time.sleep(2.5)
                self.q.put(("call", lambda: self.reload_config(kind)))
                return "Einstellungen neu geladen"
            self.run_bg(work, ok_msg=lambda r: r)
            return
        nb = getattr(self, f"{kind}_nb")
        cur = nb.index("current") if nb.tabs() else 0
        for t in nb.tabs():
            nb.forget(t)
        for w in nb.winfo_children():
            w.destroy()
        forms = {}
        root = self._cfg_root(kind)
        for fd in root.DESCRIPTOR.fields:
            if fd.name == "version" or fd.type != FieldDescriptor.TYPE_MESSAGE:
                continue
            sf = ScrollFrame(nb)
            form = ProtoForm(sf.inner, getattr(root, fd.name), section=fd.name)
            form.frame.pack(fill="both", expand=True, padx=6, pady=6)
            nb.add(sf, text=SECTION_DE.get(fd.name, pretty(fd.name)))
            forms[fd.name] = form
        setattr(self, f"{kind}_forms", forms)
        setattr(self, f"{kind}_order", [fd.name for fd in root.DESCRIPTOR.fields
                                        if fd.name != "version" and fd.type == FieldDescriptor.TYPE_MESSAGE])
        if nb.tabs():
            nb.select(min(cur, len(nb.tabs()) - 1))

    def write_section(self, kind, name):
        """Schreibt einen Config-/Modul-Abschnitt per Admin-Nachricht auf das Gerät."""
        ln = self.iface.localNode
        p = admin_pb2.AdminMessage()
        src = getattr(self._cfg_root(kind), name)
        if kind == "config":
            getattr(p.set_config, name).CopyFrom(src)
        else:
            getattr(p.set_module_config, name).CopyFrom(src)
        ln._sendAdmin(p)

    def save_config(self, kind, all_sections):
        if not self.need_iface():
            return
        forms = getattr(self, f"{kind}_forms")
        order = getattr(self, f"{kind}_order", [])
        nb = getattr(self, f"{kind}_nb")
        if not forms:
            return
        names = order if all_sections else [order[nb.index("current")]]
        root = self._cfg_root(kind)
        try:
            # erst validieren (auf Kopien), dann übernehmen
            for n in names:
                tmp = type(getattr(root, n))()
                tmp.CopyFrom(getattr(root, n))
                forms[n].apply_to(tmp)
            for n in names:
                forms[n].apply_to(getattr(root, n))
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        label = ", ".join(SECTION_DE.get(n, n) for n in names)
        if not messagebox.askyesno(APP_NAME, f"Einstellungen auf das Gerät schreiben?\n\n{label}"):
            return

        def work():
            ln = self.iface.localNode
            multi = len(names) > 1
            if multi:
                ln.beginSettingsTransaction()
            for n in names:
                self.write_section(kind, n)
                time.sleep(0.2)
            if multi:
                ln.commitSettingsTransaction()
            return f"Gespeichert: {label}"
        self.run_bg(work, ok_msg=lambda r: r, popup=True)

    # ==================================================================== GERÄT / ADMIN
    def _build_device_tab(self):
        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text="🛠 Gerät")
        f = sf.inner
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        own = ttk.LabelFrame(f, text="Besitzer / Benutzer", padding=8)
        own.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        own.columnconfigure(1, weight=1)
        self.own_long = tk.StringVar()
        self.own_short = tk.StringVar()
        self.own_lic = tk.BooleanVar()
        self.own_unmsg = tk.BooleanVar()
        ttk.Label(own, text="Langer Name:").grid(row=0, column=0, sticky="w")
        ttk.Entry(own, textvariable=self.own_long).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(own, text="Kurzname (max. 4):").grid(row=1, column=0, sticky="w")
        ttk.Entry(own, textvariable=self.own_short, width=8).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Checkbutton(own, text="Lizenzierter Funkamateur (keine Verschlüsselung!)",
                        variable=self.own_lic).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(own, text="Nicht anschreibbar (unmessagable)", variable=self.own_unmsg).grid(
            row=3, column=1, sticky="w")
        ttk.Button(own, text="💾 Speichern", command=self.save_owner).grid(row=4, column=1, sticky="e", pady=4)

        info = ttk.LabelFrame(f, text="Geräteinformationen", padding=8)
        info.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=6, pady=6)
        self.info_text = tk.Text(info, height=18, width=50, font=("Consolas", 9), wrap="word")
        self.info_text.pack(fill="both", expand=True)

        pos = ttk.LabelFrame(f, text="Feste Position", padding=8)
        pos.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.fix_lat, self.fix_lon, self.fix_alt = tk.StringVar(), tk.StringVar(), tk.StringVar(value="0")
        for i, (lbl, v) in enumerate((("Breitengrad:", self.fix_lat), ("Längengrad:", self.fix_lon),
                                      ("Höhe (m):", self.fix_alt))):
            ttk.Label(pos, text=lbl).grid(row=i, column=0, sticky="w")
            ttk.Entry(pos, textvariable=v, width=18).grid(row=i, column=1, sticky="w", pady=2)
        bp = ttk.Frame(pos)
        bp.grid(row=3, column=0, columnspan=2, pady=4, sticky="w")
        ttk.Button(bp, text="📍 Feste Position setzen", command=self.set_fixed_pos).pack(side="left")
        ttk.Button(bp, text="Entfernen", command=self.remove_fixed_pos).pack(side="left", padx=2)
        ttk.Button(bp, text="📡 Position jetzt senden", command=self.send_position).pack(side="left")

        msgs = ttk.LabelFrame(f, text="Vorgefertigte Nachrichten & Klingelton", padding=8)
        msgs.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
        msgs.columnconfigure(1, weight=1)
        self.canned = tk.StringVar()
        self.ringtone = tk.StringVar()
        ttk.Label(msgs, text="Nachrichten (mit | trennen):").grid(row=0, column=0, sticky="w")
        ttk.Entry(msgs, textvariable=self.canned).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(msgs, text="Klingelton (RTTTL):").grid(row=1, column=0, sticky="w")
        ttk.Entry(msgs, textvariable=self.ringtone).grid(row=1, column=1, sticky="ew", pady=2)
        bm = ttk.Frame(msgs)
        bm.grid(row=2, column=1, sticky="e")
        ttk.Button(bm, text="Vom Gerät lesen", command=self.load_canned).pack(side="left")
        ttk.Button(bm, text="💾 Speichern", command=self.save_canned).pack(side="left", padx=2)

        bk = ttk.LabelFrame(f, text="Backup & Wiederherstellung (offline, JSON-Datei)", padding=8)
        bk.grid(row=2, column=1, sticky="nsew", padx=6, pady=6)
        ttk.Button(bk, text="📤 Komplette Konfiguration exportieren…", command=self.backup).pack(fill="x", pady=2)
        ttk.Button(bk, text="📥 Konfiguration importieren…", command=self.restore).pack(fill="x", pady=2)
        ttk.Label(bk, text="Enthält Besitzer, alle Geräte-/Moduleinstellungen und Kanäle (inkl. Schlüssel!).",
                  foreground="gray", wraplength=380).pack(anchor="w")

        adm = ttk.LabelFrame(f, text="Gerätebefehle", padding=8)
        adm.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        cmds = (
            ("🕒 Uhrzeit vom PC setzen", self.cmd_settime, False),
            ("🔁 Neustart", lambda: self.iface.localNode.reboot(5), True),
            ("⏻ Herunterfahren", lambda: self.iface.localNode.shutdown(5), True),
            ("🧹 Knoten-DB zurücksetzen", lambda: self.iface.localNode.resetNodeDb(), True),
            ("⚠ Werksreset (Einstellungen)", lambda: self.iface.localNode.factoryReset(False), True),
            ("☢ Werksreset (komplett)", lambda: self.iface.localNode.factoryReset(True), True),
            ("📶 Eigene Info senden (NodeInfo)", self.cmd_send_nodeinfo, False),
            ("⬆ OTA/DFU-Modus", lambda: self.iface.localNode.enterDFUMode(), True),
        )
        for i, (txt, fn, confirm) in enumerate(cmds):
            ttk.Button(adm, text=txt, command=lambda t=txt, fn=fn, c=confirm: self.dev_cmd(t, fn, c)).grid(
                row=i // 4, column=i % 4, sticky="ew", padx=3, pady=3)
        for c in range(4):
            adm.columnconfigure(c, weight=1)

        app = ttk.LabelFrame(f, text="Über", padding=8)
        app.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        ttk.Label(app, text=f"{APP_NAME} {APP_VERSION} – Meshtastic-Python {_mt_version()}"
                            f"\nLokale Daten (Chats, Einstellungen, Log): {DATA_DIR}\n"
                            f"Bluetooth: {'verfügbar' if HAS_BLE else 'nicht verfügbar'} · "
                            f"QR: {'verfügbar' if HAS_QR else 'nicht verfügbar'}").pack(anchor="w")
        ttk.Button(app, text="📁 Datenordner öffnen", command=self.open_datadir).pack(anchor="w", pady=4)

    def open_datadir(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(DATA_DIR)  # noqa
            else:
                import subprocess
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", DATA_DIR])
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def refresh_device_tab(self):
        if not self.iface:
            return
        info = self.iface.getMyNodeInfo() or {}
        u = info.get("user", {})
        self.own_long.set(u.get("longName", ""))
        self.own_short.set(u.get("shortName", ""))
        self.own_lic.set(bool(u.get("isLicensed")))
        self.own_unmsg.set(bool(u.get("isUnmessagable")))
        p = info.get("position", {})
        if "latitude" in p:
            self.fix_lat.set(str(p.get("latitude")))
            self.fix_lon.set(str(p.get("longitude")))
            self.fix_alt.set(str(p.get("altitude", 0)))
        lines = []
        try:
            if self.iface.metadata:
                lines.append("== Metadaten ==")
                lines.append(json_format.MessageToJson(self.iface.metadata, indent=1))
            if self.iface.myInfo:
                lines.append("== MyNodeInfo ==")
                lines.append(json_format.MessageToJson(self.iface.myInfo, indent=1))
            lines.append("== Node ==")
            lines.append(json.dumps(info, indent=1, ensure_ascii=False, default=str))
        except Exception as e:
            lines.append(str(e))
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("1.0", "\n".join(lines))
        self.info_text.configure(state="disabled")

    def save_owner(self):
        if not self.need_iface():
            return
        ln, sn = self.own_long.get().strip(), self.own_short.get().strip()
        if not ln or not sn:
            messagebox.showerror(APP_NAME, "Langer Name und Kurzname dürfen nicht leer sein.")
            return
        if len(sn) > 4:
            messagebox.showerror(APP_NAME, "Kurzname max. 4 Zeichen.")
            return
        lic, unm = self.own_lic.get(), self.own_unmsg.get()
        self.run_bg(lambda: self.iface.localNode.setOwner(long_name=ln, short_name=sn, is_licensed=lic,
                                                          is_unmessagable=unm),
                    ok_msg="Besitzer gespeichert", popup=True)

    def set_fixed_pos(self):
        if not self.need_iface():
            return
        try:
            lat = float(self.fix_lat.get().replace(",", "."))
            lon = float(self.fix_lon.get().replace(",", "."))
            alt = int(float(self.fix_alt.get().replace(",", ".") or 0))
            assert -90 <= lat <= 90 and -180 <= lon <= 180
        except Exception:
            messagebox.showerror(APP_NAME, "Ungültige Koordinaten.")
            return
        self.run_bg(lambda: self.iface.localNode.setFixedPosition(lat, lon, alt),
                    ok_msg="Feste Position gesetzt", popup=True)

    def remove_fixed_pos(self):
        if self.need_iface():
            self.run_bg(lambda: self.iface.localNode.removeFixedPosition(), ok_msg="Feste Position entfernt",
                        popup=True)

    def send_position(self):
        if not self.need_iface():
            return
        try:
            lat = float(self.fix_lat.get().replace(",", ".")) if self.fix_lat.get() else 0.0
            lon = float(self.fix_lon.get().replace(",", ".")) if self.fix_lon.get() else 0.0
            alt = int(float(self.fix_alt.get() or 0))
        except Exception:
            lat = lon = 0.0
            alt = 0
        self.run_bg(lambda: self.iface.sendPosition(lat, lon, alt), ok_msg="Position gesendet")

    def load_canned(self):
        if not self.need_iface():
            return
        ln = self.iface.localNode

        def work():
            c = ln.get_canned_message() or ""
            r = ln.get_ringtone() or ""
            self.q.put(("call", lambda: (self.canned.set(c), self.ringtone.set(r))))
            return "Vorgefertigte Nachrichten/Klingelton gelesen"
        self.run_bg(work, ok_msg=lambda r: r)

    def save_canned(self):
        if not self.need_iface():
            return
        c, r = self.canned.get(), self.ringtone.get()
        ln = self.iface.localNode

        def work():
            ln.set_canned_message(c)
            if r:
                time.sleep(0.3)
                ln.set_ringtone(r)
        self.run_bg(work, ok_msg="Gespeichert", popup=True)

    def cmd_settime(self):
        self.iface.localNode.setTime(int(time.time()))

    def cmd_send_nodeinfo(self):
        u = self.iface.getMyUser() or {}
        user = mesh_pb2.User()
        json_format.ParseDict(u, user, ignore_unknown_fields=True)
        self.iface.sendData(user, portNum=portnums_pb2.PortNum.NODEINFO_APP, wantResponse=True)

    def dev_cmd(self, title, fn, confirm):
        if not self.need_iface():
            return
        if confirm and not messagebox.askyesno(APP_NAME, f"Wirklich ausführen?\n\n{title}"):
            return
        self.run_bg(fn, ok_msg=f"Ausgeführt: {title}")

    def backup(self):
        if not self.need_iface():
            return
        ln = self.iface.localNode
        u = self.iface.getMyUser() or {}
        data = {
            "app": APP_NAME, "version": APP_VERSION, "exported": _dt.datetime.now().isoformat(),
            "node_id": self.device_id,
            "owner": {"long_name": u.get("longName"), "short_name": u.get("shortName"),
                      "is_licensed": u.get("isLicensed", False)},
            "channel_url": ln.getURL(includeAll=True),
            "channels": [json_format.MessageToDict(c) for c in ln.channels or []],
            "config": json_format.MessageToDict(ln.localConfig),
            "module_config": json_format.MessageToDict(ln.moduleConfig),
        }
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                          initialfile=f"meshtastic_backup_{self.device_id.strip('!')}.json")
        if not fn:
            return
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        messagebox.showinfo(APP_NAME, f"Backup gespeichert:\n{fn}\n\nAchtung: enthält Schlüssel!")

    def restore(self):
        if not self.need_iface():
            return
        fn = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not fn:
            return
        try:
            with open(fn, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Datei ungültig: {e}")
            return
        if not messagebox.askyesno(APP_NAME, "Konfiguration aus Backup auf das Gerät schreiben?\n"
                                             "Das Gerät startet danach neu."):
            return
        ln = self.iface.localNode

        def work():
            ln.beginSettingsTransaction()
            o = data.get("owner") or {}
            if o.get("long_name"):
                ln.setOwner(long_name=o["long_name"], short_name=o.get("short_name"),
                            is_licensed=bool(o.get("is_licensed")))
            if data.get("config"):
                json_format.ParseDict(data["config"], ln.localConfig, ignore_unknown_fields=True)
                if not 1 <= ln.localConfig.lora.hop_limit <= HOP_LIMIT_MAX:
                    ln.localConfig.lora.hop_limit = min(max(ln.localConfig.lora.hop_limit, 1), HOP_LIMIT_MAX)
                for fd in ln.localConfig.DESCRIPTOR.fields:
                    if fd.name != "version" and ln.localConfig.HasField(fd.name):
                        self.write_section("config", fd.name)
                        time.sleep(0.2)
            if data.get("module_config"):
                json_format.ParseDict(data["module_config"], ln.moduleConfig, ignore_unknown_fields=True)
                for fd in ln.moduleConfig.DESCRIPTOR.fields:
                    if fd.name != "version" and ln.moduleConfig.HasField(fd.name):
                        self.write_section("module", fd.name)
                        time.sleep(0.2)
            for cd in data.get("channels") or []:
                ch = channel_pb2.Channel()
                json_format.ParseDict(cd, ch, ignore_unknown_fields=True)
                if 0 <= ch.index < len(ln.channels):
                    ln.channels[ch.index].CopyFrom(ch)
                    ln.writeChannel(ch.index)
                    time.sleep(0.2)
            ln.commitSettingsTransaction()
            return "Backup wiederhergestellt"
        self.run_bg(work, ok_msg=lambda r: r, popup=True)

    # ==================================================================== FIRMWARE-FLASHER
    def _build_flasher_tab(self):
        if not HAS_FLASHER:
            return
        self.flasher = FlasherFrame(self.nb, data_dir=DATA_DIR, settings=self.settings, save_settings=save_settings,
                                    get_iface=lambda: self.iface, disconnect_app=self._disconnect_sync)
        self.nb.add(self.flasher, text="⚡ Firmware")

    # ==================================================================== LOG
    def _build_log_tab(self):
        f = ttk.Frame(self.nb, padding=4)
        self.nb.add(f, text="📜 Log")
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.show_devlog = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Geräte-Debuglog anzeigen", variable=self.show_devlog).pack(side="left")
        ttk.Button(top, text="Leeren", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = tk.Text(f, wrap="none", font=("Consolas", 9))
        sb = ttk.Scrollbar(f, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    def log_msg(self, msg):
        log.info(msg)
        self._append_log(f"[{time.strftime('%H:%M:%S')}] {msg}", force=True)

    def _append_log(self, line, force=False):
        if not force and not self.show_devlog.get():
            return
        self.log_text.insert("end", line.rstrip() + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 5000:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.see("end")

    def set_status(self, txt):
        self.statusbar.configure(text=txt)

    def _on_close(self):
        self._save_conn_settings()
        self._manual_disconnect = True
        if self.iface:
            try:
                self.iface.close()
            except BaseException:
                pass
        self.destroy()


# --------------------------------------------------------------------------------------
# QR-Code-Anzeige (offline, reines Tk – kein Internet, kein PIL nötig)
# --------------------------------------------------------------------------------------
class QRCanvas(tk.Canvas):
    def __init__(self, parent, size=300):
        super().__init__(parent, width=size, height=size, bg="white", highlightthickness=1,
                         highlightbackground="#ccc")
        self.size = size
        self.data = ""

    def show(self, data):
        self.data = data
        self.delete("all")
        if not data:
            self.create_text(self.size / 2, self.size / 2, text="(kein QR-Code)")
            return
        if not HAS_QR:
            self.create_text(self.size / 2, self.size / 2, text="Paket 'qrcode' fehlt", fill="red")
            return
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(data)
        qr.make(fit=True)
        m = qr.get_matrix()
        n = len(m)
        cell = self.size / n
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    self.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                          fill="black", outline="black")

    def save_png(self, app):
        if not self.data or not HAS_QR:
            return
        fn = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")],
                                          initialfile="meshtastic_qr.png")
        if not fn:
            return
        try:
            img = qrcode.make(self.data, box_size=10, border=3)
            img.save(fn)
            app.set_status(f"QR gespeichert: {fn}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"PNG-Speichern fehlgeschlagen (Pillow installiert?): {e}")


class QRWindow(tk.Toplevel):
    def __init__(self, app, title, url):
        super().__init__(app)
        self.title(title)
        self.resizable(False, False)
        ttk.Label(self, text=title, style="Big.TLabel").pack(pady=6)
        c = QRCanvas(self, size=340)
        c.pack(padx=10)
        c.show(url)
        v = tk.StringVar(value=url)
        ttk.Entry(self, textvariable=v, state="readonly", width=60).pack(padx=10, pady=6, fill="x")
        b = ttk.Frame(self)
        b.pack(pady=6)
        ttk.Button(b, text="📋 Kopieren", command=lambda: app.copy(url)).pack(side="left")
        ttk.Button(b, text="🖼 PNG speichern", command=lambda: c.save_png(app)).pack(side="left", padx=4)
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="left")


def main():
    if "--selftest" in sys.argv:
        ok_map = False
        if HAS_MAP:
            import mapview
            ok_map = len(mapview.VectorData(mapview.resource_path("worldmap.json.gz")).layers["land2"]) > 200
        print(f"{APP_NAME} {APP_VERSION} OK – meshtastic {_mt_version()}, "
              f"BLE={load_ble() is not None}, QR={HAS_QR}, Karte={ok_map}")
        return
    app = MeshApp()
    app.mainloop()


if __name__ == "__main__":
    main()
