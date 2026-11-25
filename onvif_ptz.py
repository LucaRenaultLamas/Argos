"""
ONVIF via SOAP direto (sem bibliotecas externas).
- PtzController: ContinuousMove / Stop / posições salvas (presets)
- probe_stream: descobre as URLs de vídeo (GetProfiles + GetStreamUri)
Autenticação WS-UsernameToken (PasswordDigest).
"""
import base64
import hashlib
import secrets
import datetime
import re
import requests

SCHEMA = "http://www.onvif.org/ver10/schema"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
DEV_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"


def _security_header(user, password):
    if not user and not password:
        return ""
    nonce = secrets.token_bytes(16)
    nonce_b64 = base64.b64encode(nonce).decode()
    created = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" '
        'xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<UsernameToken><Username>{u}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{d}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{n}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{c}</Created>'
        "</UsernameToken></Security></s:Header>"
    ).format(u=user, d=digest, n=nonce_b64, c=created)


def _soap(url, user, password, body_inner, timeout=6):
    env = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:trt="%s" xmlns:tt="%s" xmlns:tptz="%s" xmlns:tds="%s">'
        % (MEDIA_NS, SCHEMA, PTZ_NS, DEV_NS)
        + _security_header(user, password)
        + "<s:Body>"
        + body_inner
        + "</s:Body></s:Envelope>"
    )
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    resp = requests.post(url, data=env.encode("utf-8"), headers=headers, timeout=timeout)
    return resp.text


def _path_from_uri(uri):
    m = re.match(r"rtsp://[^/]+(/.*)$", uri)
    return m.group(1) if m else uri


def probe_stream(onvif_url, user, password):
    """
    Retorna dict:
      { ok, error, profiles:[{token, uri, path}], hd_path, sd_path, ptz_token, has_ptz }
    """
    out = {"ok": False, "error": "", "profiles": [], "hd_path": "", "sd_path": "",
           "ptz_token": "", "has_ptz": False}
    try:
        txt = _soap(onvif_url, user, password, "<trt:GetProfiles/>")
    except Exception as e:
        out["error"] = "sem resposta ONVIF: %s" % e
        return out
    if "GetProfilesResponse" not in txt:
        if re.search(r"(?i)NotAuthorized|not authorized", txt):
            out["error"] = "usuário ou senha incorretos"
        else:
            out["error"] = "resposta ONVIF inesperada"
        return out

    tokens = []
    for m in re.finditer(r'token="([^"]+)"', txt):
        t = m.group(1)
        if t not in tokens:
            tokens.append(t)
    out["has_ptz"] = "ptz" in txt.lower() or "PTZConfiguration" in txt

    for tk in tokens:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup><trt:ProfileToken>%s</trt:ProfileToken></trt:GetStreamUri>"
        ) % tk
        try:
            r = _soap(onvif_url, user, password, body)
        except Exception:
            continue
        m = re.search(r"<[^>]*Uri>(rtsp://[^<]+)<", r)
        if m:
            uri = m.group(1).strip()
            out["profiles"].append({"token": tk, "uri": uri, "path": _path_from_uri(uri)})

    if out["profiles"]:
        out["ok"] = True
        # main = profile0 / primeiro ; sub = profile1 / segundo
        paths = [p["path"] for p in out["profiles"]]
        hd = next((p for p in paths if "profile0" in p or p.endswith("0")), paths[0])
        sd = next((p for p in paths if "profile1" in p or p.endswith("1")), paths[-1])
        out["hd_path"] = hd
        out["sd_path"] = sd
        out["ptz_token"] = out["profiles"][0]["token"]
    else:
        out["error"] = "autenticou, mas nenhum perfil retornou URL de vídeo"
    return out


class PtzController:
    def __init__(self, cam):
        self.cam = cam
        self.user = cam.get("user", "")
        self.password = cam.get("password", "")
        self.token = cam.get("ptz_token", "PROFILE_000")
        self._ptz_url = None

    def _discover(self):
        if self._ptz_url:
            return self._ptz_url
        dev = self.cam["onvif_url"]
        try:
            body = '<GetServices xmlns="%s"><IncludeCapability>false</IncludeCapability></GetServices>' % DEV_NS
            txt = _soap(dev, self.user, self.password, body)
            for m in re.finditer(r"<[^>]*Service>(.*?)</[^>]*Service>", txt, re.S):
                block = m.group(1)
                if "ptz/wsdl" in block:
                    xaddr = re.search(r"<[^>]*XAddr>([^<]+)<", block)
                    if xaddr:
                        self._ptz_url = xaddr.group(1).strip()
                        break
        except Exception:
            pass
        if not self._ptz_url:
            self._ptz_url = dev
        return self._ptz_url

    def continuous_move(self, pan=0.0, tilt=0.0, zoom=0.0):
        url = self._discover()
        body = (
            '<ContinuousMove xmlns="%s">'
            "<ProfileToken>%s</ProfileToken>"
            "<Velocity>"
            '<PanTilt x="%s" y="%s" xmlns="%s"/>'
            '<Zoom x="%s" xmlns="%s"/>'
            "</Velocity>"
            "</ContinuousMove>"
        ) % (PTZ_NS, self.token, pan, tilt, SCHEMA, zoom, SCHEMA)
        return _soap(url, self.user, self.password, body)

    def stop(self):
        url = self._discover()
        body = (
            '<Stop xmlns="%s"><ProfileToken>%s</ProfileToken>'
            "<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>"
        ) % (PTZ_NS, self.token)
        return _soap(url, self.user, self.password, body)

    # ---- posições salvas (presets) ----
    @staticmethod
    def _falha(txt):
        """Mensagem de erro de uma resposta SOAP (Fault), ou None se deu certo."""
        if "Fault" not in txt:
            return None
        m = re.search(r"<[^>]*Text[^>]*>([^<]+)<", txt) or re.search(r"<[^>]*Reason[^>]*>\s*([^<]+)<", txt)
        sub = re.search(r"<[^>]*Subcode>.*?<[^>]*Value>([^<]+)<", txt, re.S)
        return (m.group(1).strip() if m else "a câmera recusou o comando") + (
            f" ({sub.group(1).strip()})" if sub else "")

    def get_presets(self):
        url = self._discover()
        body = '<GetPresets xmlns="%s"><ProfileToken>%s</ProfileToken></GetPresets>' % (PTZ_NS, self.token)
        txt = _soap(url, self.user, self.password, body)
        erro = self._falha(txt)
        if erro:
            raise RuntimeError(erro)
        out = []
        # <Preset token="1"><Name>Rua</Name>...</Preset>  ou  <Preset token="3"/>
        for m in re.finditer(r"<([\w-]+:)?Preset\b([^>]*?)(/>|>(.*?)</(?:[\w-]+:)?Preset>)", txt, re.S):
            tk = re.search(r'token="([^"]+)"', m.group(2))
            if not tk:
                continue
            nm = re.search(r"<[^>]*Name>([^<]*)<", m.group(4) or "")
            out.append({"token": tk.group(1), "name": _unesc(nm.group(1)).strip() if nm else ""})
        return out

    def goto_preset(self, token):
        url = self._discover()
        body = ('<GotoPreset xmlns="%s"><ProfileToken>%s</ProfileToken><PresetToken>%s</PresetToken>'
                '</GotoPreset>') % (PTZ_NS, self.token, _esc(token))
        erro = self._falha(_soap(url, self.user, self.password, body))
        if erro:
            raise RuntimeError(erro)

    def set_preset(self, name, token=None):
        url = self._discover()
        extra = "<PresetToken>%s</PresetToken>" % _esc(token) if token else ""
        body = ('<SetPreset xmlns="%s"><ProfileToken>%s</ProfileToken><PresetName>%s</PresetName>%s'
                '</SetPreset>') % (PTZ_NS, self.token, _esc(name), extra)
        txt = _soap(url, self.user, self.password, body)
        erro = self._falha(txt)
        if erro:
            raise RuntimeError(erro)
        m = re.search(r"<[^>]*PresetToken>([^<]+)<", txt)
        return m.group(1).strip() if m else (token or "")

    def remove_preset(self, token):
        url = self._discover()
        body = ('<RemovePreset xmlns="%s"><ProfileToken>%s</ProfileToken><PresetToken>%s</PresetToken>'
                '</RemovePreset>') % (PTZ_NS, self.token, _esc(token))
        erro = self._falha(_soap(url, self.user, self.password, body))
        if erro:
            raise RuntimeError(erro)


def _esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _unesc(t):
    import html
    return html.unescape(str(t))
