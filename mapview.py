# -*- coding: utf-8 -*-
"""
Offline-Weltkarte für Meshtastic Desktop (reines Tkinter, keine Internetverbindung nötig).

Ebenen:
  * Eingebaute Vektorkarte (Natural Earth, Public Domain): Länder, Bundesländer/Provinzen,
    Seen, Flüsse, Hauptstraßen, Städte – funktioniert immer offline.
  * Optional Rasterkacheln aus einer lokalen MBTiles-Datei (offline, beliebig detailliert).
  * Optional Kachel-Cache: Kacheln werden (nur wenn ausdrücklich eingeschaltet) einmalig online
    geladen und lokal gespeichert – danach offline verfügbar.
"""
import base64
import gzip
import io
import json
import math
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import urllib.request
from array import array
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk  # für JPEG-Kacheln
    HAS_PIL = True
except Exception:
    HAS_PIL = False

TILE = 256
MIN_Z, MAX_Z = 1, 18
VEC_MAX_Z = 15
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "MeshtasticDesktop/1.0 (offline map cache)"
MAX_BULK_TILES = 3000

COL = {
    "ocean": "#aad3df", "land": "#f2efe9", "border": "#9e9cab", "admin1": "#c4b5d4",
    "lake": "#aad3df", "river": "#8ab8d8", "road_major": "#e8a06b", "road": "#d6b78f",
    "city": "#333333", "country": "#6b5f7a",
}


def merc_y(lat):
    lat = max(-85.0511, min(85.0511, lat))
    r = math.radians(lat)
    return 0.5 - math.log(math.tan(math.pi / 4 + r / 2)) / (2 * math.pi)


def merc_x(lon):
    return (lon + 180.0) / 360.0


def inv_lat(my):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my))))


def inv_lon(mx):
    return mx * 360.0 - 180.0


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ======================================================================================
# Vektordaten
# ======================================================================================
class VectorData:
    def __init__(self, path):
        with gzip.open(path, "rb") as f:
            raw = json.loads(f.read())
        S = raw.get("scale", 10000)
        self.attribution = raw.get("attribution", "")
        self.layers = {}
        for key in ("land0", "land1", "land2", "admin1", "lakes", "lakes_eu", "rivers", "rivers_eu", "roads"):
            feats = []
            for bbox, parts, props in raw.get(key, []):
                mparts = []
                for d in parts:
                    xs, ys = [], []
                    x = y = 0
                    for i in range(0, len(d), 2):
                        x += d[i]
                        y += d[i + 1]
                        xs.append(x)
                        ys.append(y)
                    a = array("d")
                    for xi, yi in zip(xs, ys):
                        a.append((xi / S + 180.0) / 360.0)
                        a.append(merc_y(yi / S))
                    mparts.append(a)
                nb = (merc_x(bbox[0] / S), merc_y(bbox[3] / S), merc_x(bbox[2] / S), merc_y(bbox[1] / S))
                feats.append((nb, mparts, props))
            self.layers[key] = feats
        self.labels = [(n, merc_x(x), merc_y(y), mz) for n, x, y, mz in raw.get("labels", [])]
        self.cities = [(n, merc_x(x), merc_y(y), mz, pop) for n, x, y, mz, pop in raw.get("cities", [])]


# ======================================================================================
# Kachelquellen
# ======================================================================================
class MBTiles:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        meta = dict(self.db.execute("SELECT name, value FROM metadata").fetchall())
        self.format = (meta.get("format") or "png").lower()
        if self.format in ("pbf", "mvt"):
            raise ValueError("Vektor-MBTiles (pbf) werden nicht unterstützt – bitte Raster-MBTiles (png/jpg) "
                             "verwenden.")
        self.name = meta.get("name", os.path.basename(path))
        self.minzoom = int(meta.get("minzoom", 0) or 0)
        self.maxzoom = int(meta.get("maxzoom", 18) or 18)

    def get(self, z, x, y):
        with self.lock:
            r = self.db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                (z, x, (1 << z) - 1 - y)).fetchone()
        return r[0] if r else None


class TileCache:
    """Lokaler Kachel-Cache (SQLite). Lädt fehlende Kacheln nur, wenn online erlaubt."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS tiles(src TEXT, z INT, x INT, y INT, data BLOB, ts REAL,"
                        " PRIMARY KEY(src, z, x, y))")
        self.db.commit()

    def get(self, src, z, x, y):
        with self.lock:
            r = self.db.execute("SELECT data FROM tiles WHERE src=? AND z=? AND x=? AND y=?",
                                (src, z, x, y)).fetchone()
        return r[0] if r else None

    def has(self, src, z, x, y):
        with self.lock:
            return self.db.execute("SELECT 1 FROM tiles WHERE src=? AND z=? AND x=? AND y=?",
                                   (src, z, x, y)).fetchone() is not None

    def put(self, src, z, x, y, data):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO tiles VALUES(?,?,?,?,?,?)", (src, z, x, y, data, time.time()))
            self.db.commit()

    def stats(self):
        with self.lock:
            n, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)),0) FROM tiles").fetchone()
        return n, size

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM tiles")
            self.db.commit()
            self.db.execute("VACUUM")


def fetch_tile(url_tmpl, z, x, y, timeout=15):
    url = url_tmpl.format(z=z, x=x, y=y, s="abc"[(x + y) % 3])
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ======================================================================================
# Karten-Widget
# ======================================================================================
class OfflineMap(ttk.Frame):
    def __init__(self, parent, data_dir, settings, save_settings, get_nodes, my_id_func,
                 on_node_action=None, on_set_position=None):
        super().__init__(parent)
        self.settings = settings
        self.save_settings = save_settings
        self.get_nodes = get_nodes          # -> dict id -> node
        self.my_id = my_id_func
        self.on_node_action = on_node_action or (lambda action, nid: None)
        self.on_set_position = on_set_position or (lambda lat, lon: None)
        self.vec = None
        self.z = int(settings.get("map_zoom", 3))
        self.cx = settings.get("map_cx", merc_x(10.0))   # Mitte (normiert 0..1)
        self.cy = settings.get("map_cy", merc_y(50.0))
        self.mbtiles = None
        self.cache = TileCache(os.path.join(data_dir, "tilecache.db"))
        self.tile_url = settings.get("map_tile_url", DEFAULT_TILE_URL)
        self.images = {}       # (z,x,y) -> PhotoImage  (LRU)
        self.pending = set()
        self.fetch_q = queue.Queue()
        self.result_q = queue.Queue()
        self._redraw_job = None
        self._drag = None
        self._bulk_cancel = False
        self._workers_started = False

        # ---------------- Werkzeugleiste
        tb = ttk.Frame(self)
        tb.pack(fill="x")
        ttk.Button(tb, text="＋", width=3, command=lambda: self.zoom_by(1)).pack(side="left")
        ttk.Button(tb, text="－", width=3, command=lambda: self.zoom_by(-1)).pack(side="left", padx=(0, 4))
        ttk.Button(tb, text="🔎 Alle Knoten", command=self.fit_nodes).pack(side="left")
        ttk.Button(tb, text="📡 Mein Knoten", command=self.center_me).pack(side="left", padx=2)
        ttk.Button(tb, text="🌍 Welt", command=lambda: self.set_view(merc_x(10), merc_y(30), 2)).pack(side="left")
        tb2 = ttk.Frame(self)
        tb2.pack(fill="x", pady=(2, 2))
        self.v_roads = tk.BooleanVar(value=settings.get("map_roads", True))
        self.v_water = tk.BooleanVar(value=settings.get("map_water", True))
        self.v_cities = tk.BooleanVar(value=settings.get("map_cities", True))
        self.v_admin = tk.BooleanVar(value=settings.get("map_admin", True))
        self.v_labels = tk.BooleanVar(value=settings.get("map_labels", True))
        self.v_old = tk.BooleanVar(value=settings.get("map_old", True))
        for txt, var in (("Straßen", self.v_roads), ("Gewässer", self.v_water), ("Städte", self.v_cities),
                         ("Grenzen", self.v_admin), ("Knotennamen", self.v_labels),
                         ("Alte Knoten (>24h)", self.v_old)):
            ttk.Checkbutton(tb2, text=txt, variable=var, command=self._opts_changed).pack(side="left", padx=(0, 6))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(tb, text="Hintergrund:").pack(side="left")
        self.v_src = tk.StringVar(value=settings.get("map_src", "Vektor (eingebaut)"))
        self.src_cb = ttk.Combobox(tb, textvariable=self.v_src, state="readonly", width=22,
                                   values=["Vektor (eingebaut)", "MBTiles-Datei", "Kachel-Cache"])
        self.src_cb.pack(side="left", padx=2)
        self.src_cb.bind("<<ComboboxSelected>>", lambda e: self._src_changed())
        ttk.Button(tb, text="⚙", width=3, command=self.tile_settings).pack(side="left")

        # ---------------- Zeichenfläche
        self.canvas = tk.Canvas(self, bg=COL["ocean"], highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.status = ttk.Label(self, text="Karte wird geladen …", anchor="w")
        self.status.pack(fill="x")

        c = self.canvas
        c.bind("<Configure>", lambda e: self.schedule_redraw(10))
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", self._dbl)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1))
        c.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, -1))
        c.bind("<Motion>", self._mouse_pos)
        c.bind("<Button-3>", self._context)
        c.bind("<Button-2>", self._context)

        if self.v_src.get() == "MBTiles-Datei" and settings.get("map_mbtiles"):
            try:
                self.mbtiles = MBTiles(settings["map_mbtiles"])
            except Exception:
                self.v_src.set("Vektor (eingebaut)")

        threading.Thread(target=self._load_vec, daemon=True).start()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ Laden
    def _load_vec(self):
        try:
            t = time.time()
            v = VectorData(resource_path("worldmap.json.gz"))
            self.result_q.put(("vec", v, time.time() - t))
        except Exception as e:
            self.result_q.put(("vecerr", str(e), 0))

    def _poll(self):
        redraw = False
        try:
            while True:
                kind, a, b = self.result_q.get_nowait()
                if kind == "vec":
                    self.vec = a
                    redraw = True
                    self.status.configure(text=f"Offline-Weltkarte geladen ({b:.1f} s) · Mausrad = Zoom, "
                                               "Ziehen = Verschieben, Klick auf Knoten = Info, Rechtsklick = Menü")
                elif kind == "vecerr":
                    self.status.configure(text=f"Weltkarte konnte nicht geladen werden: {a}")
                elif kind == "tile":
                    self.pending.discard(a)
                    if b and a[0] == self.z:
                        self._place_tile(a, b)
                elif kind == "bulk":
                    self.status.configure(text=a)
        except queue.Empty:
            pass
        if redraw:
            self.redraw()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ Geometrie
    @property
    def W(self):
        return TILE * (1 << self.z)

    def origin(self):
        w, h = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        return self.cx * self.W - w / 2, self.cy * self.W - h / 2, w, h

    def to_screen(self, mx, my):
        ox, oy, _, _ = self.origin()
        return mx * self.W - ox, my * self.W - oy

    def to_merc(self, sx, sy):
        ox, oy, _, _ = self.origin()
        return (sx + ox) / self.W, (sy + oy) / self.W

    def set_view(self, cx, cy, z=None):
        if z is not None:
            self.z = max(MIN_Z, min(self.max_zoom(), int(z)))
        self.cx = min(max(cx, 0.0), 1.0)
        self.cy = min(max(cy, 0.0), 1.0)
        self.redraw()

    def max_zoom(self):
        if self.v_src.get() == "MBTiles-Datei" and self.mbtiles:
            return max(VEC_MAX_Z, self.mbtiles.maxzoom)
        if self.v_src.get() == "Kachel-Cache":
            return MAX_Z
        return VEC_MAX_Z

    def zoom_by(self, d):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._zoom_at(w / 2, h / 2, d)

    def _zoom_at(self, sx, sy, d):
        nz = max(MIN_Z, min(self.max_zoom(), self.z + d))
        if nz == self.z:
            return
        mx, my = self.to_merc(sx, sy)
        self.z = nz
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        # Punkt unter der Maus bleibt stehen
        self.cx = mx - (sx - w / 2) / self.W
        self.cy = my - (sy - h / 2) / self.W
        self.set_view(self.cx, self.cy)

    # ------------------------------------------------------------------ Maus
    def _press(self, e):
        self._drag = (e.x, e.y, e.x, e.y)

    def _motion(self, e):
        if not self._drag:
            return
        x0, y0, lx, ly = self._drag
        self.canvas.move("all", e.x - lx, e.y - ly)
        self._drag = (x0, y0, e.x, e.y)

    def _release(self, e):
        if not self._drag:
            return
        x0, y0, _, _ = self._drag
        self._drag = None
        dx, dy = e.x - x0, e.y - y0
        if abs(dx) + abs(dy) < 3:
            self._click(e)
            return
        self.cx -= dx / self.W
        self.cy -= dy / self.W
        self.cx = min(max(self.cx, 0.0), 1.0)
        self.cy = min(max(self.cy, 0.0), 1.0)
        self.redraw()

    def _wheel(self, e):
        self._zoom_at(e.x, e.y, 1 if e.delta > 0 else -1)

    def _dbl(self, e):
        nid = self._node_at(e.x, e.y)
        if nid:
            self.on_node_action("dm", nid)
        else:
            self._zoom_at(e.x, e.y, 1)

    def _mouse_pos(self, e):
        mx, my = self.to_merc(e.x, e.y)
        if 0 <= my <= 1:
            self._last_ll = (inv_lat(my), inv_lon(mx))
            self.status.configure(text=f"Zoom {self.z}   ·   {self._last_ll[0]:.5f}, {self._last_ll[1]:.5f}   ·   "
                                       f"{self._src_info()}")

    def _src_info(self):
        s = self.v_src.get()
        if s == "MBTiles-Datei" and self.mbtiles:
            return f"MBTiles: {self.mbtiles.name} (z{self.mbtiles.minzoom}–{self.mbtiles.maxzoom})"
        if s == "Kachel-Cache":
            return "Kachel-Cache" + (" + online laden" if self.settings.get("map_online") else " (offline)")
        return self.vec.attribution if self.vec else ""

    def _node_at(self, x, y):
        for item in self.canvas.find_overlapping(x - 4, y - 4, x + 4, y + 4):
            for t in self.canvas.gettags(item):
                if t.startswith("node:"):
                    return t[5:]
        return None

    def _click(self, e):
        nid = self._node_at(e.x, e.y)
        if nid:
            self.show_popup(nid)
        else:
            self.canvas.delete("popup")

    def _context(self, e):
        mx, my = self.to_merc(e.x, e.y)
        lat, lon = inv_lat(my), inv_lon(mx)
        nid = self._node_at(e.x, e.y)
        m = tk.Menu(self, tearoff=0)
        if nid:
            m.add_command(label="💬 Nachricht senden", command=lambda: self.on_node_action("dm", nid))
            m.add_command(label="🛰 Traceroute", command=lambda: self.on_node_action("traceroute", nid))
            m.add_command(label="ℹ Details", command=lambda: self.on_node_action("details", nid))
            m.add_command(label="📋 In Liste zeigen", command=lambda: self.on_node_action("select", nid))
            m.add_separator()
        m.add_command(label=f"📋 Koordinaten kopieren ({lat:.5f}, {lon:.5f})",
                      command=lambda: (self.clipboard_clear(), self.clipboard_append(f"{lat:.6f}, {lon:.6f}")))
        m.add_command(label="📍 Als feste Position meines Knotens setzen",
                      command=lambda: self.on_set_position(lat, lon))
        m.add_command(label="⊕ Hier zentrieren", command=lambda: self.set_view(mx, my))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _opts_changed(self):
        for k, v in (("map_roads", self.v_roads), ("map_water", self.v_water), ("map_cities", self.v_cities),
                     ("map_admin", self.v_admin), ("map_labels", self.v_labels), ("map_old", self.v_old)):
            self.settings[k] = bool(v.get())
        self.save_settings(self.settings)
        self.redraw()

    # ------------------------------------------------------------------ Zeichnen
    def schedule_redraw(self, ms=80):
        if self._redraw_job:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(ms, self.redraw)

    def _proj(self, arr, W, ox, oy, lo, hi_x, hi_y, minstep):
        out = []
        lx = ly = -1e12
        it = iter(arr)
        for x, y in zip(it, it):
            sx = x * W - ox
            sy = y * W - oy
            if sx < lo:
                sx = lo
            elif sx > hi_x:
                sx = hi_x
            if sy < lo:
                sy = lo
            elif sy > hi_y:
                sy = hi_y
            if abs(sx - lx) + abs(sy - ly) >= minstep:
                out.append(sx)
                out.append(sy)
                lx, ly = sx, sy
        return out

    def _draw_layer(self, key, view, closed, fill, outline, width, minzoom_prop=None, dash=None, filt=None):
        if not self.vec:
            return
        W = self.W
        ox, oy, w, h = self.origin()
        vx0, vy0, vx1, vy1 = view
        pad = 400
        c = self.canvas
        for bbox, parts, props in self.vec.layers.get(key, []):
            if bbox[2] < vx0 or bbox[0] > vx1 or bbox[3] < vy0 or bbox[1] > vy1:
                continue
            if minzoom_prop is not None and props is not None:
                mz = props[0] if isinstance(props, list) else props
                if mz > self.z + minzoom_prop:
                    continue
            if filt and not filt(props):
                continue
            wd = width(props) if callable(width) else width
            col = outline(props) if callable(outline) else outline
            for a in parts:
                pts = self._proj(a, W, ox, oy, -pad, w + pad, h + pad, 1.2)
                if closed:
                    if len(pts) >= 6:
                        c.create_polygon(pts, fill=fill, outline=col, width=wd, tags=("map",))
                elif len(pts) >= 4:
                    c.create_line(pts, fill=col, width=wd, dash=dash, tags=("map",))

    def redraw(self):
        self._redraw_job = None
        c = self.canvas
        c.delete("all")
        self.settings.update(map_zoom=self.z, map_cx=self.cx, map_cy=self.cy)
        ox, oy, w, h = self.origin()
        W = self.W
        view = (ox / W, oy / W, (ox + w) / W, (oy + h) / W)
        z = self.z
        if not self.vec:
            c.create_text(w / 2, h / 2, text="Weltkarte wird geladen …", font=("Segoe UI", 14))
        else:
            land = "land0" if z <= 2 else ("land1" if z <= 4 else "land2")
            self._draw_layer(land, view, True, COL["land"], COL["border"], 1)
            if self.v_admin.get() and z >= 4:
                self._draw_layer("admin1", view, False, None, COL["admin1"], 1, dash=(4, 3))
            if self.v_water.get() and z >= 3:
                self._draw_layer("lakes", view, True, COL["lake"], COL["lake"], 1, minzoom_prop=1)
                if z >= 7:
                    self._draw_layer("lakes_eu", view, True, COL["lake"], COL["lake"], 1, minzoom_prop=0)
                self._draw_layer("rivers", view, False, None, COL["river"], lambda p: 2 if z >= 8 else 1,
                                 minzoom_prop=1)
                if z >= 7:
                    self._draw_layer("rivers_eu", view, False, None, COL["river"], 1, minzoom_prop=0)
            if self.v_roads.get() and z >= 5:
                self._draw_layer("roads", view, False, None,
                                 lambda p: COL["road_major"] if p[1] else COL["road"],
                                 lambda p: (3 if z >= 9 else 2) if p[1] else (2 if z >= 10 else 1),
                                 minzoom_prop=1)
            # Länderumrisse über Straßen nochmals dünn
            if z >= 5:
                self._draw_layer(land, view, True, "", COL["border"], 1)
        self._draw_tiles(view, w, h)
        if self.vec:
            self._draw_labels(view, w, h)
        self.draw_nodes()
        self._draw_scale(w, h)
        c.create_text(w - 4, h - 4, anchor="se", text=self._attrib(), font=("Segoe UI", 7), fill="#555",
                      tags=("hud",))

    def _attrib(self):
        s = self.v_src.get()
        if s == "Kachel-Cache" and "openstreetmap" in self.tile_url:
            return "© OpenStreetMap-Mitwirkende"
        return "Natural Earth"

    def _draw_labels(self, view, w, h):
        c = self.canvas
        z = self.z
        W = self.W
        ox, oy = view[0] * W, view[1] * W
        occupied = []

        def free(x, y, tw):
            box = (x - tw / 2, y - 8, x + tw / 2, y + 8)
            for b in occupied:
                if not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]):
                    return False
            occupied.append(box)
            return True

        if 2 <= z <= 7:
            for name, mx, my, mz in self.vec.labels:
                if mz > z + 0.5:
                    continue
                x, y = mx * W - ox, my * W - oy
                if 0 <= x <= w and 0 <= y <= h and free(x, y, len(name) * 7):
                    c.create_text(x, y, text=name, fill=COL["country"], font=("Segoe UI", 9, "bold"),
                                  tags=("map", "label"))
        if self.v_cities.get() and z >= 3:
            n = 0
            for name, mx, my, mz, pop in self.vec.cities:
                if mz > z + 1:
                    break
                x, y = mx * W - ox, my * W - oy
                if 0 <= x <= w and 0 <= y <= h and free(x, y - 8, len(name) * 6.5):
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=COL["city"], outline="", tags=("map",))
                    big = pop > 1_000_000
                    c.create_text(x, y - 9, text=name, fill=COL["city"],
                                  font=("Segoe UI", 9 if big else 8, "bold" if big else "normal"), tags=("map",))
                    n += 1
                    if n > 250:
                        break

    def _draw_scale(self, w, h):
        lat = inv_lat(self.cy)
        m_per_px = 40075016.686 * math.cos(math.radians(lat)) / self.W
        target = m_per_px * 120
        nice = 1
        for v in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6, 2e6, 5e6):
            if v <= target:
                nice = v
        px = nice / m_per_px
        x0, y0 = 12, h - 14
        c = self.canvas
        c.create_rectangle(x0 - 4, y0 - 18, x0 + px + 50, y0 + 6, fill="white", outline="", stipple="gray75",
                           tags=("hud",))
        c.create_line(x0, y0, x0 + px, y0, width=3, tags=("hud",))
        c.create_line(x0, y0 - 5, x0, y0 + 3, width=2, tags=("hud",))
        c.create_line(x0 + px, y0 - 5, x0 + px, y0 + 3, width=2, tags=("hud",))
        lbl = f"{int(nice / 1000)} km" if nice >= 1000 else f"{int(nice)} m"
        c.create_text(x0 + px + 6, y0 - 2, anchor="w", text=lbl, font=("Segoe UI", 8), tags=("hud",))

    # ------------------------------------------------------------------ Kacheln
    def _tile_source(self):
        s = self.v_src.get()
        if s == "MBTiles-Datei" and self.mbtiles:
            return "mb"
        if s == "Kachel-Cache":
            return "cache"
        return None

    def _draw_tiles(self, view, w, h):
        src = self._tile_source()
        if not src:
            return
        z = self.z
        if src == "mb" and not (self.mbtiles.minzoom <= z <= self.mbtiles.maxzoom):
            return
        n = 1 << z
        ox, oy, _, _ = self.origin()
        tx0, ty0 = int(max(0, ox // TILE)), int(max(0, oy // TILE))
        tx1, ty1 = int(min(n - 1, (ox + w) // TILE)), int(min(n - 1, (oy + h) // TILE))
        want = []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                key = (z, tx, ty)
                img = self.images.get(key)
                if img is not None:
                    self._place_tile(key, img, cached=True)
                    continue
                data = self.mbtiles.get(z, tx, ty) if src == "mb" else self.cache.get(self.tile_url, z, tx, ty)
                if data:
                    self._place_tile(key, data)
                elif src == "cache" and self.settings.get("map_online"):
                    want.append(key)
        for key in want:
            if key not in self.pending:
                self.pending.add(key)
                self.fetch_q.put(key)
        if want:
            self._start_workers()

    def _make_image(self, data):
        if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:6] in (b"GIF87a", b"GIF89a"):
            return tk.PhotoImage(data=base64.b64encode(data).decode())
        if HAS_PIL:
            return ImageTk.PhotoImage(Image.open(io.BytesIO(data)))
        raise ValueError("JPEG/WebP-Kacheln benötigen Pillow")

    def _place_tile(self, key, data_or_img, cached=False):
        z, tx, ty = key
        try:
            img = data_or_img if cached else self._make_image(data_or_img)
        except Exception:
            return
        self.images[key] = img
        if len(self.images) > 400:
            for k in list(self.images)[:100]:
                self.images.pop(k, None)
        ox, oy, _, _ = self.origin()
        self.canvas.create_image(tx * TILE - ox, ty * TILE - oy, image=img, anchor="nw", tags=("map", "tile"))
        self.canvas.tag_raise("tile", "map")
        for t in ("label", "nodes", "popup", "hud"):
            self.canvas.tag_raise(t)

    def _start_workers(self):
        if self._workers_started:
            return
        self._workers_started = True
        for _ in range(2):
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            key = self.fetch_q.get()
            z, x, y = key
            url = self.tile_url
            data = None
            try:
                if self.settings.get("map_online"):
                    data = fetch_tile(url, z, x, y)
                    self.cache.put(url, z, x, y, data)
            except Exception:
                data = None
            self.result_q.put(("tile", key, data))

    # ------------------------------------------------------------------ Knoten
    def node_positions(self):
        res = []
        now = time.time()
        for nid, n in (self.get_nodes() or {}).items():
            p = n.get("position") or {}
            lat, lon = p.get("latitude"), p.get("longitude")
            if lat is None and "latitudeI" in p:
                lat, lon = p["latitudeI"] * 1e-7, p.get("longitudeI", 0) * 1e-7
            if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
                continue
            age = now - (n.get("lastHeard") or 0) if n.get("lastHeard") else 1e9
            res.append((nid, n, lat, lon, age))
        return res

    def draw_nodes(self):
        c = self.canvas
        c.delete("nodes")
        me = self.my_id()
        show_old = self.v_old.get()
        labels = self.v_labels.get()
        w, h = c.winfo_width(), c.winfo_height()
        items = self.node_positions()
        items.sort(key=lambda t: -t[4])  # neueste oben
        for nid, n, lat, lon, age in items:
            if not show_old and age > 86400 and nid != me:
                continue
            x, y = self.to_screen(merc_x(lon), merc_y(lat))
            if x < -50 or y < -50 or x > w + 50 or y > h + 50:
                continue
            u = n.get("user") or {}
            if nid == me:
                col, r = "#1565c0", 9
            elif age < 7200:
                col, r = "#2e7d32", 7
            elif age < 86400:
                col, r = "#ef6c00", 7
            else:
                col, r = "#9e9e9e", 6
            tag = ("nodes", f"node:{nid}")
            c.create_oval(x - r - 2, y - r - 2, x + r + 2, y + r + 2, fill="white", outline="", tags=tag)
            c.create_oval(x - r, y - r, x + r, y + r, fill=col, outline="black", width=1, tags=tag)
            if n.get("isFavorite"):
                c.create_text(x + r + 2, y - r - 2, text="★", fill="#f9a825", font=("Segoe UI", 10, "bold"),
                              tags=tag)
            if labels:
                txt = u.get("shortName") or nid[-4:]
                c.create_text(x + 1, y + r + 9, text=txt, fill="white", font=("Segoe UI", 9, "bold"), tags=tag)
                c.create_text(x, y + r + 8, text=txt, fill="black", font=("Segoe UI", 9, "bold"), tags=tag)
        c.tag_raise("nodes")
        c.tag_raise("popup")
        c.tag_raise("hud")
        sel = getattr(self, "_popup_node", None)
        if sel and c.find_withtag("popup"):
            self.show_popup(sel)

    def show_popup(self, nid):
        c = self.canvas
        c.delete("popup")
        n = (self.get_nodes() or {}).get(nid)
        if not n:
            return
        self._popup_node = nid
        pos = [p for p in self.node_positions() if p[0] == nid]
        if not pos:
            return
        _, _, lat, lon, age = pos[0]
        x, y = self.to_screen(merc_x(lon), merc_y(lat))
        u = n.get("user") or {}
        dm = n.get("deviceMetrics") or {}
        lines = [f"{u.get('longName', nid)}  [{u.get('shortName', '')}]", f"{nid}   {u.get('hwModel', '')}"]
        if u.get("role"):
            lines.append(f"Rolle: {u.get('role')}")
        lines.append(f"Position: {lat:.5f}, {lon:.5f}" + (f"  ({(n.get('position') or {}).get('altitude')} m)"
                                                           if (n.get('position') or {}).get('altitude') else ""))
        extra = []
        if n.get("snr") is not None:
            extra.append(f"SNR {n['snr']} dB")
        if n.get("hopsAway") is not None:
            extra.append(f"Hops {n['hopsAway']}")
        if "batteryLevel" in dm:
            extra.append(f"Akku {dm['batteryLevel']}%" if dm["batteryLevel"] <= 100 else "Akku: extern")
        if extra:
            lines.append("  ·  ".join(extra))
        lines.append("Zuletzt gehört: " + (_ago(age) if age < 1e8 else "unbekannt"))
        me = self.my_id()
        if me and me != nid:
            mp = [p for p in self.node_positions() if p[0] == me]
            if mp:
                d = haversine(mp[0][2], mp[0][3], lat, lon)
                lines.append(f"Entfernung zu mir: {d / 1000:.2f} km" if d >= 1000 else f"Entfernung zu mir: {d:.0f} m")
        lines.append("Doppelklick = Nachricht · Rechtsklick = Menü")
        txt = "\n".join(lines)
        t = c.create_text(x + 14, y - 14, text=txt, anchor="sw", font=("Segoe UI", 9), tags=("popup",))
        bb = c.bbox(t)
        w = c.winfo_width()
        dx = min(0, w - 6 - bb[2]) if bb[2] > w - 6 else (6 - bb[0] if bb[0] < 6 else 0)
        dy = 6 - bb[1] if bb[1] < 6 else 0
        c.move(t, dx, dy)
        bb = c.bbox(t)
        r = c.create_rectangle(bb[0] - 6, bb[1] - 4, bb[2] + 6, bb[3] + 4, fill="white", outline="#555",
                               tags=("popup",))
        c.tag_raise(t, r)

    def fit_nodes(self):
        pts = [(merc_x(lon), merc_y(lat)) for _, _, lat, lon, age in self.node_positions()
               if self.v_old.get() or age <= 86400]
        if not pts:
            messagebox.showinfo("Karte", "Keine Knoten mit GPS-Position bekannt.")
            return
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        w, h = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        span = max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
        z = MIN_Z
        for zz in range(MIN_Z, min(self.max_zoom(), 14) + 1):
            Wz = TILE * (1 << zz)
            if span[0] * Wz <= w - 80 and span[1] * Wz <= h - 80:
                z = zz
        self.set_view((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, z)

    def center_me(self):
        self.center_node(self.my_id(), keep_zoom=False)

    def center_node(self, nid, keep_zoom=True):
        for pid, n, lat, lon, age in self.node_positions():
            if pid == nid:
                self.set_view(merc_x(lon), merc_y(lat), self.z if keep_zoom and self.z >= 10 else 12)
                self.show_popup(nid)
                return True
        messagebox.showinfo("Karte", "Für diesen Knoten ist keine GPS-Position bekannt.")
        return False

    # ------------------------------------------------------------------ Kachel-Einstellungen
    def _src_changed(self):
        s = self.v_src.get()
        if s == "MBTiles-Datei" and not self.mbtiles:
            if not self.choose_mbtiles():
                self.v_src.set("Vektor (eingebaut)")
        self.settings["map_src"] = self.v_src.get()
        self.save_settings(self.settings)
        self.images.clear()
        if self.z > self.max_zoom():
            self.z = self.max_zoom()
        self.redraw()

    def choose_mbtiles(self):
        fn = filedialog.askopenfilename(title="MBTiles-Datei wählen",
                                        filetypes=[("MBTiles", "*.mbtiles"), ("Alle Dateien", "*.*")])
        if not fn:
            return False
        try:
            self.mbtiles = MBTiles(fn)
        except Exception as e:
            messagebox.showerror("Karte", f"MBTiles-Datei kann nicht geöffnet werden:\n{e}")
            return False
        self.settings["map_mbtiles"] = fn
        self.save_settings(self.settings)
        self.images.clear()
        return True

    def tile_settings(self):
        win = tk.Toplevel(self)
        win.title("Karten-Hintergrund / Offline-Kacheln")
        win.geometry("640x470")
        f = ttk.Frame(win, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Die eingebaute Vektorkarte funktioniert immer offline. Für mehr Details (Straßen, Orte)"
                          " gibt es zwei weitere Möglichkeiten:", wraplength=600).grid(row=0, column=0, columnspan=3,
                                                                                    sticky="w")
        ttk.Label(f, text="1) MBTiles-Datei (komplett offline)", font=("Segoe UI", 10, "bold")).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(10, 2))
        mbl = ttk.Label(f, text=self.settings.get("map_mbtiles", "(keine gewählt)"), foreground="gray")
        mbl.grid(row=2, column=0, columnspan=2, sticky="w")

        def pick():
            if self.choose_mbtiles():
                mbl.configure(text=self.settings["map_mbtiles"])
                self.v_src.set("MBTiles-Datei")
                self._src_changed()
        ttk.Button(f, text="Datei wählen…", command=pick).grid(row=2, column=2, sticky="e")
        ttk.Label(f, text="Raster-MBTiles (PNG/JPG), z. B. selbst erstellt mit QGIS, MOBAC oder TileMill.",
                  foreground="gray", wraplength=600).grid(row=3, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="2) Kachel-Cache (einmal online laden, danach offline)", font=("Segoe UI", 10, "bold")).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(14, 2))
        ttk.Label(f, text="Kachel-URL:").grid(row=5, column=0, sticky="w")
        url = tk.StringVar(value=self.tile_url)
        ttk.Entry(f, textvariable=url).grid(row=5, column=1, columnspan=2, sticky="ew")
        online = tk.BooleanVar(value=bool(self.settings.get("map_online")))
        ttk.Checkbutton(f, text="Fehlende Kacheln beim Ansehen online laden und speichern (Internet nötig)",
                        variable=online).grid(row=6, column=0, columnspan=3, sticky="w", pady=2)
        n, size = self.cache.stats()
        st = ttk.Label(f, text=f"Cache: {n} Kacheln, {size / 1048576:.1f} MB")
        st.grid(row=7, column=0, columnspan=2, sticky="w")

        def clear():
            if messagebox.askyesno("Karte", "Kachel-Cache wirklich leeren?", parent=win):
                self.cache.clear()
                self.images.clear()
                st.configure(text="Cache: 0 Kacheln")
        ttk.Button(f, text="Cache leeren", command=clear).grid(row=7, column=2, sticky="e")

        ttk.Label(f, text="Aktuellen Kartenausschnitt für offline speichern bis Zoomstufe:").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        maxz = tk.IntVar(value=min(MAX_Z, max(self.z + 3, 12)))
        ttk.Spinbox(f, from_=self.z, to=MAX_Z, textvariable=maxz, width=5).grid(row=8, column=2, sticky="e",
                                                                                  pady=(10, 0))
        cnt = ttk.Label(f, text="", foreground="gray")
        cnt.grid(row=9, column=0, columnspan=3, sticky="w")

        def count(*a):
            try:
                cnt.configure(text=f"≈ {len(self._tiles_for_view(int(maxz.get())))} Kacheln "
                                   f"(max. {MAX_BULK_TILES} pro Vorgang)")
            except Exception:
                pass
        maxz.trace_add("write", count)
        count()
        ttk.Label(f, text="Bitte die Nutzungsbedingungen des Kachelservers beachten. Bei OpenStreetMap ist "
                          "Massen-Download untersagt – nur kleine Gebiete speichern, für große Gebiete "
                          "MBTiles verwenden.", foreground="#a33", wraplength=600).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=4)

        def apply():
            self.tile_url = url.get().strip() or DEFAULT_TILE_URL
            self.settings["map_tile_url"] = self.tile_url
            self.settings["map_online"] = bool(online.get())
            self.save_settings(self.settings)
            self.images.clear()
            self.redraw()

        def bulk():
            apply()
            tiles = self._tiles_for_view(int(maxz.get()))
            if len(tiles) > MAX_BULK_TILES:
                messagebox.showwarning("Karte", f"{len(tiles)} Kacheln sind zu viele (max. {MAX_BULK_TILES}). "
                                                "Bitte weiter hineinzoomen oder kleinere Zoomstufe wählen.",
                                       parent=win)
                return
            if not messagebox.askyesno("Karte", f"{len(tiles)} Kacheln herunterladen und offline speichern?",
                                       parent=win):
                return
            self._bulk_cancel = False
            threading.Thread(target=self._bulk_download, args=(tiles,), daemon=True).start()
            win.destroy()
        bb = ttk.Frame(f)
        bb.grid(row=11, column=0, columnspan=3, sticky="e", pady=10)
        ttk.Button(bb, text="⬇ Ausschnitt offline speichern", command=bulk).pack(side="left")
        ttk.Button(bb, text="Abbrechen (laufender Download)", command=lambda: setattr(self, "_bulk_cancel", True)).pack(
            side="left", padx=4)
        ttk.Button(bb, text="Übernehmen", command=lambda: (apply(), win.destroy())).pack(side="left")

    def _tiles_for_view(self, maxz):
        ox, oy, w, h = self.origin()
        W = self.W
        x0, y0, x1, y1 = ox / W, oy / W, (ox + w) / W, (oy + h) / W
        out = []
        for z in range(self.z, maxz + 1):
            n = 1 << z
            for ty in range(max(0, int(y0 * n)), min(n - 1, int(y1 * n)) + 1):
                for tx in range(max(0, int(x0 * n)), min(n - 1, int(x1 * n)) + 1):
                    out.append((z, tx, ty))
                    if len(out) > MAX_BULK_TILES * 4:
                        return out
        return out

    def _bulk_download(self, tiles):
        url = self.tile_url
        done = err = skip = 0
        for i, (z, x, y) in enumerate(tiles):
            if self._bulk_cancel:
                self.result_q.put(("bulk", f"Download abgebrochen ({done} geladen).", None))
                return
            if self.cache.has(url, z, x, y):
                skip += 1
                continue
            try:
                self.cache.put(url, z, x, y, fetch_tile(url, z, x, y))
                done += 1
            except Exception:
                err += 1
            if i % 10 == 0:
                self.result_q.put(("bulk", f"Offline-Kacheln: {i + 1}/{len(tiles)} …", None))
            time.sleep(0.05)
        self.result_q.put(("bulk", f"Fertig: {done} Kacheln gespeichert, {skip} vorhanden, {err} Fehler. "
                                   f"Hintergrund 'Kachel-Cache' wählen.", None))


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _ago(s):
    s = int(s)
    if s < 60:
        return f"vor {s} s"
    if s < 3600:
        return f"vor {s // 60} min"
    if s < 86400:
        return f"vor {s // 3600} h"
    return f"vor {s // 86400} Tagen"
