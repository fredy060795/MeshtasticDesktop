"""Erzeugt worldmap.json.gz (kompakte Offline-Weltkarte) aus Natural-Earth-GeoJSON (Public Domain)."""
import gzip, json, sys

S = 10000  # Koordinaten als int (1e-4 Grad ~ 11 m)


def dp(pts, tol):
    """Douglas-Peucker (iterativ)."""
    n = len(pts)
    if n < 3 or tol <= 0:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / L
                t = max(0, min(1, t))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if best > tol * tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def rings(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return c
    if t == "MultiPolygon":
        return [r for poly in c for r in poly]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def enc(ring, tol, closed):
    r = dp([(p[0], p[1]) for p in ring], tol)
    if len(r) < (4 if closed else 2):
        return None
    out = []
    for x, y in r:
        out += [round(x * S), round(max(-85, min(85, y)) * S)]
    return out


def delta(flat):
    d = flat[:2]
    for i in range(2, len(flat)):
        d.append(flat[i] - flat[i - 2])
    return d


def layer(fn, tol, closed, props=lambda p: None, flt=lambda p: True):
    d = json.load(open(fn + ".geojson"))
    feats = []
    for f in d["features"]:
        if not f.get("geometry") or not flt(f["properties"]):
            continue
        parts = [e for e in (enc(r, tol, closed) for r in rings(f["geometry"])) if e]
        if not parts:
            continue
        xs = [v for p in parts for v in p[0::2]]
        ys = [v for p in parts for v in p[1::2]]
        feats.append([[min(xs), min(ys), max(xs), max(ys)], [delta(p) for p in parts], props(f["properties"])])
    print(fn, len(feats), sum(len(p) for f in feats for p in f[1]) // 2, "Punkte", file=sys.stderr)
    return feats


def mz(p, default=0):
    v = p.get("min_zoom")
    return float(v) if v not in (None, "") else default


out = {
    "format": 2, "scale": S, "attribution": "Kartendaten: Natural Earth (public domain)",
    "land0": layer("ne_110m_admin_0_countries", 0.05, True),
    "land1": layer("ne_50m_admin_0_countries", 0.02, True),
    "land2": layer("ne_10m_admin_0_countries", 0.0015, True),
    "admin1": layer("ne_10m_admin_1_states_provinces_lines", 0.003, False),
    "lakes": layer("ne_10m_lakes", 0.002, True, lambda p: mz(p, 3)),
    "lakes_eu": layer("ne_10m_lakes_europe", 0.0015, True, lambda p: 7),
    "rivers": layer("ne_10m_rivers_lake_centerlines", 0.003, False, lambda p: mz(p, 4)),
    "rivers_eu": layer("ne_10m_rivers_europe", 0.002, False, lambda p: 7),
    "roads": layer("ne_10m_roads", 0.002, False,
                   lambda p: [mz(p, 6), 1 if p.get("type") in ("Major Highway", "Beltway") or p.get("expressway") == 1 else 0],
                   flt=lambda p: "Ferry" not in (p.get("type") or "")),
}
c = json.load(open("ne_10m_admin_0_countries.geojson"))
out["labels"] = [[p.get("NAME_DE") or p["NAME"], round(p["LABEL_X"], 3), round(p["LABEL_Y"], 3),
                  float(p.get("MIN_LABEL") or 3)] for p in (f["properties"] for f in c["features"])]
pp = json.load(open("ne_10m_populated_places_simple.geojson"))
out["cities"] = sorted([[p["name"], round(p["longitude"], 4), round(p["latitude"], 4), mz(p, 7),
                         int(p.get("pop_max") or 0)] for p in (f["properties"] for f in pp["features"])],
                       key=lambda c: (c[3], -c[4]))
data = json.dumps(out, separators=(",", ":")).encode()
with gzip.open(sys.argv[1], "wb", 9) as f:
    f.write(data)
print("roh", len(data) // 1024, "KB", file=sys.stderr)
