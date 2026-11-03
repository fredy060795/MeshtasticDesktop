#!/usr/bin/env python3
"""
Lädt aktuelle Meshtastic-Firmware-Versionen herunter und packt sie als Offline-Pakete
(eine ZIP-Datei je Chip-Familie) für den Meshtastic-Desktop-Flasher.

Quelle: https://github.com/meshtastic/meshtastic.github.io (dieselben Dateien, die auch
flasher.meshtastic.org verwendet). Welche Versionen stabil/Alpha sind, kommt aus den
GitHub-Releases von meshtastic/firmware.

Aufruf (läuft im GitHub-Workflow .github/workflows/firmware-offline.yml):
    python tools/fetch_firmware.py --out out [--versions 2.8.2.4c89b69,...] [--work work]
Ausgabe:
    out/<version>/meshtastic-fw-<version>-<chip>.zip
    out/<version>/index.json
    out/<version>/release.md
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

IO_REPO = "https://github.com/meshtastic/meshtastic.github.io"
API = "https://api.github.com/repos/meshtastic/firmware/releases?per_page=40"


def gh_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "MeshtasticDesktop-firmware-fetch"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def run(*cmd, cwd=None):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def io_folders(work):
    """Liste der firmware-*-Ordner in meshtastic.github.io (ohne Dateiinhalte zu laden)."""
    repo = os.path.join(work, "io")
    if not os.path.isdir(os.path.join(repo, ".git")):
        run("git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--sparse", IO_REPO, repo)
    out = subprocess.run(["git", "ls-tree", "--name-only", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.split()
    return repo, [f for f in out if f.startswith("firmware-")]


def pick_versions(folders):
    """Neueste stabile/Beta-Version und neueste Alpha – soweit im Web-Flasher-Repo vorhanden."""
    avail = {f[len("firmware-"):] for f in folders}
    chosen = []
    try:
        rels = gh_json(API)
    except Exception as e:  # API nicht erreichbar -> einfach die neueste Ordner-Version
        print("Release-API nicht erreichbar:", e)
        rels = []
    for want_pre in (False, True):
        for r in rels:
            if r.get("draft") or bool(r.get("prerelease")) != want_pre:
                continue
            v = r["tag_name"].lstrip("v")
            if v in avail:
                name = r.get("name") or ""
                ch = "Alpha" if "alpha" in name.lower() else ("Beta" if "beta" in name.lower() else
                                                               ("Vorabversion" if want_pre else "Stabil"))
                if v not in [c[0] for c in chosen]:
                    chosen.append((v, ch, name))
                break
    if not chosen:
        def key(v):
            return [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", v)[:3]]
        v = sorted(avail, key=key)[-1]
        chosen.append((v, "Neueste", ""))
    return chosen


def checkout(repo, folder, only=None):
    with open(os.path.join(repo, ".git", "info", "sparse-checkout"), "w") as f:
        if only:  # nur bestimmte Geräte (zum Testen / kleine Pakete)
            for t in only:
                f.write(f"/{folder}/*{t}*\n")
            f.write(f"/{folder}/mt-*\n/{folder}/*.md\n/{folder}/*factory_erase*\n")
        else:
            f.write(f"/{folder}/\n")
        f.write(f"!/{folder}/*.elf\n")
    run("git", "config", "core.sparseCheckout", "true", cwd=repo)
    run("git", "config", "core.sparseCheckoutCone", "false", cwd=repo)
    run("git", "read-tree", "-mu", "HEAD", cwd=repo)
    return os.path.join(repo, folder)


def chip_of_extra(name):
    n = name.lower()
    for chip in ("esp32s3", "esp32c3", "esp32c6", "esp32s2", "esp32"):
        if f"mt-{chip}-ota" in n:
            return chip
    if "nrf52" in n:
        return "nrf52840"
    return None


def pack(src, version, channel, title, out):
    dest = os.path.join(out, version)
    os.makedirs(dest, exist_ok=True)
    manifests = {}
    for fn in sorted(os.listdir(src)):
        if fn.endswith(".mt.json"):
            with open(os.path.join(src, fn), encoding="utf-8") as f:
                manifests[fn] = json.load(f)
    groups = {}            # chip -> set(dateinamen)
    devices = []
    for fn, m in manifests.items():
        chip = (m.get("mcu") or m.get("architecture") or "other").replace("-", "").lower()
        files = [x["name"] for x in m.get("files", []) if not x["name"].endswith(".elf")
                 and os.path.exists(os.path.join(src, x["name"]))]
        g = groups.setdefault(chip, set())
        g.add(fn)
        g.update(files)
        devices.append({
            "target": m.get("platformioTarget"), "displayName": m.get("displayName"),
            "hwModelSlug": m.get("hwModelSlug"), "mcu": chip, "architecture": m.get("architecture"),
            "supportLevel": m.get("supportLevel"), "activelySupported": m.get("activelySupported"),
            "requiresDfu": m.get("requiresDfu", False), "manifest": fn, "files": files,
            "bytes": sum(os.path.getsize(os.path.join(src, x)) for x in files),
        })
    for fn in os.listdir(src):
        chip = chip_of_extra(fn)
        if chip and chip in groups:
            groups[chip].add(fn)
    extras = [fn for fn in os.listdir(src) if fn.endswith((".md", ".sh", ".bat"))]
    meta = {"version": version, "channel": channel, "title": title,
            "source": IO_REPO + "/tree/master/firmware-" + version}
    assets = []
    for chip, names in sorted(groups.items()):
        zname = f"meshtastic-fw-{version}-{chip}.zip"
        zpath = os.path.join(dest, zname)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("meshdesk-firmware.json", json.dumps(dict(meta, chip=chip), indent=2))
            for n in sorted(names | set(extras)):
                z.write(os.path.join(src, n), n)
        assets.append({"name": zname, "chip": chip, "bytes": os.path.getsize(zpath),
                       "devices": sum(1 for d in devices if d["mcu"] == chip)})
        print(f"{zname}: {assets[-1]['bytes'] // 1048576} MB, {assets[-1]['devices']} Geräte", flush=True)
    index = dict(meta, assets=assets, devices=sorted(devices, key=lambda d: (d["displayName"] or "").lower()))
    with open(os.path.join(dest, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, ensure_ascii=False)
    notes = ""
    rn = os.path.join(src, "release_notes.md")
    if os.path.exists(rn):
        notes = open(rn, encoding="utf-8").read()
    with open(os.path.join(dest, "release.md"), "w", encoding="utf-8") as f:
        f.write(f"Offline-Firmware-Paket für den Flasher in **Meshtastic Desktop**.\n\n"
                f"- Version: `{version}` ({channel})\n- Geräte: {len(devices)}\n"
                f"- Quelle: {meta['source']}\n\n"
                "Im Flasher: **Paket importieren…** und die ZIP-Datei(en) für deine Chip-Familie wählen "
                "(z. B. `esp32s3` für Heltec V3/T-Beam S3, `esp32` für T-Beam/ältere Boards, "
                "`nrf52840` für RAK4631/T-Echo, `rp2040` für Pico-Boards).\n\n---\n\n" + notes[:60000])
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--work", default="work")
    ap.add_argument("--versions", default="", help="kommagetrennt, leer = automatisch")
    ap.add_argument("--only", default="", help="nur diese Geräte-Targets (kommagetrennt, Test)")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    repo, folders = io_folders(a.work)
    if a.versions.strip():
        chosen = [(v.strip().lstrip("v"), "Manuell", "") for v in a.versions.split(",") if v.strip()]
    else:
        chosen = pick_versions(folders)
    print("Versionen:", chosen, flush=True)
    done = []
    for v, ch, title in chosen:
        folder = f"firmware-{v}"
        if folder not in folders:
            print("Nicht vorhanden:", folder)
            continue
        src = checkout(repo, folder, [t.strip() for t in a.only.split(",") if t.strip()] or None)
        pack(src, v, ch, title, a.out)
        done.append(v)
        shutil.rmtree(src, ignore_errors=True)
    with open(os.path.join(a.out, "versions.txt"), "w") as f:
        f.write("\n".join(done))
    if not done:
        sys.exit("Keine Firmware-Version verarbeitet")


if __name__ == "__main__":
    main()
