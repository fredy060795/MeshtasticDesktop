#!/usr/bin/env python3
"""
Macht aus dem Meshtastic-Firmware-Quellcode die TAKweb-Firmware für ESP32-Geräte:

* Das Gerät öffnet IMMER ein eigenes WLAN (Access Point): Name „TAKweb“, Passwort „12345678“
  (beim Bauen: TAKWEB_AP_SSID / TAKWEB_AP_PSK) – auch ganz ohne Internet die letzte Rückfallebene.
* Zusätzlich (gleichzeitig) verbindet es sich mit einem WLAN mit Internet, z. B. dem Handy-Hotspot: das ist die normale
  Meshtastic-WLAN-Einstellung (Netzwerk → WLAN-Name/Passwort, in TAKweb: Meshtastic → „Internet over WLAN“).
  Leer = nur das eigene WLAN. Mit Internet hat das Gerät Uhrzeit (NTP) und kann z. B. per MQTT ans Netz.
* ESP32-S3-Geräte (XIAO ESP32-S3, Heltec V3, T-Beam S3, T3S3): Bluetooth bleibt neben dem WLAN an – die
  Meshtastic-App oder ATAK verbinden sich weiter per Bluetooth (normale Meshtastic-Firmware schaltet es bei WLAN ab).
  Alte ESP32 (Heltec V2, T-Beam, T-LoRa) haben dafür zu wenig Speicher: dort bleibt es bei WLAN ohne Bluetooth.
* Adresse immer 192.168.4.1 – https://192.168.4.1 (Zertifikat des Geräts einmal akzeptieren). Ein kleiner
  Namensdienst (DNS) beantwortet „takweb“ (https://takweb/, https://takweb.lan) mit dieser Adresse, dazu mDNS
  https://takweb.local. Alle anderen Namen gibt es hier nicht: das Handy erkennt „WLAN ohne Internet“ und nutzt
  fürs Internet weiter seine mobilen Daten.
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
#include <esp_netif.h>
#define TAKWEB_AP_SSID %(ssid)s
#define TAKWEB_AP_PSK %(psk)s
static WiFiUDP takwebDns;
static int32_t takwebNoUpstream()
{
    return 600000; // kein Internet-WLAN eingestellt: nur das eigene WLAN
}
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
        // nur "takweb" (takweb, takweb.local, takweb.lan …) gehört uns – alle anderen Namen gibt es hier nicht.
        // So merkt das Handy "WLAN ohne Internet" und nimmt fürs Internet weiter seine mobilen Daten.
        static const char me[] = "takweb";
        bool ours = q[12] == 6;
        for (int k = 0; ours && k < 6; k++)
            ours = (q[13 + k] | 0x20) == me[k];
        bool isA = q[i + 1] == 0 && q[i + 2] == 1 && q[i + 3] == 0 && q[i + 4] == 1;
        uint8_t r[512 + 16];
        memcpy(r, q, qend);
        r[2] = 0x84 | (q[2] & 0x01); // Antwort, autoritativ, RD wie angefragt
        r[3] = ours ? 0x80 : 0x83;   // RA, NOERROR – fremde Namen: NXDOMAIN (gibt es nicht)
        isA = isA && ours;
        r[6] = 0;
        r[7] = isA ? 1 : 0; // nur A-Anfragen nach takweb bekommen die Adresse
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
        {
            // TAKweb: IMMER ein eigenes WLAN – und gleichzeitig, falls eingestellt, das WLAN mit Internet (Handy-Hotspot)
            const char *upName = config.network.wifi_ssid;
            bool upstream = upName[0] && strcmp(upName, TAKWEB_AP_SSID) != 0;
            // den Wiederverbinder VOR allem anderen anlegen: WLAN-Ereignisse greifen auf ihn zu
            wifiReconnect = new Periodic("WifiConnect", upstream ? reconnectWiFi : takwebNoUpstream);
            WiFi.persistent(false);
            WiFi.mode(upstream ? WIFI_AP_STA : WIFI_AP);
            WiFi.softAPConfig(IPAddress(%(ip)s), IPAddress(%(ip)s), IPAddress(255, 255, 255, 0));
            WiFi.softAP(TAKWEB_AP_SSID, strlen(TAKWEB_AP_PSK) >= 8 ? TAKWEB_AP_PSK : NULL);
            {
                // per DHCP KEINEN Router angeben: das Handy sieht ein reines Nahbereichs-WLAN und nimmt fürs
                // Internet (Karten, TAK-Server) weiter seine mobilen Daten – TAKweb kommt trotzdem von hier
                esp_netif_t *apif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
                if (apif) {
                    uint8_t offerRouter = 0;
                    esp_netif_dhcps_stop(apif);
                    esp_netif_dhcps_option(apif, ESP_NETIF_OP_SET, ESP_NETIF_ROUTER_SOLICITATION_ADDRESS, &offerRouter,
                                           sizeof(offerRouter));
                    esp_netif_dhcps_start(apif);
                }
            }
            WiFi.setSleep(false);
            esp_wifi_set_ps(WIFI_PS_NONE);
            if (upstream)
                WiFi.onEvent(WiFiEvent); // nur mit Internet-WLAN: Verbinden/Trennen/IP wie in Meshtastic
            LOG_INFO("TAKweb WLAN '%%s' offen: https://192.168.4.1 bzw. https://takweb.local", TAKWEB_AP_SSID);
#if !MESHTASTIC_EXCLUDE_WEBSERVER
            createSSLCert();
#endif
            takwebDns.begin(53);
            new Periodic("TakwebDns", takwebDnsLoop);
            if (upstream) {
                // Internet-WLAN (z. B. Handy-Hotspot); feste Adresse wie in Meshtastic einstellbar (iPhone: 172.20.10.14)
                if (config.network.address_mode == meshtastic_Config_NetworkConfig_AddressMode_STATIC &&
                    config.network.ipv4_config.ip != 0)
                    WiFi.config(config.network.ipv4_config.ip, config.network.ipv4_config.gateway,
                                config.network.ipv4_config.subnet, config.network.ipv4_config.dns);
                WiFi.setAutoReconnect(true);
                LOG_INFO("TAKweb: verbinde zusaetzlich mit dem Internet-WLAN '%%s'", upName);
            }
            onNetworkConnected();
            return true;
        }
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
    ssid, psk = os.environ.get("TAKWEB_AP_SSID", "TAKweb"), os.environ.get("TAKWEB_AP_PSK", "12345678")
    lit = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    s = sub_once(s, "bool initWifi()\n{", HELPERS % {"ssid": lit(ssid), "psk": lit(psk)} + "bool initWifi()\n{", "initWifi")
    # das eigene WLAN läuft auch ohne eingestelltes Internet-WLAN (leerer WLAN-Name)
    cond = "if (config.network.wifi_enabled && config.network.wifi_ssid[0]) {\n"
    i = s.index("bool initWifi()\n{")
    j = s.index(cond, i)
    if j - i > 200:
        sys.exit("Patch: Anfang von initWifi passt nicht.")
    s = s[:j] + "if (config.network.wifi_enabled) {\n" + AP_START + s[j + len(cond):]
    s = sub_once(s, "if (config.network.wifi_enabled && (config.network.wifi_ssid[0])) {",
                 "if (config.network.wifi_enabled) {", "isWifiAvailable")
    if 'MDNS.begin("Meshtastic")' not in s:
        sys.exit("Patch: mDNS-Start nicht gefunden.")
    s = s.replace('MDNS.begin("Meshtastic")', 'MDNS.begin("takweb")')
    s = s.replace('LOG_INFO("mDNS Host: Meshtastic.local");', 'LOG_INFO("mDNS Host: takweb.local");')
    open(p, "w", encoding="utf-8").write(s)
    print(f"WiFiAPClient.cpp: eigenes WLAN „{ssid}“ + Internet-WLAN + DNS eingebaut")


def patch_ble(fw):
    """Bluetooth neben dem WLAN behalten – nur auf ESP32-S3 (genug Speicher)."""
    p = os.path.join(fw, "src", "platform", "esp32", "main-esp32.cpp")
    s = open(p, encoding="utf-8").read()
    if "TAKWEB_BLE_WITH_WIFI" in s:
        return
    head = ("\n// TAKweb: auf ESP32-S3 bleibt Bluetooth neben dem WLAN an (genug Speicher), alte ESP32: wie Meshtastic\n"
            "#if defined(CONFIG_IDF_TARGET_ESP32S3)\n#define TAKWEB_BLE_WITH_WIFI 1\n#else\n#define TAKWEB_BLE_WITH_WIFI 0\n#endif\n")
    # nach dem letzten #include am Dateianfang einfügen
    lines = s.split("\n")
    last = max(i for i, l in enumerate(lines[:80]) if l.startswith("#include"))
    lines.insert(last + 1, head)
    s = "\n".join(lines)
    s = sub_once(s, "if (!isWifiAvailable() && config.bluetooth.enabled == true)",
                 "if ((TAKWEB_BLE_WITH_WIFI || !isWifiAvailable()) && config.bluetooth.enabled == true)", "Bluetooth bei WLAN")
    # neuere Firmware: eigene Abfrage, ob das Netzwerk Bluetooth abschaltet (gibt sonst den BT-Speicher frei)
    s = s.replace("static bool isNetworkConfiguredToDisableBluetooth()\n{\n#if HAS_WIFI\n    return isWifiAvailable();",
                  "static bool isNetworkConfiguredToDisableBluetooth()\n{\n#if HAS_WIFI\n    return !TAKWEB_BLE_WITH_WIFI && isWifiAvailable();")
    open(p, "w", encoding="utf-8").write(s)
    print("main-esp32.cpp: Bluetooth bleibt auf ESP32-S3 neben dem WLAN an")


def patch_prefs(fw, region="EU_868"):
    p = os.path.join(fw, "userPrefs.jsonc")
    prefs = json.loads(re.sub("//.*", "", open(p, encoding="utf-8").read(), flags=re.MULTILINE))
    prefs["USERPREFS_NETWORK_WIFI_ENABLED"] = "true"  # WLAN an (eigenes WLAN; ein Internet-WLAN ist anfangs leer)
    # LoRa-Region vorbelegt: ohne Region sendet Meshtastic gar nicht – und mit WLAN an (Bluetooth aus) fragt keine App
    if region and re.fullmatch(r"[A-Z0-9_]+", region):
        prefs["USERPREFS_CONFIG_LORA_REGION"] = f"meshtastic_Config_LoRaConfig_RegionCode_{region}"
    json.dump(prefs, open(p, "w", encoding="utf-8"), indent=2)
    print(f"userPrefs.jsonc: WLAN an, LoRa-Region {region or '(nicht gesetzt)'}")


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
    patch_ble(fw)
    patch_prefs(fw, region=os.environ.get("TAKWEB_REGION", "EU_868"))
    if len(sys.argv) > 2:
        add_takweb(fw, sys.argv[2])
