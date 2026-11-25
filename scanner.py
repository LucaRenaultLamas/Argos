"""
Varredura de rede FORTE para encontrar câmeras de qualquer tipo, no próprio PC.
Combina:
- WS-Discovery (ONVIF) por multicast
- Descoberta de TODOS os dispositivos da rede via ARP (poke UDP + arp -a)
- Varredura de muitas portas típicas de câmera
- Confirmação ativa de RTSP (OPTIONS) e ONVIF (GetSystemDateAndTime)
- Leitura de banner/título das interfaces web
- Identificação de fabricante pelo MAC (OUI)
Roda em todas as sub-redes /24 em que o PC está.
"""
import socket
import uuid
import re
import time
import ipaddress
import subprocess
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:
    requests = None

# Portas típicas de câmera (RTSP, web, vendor-specific)
CAMERA_PORTS = [80, 81, 82, 88, 443, 554, 555, 654, 1935, 2020, 3702, 5000, 5543,
                5544, 6036, 6668, 6688, 7001, 7070, 7080, 8000, 8001, 8080, 8081,
                8090, 8181, 8443, 8554, 8555, 8600, 8880, 8888, 8899, 8999, 9000,
                9988, 10554, 34567, 37777, 37779, 49152, 52869, 55555]
RTSP_PORTS = [554, 5544, 8554, 10554, 7070, 1935, 555, 5543, 8555]
ONVIF_PORTS = [80, 8080, 8000, 8081, 2020, 8899, 6688, 5000, 8181, 8999, 8888]
WEB_PORTS = [80, 81, 82, 88, 443, 8000, 8080, 8081, 8090, 8181, 8443, 8888, 9000]

# Prefixos de MAC (OUI) de fabricantes de câmera conhecidos
CAMERA_OUIS = {
    "bc-ad-28": "Hikvision", "c0-56-e3": "Hikvision", "4c-bd-8f": "Hikvision",
    "44-19-b6": "Hikvision", "28-57-be": "Hikvision", "a4-14-37": "Hikvision",
    "54-c4-15": "Hikvision", "e0-ca-3c": "Hikvision", "c8-5b-76": "Hikvision",
    "18-80-25": "Hikvision",
    "3c-ef-8c": "Dahua", "90-02-a9": "Dahua", "e0-50-8b": "Dahua",
    "14-a7-8b": "Dahua", "a0-bd-1d": "Dahua", "08-ed-ed": "Dahua", "bc-32-5f": "Dahua",
    "ec-71-db": "Reolink", "00-40-8c": "Axis", "ac-cc-8e": "Axis", "b8-a4-4f": "Axis",
    "00-62-6e": "Foscam", "00-e0-f8": "Sony", "00-1a-07": "Grandstream",
    "78-11-dc": "Xiaomi", "64-09-80": "Xiaomi", "28-6c-07": "Xiaomi",
    "2c-aa-8e": "Wyze", "7c-78-b2": "Wyze",
    "18-9e-2d": "Câmera genérica (Huaxinwei)", "00-f6-20": "Câmera IP",
    "a4-77-33": "Google/Nest", "f4-f5-d8": "Google/Nest",
    "00-12-fb": "Samsung/Hanwha", "e4-30-22": "Hanwha",
    "a0-c5-89": "Intelbras", "40-2c-76": "Intelbras", "48-a9-8a": "Intelbras",
}


def local_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _is_private(ip):
    try:
        p = ipaddress.ip_address(ip)
        return p.is_private and not p.is_loopback and not p.is_link_local
    except Exception:
        return False


def local_subnets():
    subs = set()
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    for m in re.finditer(r"IPv4[^\n:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out):
        ip = m.group(1)
        if _is_private(ip):
            subs.add(".".join(ip.split(".")[:3]))
    if not subs:
        ip = local_ipv4()
        if ip != "127.0.0.1":
            subs.add(".".join(ip.split(".")[:3]))
    return sorted(subs)[:4]


def ws_discovery(timeout=4.0):
    """ONVIF WS-Discovery: envia 2 probes (câmera e dispositivo) e coleta respostas."""
    found = {}
    probes = []
    for types in ("dn:NetworkVideoTransmitter", "dn:Device tds:Device"):
        mid = uuid.uuid4()
        probes.append((
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
            "<e:Header><w:MessageID>uuid:%s</w:MessageID>"
            '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
            '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
            "</e:Header><e:Body><d:Probe><d:Types>%s</d:Types></d:Probe></e:Body></e:Envelope>"
        ) % (mid, types))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        local = local_ipv4()
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        except Exception:
            pass
        sock.bind((local, 0))
        sock.settimeout(timeout)
        for p in probes:
            try:
                sock.sendto(p.encode("utf-8"), ("239.255.255.250", 3702))
            except Exception:
                pass
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            txt = data.decode("utf-8", errors="ignore")
            ip = addr[0]
            xaddr = ""
            m = re.search(r"<[^>]*XAddrs>([^<]+)<", txt)
            if m:
                urls = m.group(1).split()
                xaddr = next((u for u in urls if ip in u), urls[0] if urls else "")
            scopes = ""
            ms = re.search(r"<[^>]*Scopes>([^<]+)<", txt)
            if ms:
                scopes = ms.group(1)
            name = ""
            mn = re.search(r"onvif://www\.onvif\.org/name/([^\s]+)", scopes)
            if mn:
                name = mn.group(1).replace("%20", " ")
            found[ip] = {
                "onvif_url": xaddr,
                "name": name or "Câmera ONVIF",
                "has_ptz": "/type/ptz" in scopes,
            }
    finally:
        sock.close()
    return found


def _test_port(ip, port, ms=500):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ms / 1000.0)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _udp_poke(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.sendto(b"\x00", (ip, 9))
        s.close()
    except Exception:
        pass


def arp_table(subnets):
    """Lê a tabela ARP e retorna { ip: mac } para as sub-redes informadas."""
    result = {}
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return result
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", out):
        ip, mac = m.group(1), m.group(2).lower().replace(":", "-")
        if mac == "ff-ff-ff-ff-ff-ff":
            continue
        if any(ip.startswith(b + ".") for b in subnets):
            result[ip] = mac
    return result


def _rtsp_options(ip, port, timeout=2.0):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.settimeout(timeout)
        req = ("OPTIONS rtsp://%s:%d/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: scan\r\n\r\n" % (ip, port))
        s.sendall(req.encode())
        data = s.recv(1024).decode("latin1", errors="ignore")
        s.close()
        if data.startswith("RTSP"):
            sv = re.search(r"(?im)^Server:\s*(.+)$", data)
            return sv.group(1).strip() if sv else "RTSP"
    except Exception:
        return None
    return None


def _http_probe(ip, port, timeout=2.0):
    if requests is None:
        return None
    scheme = "https" if port in (443, 8443) else "http"
    try:
        r = requests.get("%s://%s:%d/" % (scheme, ip, port), timeout=timeout,
                         verify=False, allow_redirects=False)
        server = r.headers.get("Server", "")
        wa = r.headers.get("WWW-Authenticate", "")
        title = ""
        tm = re.search(r"<title>(.*?)</title>", r.text or "", re.I | re.S)
        if tm:
            title = tm.group(1).strip()[:60]
        return {"server": server, "title": title, "auth": wa}
    except Exception:
        return None


def _onvif_probe(ip, port, timeout=2.0):
    if requests is None:
        return None
    url = "http://%s:%d/onvif/device_service" % (ip, port)
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            '<s:Body><GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
            '</s:Body></s:Envelope>')
    try:
        r = requests.post(url, data=body, headers={"Content-Type": "application/soap+xml"},
                          timeout=timeout)
        if "SystemDateAndTime" in r.text or "onvif" in r.text.lower():
            return url
    except Exception:
        return None
    return None


def _looks_like_camera(text):
    t = (text or "").lower()
    kws = ["ipcam", "ip camera", "netcam", "webcam", "nvr", "dvr", "hikvision",
           "dahua", "reolink", "axis", "onvif", "rtsp", "camera", "surveillance",
           "foscam", "amcrest", "ezviz", "tapo", "intelbras", "goahead", "boa/"]
    return any(x in t for x in kws)


def _oui_vendor(mac):
    if not mac:
        return ""
    return CAMERA_OUIS.get(mac[:8], "")


def _probe_host(ip, mac, onvif_disc):
    open_ports = [p for p in CAMERA_PORTS if _test_port(ip, p)]
    rtsp_ports, rtsp_server = [], ""
    for p in open_ports:
        if p in RTSP_PORTS:
            sv = _rtsp_options(ip, p)
            if sv:
                rtsp_ports.append(p)
                rtsp_server = rtsp_server or sv

    onvif_url = onvif_disc.get(ip, {}).get("onvif_url", "")
    name = onvif_disc.get(ip, {}).get("name", "")
    has_ptz = onvif_disc.get(ip, {}).get("has_ptz", False)
    onvif = ip in onvif_disc
    if not onvif_url:
        for p in [pp for pp in ONVIF_PORTS if pp in open_ports] or ONVIF_PORTS:
            u = _onvif_probe(ip, p)
            if u:
                onvif = True
                onvif_url = u
                break

    http_server, title = "", ""
    for p in [pp for pp in WEB_PORTS if pp in open_ports]:
        info = _http_probe(ip, p)
        if info:
            http_server = http_server or info["server"]
            title = title or info["title"]
            if _looks_like_camera(info["server"] + " " + info["title"] + " " + info["auth"]):
                title = title or "câmera (web)"
            break

    vendor = _oui_vendor(mac)
    is_camera = bool(onvif or rtsp_ports or vendor or _looks_like_camera(http_server + " " + title))

    reasons = []
    if onvif:
        reasons.append("ONVIF")
    if rtsp_ports:
        reasons.append("RTSP:" + ",".join(str(p) for p in rtsp_ports))
    if vendor:
        reasons.append(vendor)
    if not name:
        name = vendor or (title if title else ("Câmera" if is_camera else "Dispositivo"))

    return {
        "ip": ip, "mac": mac or "", "vendor": vendor,
        "onvif": onvif, "onvif_url": onvif_url,
        "rtsp_ports": rtsp_ports, "all_ports": open_ports,
        "rtsp_server": rtsp_server, "http_server": http_server, "title": title,
        "has_ptz": has_ptz, "name": name,
        "is_camera": is_camera, "reason": ", ".join(reasons),
    }


def scan_network():
    """Varredura forte. Retorna { devices: [...], subnets: [...] }."""
    subnets = local_subnets()
    onvif_disc = ws_discovery(timeout=4.0)

    # descobre todos os hosts: poke UDP em toda a /24 para popular o ARP
    all_hosts = [f"{b}.{i}" for b in subnets for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=200) as ex:
        list(ex.map(_udp_poke, all_hosts))
    time.sleep(0.5)
    arp = arp_table(subnets)

    # candidatos = hosts no ARP + quem respondeu ao ONVIF
    candidates = set(arp.keys()) | set(onvif_disc.keys())

    devices = []
    with ThreadPoolExecutor(max_workers=48) as ex:
        futs = {ex.submit(_probe_host, ip, arp.get(ip, ""), onvif_disc): ip for ip in candidates}
        for f in futs:
            try:
                d = f.result(timeout=25)
            except Exception:
                continue
            if d["is_camera"] or d["onvif"] or d["rtsp_ports"]:
                devices.append(d)

    devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    return {"devices": devices, "subnets": subnets}


if __name__ == "__main__":
    import json
    print(json.dumps(scan_network(), indent=2, ensure_ascii=False))
