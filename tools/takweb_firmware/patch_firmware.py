#!/usr/bin/env python3
"""
Macht aus dem Meshtastic-Firmware-Quellcode die TAKweb-Firmware für ESP32-Geräte:

* Das Gerät öffnet ein eigenes WLAN (Access Point) statt sich mit einem zu verbinden.
  Vorgabe: Name „TAKweb“, Passwort „12345678“ (änderbar wie gewohnt: Netzwerk → WLAN-Name/Passwort).
* Adresse immer 192.168.4.1. Ein eigener Namensdienst (DNS) beantwortet JEDEN Namen mit dieser Adresse:
  http://takweb/, http://takweb.local, http://192.168.4.1 – alles landet bei TAKweb. Handys zeigen nach dem
  Verbinden meist von selbst die Anmeldeseite (= TAKweb) an.
* mDNS-Name „takweb“ statt „Meshtastic“.
* Die TAKweb-Dateien kommen in data/static (→ LittleFS-Abbild): Firmware flashen = TAKweb ist drauf.

Aufruf:  python patch_firmware.py <firmware-ordner> [<ordner mit TAKweb-*.gz>]
"""
import json
import os
import re
import shutil
import sys

AP_IP = "192, 168, 4, 1"

HELPERS = r'''
// ---- TAKweb: eigenes WLAN (Access Point) + kleiner Namensdienst, der jeden Namen mit dem Gerät beantwortet ----
// (eigener DNS-Responder: die Arduino-Bibliothek DNSServer ist in der Firmware abgeschaltet)
#ifdef ARCH_ESP32
#include <WiFiUdp.h>
static WiFiUDP takwebDns;
static int32_t takwebDnsLoop()
{
    uint8_t q[512];
    for (int n = 0; n < 8; n++) { // mehrere Anfragen pro Durchlauf
        int len = takwebDns.parsePacket();
        if (len <= 0)
            break;
        if (len > (int)sizeof(q)) {
            takwebDns.flush();
            continue;
        }
        len = takwebDns.read(q, sizeof(q));
        // nur normale Anfragen mit genau einer Frage
        if (len < 17 || (q[2] & 0x80) || ((q[2] >> 3) & 0x0f) != 0 || q[4] != 0 || q[5] != 1)
            continue;
        int i = 12;
        while (i < len && q[i] != 0) {
            if (q[i] & 0xc0) { // keine Zeiger in Fragen
                i = len;
                break;
            }
            i += q[i] + 1;
        }
        if (i + 5 > len)
            continue;
        int qend = i + 5; // Name-Ende (0) + Typ (2) + Klasse (2)
        bool isA = q[i + 1] == 0 && q[i + 2] == 1 && q[i + 3] == 0 && q[i + 4] == 1;
        uint8_t r[512 + 16];
        memcpy(r, q, qend);
        r[2] = 0x84 | (q[2] & 0x01); // Antwort, autoritativ, RD wie angefragt
        r[3] = 0x80;                 // RA, NOERROR
        r[6] = 0;
        r[7] = isA ? 1 : 0; // nur A-Anfragen bekommen die Adresse, sonst leere Antwort (Handy fragt dann A)
        r[8] = r[9] = r[10] = r[11] = 0;
        int o = qend;
        if (isA) {
            const uint8_t a[] = {0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 192, 168, 4, 1};
            memcpy(r + o, a, sizeof(a));
            o += sizeof(a);
        }
        takwebDns.beginPacket(takwebDns.remoteIP(), takwebDns.remotePort());
        takwebDns.write(r, o);
        takwebDns.endPacket();
    }
    return 20;
}
#endif

'''

AP_START = r'''
#ifdef ARCH_ESP32
            // TAKweb: das Gerät öffnet ein eigenes WLAN, statt sich mit einem zu verbinden
            WiFi.mode(WIFI_AP);
            WiFi.softAPConfig(IPAddress(%(ip)s), IPAddress(%(ip)s), IPAddress(255, 255, 255, 0));
            WiFi.softAP(wifiName, (wifiPsw && strlen(wifiPsw) >= 8) ? wifiPsw : NULL);
            WiFi.setSleep(false);
            esp_wifi_set_ps(WIFI_PS_NONE);
            LOG_INFO("TAKweb WLAN '%%s' offen: http://192.168.4.1 (jeder Name fuehrt hierher)", wifiName);
#if !MESHTASTIC_EXCLUDE_WEBSERVER
            createSSLCert();
#endif
            takwebDns.begin(53);
            new Periodic("TakwebDns", takwebDnsLoop);
            onNetworkConnected();
            return true;
#endif
''' % {"ip": AP_IP}


def sub_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        sys.exit(f"Patch: Stelle für {what} {n}× gefunden (erwartet 1) – Firmware-Version passt nicht zum Patch.")
    return text.replace(old, new)


def patch_wifi(fw):
    p = os.path.join(fw, "src", "mesh", "wifi", "WiFiAPClient.cpp")
    s = open(p, encoding="utf-8").read()
    if "takwebDns" in s:
        print("WiFiAPClient.cpp: schon gepatcht")
        return
    s = sub_once(s, "bool initWifi()\n{", HELPERS + "bool initWifi()\n{", "initWifi")
    # im ersten "if (*wifiName) {" nach initWifi den Access Point starten
    i = s.index("bool initWifi()\n{")
    m = re.compile(r"\n(\s*)if \(\*wifiName\) \{\n").search(s, i)
    if not m:
        sys.exit("Patch: 'if (*wifiName)' in initWifi nicht gefunden.")
    s = s[:m.end()] + AP_START + s[m.end():]
    if 'MDNS.begin("Meshtastic")' not in s:
        sys.exit("Patch: mDNS-Start nicht gefunden.")
    s = s.replace('MDNS.begin("Meshtastic")', 'MDNS.begin("takweb")')
    s = s.replace('LOG_INFO("mDNS Host: Meshtastic.local");', 'LOG_INFO("mDNS Host: takweb.local");')
    open(p, "w", encoding="utf-8").write(s)
    print("WiFiAPClient.cpp: Access Point + DNS eingebaut")


def patch_prefs(fw, ssid="TAKweb", psk="12345678"):
    p = os.path.join(fw, "userPrefs.jsonc")
    prefs = json.loads(re.sub("//.*", "", open(p, encoding="utf-8").read(), flags=re.MULTILINE))
    # Achtung: userPrefs macht aus reinen Ziffern ("12345678") eine Zahl statt Text – das Passwort setzen wir
    # deshalb direkt in NodeDB.cpp (der Eintrag hier sorgt nur dafür, dass die Stelle übersetzt wird)
    prefs.update({"USERPREFS_NETWORK_WIFI_ENABLED": "true", "USERPREFS_NETWORK_WIFI_SSID": ssid,
                  "USERPREFS_NETWORK_WIFI_PSK": "takweb-psk"})
    json.dump(prefs, open(p, "w", encoding="utf-8"), indent=2)
    n = os.path.join(fw, "src", "mesh", "NodeDB.cpp")
    s = open(n, encoding="utf-8").read()
    lit = '"' + psk.replace("\\", "\\\\").replace('"', '\\"') + '"'
    s = sub_once(s, "strncpy(config.network.wifi_psk, USERPREFS_NETWORK_WIFI_PSK,",
                 f"strncpy(config.network.wifi_psk, {lit},", "WLAN-Passwort (NodeDB.cpp)")
    open(n, "w", encoding="utf-8").write(s)
    print(f"userPrefs.jsonc: WLAN an, „{ssid}“ / „{psk}“")


def add_takweb(fw, src):
    dst = os.path.join(fw, "data", "static")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    n = 0
    for fn in sorted(os.listdir(src)):
        if fn.endswith(".gz"):
            shutil.copyfile(os.path.join(src, fn), os.path.join(dst, fn))
            n += 1
    if not os.path.exists(os.path.join(dst, "index.html.gz")):
        sys.exit("TAKweb: index.html.gz fehlt")
    print(f"data/static: {n} TAKweb-Dateien")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    fw = sys.argv[1]
    patch_wifi(fw)
    patch_prefs(fw)
    if len(sys.argv) > 2:
        add_takweb(fw, sys.argv[2])
