"""
Alertas, histórico de detecções, estatísticas e mapa de calor.

- AlertEngine: decide quando avisar (tipo, horário, área, objeto parado) e envia
  por WhatsApp (conectado direto no sistema) ou Telegram, com texto + foto.
- Store: banco SQLite com os avistamentos (para busca e gráficos) e os eventos.
- Heatmap: acumula o movimento de cada câmera por dia.
- HealthMonitor: câmera offline, tampada/escura/embaçada ou fora de posição.
"""
import datetime
import json
import math
import os
import queue
import shutil
import sqlite3
import threading
import time

import cv2
import numpy as np
import requests

LABELS = ["Pessoa", "Carro", "Moto", "Bicicleta", "Animal", "Desconhecido"]

DEFAULTS = {
    "enabled": False,            # Enviar alertas
    "send_image": True,          # Enviar imagem junto (Telegram)
    "channel": "whatsapp",       # "whatsapp" (conectado pelo QR code) ou "telegram"
    "telegram_token": "",
    "telegram_chat_id": "",
    "cooldown_s": 60,            # intervalo mínimo entre alertas iguais (mesma câmera e tipo)
    "zone_only": True,           # câmeras com área desenhada: só alerta dentro dela
    "types": {"Pessoa": True, "Carro": False, "Moto": True,
              "Bicicleta": False, "Animal": False, "Desconhecido": False},
    "rules": [],                 # [{id, enabled, mode: only|ignore, label, start, end, cam}]
    "loiter_enabled": False,     # Objeto parado tempo demais
    "loiter_minutes": 5,
    "tamper_enabled": True,      # Câmera tampada / fora de posição
    "offline_enabled": True,     # Câmera offline
    "offline_minutes": 3,
    "follow_types": ["Pessoa", "Carro", "Moto"],   # o que o PTZ segue
    "keep_days": 30,             # quanto tempo guardar o histórico
    "clips_mode": "todas",       # clipes de vídeo: todas (detecções) | alertas | desligado
    "plates_alert_unknown": False,   # avisar placa que não está na lista de conhecidas
    "plates_known": "",          # placas conhecidas (uma por linha: "ABC1D23 nome")
    "plates_watch": "",          # placas que geram aviso sempre que aparecem
    "sys_down_enabled": True,    # avisar quando o Argos volta depois de ficar parado
    "sys_down_minutes": 2,
    "disk_enabled": True,        # avisar disco quase cheio
    "disk_percent": 90,
}

CORES = ["branco", "preto", "cinza", "vermelho", "laranja", "amarelo", "verde", "azul",
         "roxo", "rosa", "marrom"]

ZONE_MIN_FRAMES = 3          # quadros seguidos dentro da área para valer como "entrou"

GENDER = {"Pessoa": "detectada", "Moto": "detectada", "Bicicleta": "detectada",
          "Carro": "detectado", "Animal": "detectado"}
STILL = {"Pessoa": "parada", "Moto": "parada", "Bicicleta": "parada",
         "Carro": "parado", "Animal": "parado", "Desconhecido": "parado"}


_settings_cache = {"raw": None, "val": None}


def merged_settings(config):
    """Configurações de alerta com os valores padrão preenchidos.
    É chamada muitas vezes por segundo (a cada objeto em cada quadro), então o
    resultado fica em cache até as configurações serem salvas de novo. Trate o
    retorno como somente leitura (copie antes de alterar)."""
    raw = (config.get("settings") or {}).get("alerts") or {}
    c = _settings_cache
    if c["raw"] is raw and c["val"] is not None:
        return c["val"]
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k == "types" and isinstance(v, dict):
            s["types"].update({kk: bool(vv) for kk, vv in v.items() if kk in LABELS})
        elif k in s:
            s[k] = v
    c["raw"], c["val"] = raw, s
    return s


def normalize_phone(p):
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    if not d:
        return ""
    if len(d) in (10, 11) and not d.startswith("55"):
        d = "55" + d          # número brasileiro sem DDI
    return "+" + d


def wa_chat_ids(p):
    """Destinos do WhatsApp. Aceita vários separados por vírgula ou ';'.
    Número -> 5532999999999 ; ID de grupo (…@g.us) é usado como está."""
    out = []
    for part in str(p or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "@" in part:
            out.append(part)
            continue
        d = normalize_phone(part).lstrip("+")
        if d:
            out.append(d)
    return out


def fmt_duration(minutes):
    secs = int(round(float(minutes) * 60))
    if secs < 60:
        return f"{secs} s"
    m = secs // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


def _minutes(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except Exception:
        return 0


def in_window(start, end, dt):
    s, e = _minutes(start), _minutes(end)
    m = dt.hour * 60 + dt.minute
    if s == e:
        return True                       # o dia todo
    if s < e:
        return s <= m < e
    return m >= s or m < e                # atravessa a meia-noite (ex.: 22:00–06:00)


def allowed_by_rules(s, label, cam_id, dt):
    """Retorna (permitido, motivo)."""
    if not s["types"].get(label, False):
        return False, "tipo desativado"
    rules = [r for r in s.get("rules", []) if r.get("enabled", True)
             and r.get("label", "*") in ("*", label)
             and r.get("cam", "*") in ("*", cam_id)]
    for r in rules:
        if r.get("mode") == "ignore" and in_window(r.get("start"), r.get("end"), dt):
            return False, "regra: ignorar"
    onlys = [r for r in rules if r.get("mode") == "only"]
    if onlys and not any(in_window(r.get("start"), r.get("end"), dt) for r in onlys):
        return False, "fora do horário da regra"
    return True, ""


def point_in_zone(zone, x, y):
    if not zone or len(zone) < 3:
        return None
    poly = np.array([[float(p[0]), float(p[1])] for p in zone], dtype=np.float32)
    return cv2.pointPolygonTest(poly, (float(x), float(y)), False) >= 0


def _jpeg(img, max_w=1280, q=80):
    h, w = img.shape[:2]
    if w > max_w:
        img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    return buf.tobytes() if ok else None


def cor_dominante(frame, box, label):
    """Cor predominante do objeto: tronco da pessoa (camiseta), lataria do carro etc.
    Retorna o nome da cor ou None (imagem P&B à noite, recorte pequeno, sem cor clara)."""
    try:
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = [float(v) for v in box]
        w, h = x2 - x1, y2 - y1
        if w < 12 or h < 12:
            return None
        if label == "Pessoa":
            fx, fy = (0.25, 0.75), (0.20, 0.52)       # tronco
        elif label in ("Moto", "Bicicleta"):
            fx, fy = (0.25, 0.75), (0.15, 0.55)       # piloto / tanque
        else:
            fx, fy = (0.15, 0.85), (0.30, 0.75)       # lataria, abaixo dos vidros
        a = frame[max(0, int(y1 + h * fy[0])):min(H, int(y1 + h * fy[1])),
                  max(0, int(x1 + w * fx[0])):min(W, int(x1 + w * fx[1]))]
        if a.size == 0:
            return None
        a = cv2.resize(a, (32, 32), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(a, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
        # imagem do quadro sem cor (infravermelho à noite): não dá para saber a cor
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        if float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) < 18:
            return None
        hh, ss, vv = hsv[:, 0], hsv[:, 1], hsv[:, 2]
        cont = {}

        def soma(nome, mask):
            n = int(mask.sum())
            if n:
                cont[nome] = cont.get(nome, 0) + n
        escuro = vv < 55
        sem_cor = (~escuro) & (ss < 45)
        cor = (~escuro) & (ss >= 45)
        soma("preto", escuro)
        soma("branco", sem_cor & (vv >= 175))
        soma("cinza", sem_cor & (vv < 175))
        soma("vermelho", cor & ((hh < 8) | (hh >= 170)))
        soma("marrom", cor & (hh >= 8) & (hh < 25) & (vv < 140))
        soma("laranja", cor & (hh >= 8) & (hh < 20) & (vv >= 140))
        soma("amarelo", cor & (hh >= 20) & (hh < 34) & ~((hh < 25) & (vv < 140)))
        soma("verde", cor & (hh >= 34) & (hh < 85))
        soma("azul", cor & (hh >= 85) & (hh < 130))
        soma("roxo", cor & (hh >= 130) & (hh < 150))
        soma("rosa", cor & (hh >= 150) & (hh < 170))
        if not cont:
            return None
        nome, n = max(cont.items(), key=lambda kv: kv[1])
        return nome if n >= 0.30 * len(hh) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")   # no WAL é seguro e grava bem mais rápido
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sightings(
                id INTEGER PRIMARY KEY, cam TEXT, label TEXT,
                t_start REAL, t_end REAL, cx REAL, cy REAL, in_zone INTEGER, thumb TEXT);
            CREATE INDEX IF NOT EXISTS ix_s_t ON sightings(t_start);
            CREATE INDEX IF NOT EXISTS ix_s_cam ON sightings(cam, t_start);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, ts REAL, cam TEXT, kind TEXT, label TEXT,
                msg TEXT, image TEXT, status TEXT);
            CREATE INDEX IF NOT EXISTS ix_e_t ON events(ts);
        """)
        # colunas novas (cor, placa, clipe) em bancos antigos
        for tab, cols in (("sightings", (("color", "TEXT"), ("plate", "TEXT"), ("clip", "TEXT"),
                                         ("clip_off", "REAL"))),
                          ("events", (("clip", "TEXT"), ("clip_off", "REAL")))):
            tem = {r[1] for r in self.db.execute(f"PRAGMA table_info({tab})")}
            for nome, tipo in cols:
                if nome not in tem:
                    self.db.execute(f"ALTER TABLE {tab} ADD COLUMN {nome} {tipo}")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_s_plate ON sightings(plate)")
        self.db.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def _query(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def add_sighting(self, cam, label, t, cx, cy, in_zone, thumb, color=None, clip=None, clip_off=None):
        return self._exec("INSERT INTO sightings(cam,label,t_start,t_end,cx,cy,in_zone,thumb,color,clip,clip_off) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (cam, label, t, t, cx, cy, None if in_zone is None else int(in_zone), thumb,
                           color, clip, clip_off))

    def set_sighting_plate(self, sid, plate):
        self._exec("UPDATE sightings SET plate=? WHERE id=?", (plate, sid))

    def set_sighting_clip(self, sid, clip, clip_off):
        self._exec("UPDATE sightings SET clip=?, clip_off=? WHERE id=? AND clip IS NULL", (clip, clip_off, sid))

    def update_sighting(self, sid, label, t_end, in_zone=None):
        if in_zone:
            self._exec("UPDATE sightings SET label=?, t_end=?, in_zone=1 WHERE id=?", (label, t_end, sid))
        else:
            self._exec("UPDATE sightings SET label=?, t_end=? WHERE id=?", (label, t_end, sid))

    def add_event(self, ts, cam, kind, label, msg, image, status, clip=None, clip_off=None):
        return self._exec("INSERT INTO events(ts,cam,kind,label,msg,image,status,clip,clip_off) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (ts, cam, kind, label, msg, image, status, clip, clip_off))

    def set_event_status(self, eid, status):
        self._exec("UPDATE events SET status=? WHERE id=?", (status, eid))

    def search(self, label=None, cam=None, t_from=None, t_to=None, h_from=None, h_to=None,
               zone_only=False, limit=300, color=None, plate=None):
        sql, args = "SELECT * FROM sightings WHERE 1=1", []
        labels = [x for x in (label if isinstance(label, (list, tuple)) else [label]) if x]
        if labels:
            sql += " AND label IN (%s)" % ",".join("?" * len(labels)); args += labels
        if color:
            sql += " AND color=?"; args.append(color)
        if plate:
            sql += " AND plate LIKE ?"; args.append("%" + plate + "%")
        if cam:
            sql += " AND cam=?"; args.append(cam)
        if t_from is not None:
            sql += " AND t_start>=?"; args.append(t_from)
        if t_to is not None:
            sql += " AND t_start<?"; args.append(t_to)
        if zone_only:
            sql += " AND in_zone=1"
        if h_from is None or h_to is None:
            # sem filtro de horário: o próprio banco conta e limita (não carrega tudo)
            total = self._query(sql.replace("SELECT *", "SELECT COUNT(*) AS n", 1), args)[0]["n"]
            rows = self._query(sql + " ORDER BY t_start DESC LIMIT ?", args + [int(limit)])
            return rows, total
        sql += " ORDER BY t_start DESC"
        rows = self._query(sql, args)
        if h_from is not None and h_to is not None:
            def ok(r):
                dt = datetime.datetime.fromtimestamp(r["t_start"])
                return in_window(h_from, h_to, dt)
            rows = [r for r in rows if ok(r)]
        total = len(rows)
        return rows[:limit], total

    def timeline(self, cam, t_from, t_to):
        s = self._query("SELECT id,label,t_start,t_end,in_zone,thumb,color,plate,clip,clip_off FROM sightings "
                        "WHERE cam=? AND t_start>=? AND t_start<? ORDER BY t_start", (cam, t_from, t_to))
        e = self._query("SELECT id,ts,kind,label,msg,image,clip,clip_off FROM events "
                        "WHERE cam=? AND ts>=? AND ts<? ORDER BY ts", (cam, t_from, t_to))
        return s, e

    def stats(self, t_from, t_to, cam=None):
        sql = "SELECT cam, label, t_start FROM sightings WHERE t_start>=? AND t_start<?"
        args = [t_from, t_to]
        if cam:
            sql += " AND cam=?"; args.append(cam)
        rows = self._query(sql, args)
        hours = [dict() for _ in range(24)]
        by_cam = {}
        cam_hours = {}
        days = {}
        d = datetime.date.fromtimestamp(t_from)
        last = datetime.date.fromtimestamp(t_to - 1)
        while d <= last:                       # todos os dias do período, mesmo sem detecção
            days[d.isoformat()] = [dict() for _ in range(24)]
            d += datetime.timedelta(days=1)
        for r in rows:
            dt = datetime.datetime.fromtimestamp(r["t_start"])
            h, lb = dt.hour, r["label"]
            hours[h][lb] = hours[h].get(lb, 0) + 1
            c = by_cam.setdefault(r["cam"], {})
            c[lb] = c.get(lb, 0) + 1
            ch = cam_hours.setdefault(r["cam"], [dict() for _ in range(24)])
            ch[h][lb] = ch[h].get(lb, 0) + 1
            dd = days.setdefault(dt.date().isoformat(), [dict() for _ in range(24)])
            dd[h][lb] = dd[h].get(lb, 0) + 1
        return hours, by_cam, cam_hours, [{"date": k, "hours": v} for k, v in sorted(days.items())]

    def events(self, limit=200, cam=None):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if cam:
            sql += " AND cam=?"; args.append(cam)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return self._query(sql, args)

    def purge(self, before_ts):
        with self.lock:
            thumbs = [r[0] for r in self.db.execute("SELECT thumb FROM sightings WHERE t_start<?", (before_ts,))]
            self.db.execute("DELETE FROM sightings WHERE t_start<?", (before_ts,))
            self.db.execute("DELETE FROM events WHERE ts<?", (before_ts,))
            self.db.commit()
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # não deixa o -wal crescer sem fim
            except Exception:
                pass
        return thumbs


# ---------------------------------------------------------------------------
# Envio (WhatsApp conectado no sistema / Telegram)
# ---------------------------------------------------------------------------
class Sender:
    def __init__(self, get_settings, get_phone, log):
        self.get_settings = get_settings
        self.get_phone = get_phone
        self.log = log
        self.q = queue.Queue(maxsize=200)
        self.last_error = ""
        self.last_ok = 0.0
        self.wa = None          # LocalWhatsApp (definido pelo AlertEngine)
        threading.Thread(target=self._loop, daemon=True).start()

    def enqueue(self, text, image=None, on_done=None):
        try:
            self.q.put_nowait((text, image, on_done))
        except queue.Full:
            self.log("Fila de alertas cheia: alerta descartado")

    def send_now(self, text, image=None):
        """Envio direto (usado no botão 'Enviar teste'). Retorna (ok, detalhe)."""
        return self._send(text, image)

    def _loop(self):
        while True:
            text, image, on_done = self.q.get()
            ok, info = False, ""
            for attempt in range(3):
                ok, info = self._send(text, image)
                if ok:
                    break
                time.sleep(3 + attempt * 5)
            if ok:
                self.last_ok = time.time()
                self.last_error = ""
            else:
                self.last_error = info
                self.log(f"Falha ao enviar alerta: {info}")
            if on_done:
                try:
                    on_done(ok, info)
                except Exception:
                    pass
            time.sleep(1.5)   # não estoura limites dos serviços

    @staticmethod
    def _scrub(msg, s):
        """Nunca mostra o token em mensagens de erro."""
        for k in ("telegram_token",):
            v = str(s.get(k) or "").strip()
            if v:
                msg = msg.replace(v, "***")
        return msg[:200]

    def _send(self, text, image):
        s = self.get_settings()
        try:
            if s["channel"] == "telegram":
                tok, chat = s.get("telegram_token", "").strip(), str(s.get("telegram_chat_id", "")).strip()
                if not tok or not chat:
                    return False, "Telegram: falta o token do bot ou o chat ID"
                base = f"https://api.telegram.org/bot{tok}"
                if image and s.get("send_image", True):
                    r = requests.post(base + "/sendPhoto", data={"chat_id": chat, "caption": text[:1000]},
                                      files={"photo": ("alerta.jpg", image, "image/jpeg")}, timeout=20)
                else:
                    r = requests.post(base + "/sendMessage", data={"chat_id": chat, "text": text}, timeout=15)
                j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.ok and j.get("ok"):
                    return True, "enviado (Telegram)"
                return False, "Telegram: " + self._scrub(str(j.get("description") or r.status_code), s)
            # WhatsApp conectado direto no sistema (QR code nas configurações)
            chats = wa_chat_ids(self.get_phone())
            if not chats:
                return False, "WhatsApp: falta o telefone que recebe os alertas"
            if self.wa is None:
                return False, "WhatsApp: módulo não carregado"
            return self.wa.send(chats, text, image if s.get("send_image", True) else None)
        except requests.exceptions.Timeout:
            return False, "sem resposta do serviço (tempo esgotado)"
        except requests.exceptions.ConnectionError:
            return False, "sem conexão com a internet ou com o serviço"
        except Exception as e:
            return False, "erro: " + self._scrub(str(e), s)


# ---------------------------------------------------------------------------
# Mapa de calor
# ---------------------------------------------------------------------------
HEAT_W, HEAT_H = 160, 90


class Heatmap:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.acc = {}       # cam -> (date, array)
        self.last_save = {}
        self.last_ref = {}
        self.lock = threading.Lock()

    def _paths(self, cam, date):
        return (os.path.join(self.folder, f"{cam}_{date}.npy"),
                os.path.join(self.folder, f"{cam}_{date}.jpg"))

    def add(self, cam, mask, frame):
        """mask: imagem binária de movimento (qualquer tamanho), frame: quadro colorido."""
        date = datetime.date.today().isoformat()
        small = cv2.resize(mask, (HEAT_W, HEAT_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        now = time.time()
        with self.lock:
            cur = self.acc.get(cam)
            if cur is None or cur[0] != date:
                if cur is not None:
                    self._save(cam, *cur)
                npy, _ = self._paths(cam, date)
                arr = np.load(npy) if os.path.exists(npy) else np.zeros((HEAT_H, HEAT_W), np.float32)
                cur = (date, arr)
                self.acc[cam] = cur
            cur[1][:] += small
            if now - self.last_save.get(cam, 0) > 60:
                self._save(cam, *cur)
                self.last_save[cam] = now
            if now - self.last_ref.get(cam, 0) > 3600:   # foto de fundo (atualiza a cada hora)
                _, jpg = self._paths(cam, date)
                try:
                    cv2.imwrite(jpg, cv2.resize(frame, (640, int(640 * frame.shape[0] / frame.shape[1]))))
                except Exception:
                    pass
                self.last_ref[cam] = now

    def _save(self, cam, date, arr):
        npy, _ = self._paths(cam, date)
        try:
            tmp = npy + ".tmp.npy"
            np.save(tmp, arr)
            os.replace(tmp, npy)
        except Exception:
            pass

    def flush(self):
        with self.lock:
            for cam, (date, arr) in self.acc.items():
                self._save(cam, date, arr)

    def render(self, cam, date, fallback_frame=None):
        npy, jpg = self._paths(cam, date)
        with self.lock:
            cur = self.acc.get(cam)
            arr = cur[1].copy() if cur and cur[0] == date else (np.load(npy) if os.path.exists(npy) else None)
        ref = cv2.imread(jpg) if os.path.exists(jpg) else fallback_frame
        if ref is None:
            ref = np.full((360, 640, 3), 30, np.uint8)
        H, W = ref.shape[:2]
        if arr is None or float(arr.max()) <= 0:
            out = (ref * 0.6).astype(np.uint8)
            cv2.putText(out, "Sem movimento registrado neste dia", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
            return _jpeg(out, 1280, 85)
        nz = arr[arr > 0]
        top = float(np.percentile(nz, 99)) if nz.size else float(arr.max())
        norm = np.clip(arr / max(top, 1e-6), 0, 1)
        norm = cv2.GaussianBlur(norm, (0, 0), 2.0)
        norm = cv2.resize(norm, (W, H), interpolation=cv2.INTER_CUBIC)
        norm = np.clip(norm, 0, 1)
        color = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
        a = (np.power(norm, 0.6) * 0.7)[..., None]
        out = (ref.astype(np.float32) * (1 - a) + color.astype(np.float32) * a).astype(np.uint8)
        return _jpeg(out, 1280, 85)

    def purge(self, before_date):
        for f in os.listdir(self.folder):
            try:
                d = f.split("_")[-1].split(".")[0]
                if d < before_date:
                    os.remove(os.path.join(self.folder, f))
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Motor de alertas
# ---------------------------------------------------------------------------
class AlertEngine:
    def __init__(self, base_dir, get_config, get_camera, log):
        self.get_config = get_config
        self.get_camera = get_camera
        self.log = log
        self.media = os.path.join(base_dir, "media")
        self.events_dir = os.path.join(self.media, "events")
        os.makedirs(self.events_dir, exist_ok=True)
        self.store = Store(os.path.join(self.media, "eventos.db"))
        self.heat = Heatmap(os.path.join(self.media, "heatmap"))
        self.sender = Sender(self.settings, self.phone, log)
        from whatsapp_local import LocalWhatsApp
        self.wa = LocalWhatsApp(base_dir, log)
        self.sender.wa = self.wa
        self.cool = {}
        self.cool_lock = threading.Lock()
        self.clips = None        # ClipManager (definido pelo app)
        threading.Thread(target=self._housekeeping, daemon=True).start()

    # ---- configurações ----
    def settings(self):
        return merged_settings(self.get_config())

    def phone(self):
        return (self.get_config().get("settings") or {}).get("alert_phone", "")

    def _cam_name(self, cam_id):
        c = self.get_camera(cam_id) or {}
        return c.get("name", cam_id)

    # ---- arquivos ----
    def _day_dir(self, ts):
        d = os.path.join(self.events_dir, datetime.date.fromtimestamp(ts).isoformat())
        os.makedirs(d, exist_ok=True)
        return d

    def _save_img(self, ts, cam_id, tag, data):
        if not data:
            return None
        d = self._day_dir(ts)
        name = f"{cam_id}_{time.strftime('%H-%M-%S', time.localtime(ts))}_{tag}.jpg"
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        return f"{os.path.basename(d)}/{name}"

    # ---- emissão de alerta ----
    def clip_para(self, cam_id, now, ev=None):
        """Clipe de vídeo para um alerta (respeita a opção "Clipes de vídeo")."""
        if self.clips is None or self.settings().get("clips_mode", "todas") == "desligado":
            return None, None
        if ev is not None and ev.get("clip"):
            return ev["clip"]
        try:
            return self.clips.disparar(cam_id, now)
        except Exception:
            return None, None

    def emit(self, cam_id, kind, label, text, image_bytes=None, respect_cooldown=True, key=None, clip=None):
        s = self.settings()
        now = time.time()
        if respect_cooldown:
            k = key or (cam_id, kind, label)
            with self.cool_lock:
                if now - self.cool.get(k, 0) < float(s.get("cooldown_s", 60)):
                    return None
                self.cool[k] = now
        img_rel = self._save_img(now, cam_id, kind, image_bytes) if cam_id else None
        status = "pendente" if s["enabled"] else "envio desativado"
        clip_rel, clip_off = clip if clip else (None, None)
        eid = self.store.add_event(now, cam_id, kind, label, text, img_rel, status, clip_rel, clip_off)
        self.log(f"Alerta [{self._cam_name(cam_id) if cam_id else 'sistema'}]: {text}")
        if s["enabled"]:
            def done(ok, info, eid=eid):
                self.store.set_event_status(eid, info if ok else "falhou: " + info)
            self.sender.enqueue(text, image_bytes if s.get("send_image", True) else None, done)
        return eid

    def _stamp(self):
        return time.strftime("%d/%m %H:%M:%S")

    def _snapshot(self, frame, box=None, label=None, color=(0, 0, 255)):
        img = frame.copy()
        if box is not None:
            x1, y1, x2, y2 = [int(v) for v in box]
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
            if label:
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(img, (x1, max(0, y1 - th - 10)), (x1 + tw + 10, y1), color, -1)
                cv2.putText(img, label, (x1 + 5, max(14, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            (0, 0, 0), 2, cv2.LINE_AA)
        return _jpeg(img)

    # ---- objetos rastreados ----
    def on_track(self, cam_id, t, label, frame, now, color=(0, 0, 255)):
        """Chamado a cada quadro para cada objeto exibido."""
        if not label:
            return
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = t["box"]
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        bh = max(1.0, y2 - y1)
        ev = t.get("ev")
        if ev is not None:
            ev["label"] = label
        if ev is None:
            ev = t["ev"] = {"label": label, "origin": (cx, cy), "moved": False, "sid": None, "alerted": False,
                            "first": now, "still_c": None, "still_since": None, "loiter_sent": False,
                            "last_upd": 0.0, "zone_hit": False}
        # só objetos que chegaram/se mexeram (carro estacionado desde sempre não conta)
        if not ev["moved"]:
            ox, oy = ev["origin"]
            if math.hypot(cx - ox, cy - oy) > 0.6 * bh:
                ev["moved"] = True
            else:
                return
        cam = self.get_camera(cam_id) or {}
        in_zone = point_in_zone(cam.get("alert_zone"), cx / W, y2 / H)   # "pés" do objeto
        # precisa estar dentro da área em 3 quadros seguidos: um quadro com a caixa
        # "esticada" (farol, reflexo) não basta para contar como entrada na área
        ev["zone_n"] = ev.get("zone_n", 0) + 1 if in_zone else 0
        if in_zone and not ev["zone_hit"] and ev["zone_n"] >= ZONE_MIN_FRAMES:
            ev["zone_hit"] = True
            ev["zone_shot"] = (t["box"], frame.copy())    # foto do momento em que entrou na área
        if in_zone and not ev["zone_hit"]:
            in_zone = False          # ainda não confirmou: trata como fora

        # histórico (busca e gráficos)
        if ev["sid"] is None:
            # foto da câmera inteira no momento (sem recorte nem zoom no objeto)
            thumb = self._save_img(now, cam_id, f"obj{t['id']}", _jpeg(frame, 640, 72))
            cor = cor_dominante(frame, (x1, y1, x2, y2), label)
            clip = (None, None)
            if self.clips is not None and self.settings().get("clips_mode", "todas") == "todas":
                try:
                    clip = self.clips.disparar(cam_id, now)
                except Exception:
                    clip = (None, None)
            if clip[0]:
                ev["clip"] = clip
            ev["sid"] = self.store.add_sighting(cam_id, label, now, cx / W, cy / H, in_zone, thumb,
                                                cor, clip[0], clip[1])
            ev["last_upd"] = now
            ev["last_pos"] = (cx, cy)
        elif now - ev["last_upd"] > 2.0:
            self.store.update_sighting(ev["sid"], label, now, in_zone)
            ev["last_upd"] = now
            # ainda se mexendo: o clipe continua gravando
            lp = ev.get("last_pos") or (cx, cy)
            if self.clips is not None and ev.get("clip") and math.hypot(cx - lp[0], cy - lp[1]) > 0.25 * bh:
                self.clips.estender(cam_id, now)
            ev["last_pos"] = (cx, cy)

        s = self.settings()
        zone_ok = (not s.get("zone_only", True)) or in_zone is None or in_zone
        dt = datetime.datetime.fromtimestamp(now)

        # alerta de detecção (uma vez por objeto, depois que a etiqueta firmou).
        # Vale também para quem só passou pela área e já saiu (moto rápida).
        passed_zone = zone_ok or ev["zone_hit"]
        if not ev["alerted"] and passed_zone and now - ev["first"] >= 0.7:
            ok, _why = allowed_by_rules(s, label, cam_id, dt)
            if ok:
                if label == "Desconhecido":
                    what = "Movimento desconhecido detectado"
                else:
                    what = f"{label} {GENDER.get(label, 'detectado')}"
                where = " na área de alerta" if ev["zone_hit"] else ""
                text = f"🚨 {what}{where} — câmera {self._cam_name(cam_id)} ({self._stamp()})"
                shot_box, shot_frame = t["box"], frame
                if ev["zone_hit"] and not in_zone and ev.get("zone_shot"):
                    shot_box, shot_frame = ev["zone_shot"]
                clip = self.clip_para(cam_id, now, ev)
                if clip[0] and not ev.get("clip") and ev.get("sid"):
                    ev["clip"] = clip
                    try:
                        self.store.set_sighting_clip(ev["sid"], clip[0], clip[1])
                    except Exception:
                        pass
                self.emit(cam_id, "deteccao", label, text, self._snapshot(shot_frame, shot_box, label, color),
                          clip=clip)
                ev["alerted"] = True          # (em intervalo mínimo também não repete para este objeto)
                ev.pop("zone_shot", None)

        # objeto parado tempo demais
        if s.get("loiter_enabled") and zone_ok:
            if ev["still_c"] is None or math.hypot(cx - ev["still_c"][0], cy - ev["still_c"][1]) > 0.5 * bh:
                ev["still_c"], ev["still_since"] = (cx, cy), now
            elif not ev["loiter_sent"]:
                mins = max(0.5, float(s.get("loiter_minutes", 5)))
                if now - ev["still_since"] >= mins * 60:
                    ok, _why = allowed_by_rules(s, label, cam_id, dt)
                    ev["loiter_sent"] = True
                    if ok:
                        text = (f"⏱ {label if label != 'Desconhecido' else 'Objeto'} "
                                f"{STILL.get(label, 'parado')} há {fmt_duration(mins)} — "
                                f"câmera {self._cam_name(cam_id)} ({self._stamp()})")
                        self.emit(cam_id, "parado", label, text,
                                  self._snapshot(frame, t["box"], label, color),
                                  key=(cam_id, "parado", t["id"]), clip=self.clip_para(cam_id, now))

    def on_track_end(self, cam_id, t):
        ev = t.get("ev")
        if ev and ev.get("sid"):
            try:
                self.store.update_sighting(ev["sid"], ev.get("label") or t.get("label") or "Desconhecido", t.get("last", time.time()),
                                           ev.get("zone_hit"))
            except Exception:
                pass

    # ---- placas ----
    def on_plate(self, cam_id, ev, plate, image_bytes, label="Carro"):
        """Placa confirmada de um veículo. Grava no histórico e avisa se for o caso."""
        import placas as _pl
        if ev.get("sid"):
            self.store.set_sighting_plate(ev["sid"], plate)
        s = self.settings()
        conhecidas = _pl.parse_lista(s.get("plates_known"))
        vigiadas = _pl.parse_lista(s.get("plates_watch"))
        name = self._cam_name(cam_id)
        quando = self._stamp()
        if plate in vigiadas:
            apelido = f" ({vigiadas[plate]})" if vigiadas[plate] else ""
            text = f"🚨 Placa da lista de aviso: {plate}{apelido} — câmera {name} ({quando})"
        elif s.get("plates_alert_unknown") and plate not in conhecidas:
            text = f"🚗 Placa desconhecida: {plate} — câmera {name} ({quando})"
        else:
            self.log(f"[{name}] placa lida: {plate}"
                     + (f" ({conhecidas[plate]})" if conhecidas.get(plate) else ""))
            return
        self.emit(cam_id, "placa", label, text, image_bytes, key=(cam_id, "placa", plate),
                  clip=self.clip_para(cam_id, time.time(), ev))

    # ---- avisos do próprio sistema (não são de uma câmera) ----
    def system_alert(self, kind, text):
        self.emit(None, kind, None, text, None, respect_cooldown=False)

    # ---- eventos do sistema (offline, tampada...) ----
    def system_event(self, cam_id, kind, text, frame=None):
        s = self.settings()
        if kind in ("offline", "online") and not s.get("offline_enabled", True):
            return
        if kind in ("tampada", "posicao") and not s.get("tamper_enabled", True):
            return
        img = _jpeg(frame) if frame is not None else None
        self.emit(cam_id, kind, None, f"{text} ({self._stamp()})", img, respect_cooldown=False)

    # ---- limpeza ----
    def _housekeeping(self):
        while True:
            time.sleep(60)
            try:
                self.heat.flush()
            except Exception:
                pass
            if int(time.time()) % 3600 < 60:
                self.purge()

    def purge(self):
        try:
            days = max(1, int(self.settings().get("keep_days", 30)))
        except Exception:
            days = 30
        cutoff = time.time() - days * 86400
        cutoff_date = datetime.date.fromtimestamp(cutoff).isoformat()
        try:
            self.store.purge(cutoff)
            for d in os.listdir(self.events_dir):
                if d < cutoff_date:
                    shutil.rmtree(os.path.join(self.events_dir, d), ignore_errors=True)
            self.heat.purge(cutoff_date)
        except Exception as e:
            self.log(f"Limpeza do histórico falhou: {e}")


# ---------------------------------------------------------------------------
# Saúde das câmeras: offline, tampada/escura/embaçada, fora de posição
# ---------------------------------------------------------------------------
class HealthMonitor:
    COVER_S = 10          # condição precisa durar isso para avisar
    MOVED_S = 30
    MOVED_CORR = 0.35     # semelhança mínima com a imagem de referência

    def __init__(self, engine, list_cams, frame_source, ptz_recent, log):
        self.engine = engine
        self.list_cams = list_cams          # () -> [cam_id]
        self.frame_source = frame_source    # cam_id -> (connected, frame, idade_s)
        self.ptz_recent = ptz_recent        # cam_id -> bool (PTZ mexeu há pouco)
        self.log = log
        self.st = {}
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _structure(gray):
        g = cv2.GaussianBlur(cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA), (3, 3), 0).astype(np.float32)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
        m = cv2.magnitude(gx, gy)
        m -= m.mean()
        sd = m.std()
        return m / sd if sd > 1e-6 else m

    def _state(self, cam):
        return self.st.setdefault(cam, {
            "off_since": None, "off_sent": False,
            "cover_since": None, "cover_sent": False,
            "moved_since": None, "moved_sent": False,
            "ref": None, "ref_t": 0.0, "stable_since": None,
            "lap_base": None, "bw": None,
        })

    def _loop(self):
        while True:
            time.sleep(1.0)
            for cam in list(self.list_cams()):
                try:
                    self._check(cam)
                except Exception as e:
                    self.log(f"[{cam}] monitor de saúde: {e}")
            for cam in list(self.st):
                if cam not in self.list_cams():
                    self.st.pop(cam, None)

    def _check(self, cam):
        st = self._state(cam)
        now = time.time()
        s = self.engine.settings()
        name = self.engine._cam_name(cam)
        connected, frame, age = self.frame_source(cam)

        # ---- offline ----
        if not connected or frame is None or age > 20:
            if st["off_since"] is None:
                st["off_since"] = now
            mins = max(0.5, float(s.get("offline_minutes", 3)))
            if not st["off_sent"] and now - st["off_since"] >= mins * 60:
                st["off_sent"] = True
                self.engine.system_event(cam, "offline", f"📴 Câmera {name} está offline há {fmt_duration(mins)}")
            st["ref"] = None
            return
        if st["off_sent"]:
            self.engine.system_event(cam, "online", f"✅ Câmera {name} voltou a funcionar", frame)
        st["off_since"], st["off_sent"] = None, False

        # PTZ se mexendo: a imagem muda de propósito
        if self.ptz_recent(cam):
            st["ref"], st["stable_since"] = None, None
            st["moved_since"], st["cover_since"] = None, None
            return

        small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mean, std = float(gray.mean()), float(gray.std())
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        # ---- tampada / escura / embaçada ----
        dark = mean < 20 and std < 12
        uniform = std < 8
        blurry = st["lap_base"] is not None and st["lap_base"] > 20 and lap < 0.15 * st["lap_base"]
        covered = dark or uniform or blurry
        if covered:
            if st["cover_since"] is None:
                st["cover_since"] = now
            if not st["cover_sent"] and now - st["cover_since"] >= self.COVER_S:
                st["cover_sent"] = True
                why = "escura" if dark else ("tampada" if uniform else "embaçada / fora de foco")
                self.engine.system_event(cam, "tampada", f"⚠️ Câmera {name}: imagem {why}", frame)
            return
        st["cover_since"], st["cover_sent"] = None, False
        # trocou dia/noite (infravermelho)? recomeça as referências
        try:
            bw = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) < 18
        except Exception:
            bw = False
        if st["bw"] is not None and bw != st["bw"]:
            st["ref"], st["lap_base"], st["stable_since"] = None, None, None
        st["bw"] = bw
        st["lap_base"] = lap if st["lap_base"] is None else 0.98 * st["lap_base"] + 0.02 * lap

        # ---- fora de posição (a cena mudou e não voltou) ----
        cur = self._structure(gray)
        if st["ref"] is None:
            if st["stable_since"] is None:
                st["stable_since"] = now
            if now - st["stable_since"] >= 20:
                st["ref"], st["ref_t"] = cur, now
            return
        corr = float((cur * st["ref"]).mean())
        if corr < self.MOVED_CORR:
            if st["moved_since"] is None:
                st["moved_since"] = now
            if not st["moved_sent"] and now - st["moved_since"] >= self.MOVED_S:
                st["moved_sent"] = True
                self.engine.system_event(
                    cam, "posicao", f"⚠️ Câmera {name} parece ter sido movida (a imagem mudou)", frame)
                st["ref"], st["ref_t"] = cur, now      # nova posição vira a referência
        else:
            st["moved_since"], st["moved_sent"] = None, False
            if corr > 0.8 and now - st["ref_t"] > 600:
                st["ref"], st["ref_t"] = cur, now      # acompanha mudanças lentas (sombra, luz)


# ---------------------------------------------------------------------------
# Vigia do próprio Argos: avisa quando ele volta depois de ficar parado e quando
# o disco está quase cheio
# ---------------------------------------------------------------------------
class SystemWatch:
    BATIDA_S = 30          # grava "estou vivo" a cada 30 s

    def __init__(self, engine, base_dir, wa_ready, log):
        self.engine = engine
        self.log = log
        self.wa_ready = wa_ready                   # () -> bool (WhatsApp conectado)
        self.arq = os.path.join(base_dir, "media", "batimento.json")
        self.base_dir = base_dir
        self.disco_avisado = False
        self.inicio = time.time()
        self.ultima_parada = None                  # (de, até) da última parada detectada
        anterior = None
        try:
            with open(self.arq, "r", encoding="utf-8") as f:
                anterior = json.load(f)
        except Exception:
            pass
        self._bater()
        threading.Thread(target=self._loop, args=(anterior,), daemon=True, name="vigia").start()

    def _bater(self):
        try:
            tmp = self.arq + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"t": time.time(), "inicio": self.inicio}, f)
            os.replace(tmp, self.arq)
        except Exception:
            pass

    def disco(self):
        """(percentual usado, livre em GB, letra/raiz) do disco onde ficam as gravações."""
        u = shutil.disk_usage(os.path.join(self.base_dir, "media"))
        raiz = os.path.splitdrive(os.path.abspath(self.base_dir))[0] or "/"
        return 100.0 * u.used / u.total, u.free / 1024 ** 3, raiz

    def _parada(self, anterior):
        if not anterior or not anterior.get("t"):
            return
        de, ate = float(anterior["t"]), self.inicio
        self.ultima_parada = (de, ate)
        s = self.engine.settings()
        mins = (ate - de) / 60.0
        if not s.get("sys_down_enabled", True) or mins < max(0.5, float(s.get("sys_down_minutes", 2))):
            return
        # espera o WhatsApp conectar (até 3 min) para o aviso não se perder
        t0 = time.time()
        while time.time() - t0 < 180 and not self.wa_ready():
            time.sleep(3)
        f = lambda t: time.strftime("%H:%M", time.localtime(t))
        dia = time.strftime("%d/%m", time.localtime(de))
        texto = (f"⚠️ O Argos ficou parado das {f(de)} ({dia}) às {f(ate)} — {fmt_duration(mins)} sem vigiar. "
                 "Já voltou a funcionar.")
        self.engine.system_alert("sistema", texto)

    def _loop(self, anterior):
        try:
            self._parada(anterior)
        except Exception as e:
            self.log(f"Vigia: {e}")
        n = 0
        while True:
            self._bater()
            if n % 10 == 0:            # a cada 5 min
                try:
                    self._checa_disco()
                except Exception as e:
                    self.log(f"Vigia (disco): {e}")
            n += 1
            time.sleep(self.BATIDA_S)

    def _checa_disco(self):
        s = self.engine.settings()
        pct, livre, raiz = self.disco()
        lim = max(50.0, min(99.0, float(s.get("disk_percent", 90))))
        if pct >= lim and not self.disco_avisado:
            self.disco_avisado = True
            if s.get("disk_enabled", True):
                self.engine.system_alert(
                    "disco", f"💾 Disco {raiz} quase cheio: {pct:.0f}% ocupado, restam {livre:.1f} GB. "
                             f"O Argos guarda {int(float(s.get('keep_days', 30)))} dias de gravações.")
        elif pct < lim - 2:
            self.disco_avisado = False
