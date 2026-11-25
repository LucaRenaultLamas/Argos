"""
Argos — sistema local de câmeras, servidor Flask.

- Lê o RTSP de cada câmera com OpenCV (uma conexão por câmera/qualidade).
- Reentrega como MJPEG, que o navegador exibe num <img> comum.
- Controle PTZ via ONVIF (onvif_ptz.py).
- Varredura de rede e cadastro de câmeras (scanner.py).

Rode:  python app.py
Abra:  http://localhost:8080
"""
import os

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    # conexão rápida: menos tempo analisando o vídeo antes da 1ª imagem e sem buffer extra
    "rtsp_transport;tcp|stimeout;5000000|max_delay;500000|fflags;nobuffer|flags;low_delay"
    "|analyzeduration;1000000|probesize;500000",
)


def _fora_do_modo_economia():
    """O Windows 11 coloca processos sem janela no "modo de eficiência": eles vão para os
    núcleos mais lentos e o relógio fica menos preciso. Como o Argos.exe liga o servidor
    escondido, o servidor pede para ficar fora desse modo (vale só para ele)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _Throttle(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        # EXECUTION_SPEED (1) e IGNORE_TIMER_RESOLUTION (4) controlados por nós e desligados
        st = _Throttle(1, 0x1 | 0x4, 0)
        k = ctypes.windll.kernel32
        k.SetProcessInformation(k.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st))
    except Exception:
        pass


_fora_do_modo_economia()

import json
import sys
import time
import datetime
import re
import base64
import unicodedata
import threading
import socket
from concurrent.futures import ThreadPoolExecutor
import tempfile
import subprocess

import cv2

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = None

import logging
from collections import deque

# ---------------- Log em memória (cmd fica silencioso) ----------------
LOG_BUF = deque(maxlen=2000)
LOG_SEQ = {"n": 0}


class _BufHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        LOG_BUF.append(msg)
        LOG_SEQ["n"] += 1


_bufh = _BufHandler()
_bufh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%d/%m %H:%M:%S"))
_handlers = [_bufh]
# também grava em arquivo (sistema.log), para dar para investigar depois de uma queda
try:
    from logging.handlers import RotatingFileHandler
    _fileh = RotatingFileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sistema.log"),
                                 maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    _fileh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%d/%m %H:%M:%S"))
    _handlers.append(_fileh)
except Exception:
    pass
logging.basicConfig(level=logging.INFO, handlers=_handlers, force=True)

# quedas graves (do Python ou de bibliotecas nativas) vão para falhas.log
import faulthandler
import traceback
try:
    _crashf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "falhas.log"), "a", encoding="utf-8")
    faulthandler.enable(file=_crashf)
except Exception:
    _crashf = None


def _thread_excepthook(args):
    msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    logging.getLogger("sistema").error(f"Erro na thread {getattr(args.thread, 'name', '?')}:\n{msg}")


threading.excepthook = _thread_excepthook
logging.getLogger("werkzeug").setLevel(logging.WARNING)   # não loga cada requisição
_applog = logging.getLogger("sistema")


def log(msg):
    try:
        _applog.info(msg)
    except Exception:
        pass
from flask import Flask, Response, jsonify, request, send_from_directory

from onvif_ptz import PtzController, probe_stream
import scanner
import alerts
import clipes
import placas
try:
    import psutil
except Exception:
    psutil = None

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "cameras.json")
CONFIG_LOCK = threading.Lock()

MEDIA_DIR = os.path.join(BASE, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    """Grava cameras.json de forma atômica."""
    with CONFIG_LOCK:
        fd, tmp = tempfile.mkstemp(dir=BASE, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


BOOT_ID = str(time.time())  # muda a cada (re)início; o front usa para se recarregar sozinho

CONFIG = load_config()
SERVER = CONFIG.get("server", {})
JPEG_QUALITY = int(SERVER.get("jpeg_quality", 70))
GRID_FPS = int(SERVER.get("grid_fps", 15))

CAMERAS = {c["id"]: c for c in CONFIG["cameras"]}
PTZ = {c["id"]: PtzController(c) for c in CONFIG["cameras"] if c.get("ptz")}
PTZ_ACTIVITY = {}   # cam_id -> último instante em que o PTZ se mexeu
PATROL_MOVING = {}  # cam_id -> até quando a ronda está girando a câmera

# alertas, histórico, estatísticas e mapa de calor
ENGINE = alerts.AlertEngine(BASE, lambda: CONFIG, lambda cid: CAMERAS.get(cid), log)


def ptz_recent(cam_id, secs=2.5):
    return time.time() - PTZ_ACTIVITY.get(cam_id, 0) < secs


def safe_name(s):
    """Transforma o nome da câmera num texto seguro para nome de arquivo."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s or "camera"


def rtsp_url(cam, quality):
    path = cam["hd_path"] if quality == "hd" else cam["sd_path"]
    user = cam.get("user", "")
    pwd = cam.get("password", "")
    auth = f"{user}:{pwd}@" if user else ""
    return f"rtsp://{auth}{cam['host']}:{cam['rtsp_port']}{path}"


CAPTURE_TIMEOUT_MS = int(SERVER.get("capture_timeout_ms", 6000))


_CAP_LOCK = threading.Lock()


def _mk_capture(url, transport):
    """Abre o RTSP com um transporte específico (tcp ou udp). Troca a opção do
    ffmpeg só durante a abertura, protegida por lock (o OpenCV lê a variável de
    ambiente no momento de criar o VideoCapture)."""
    seg = int(CAPTURE_TIMEOUT_MS) * 1000  # microsegundos para o stimeout do ffmpeg
    opts = ("rtsp_transport;%s|stimeout;%d|max_delay;500000|fflags;nobuffer|flags;low_delay"
            % (transport, seg))
    with _CAP_LOCK:
        antigo = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
        try:
            try:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, CAPTURE_TIMEOUT_MS,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, CAPTURE_TIMEOUT_MS,
                ])
            except Exception:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        finally:
            if antigo is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = antigo
    return cap


def open_capture(url):
    """Abre o RTSP tentando TCP e, se não abrir, caindo para UDP (câmeras baratas
    que só falam RTP/UDP davam 'stream timeout' e ficavam sem imagem)."""
    for transporte in ("tcp", "udp"):
        cap = _mk_capture(url, transporte)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            cap.release()
    return _mk_capture(url, "tcp")


class CameraWorker:
    def __init__(self, cam, quality, pinned):
        self.cam = cam
        self.quality = quality
        self.pinned = pinned
        self.url = rtsp_url(cam, quality)
        self.jpeg = None
        self.jpeg_seq = -1        # quadro que gerou o jpeg em cache
        self.raw = None
        self.raw_t = 0.0
        self.seq = 0              # conta os quadros recebidos
        self.lock = threading.Lock()
        self.enc_lock = threading.Lock()
        self.running = True
        self.connected = False
        self.last_access = time.time()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            cap = open_capture(self.url)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                self.connected = False
                cap.release()
                time.sleep(2.0)
                continue
            if not self.connected:
                log(f"[{self.cam.get('id')}] {self.quality.upper()} conectada ({self.cam.get('name','')})")
            self.connected = True
            fail = 0
            while self.running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    fail += 1
                    if fail > 15:
                        break
                    time.sleep(0.05)
                    continue
                fail = 0
                # Só guarda o quadro. O JPEG é gerado sob demanda em get_jpeg(), apenas
                # quando alguém está assistindo (antes comprimia todos os quadros sempre).
                with self.lock:
                    self.raw = frame   # quadro limpo (gravação não leva as caixas)
                    self.raw_t = time.time()
                    self.seq += 1
            if self.connected:
                log(f"[{self.cam.get('id')}] {self.quality.upper()} desconectada")
            self.connected = False
            cap.release()
            time.sleep(1.0)

    def get_jpeg(self):
        """JPEG do quadro mais recente (com as caixas da detecção). Comprime no máximo
        uma vez por quadro, mesmo com várias telas assistindo."""
        self.last_access = time.time()
        with self.lock:
            frame, seq = self.raw, self.seq
        if frame is None:
            return None
        with self.enc_lock:
            if seq != self.jpeg_seq:
                disp = frame
                dets = _detections.get(self.cam.get("id")) if self.cam.get("detect", True) else None
                if dets:
                    disp = frame.copy()
                    draw_boxes(disp, dets)
                ok, buf = cv2.imencode(".jpg", disp, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                if ok:
                    self.jpeg, self.jpeg_seq = buf.tobytes(), seq
            return self.jpeg

    def get_jpeg_seq(self):
        """(jpeg, nº do quadro) — o stream usa para não reenviar a mesma imagem."""
        jpeg = self.get_jpeg()
        return jpeg, self.jpeg_seq

    def get_raw(self):
        self.last_access = time.time()
        with self.lock:
            return self.raw


class WorkerManager:
    def __init__(self):
        self.workers = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._janitor, daemon=True).start()

    def get(self, cam_id, quality):
        key = (cam_id, quality)
        with self.lock:
            w = self.workers.get(key)
            if w is None:
                cam = CAMERAS[cam_id]
                pinned = quality == "sd"
                w = CameraWorker(cam, quality, pinned).start()
                self.workers[key] = w
            return w

    def drop_camera(self, cam_id):
        with self.lock:
            for key in list(self.workers.keys()):
                if key[0] == cam_id:
                    self.workers[key].stop()
                    del self.workers[key]

    def _janitor(self):
        while True:
            time.sleep(10)
            now = time.time()
            with self.lock:
                for key, w in list(self.workers.items()):
                    if not w.pinned and now - w.last_access > 30:
                        w.stop()
                        del self.workers[key]


manager = WorkerManager()


# ---------------- Detecção de objetos (YOLO) + movimento ----------------
DETECT = {"model": None, "available": True, "lock": threading.Lock(), "ms": deque(maxlen=50)}
_detections = {}     # cam_id -> [ {box:[x1,y1,x2,y2] normalizado, label, color BGR} ]
detectors = {}       # cam_id -> DetectWorker

VEHICLE_CLASSES = {2, 5, 7}      # car, bus, truck (COCO)
MOTO_CLASS = 3                   # motorcycle
BIKE_CLASS = 1                   # bicycle
ANIMAL_CLASSES = {14, 15, 16, 17, 18, 19}   # bird, cat, dog, horse, sheep, cow
# Só estas classes do YOLO interessam. Qualquer outra (cadeira, vaso, banco,
# semáforo...) é ignorada, em vez de virar "Desconhecido".
TRACK_CLASSES = sorted({0, BIKE_CLASS, MOTO_CLASS} | VEHICLE_CLASSES | ANIMAL_CLASSES)

COL_PERSON = (0, 0, 255)         # vermelho
COL_CAR = (0, 255, 255)          # amarelo
COL_MOTO = (0, 165, 255)         # laranja
COL_ANIMAL = (255, 0, 255)       # magenta
COL_UNK = (0, 255, 0)            # verde
LABEL_COLORS = {"Pessoa": COL_PERSON, "Carro": COL_CAR, "Moto": COL_MOTO,
                "Bicicleta": COL_MOTO, "Animal": COL_ANIMAL, "Desconhecido": COL_UNK}

# ---- Ajustes da detecção ----
YOLO_CONF_MIN = 0.20      # confiança mínima para o YOLO considerar algo
YOLO_CONF_SHOW = 0.30     # a partir daqui a detecção do YOLO vale como "voto" de classe
YOLO_CONF_NIGHT = 0.45    # à noite (P&B) exige confiança maior: evita "carro"/"pessoa" fantasma
LABEL_HOLD_S = 0.7        # mantém a caixa na tela se o objeto "piscar" por um instante
TRACK_TTL_S = 2.5         # objeto reconhecido continua sendo acompanhado por esse tempo
MOTION_TTL_S = 0.6        # objeto só de movimento some se não for visto por esse tempo
SUPPRESS_MARGIN = 0.25    # margem extra em volta das caixas do YOLO (fração do tamanho da caixa)
UNK_MIN_AGE_S = 1.2       # movimento precisa durar isso antes de ser marcado
UNK_MIN_HITS = 6          # ...e aparecer em pelo menos tantos quadros
UNK_RECHECK_S = 0.5       # intervalo entre novas tentativas de classificar o recorte
LABEL_SWITCH_N = 3        # quadros seguidos exigidos para trocar a etiqueta de um objeto

# Velocidade medida em "alturas do próprio objeto por segundo" (não depende da distância).
# Pessoa andando ≈ 0,5–1,5; correndo ≈ 2–3; moto/carro na rua ≈ 4–10.
SPEED_WINDOW_S = 1.0      # janela usada para medir a velocidade
FAST_SPEED = 3.0          # acima disso não pode ser pessoa a pé
PERSON_MAX_SPEED = 2.5    # movimento sem classe só vira "Pessoa" abaixo disso

# Tamanho comparado ao de uma pessoa naquela mesma altura da imagem (aprendido sozinho
# a partir das pessoas que o YOLO reconhece com segurança). Área = largura×altura,
# em unidades de "altura de pessoa ao quadrado": pessoa ≈ 0,3; moto ≈ 0,5–1,2; carro ≥ 1,6.
CAR_MIN_AREA = 1.6
PERSON_MAX_AREA = 0.6
PERSON_MODEL_MIN = 15     # quantas pessoas precisa ver antes de usar o modelo de tamanho
# Sem o modelo de tamanho ainda: fração da área da imagem acima da qual um objeto rápido é carro
CAR_MIN_FRAME_FRAC = 0.03
# Sem movimento na imagem, o YOLO roda no quadro inteiro só a cada X segundos
YOLO_BOX_TRUST_S = 1.5   # por quanto tempo a caixa do YOLO vale mais que a do movimento
YOLO_IDLE_S = 0.5       # (menor que LABEL_HOLD_S, para a caixa de carro parado não piscar)


def _get_model():
    if DETECT["model"] is None and DETECT["available"]:
        try:
            from ultralytics import YOLO
            DETECT["model"] = YOLO("yolov8n.pt")
            log("Modelo de detecção (YOLO) carregado")
        except Exception as e:
            DETECT["available"] = False
            log(f"Detecção indisponível: {e}")
    return DETECT["model"]


def _classify(cls):
    if cls == 0:
        return "Pessoa"
    if cls == MOTO_CLASS:
        return "Moto"
    if cls == BIKE_CLASS:
        return "Bicicleta"
    if cls in VEHICLE_CLASSES:
        return "Carro"
    if cls in ANIMAL_CLASSES:
        return "Animal"
    return None


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _overlap_frac(a, b):
    """Fração da área de `a` que fica dentro de `b`."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area = (a[2] - a[0]) * (a[3] - a[1])
    return inter / area if area > 0 else 0.0


def _expand(b, frac, w, h):
    mx, my = (b[2] - b[0]) * frac, (b[3] - b[1]) * frac
    return (max(0.0, b[0] - mx), max(0.0, b[1] - my), min(float(w), b[2] + mx), min(float(h), b[3] + my))


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _yolo(model, img, imgsz):
    """Roda o YOLO só nas classes de interesse. Retorna [(x1,y1,x2,y2,cls,conf)]."""
    out = []
    with DETECT["lock"]:
        t0 = time.time()
        res = model.predict(img, imgsz=imgsz, conf=YOLO_CONF_MIN, classes=TRACK_CLASSES, verbose=False)
        DETECT["ms"].append((time.time() - t0) * 1000)
    for r in res:
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            out.append((x1, y1, x2, y2, int(b.cls[0]), float(b.conf[0])))
    return out


def _merge_riders(dets):
    """Quem está em cima de uma moto/bicicleta é o piloto, não uma 'Pessoa' a pé:
    junta a caixa do piloto com a do veículo."""
    two = [d for d in dets if d[4] in (MOTO_CLASS, BIKE_CLASS)]
    if not two:
        return dets
    out, riders = [], set()
    for i, p in enumerate(dets):
        if p[4] != 0:
            continue
        pcx = (p[0] + p[2]) / 2
        for m in two:
            mb = _expand(m[:4], 0.3, 1e9, 1e9)
            # metade de baixo da pessoa sobre o veículo e centro dentro da largura dele
            lower = (p[0], (p[1] + p[3]) / 2, p[2], p[3])
            if _overlap_frac(lower, mb) > 0.3 and m[0] - (m[2] - m[0]) * 0.2 <= pcx <= m[2] + (m[2] - m[0]) * 0.2:
                riders.add(i)
                break
    for i, d in enumerate(dets):
        if i in riders:
            continue
        if d[4] in (MOTO_CLASS, BIKE_CLASS):
            box = d[:4]
            for j in riders:
                p = dets[j]
                if _overlap_frac((p[0], (p[1] + p[3]) / 2, p[2], p[3]), _expand(box, 0.3, 1e9, 1e9)) > 0.3:
                    box = _union(box, p[:4])
            d = (*box, d[4], max(d[5], 0.0))
        out.append(d)
    return out


def draw_boxes(img, boxes):
    h, w = img.shape[:2]
    for b in boxes:
        x1, y1, x2, y2 = b["box"]
        x1, y1, x2, y2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
        color = tuple(b["color"])
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        label = b["label"]
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(img, label, (x1 + 3, max(10, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


FOLLOW_DEAD_X = 0.12      # o PTZ só se mexe se o objeto sair desta faixa do centro
FOLLOW_DEAD_Y = 0.15
FOLLOW_INTERVAL_S = 0.7   # intervalo mínimo entre correções
FOLLOW_STEP_S = 0.25      # duração de cada correção


class PtzFollower:
    """Manda pequenos movimentos ao PTZ sem travar a detecção."""

    def __init__(self):
        self.pending = {}
        self.cv = threading.Condition()
        threading.Thread(target=self._loop, daemon=True).start()

    def nudge(self, cam_id, pan, tilt):
        with self.cv:
            self.pending[cam_id] = (pan, tilt)
            self.cv.notify()

    def _loop(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                cam_id, (pan, tilt) = self.pending.popitem()
            ctrl = PTZ.get(cam_id)
            if not ctrl:
                continue
            try:
                PTZ_ACTIVITY[cam_id] = time.time()
                ctrl.continuous_move(pan, tilt, 0.0)
                time.sleep(FOLLOW_STEP_S)
                ctrl.stop()
            except Exception as e:
                log(f"[{cam_id}] PTZ seguir: {e}")
            PTZ_ACTIVITY[cam_id] = time.time()


FOLLOWER = PtzFollower()


class DetectWorker:
    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.running = True
        self.frames = 0
        self.bg = cv2.createBackgroundSubtractorMOG2(history=400, varThreshold=50, detectShadows=False)
        self.tracks = []
        self.next_id = 1
        self.person_samples = []   # (y_base/H, altura/H) de pessoas reconhecidas com segurança
        self.person_fit = None     # (a, b): altura_pessoa/H ≈ a * (y_base/H) + b
        self.follow_id = None      # objeto que o PTZ está seguindo
        self.follow_t = 0.0
        self.last_seq = -1         # último quadro processado
        self.yolo_t = 0.0          # última vez que o YOLO rodou no quadro inteiro
        threading.Thread(target=self._loop, daemon=True).start()

    # --- modelo de tamanho: altura esperada de uma pessoa em cada ponto da imagem ---
    def _learn_person(self, box, H, speed):
        if speed is not None and speed > 2.0:
            return
        self.person_samples.append(((box[3]) / H, (box[3] - box[1]) / H))
        if len(self.person_samples) > 300:
            self.person_samples = self.person_samples[-300:]
        if len(self.person_samples) >= PERSON_MODEL_MIN and len(self.person_samples) % 5 == 0:
            ys = [s[0] for s in self.person_samples]
            hs = [s[1] for s in self.person_samples]
            n = float(len(ys))
            my, mh = sum(ys) / n, sum(hs) / n
            vy = sum((y - my) ** 2 for y in ys)
            if vy > 1e-4:
                a = sum((y - my) * (hh - mh) for y, hh in zip(ys, hs)) / vy
                b = mh - a * my
            else:                       # pessoas sempre na mesma faixa: usa a média
                a, b = 0.0, mh
            self.person_fit = (a, b)

    def _expected_person_h(self, box, H):
        if not self.person_fit:
            return None
        a, b = self.person_fit
        eh = (a * (box[3] / H) + b) * H
        return eh if eh > 4 else None

    # --- velocidade em alturas do objeto por segundo ---
    def _speed(self, t, now):
        hist = [p for p in t["hist"] if now - p[0] <= SPEED_WINDOW_S]
        if len(hist) < 3 or hist[-1][0] - hist[0][0] < 0.35:
            return None
        (t0, x0, y0, _), (t1, x1, y1, _) = hist[0], hist[-1]
        hs = sorted(p[3] for p in hist)
        hmed = hs[len(hs) // 2]
        if hmed <= 0:
            return None
        return (((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5) / (t1 - t0) / hmed

    # --- decisão final: classe do YOLO corrigida por velocidade e tamanho ---
    def _decide(self, yolo_label, speed, box, W, H):
        w, h = max(1.0, box[2] - box[0]), max(1.0, box[3] - box[1])
        tall = h / w                                   # >1 = mais alto que largo
        fast = speed is not None and speed >= FAST_SPEED
        eh = self._expected_person_h(box, H)
        area = (w * h) / (eh * eh) if eh else None    # tamanho relativo a uma pessoa ali
        frame_frac = (w * h) / float(W * H)

        def vehicle_by_size():
            if area is not None:
                return "Carro" if area >= CAR_MIN_AREA else "Moto"
            return "Carro" if frame_frac >= CAR_MIN_FRAME_FRAC else "Moto"

        if yolo_label == "Pessoa":
            # pessoa a pé não anda tão rápido: é piloto de moto (ou carro)
            if fast:
                return vehicle_by_size()
            if area is not None and area >= CAR_MIN_AREA * 1.5:
                return "Carro"                          # grande demais para ser uma pessoa
            return "Pessoa"
        if yolo_label == "Bicicleta":
            return "Moto" if (speed is not None and speed >= FAST_SPEED * 1.6) else "Bicicleta"
        if yolo_label == "Carro":
            # "carro" pequeno e estreito andando rápido costuma ser moto
            if fast and area is not None and area < 0.9 and tall > 0.9:
                return "Moto"
            return "Carro"
        if yolo_label in ("Moto", "Animal"):
            if yolo_label == "Animal" and fast:
                return vehicle_by_size()
            return yolo_label

        # sem classe do YOLO: só movimento → decide por velocidade e tamanho
        if fast:
            return vehicle_by_size()
        if area is not None:
            if area >= CAR_MIN_AREA:
                return "Carro"
            if area <= PERSON_MAX_AREA and tall >= 1.3 and (speed is None or speed < PERSON_MAX_SPEED):
                return "Pessoa"
        else:
            # ainda sem modelo de tamanho: objeto grande, largo e em movimento é carro
            if frame_frac >= CAR_MIN_FRAME_FRAC and tall < 0.8 and speed is not None and speed >= 1.5:
                return "Carro"
            if tall >= 1.8 and speed is not None and speed < PERSON_MAX_SPEED:
                return "Pessoa"
        return None   # sem certeza

    # --- segunda chance: roda o YOLO só no recorte do movimento --------------
    def _classify_crop(self, model, frame, box):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in _expand(box, 0.35, w, h)]
        if x2 - x1 < 16 or y2 - y1 < 16:
            return None, 0.0, None
        try:
            dets = _merge_riders(_yolo(model, frame[y1:y2, x1:x2], 320))
        except Exception:
            return None, 0.0, None
        best, bconf, bbox = None, 0.0, None
        for d in dets:
            label = _classify(d[4])
            if label and d[5] > bconf:
                # caixa do objeto reconhecido, em coordenadas do quadro inteiro
                best, bconf, bbox = label, d[5], (d[0] + x1, d[1] + y1, d[2] + x1, d[3] + y1)
        return best, bconf, bbox

    # --- rastreamento ---------------------------------------------------------
    def _match(self, box, now, used):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        best, best_score = None, 0.0
        for t in self.tracks:
            if t["id"] in used:
                continue
            tb = t["box"]
            # posição prevista pela velocidade atual
            dt = now - t["last"]
            px = (tb[0] + tb[2]) / 2 + t["vx"] * dt
            py = (tb[1] + tb[3]) / 2 + t["vy"] * dt
            size = max(tb[2] - tb[0], tb[3] - tb[1], 1.0)
            dist = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
            score = max(_iou(tb, box), 1.0 - dist / (0.8 * size))
            if score > best_score:
                best, best_score = t, score
        return best if best_score > 0.2 else None

    def _update_track(self, t, box, now):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        ocx, ocy = (t["box"][0] + t["box"][2]) / 2, (t["box"][1] + t["box"][3]) / 2
        dt = now - t["last"]
        if 0 < dt < 1.0:
            t["vx"] = 0.6 * t["vx"] + 0.4 * (cx - ocx) / dt
            t["vy"] = 0.6 * t["vy"] + 0.4 * (cy - ocy) / dt
        t["box"] = box
        t["last"] = now
        t["hits"] += 1
        t["hist"].append((now, cx, cy, box[3] - box[1]))
        t["hist"] = [p for p in t["hist"] if now - p[0] <= SPEED_WINDOW_S * 2]

    def _new_track(self, box, now):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        t = {"id": self.next_id, "box": box, "first": now, "last": now, "hits": 1,
             "vx": 0.0, "vy": 0.0, "hist": [(now, cx, cy, box[3] - box[1])],
             "votes": {}, "yolo_last": 0.0, "label": None, "pending": None, "pending_n": 0,
             "checked": 0.0}
        self.next_id += 1
        self.tracks.append(t)
        return t

    def _set_label(self, t, label):
        """Troca a etiqueta só depois de alguns quadros seguidos (evita piscar)."""
        if label == t["label"]:
            t["pending"], t["pending_n"] = None, 0
            return
        if t["label"] is None or t["label"] == "Desconhecido":
            t["label"] = label
            t["pending"], t["pending_n"] = None, 0
            return
        if label == t["pending"]:
            t["pending_n"] += 1
        else:
            t["pending"], t["pending_n"] = label, 1
        if t["pending_n"] >= LABEL_SWITCH_N:
            t["label"] = label
            t["pending"], t["pending_n"] = None, 0

    def _loop(self):
        while self.running:
            if not CAMERAS.get(self.cam_id, {}).get("detect", True):
                _detections.pop(self.cam_id, None)
                self.tracks = []
                time.sleep(0.3)
                continue
            w = manager.workers.get((self.cam_id, "sd"))
            if w is None:
                time.sleep(0.2)
                continue
            with w.lock:
                frame, seq, ft = w.raw, w.seq, w.raw_t
            now = time.time()
            # só processa quadro NOVO e recente (câmera travada/offline não gasta CPU
            # rodando o YOLO de novo na mesma imagem)
            if frame is None or seq == self.last_seq or now - ft > 3.0:
                time.sleep(0.05)
                continue
            self.last_seq = seq
            h, wd = frame.shape[:2]
            model = _get_model()
            strong, weak = [], []

            # 1) movimento (barato): decide se vale rodar o YOLO neste quadro
            self.frames += 1
            fg = None
            moving_cam = False
            motion = True
            is_bw = False
            try:
                small = cv2.resize(frame, (wd // 2, h // 2))
                # detecta modo preto-e-branco (noturno/IR): saturação de cor muito baixa
                try:
                    sat = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
                except Exception:
                    sat = 255.0
                is_bw = sat < 18
                # em P&B há mais ruído → exige blocos maiores e mais sólidos
                min_frac = 0.012 if is_bw else 0.004
                fill_min = 0.45 if is_bw else 0.30
                open_it = 2 if is_bw else 1
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (5, 5), 0)
                moving_cam = ptz_recent(self.cam_id)
                fg = self.bg.apply(gray, learningRate=1.0 if moving_cam else -1)
                _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
                k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
                fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k, iterations=open_it)  # remove ruído fino
                fg = cv2.dilate(fg, k, iterations=2)                              # junta o objeto
                sh, sw = fg.shape[:2]
                frame_area = float(sw * sh)
                motion = cv2.countNonZero(fg) >= frame_area * min_frac * 0.5
            except Exception:
                fg = None

            # 2) YOLO no quadro inteiro — só quando algo se mexe, quando já há objetos
            #    sendo acompanhados, ou a cada YOLO_IDLE_S por garantia. Com a rua parada,
            #    isso corta a maior parte do uso de CPU.
            # (carro estacionado reconhecido não conta como "objeto ativo")
            active = any((t.get("ev") or {}).get("moved") or not t["votes"] for t in self.tracks)
            run_yolo = (motion or active or moving_cam or self.frames <= 25
                        or now - self.yolo_t >= YOLO_IDLE_S)
            if model is not None and run_yolo:
                self.yolo_t = now
                try:
                    thr_show = YOLO_CONF_NIGHT if is_bw else YOLO_CONF_SHOW
                    for d in _merge_riders(_yolo(model, frame, 480)):
                        if not _classify(d[4]):
                            continue
                        (strong if d[5] >= thr_show else weak).append(d)
                except Exception:
                    pass

            used = set()
            for d in strong:
                box, label, conf = d[:4], _classify(d[4]), d[5]
                t = self._match(box, now, used) or self._new_track(box, now)
                used.add(t["id"])
                self._update_track(t, box, now)
                for k in list(t["votes"]):
                    t["votes"][k] *= 0.85          # votos antigos perdem peso
                t["votes"][label] = t["votes"].get(label, 0.0) + conf
                t["yolo_last"] = now

            # manchas de movimento viram objetos
            if fg is not None and motion:
                try:
                    if self.frames > 25 and not moving_cam:
                        # mapa de calor (ignora quadros em que a imagem toda "mexeu")
                        try:
                            if float(fg.mean()) < 255 * 0.35:
                                ENGINE.heat.add(self.cam_id, fg, frame)
                        except Exception:
                            pass
                        # espera o fundo estabilizar antes de marcar
                        cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:6]
                        for c in cnts:
                            area = cv2.contourArea(c)
                            x, y, bw, bh = cv2.boundingRect(c)
                            box_area = float(bw * bh)
                            if box_area < frame_area * min_frac:     # pequeno demais (ruído)
                                continue
                            if box_area > frame_area * (0.15 if is_bw else 0.35):   # grande demais (luz/ruído de fundo)
                                continue
                            if box_area == 0 or area / box_area < fill_min:   # esparso demais (não é objeto sólido)
                                continue
                            box = (x * 2.0, y * 2.0, (x + bw) * 2.0, (y + bh) * 2.0)
                            # já explicado por uma detecção do YOLO neste quadro
                            if any(_overlap_frac(box, _expand(d[:4], SUPPRESS_MARGIN, wd, h)) > 0.2 for d in strong + weak):
                                continue
                            t = self._match(box, now, used)
                            if t is None:
                                t = self._new_track(box, now)
                            used.add(t["id"])
                            if t["votes"] and now - t["yolo_last"] < YOLO_BOX_TRUST_S:
                                # objeto já reconhecido pelo YOLO: a mancha de movimento (que à
                                # noite inclui farol e reflexo) não pode inflar a caixa dele
                                tb = t["box"]
                                tarea = max(1.0, (tb[2] - tb[0]) * (tb[3] - tb[1]))
                                if box_area * 4.0 > tarea * 1.8:   # box_area está na escala 1/2
                                    t["last"] = now                # só mantém o objeto vivo
                                    continue
                            self._update_track(t, box, now)
                except Exception:
                    pass

            # 3) limpa objetos que sumiram
            alive = []
            for t in self.tracks:
                if now - t["last"] <= (TRACK_TTL_S if t["votes"] else MOTION_TTL_S):
                    alive.append(t)
                else:
                    try:
                        ENGINE.on_track_end(self.cam_id, t)
                    except Exception:
                        pass
            self.tracks = alive

            # 4) decide a etiqueta de cada objeto
            # (ronda girando a câmera: tudo "se mexe" na imagem; não conta como detecção)
            em_ronda = PATROL_MOVING.get(self.cam_id, 0) > now
            boxes = []
            for t in self.tracks:
                if now - t["last"] > LABEL_HOLD_S:
                    continue
                speed = self._speed(t, now)
                yolo_label = max(t["votes"], key=t["votes"].get) if t["votes"] else None

                if yolo_label is None:
                    # só movimento: precisa durar antes de ser marcado
                    if now - t["first"] < UNK_MIN_AGE_S or t["hits"] < UNK_MIN_HITS:
                        continue
                    # segunda chance: tenta reconhecer o recorte ampliado
                    if model is not None and now - t["checked"] >= UNK_RECHECK_S:
                        t["checked"] = now
                        lbl, conf, cbox = self._classify_crop(model, frame, t["box"])
                        if lbl and conf < (YOLO_CONF_NIGHT if is_bw else YOLO_CONF_SHOW):
                            lbl = None    # à noite, recorte com confiança fraca não vira alerta
                        if lbl:
                            t["votes"][lbl] = t["votes"].get(lbl, 0.0) + max(conf, YOLO_CONF_SHOW)
                            yolo_label = lbl
                            # a mancha de movimento costuma ser bem maior que o objeto (farol,
                            # sombra, reflexo): passa a usar a caixa justa que o YOLO achou
                            if cbox is not None:
                                t["box"], t["yolo_last"] = cbox, now

                if yolo_label is None and is_bw:
                    # à noite (P&B) farol e reflexo se mexem como carro: sem confirmação
                    # do YOLO, fica "Desconhecido" (que não dispara alerta por padrão)
                    label = "Desconhecido"
                else:
                    label = self._decide(yolo_label, speed, t["box"], wd, h) or "Desconhecido"
                self._set_label(t, label)

                # aprende o tamanho de uma pessoa com detecções seguras e lentas
                if yolo_label == "Pessoa" and t["label"] == "Pessoa" and t["yolo_last"] == now \
                        and t["votes"].get("Pessoa", 0) >= 1.0:
                    self._learn_person(t["box"], h, speed)

                X1, Y1, X2, Y2 = t["box"]
                lbl = t["label"] or label
                boxes.append({"box": [X1/wd, Y1/h, X2/wd, Y2/h], "label": lbl,
                              "color": LABEL_COLORS.get(lbl, COL_UNK)})
                try:
                    if lbl and not em_ronda:
                        ENGINE.on_track(self.cam_id, t, lbl, frame, now, LABEL_COLORS.get(lbl, COL_UNK))
                except Exception as e:
                    log(f"[{self.cam_id}] alerta: {e}")
            _detections[self.cam_id] = boxes
            if em_ronda:
                self.tracks = []          # nova vista: recomeça o acompanhamento
            try:
                self._follow(now, wd, h)
            except Exception as e:
                log(f"[{self.cam_id}] seguir objeto: {e}")
            time.sleep(0.08)

    # --- PTZ segue o objeto ---------------------------------------------------
    def _follow(self, now, W, H):
        cam = CAMERAS.get(self.cam_id, {})
        if not cam.get("follow") or self.cam_id not in PTZ:
            self.follow_id = None
            return
        types = set(ENGINE.settings().get("follow_types") or [])
        cands = [t for t in self.tracks if now - t["last"] <= 0.3 and t.get("label") in types
                 and (t.get("ev") or {}).get("moved")]
        if not cands:
            self.follow_id = None
            return
        tgt = next((t for t in cands if t["id"] == self.follow_id), None)
        if tgt is None:
            tgt = max(cands, key=lambda t: (t["box"][2] - t["box"][0]) * (t["box"][3] - t["box"][1]))
            self.follow_id = tgt["id"]
        if now - self.follow_t < FOLLOW_INTERVAL_S:
            return
        x1, y1, x2, y2 = tgt["box"]
        ex = (x1 + x2) / 2 / W - 0.5          # >0: objeto à direita
        ey = (y1 + y2) / 2 / H - 0.5          # >0: objeto embaixo
        pan = tilt = 0.0
        if abs(ex) > FOLLOW_DEAD_X:
            pan = max(-0.6, min(0.6, ex * 1.4))
            pan = pan if abs(pan) >= 0.15 else 0.15 * (1 if pan > 0 else -1)
        if abs(ey) > FOLLOW_DEAD_Y:
            tilt = max(-0.6, min(0.6, -ey * 1.4))
            tilt = tilt if abs(tilt) >= 0.15 else 0.15 * (1 if tilt > 0 else -1)
        if cam.get("follow_invert"):
            pan, tilt = -pan, -tilt
        if pan or tilt:
            self.follow_t = now
            FOLLOWER.nudge(self.cam_id, pan, tilt)


def start_detector(cam_id):
    if cam_id not in detectors:
        detectors[cam_id] = DetectWorker(cam_id)


def stop_detector(cam_id):
    d = detectors.pop(cam_id, None)
    if d:
        d.running = False
    _detections.pop(cam_id, None)


for _cid in CAMERAS:
    manager.get(_cid, "sd")
    start_detector(_cid)


def _health_frame(cam_id):
    w = manager.workers.get((cam_id, "sd"))
    if w is None:
        return False, None, 9999
    with w.lock:
        frame, t = w.raw, w.raw_t
    return bool(w.connected), frame, (time.time() - t) if t else 9999


HEALTH = alerts.HealthMonitor(ENGINE, lambda: list(CAMERAS.keys()), _health_frame, ptz_recent, log)


# ---------------- Clipes de vídeo (detecções e alertas) ----------------
def _sd_frame(cam_id):
    w = manager.workers.get((cam_id, "sd"))
    if w is None:
        return None, None, 0
    with w.lock:
        return w.raw, w.seq, w.raw_t


CLIPS = clipes.ClipManager(ENGINE.events_dir, FFMPEG_BIN, _sd_frame, lambda: list(CAMERAS.keys()), log)
ENGINE.clips = CLIPS


# ---------------- Leitura de placas ----------------
LEITOR = placas.LeitorPlacas(os.path.join(BASE, "modelos"), log)
PLATE_TRY_S = 0.6        # intervalo entre tentativas de ler a placa do mesmo veículo
PLATE_MAX_TRIES = 15     # tentativas por veículo


class PlateWorker:
    """Para as câmeras com "Ler placas" ligado: pega o quadro HD, recorta cada carro/moto
    que está se mexendo e tenta ler a placa. A mesma placa precisa ser lida mais de uma
    vez (ou com confiança alta) para valer."""

    def __init__(self):
        self.lidas = 0
        threading.Thread(target=self._loop, daemon=True, name="placas").start()

    def _loop(self):
        while True:
            time.sleep(0.25)
            cams = [cid for cid, c in list(CAMERAS.items()) if c.get("plates")]
            if not cams:
                continue
            if not LEITOR.pronto():
                time.sleep(2)
                continue
            for cid in cams:
                try:
                    self._camera(cid)
                except Exception as e:
                    log(f"[{cid}] placas: {e}")
                    time.sleep(1)

    def _camera(self, cid):
        det = detectors.get(cid)
        sd = manager.workers.get((cid, "sd"))
        if det is None or sd is None:
            return
        hd = manager.get(cid, "hd")              # mantém o HD ligado nesta câmera
        hd_frame = hd.get_raw()
        with sd.lock:
            sd_frame = sd.raw
        if sd_frame is None:
            return
        now = time.time()
        for t in list(det.tracks):
            ev = t.get("ev")
            if not ev or not ev.get("moved") or t.get("label") not in ("Carro", "Moto"):
                continue
            if now - t["last"] > 0.4 or ev.get("plate_done") or ev.get("plate_n", 0) >= PLATE_MAX_TRIES:
                continue
            if now - ev.get("plate_t", 0) < PLATE_TRY_S:
                continue
            ev["plate_t"] = now
            ev["plate_n"] = ev.get("plate_n", 0) + 1
            usa_hd = hd_frame is not None and now - (hd.raw_t or 0) < 2.0
            src = hd_frame if usa_hd else sd_frame
            sx, sy = src.shape[1] / sd_frame.shape[1], src.shape[0] / sd_frame.shape[0]
            x1, y1, x2, y2 = t["box"]
            w, h = x2 - x1, y2 - y1
            X1, Y1 = max(0, int((x1 - w * 0.25) * sx)), max(0, int((y1 - h * 0.15) * sy))
            X2, Y2 = min(src.shape[1], int((x2 + w * 0.25) * sx)), min(src.shape[0], int((y2 + h * 0.25) * sy))
            if X2 - X1 < 60 or Y2 - Y1 < 40:
                continue
            rec = src[Y1:Y2, X1:X2]
            r = LEITOR.ler_veiculo(rec)
            if not r or r[1] < 0.35:
                continue
            txt, conf, (px1, py1, px2, py2) = r
            votos = ev.setdefault("plate_votes", {})
            votos[txt] = votos.get(txt, 0.0) + conf
            best = max(votos, key=votos.get)
            if votos[best] >= 1.2 or conf >= 0.9:
                ev["plate_done"], ev["plate"] = True, best
                self.lidas += 1
                img = rec.copy()
                cv2.rectangle(img, (px1, py1), (px2, py2), (0, 255, 255), 2)
                cv2.putText(img, best, (px1, max(18, py1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 255, 255), 2, cv2.LINE_AA)
                ENGINE.on_plate(cid, ev, best, alerts._jpeg(img), t.get("label") or "Carro")


PLATES = PlateWorker()


# ---------------- Posições salvas e ronda (PTZ) ----------------
PRESETS = {}   # cam_id -> (instante, [ {token, name} ])


def presets_de(cam_id, fresco=False):
    ctrl = PTZ.get(cam_id)
    if not ctrl:
        return []
    c = PRESETS.get(cam_id)
    if c and not fresco and time.time() - c[0] < 300:
        return c[1]
    lista = ctrl.get_presets()
    nomes = CAMERAS.get(cam_id, {}).get("preset_names") or {}
    for p in lista:
        p["name"] = nomes.get(p["token"]) or p["name"] or f"Posição {p['token']}"
    PRESETS[cam_id] = (time.time(), lista)
    return lista


class Patrol:
    """Ronda: gira a câmera entre as posições salvas. Pausa quando detecta algo
    (pessoa, carro…), quando alguém mexe no PTZ ou quando o "seguir objeto" age."""
    PAUSA_DETECCAO_S = 15
    PAUSA_MANUAL_S = 60

    def __init__(self):
        self.st = {}
        threading.Thread(target=self._loop, daemon=True, name="ronda").start()

    def estado(self, cid):
        st = self.st.get(cid)
        p = (CAMERAS.get(cid) or {}).get("patrol") or {}
        if not p.get("enabled") or not st:
            return {"ativa": False}
        now = time.time()
        if now < st["pausa_ate"]:
            return {"ativa": True, "pausada": True, "motivo": st["motivo"],
                    "volta_em": round(st["pausa_ate"] - now)}
        return {"ativa": True, "pausada": False, "motivo": st.get("motivo_fixo", ""),
                "posicao": st.get("nome", "")}

    def _ocupada(self, cid, now):
        det = detectors.get(cid)
        if det is None:
            return None
        for t in list(det.tracks):
            lb = t.get("label")
            if lb and lb != "Desconhecido" and (t.get("ev") or {}).get("moved") and now - t["last"] < 1.0:
                return lb
        return None

    def _loop(self):
        while True:
            time.sleep(1.0)
            now = time.time()
            for cid, cam in list(CAMERAS.items()):
                p = cam.get("patrol") or {}
                if not p.get("enabled") or cid not in PTZ:
                    self.st.pop(cid, None)
                    continue
                st = self.st.get(cid)
                if st is None:      # ronda acabou de ligar: movimentos anteriores não contam
                    st = self.st[cid] = {"idx": -1, "prox": 0.0, "propria": PTZ_ACTIVITY.get(cid, 0.0),
                                         "pausa_ate": 0.0, "motivo": ""}
                act = PTZ_ACTIVITY.get(cid, 0.0)
                if act > st["propria"] + 0.5:                 # mexeram no PTZ (manual/seguir)
                    st["propria"] = act
                    ate = max(act, now) + self.PAUSA_MANUAL_S
                    if ate > st["pausa_ate"]:
                        st["pausa_ate"], st["motivo"] = ate, "o PTZ foi movido"
                lb = self._ocupada(cid, now)
                if lb and now + self.PAUSA_DETECCAO_S >= st["pausa_ate"]:
                    st["pausa_ate"], st["motivo"] = now + self.PAUSA_DETECCAO_S, f"detectou {lb}"
                if now < st["pausa_ate"]:
                    st["prox"] = max(st["prox"], st["pausa_ate"])
                    continue
                if now < st["prox"]:
                    continue
                try:
                    todos = presets_de(cid)
                except Exception as e:
                    st["motivo_fixo"] = f"não consegui ler as posições: {e}"
                    st["prox"] = now + 60
                    continue
                escolhidos = [x for x in todos if not p.get("tokens") or x["token"] in p["tokens"]]
                if len(escolhidos) < 2:
                    st["motivo_fixo"] = "precisa de pelo menos 2 posições salvas"
                    st["prox"] = now + 30
                    continue
                st["motivo_fixo"] = ""
                st["idx"] = (st["idx"] + 1) % len(escolhidos)
                alvo = escolhidos[st["idx"]]
                try:
                    PTZ_ACTIVITY[cid] = time.time() + 4      # detecção e saúde: imagem mudando
                    PATROL_MOVING[cid] = time.time() + 5
                    st["propria"] = PTZ_ACTIVITY[cid]
                    PTZ[cid].goto_preset(alvo["token"])
                    st["nome"] = alvo["name"]
                except Exception as e:
                    st["motivo_fixo"] = f"a câmera recusou: {e}"
                st["prox"] = time.time() + max(5, int(p.get("dwell_s", 20) or 20))


PATROL = Patrol()


# ---------------- Vigia do sistema e diagnóstico ----------------
def _wa_pronto():
    """Pode mandar o aviso de parada? Espera só enquanto o WhatsApp ainda está conectando."""
    s = ENGINE.settings()
    return s.get("channel") == "telegram" or ENGINE.wa.state not in ("iniciando", "conectando")


WATCH = alerts.SystemWatch(ENGINE, BASE, _wa_pronto, log)
STARTED = time.time()


class Diag:
    """Mede de tempos em tempos o que a tela de diagnóstico mostra."""

    def __init__(self):
        self.prev = {}
        self.fps = {}
        self.det_fps = {}
        self.cpu_proc = None
        self.cpu_sys = None
        self.media_gb = None
        self.media_t = 0.0
        self.proc = psutil.Process() if psutil else None
        if self.proc:
            try:
                self.proc.cpu_percent(None)
                psutil.cpu_percent(None)
            except Exception:
                pass
        threading.Thread(target=self._loop, daemon=True, name="diagnostico").start()

    def _tamanho_media(self):
        total = 0
        for raiz, _d, arqs in os.walk(os.path.join(BASE, "media")):
            for a in arqs:
                try:
                    total += os.path.getsize(os.path.join(raiz, a))
                except OSError:
                    pass
        return total / 1024 ** 3

    def _loop(self):
        while True:
            time.sleep(2.0)
            now = time.time()
            try:
                for key, w in list(manager.workers.items()):
                    p = self.prev.get(key)
                    if p:
                        self.fps[key] = max(0.0, (w.seq - p[1]) / max(0.5, now - p[0]))
                    self.prev[key] = (now, w.seq)
                for cid, d in list(detectors.items()):
                    p = self.prev.get(("det", cid))
                    if p:
                        self.det_fps[cid] = max(0.0, (d.frames - p[1]) / max(0.5, now - p[0]))
                    self.prev[("det", cid)] = (now, d.frames)
                if self.proc:
                    self.cpu_proc = self.proc.cpu_percent(None) / (psutil.cpu_count() or 1)
                    self.cpu_sys = psutil.cpu_percent(None)
                if now - self.media_t > 180:
                    self.media_t = now
                    self.media_gb = self._tamanho_media()
            except Exception as e:
                log(f"Diagnóstico: {e}")


DIAG = Diag()


class _CVRecorder:
    """Gravação de reserva via OpenCV (usada só se o ffmpeg não estiver disponível)."""

    def __init__(self, worker, path, fps=15):
        self.worker = worker
        self.path = path
        self.fps = fps
        self._run = True
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    @property
    def running(self):
        return self._run

    def stop(self):
        self._run = False
        self.thread.join(timeout=5)

    def _loop(self):
        writer = None
        interval = 1.0 / self.fps
        for _ in range(100):
            if self.worker.get_raw() is not None:
                break
            time.sleep(0.1)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        next_t = time.time()
        while self._run:
            frame = self.worker.get_raw()
            if frame is not None:
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(self.path, fourcc, self.fps, (w, h))
                writer.write(frame)
            next_t += interval
            dt = next_t - time.time()
            time.sleep(dt if dt > 0 else 0)
            if dt <= 0:
                next_t = time.time()
        if writer is not None:
            writer.release()


class Recorder:
    """
    Grava a câmera em .mp4. Preferência: ffmpeg com cópia direta do H.264 (sem recomprimir),
    o que gera um arquivo tocável no navegador e com baixo uso de CPU.
    Se o ffmpeg não estiver disponível, cai no gravador OpenCV.
    """

    def __init__(self, cam_id, cam, path):
        self.cam_id = cam_id
        self.path = path
        self.proc = None
        self.cv = None
        url = rtsp_url(cam, "hd")
        self.logpath = path + ".log"
        if FFMPEG_BIN:
            # vídeo em cópia direta (sem recomprimir); áudio convertido p/ AAC
            # porque estas câmeras mandam áudio PCM mu-law, que não cabe em .mp4
            args = [
                FFMPEG_BIN, "-loglevel", "warning",
                "-rtsp_transport", "tcp", "-i", url,
                "-c:v", "copy", "-c:a", "aac",
                "-movflags", "+faststart",
                "-y", path,
            ]
            self._log = open(self.logpath, "w")
            self._log.write("FFMPEG=%s\nURL=%s\n\n" % (FFMPEG_BIN, url))
            self._log.flush()
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE,
                stdout=self._log, stderr=self._log)
        else:
            with open(self.logpath, "w") as f:
                f.write("FFMPEG_BIN=None -> usando OpenCV\n")
            self.cv = _CVRecorder(manager.get(cam_id, "hd"), path).start()

    def start(self):
        return self

    @property
    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        return self.cv is not None and self.cv.running

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.communicate(input=b"q", timeout=6)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        elif self.cv is not None:
            self.cv.stop()


recorders = {}  # cam_id -> Recorder
recorders_lock = threading.Lock()
finalizing = set()  # arquivos ainda sendo finalizados pelo ffmpeg
finalizing_lock = threading.Lock()

app = Flask(__name__, static_folder=None)


# ---------------- Segurança ----------------
# 1) Bloqueia pedidos que alteram algo vindos de OUTRO site (CSRF): sem isto,
#    qualquer página aberta no navegador poderia apagar câmeras, vídeos etc.
# 2) Senha de acesso opcional (server.password no cameras.json): exigida para
#    quem acessa de outro aparelho da rede. Neste próprio PC (localhost) não pede.
import hmac
from urllib.parse import urlparse

_LOCAL_ADDRS = {"127.0.0.1", "::1"}
TOKEN_MASK = "********"


def _access_password():
    return str(os.environ.get("ARGOS_PASSWORD") or SERVER.get("password") or "")


@app.before_request
def _security_gate():
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify({"ok": False, "error": "pedido de outro site bloqueado"}), 403
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({"ok": False, "error": "pedido de outro site bloqueado"}), 403
    pwd = _access_password()
    if pwd and request.remote_addr not in _LOCAL_ADDRS:
        a = request.authorization
        given = (a.password or "") if a else ""
        if not hmac.compare_digest(given.encode("utf-8"), pwd.encode("utf-8")):
            return Response("Acesso restrito ao Argos", 401,
                            {"WWW-Authenticate": 'Basic realm="Argos", charset="UTF-8"'})


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.route("/api/cameras")
def api_cameras():
    out = []
    for c in CONFIG["cameras"]:
        out.append({
            "id": c["id"], "name": c["name"], "host": c["host"],
            "ptz": bool(c.get("ptz")),
            "detect": c.get("detect", True),
        })
    return jsonify(out)


def mjpeg_generator(cam_id, quality):
    worker = manager.get(cam_id, quality)
    interval = 1.0 / max(1, GRID_FPS)
    last_seq, last_sent = None, 0.0
    while True:
        t0 = time.time()
        if cam_id not in CAMERAS:
            return  # câmera excluída: encerra o stream e libera a conexão
        jpeg, seq = worker.get_jpeg_seq()
        if jpeg is None and quality == "hd":
            # HD ainda conectando (só liga quando alguém pede): enquanto não chega o
            # 1º quadro HD, mostra o SD, para a imagem não sumir ao clicar em HD
            try:
                sd_jpeg, sd_seq = manager.get(cam_id, "sd").get_jpeg_seq()
                if sd_jpeg is not None:
                    jpeg, seq = sd_jpeg, ("sd", sd_seq)
            except Exception:
                pass
        if jpeg is None:
            # câmera offline/sem quadro ainda: a cada 2 s manda uma linha em branco
            # (ignorada antes do 1º quadro). Sem isso o navegador fica com a conexão
            # "pendurada" para sempre, e com várias câmeras offline as 6 conexões
            # do navegador se esgotam e botões como Excluir param de responder.
            if t0 - last_sent >= 2.0:
                last_sent = t0
                yield b"\r\n"
            time.sleep(min(0.1, interval))
            continue
        # só manda quando chegou quadro novo (câmera travada não gasta rede);
        # a cada 2 s reenvia o último para manter a conexão viva
        if seq == last_seq and t0 - last_sent < 2.0:
            time.sleep(min(0.1, interval))
            continue
        last_seq, last_sent = seq, t0
        # um único pedaço por quadro (antes eram 3 envios separados)
        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
               + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
        dt = interval - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)


@app.route("/stream/<cam_id>")
def stream(cam_id):
    if cam_id not in CAMERAS:
        return "camera desconhecida", 404
    quality = "hd" if request.args.get("q") == "hd" else "sd"
    return Response(
        mjpeg_generator(cam_id, quality),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/status/<cam_id>")
def status(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"error": "desconhecida"}), 404
    w = manager.workers.get((cam_id, "sd"))
    rec = recorders.get(cam_id)
    return jsonify({
        "connected": bool(w and w.connected),
        "recording": bool(rec and rec.running),
    })


# ---------------- Captura de imagem ----------------
@app.route("/api/capture/<cam_id>", methods=["POST"])
def api_capture(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "desconhecida"}), 404
    quality = "hd" if (request.args.get("q") == "hd") else "sd"
    worker = manager.get(cam_id, quality)
    jpeg = None
    for _ in range(50):
        jpeg = worker.get_jpeg()
        if jpeg:
            break
        time.sleep(0.1)
    if not jpeg:
        return jsonify({"ok": False, "error": "sem imagem"}), 503
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    fname = f"{safe_name(CAMERAS[cam_id]['name'])}_{ts}.jpg"
    with open(os.path.join(IMAGES_DIR, fname), "wb") as f:
        f.write(jpeg)
    return jsonify({"ok": True, "file": fname, "url": f"/media/images/{fname}"})


# ---------------- Gravação de vídeo ----------------
@app.route("/api/record/<cam_id>/start", methods=["POST"])
def api_record_start(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "desconhecida"}), 404
    with recorders_lock:
        if cam_id in recorders and recorders[cam_id].running:
            return jsonify({"ok": False, "error": "já está gravando"}), 400
        cam = CAMERAS[cam_id]
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{safe_name(cam['name'])}_{ts}.mp4"
        path = os.path.join(VIDEOS_DIR, fname)
        rec = Recorder(cam_id, cam, path).start()
        recorders[cam_id] = rec
    log(f"[{cam_id}] gravação iniciada: {fname}")
    return jsonify({"ok": True, "file": fname})


@app.route("/api/record/<cam_id>/stop", methods=["POST"])
def api_record_stop(cam_id):
    # remove o gravador na hora (o status já passa a "não gravando" imediatamente)
    with recorders_lock:
        rec = recorders.pop(cam_id, None)
    if not rec:
        return jsonify({"ok": False, "error": "não está gravando"}), 400
    fname = os.path.basename(rec.path)
    with finalizing_lock:
        finalizing.add(fname)
    # finaliza o .mp4 em segundo plano para não travar a resposta
    def _finalize():
        try:
            rec.stop()
        finally:
            with finalizing_lock:
                finalizing.discard(fname)
    threading.Thread(target=_finalize, daemon=True).start()
    log(f"[{cam_id}] gravação encerrada: {fname}")
    return jsonify({"ok": True, "file": fname, "url": f"/media/videos/{fname}"})


# ---------------- Biblioteca de mídia ----------------
def _list_media(folder, url_prefix, exts, min_size=1):
    items = []
    for name in os.listdir(folder):
        if os.path.splitext(name)[1].lower() not in exts:
            continue
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            st = os.stat(p)
            if st.st_size < min_size:
                continue
            items.append({
                "name": name,
                "url": f"{url_prefix}/{name}",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


@app.route("/api/media/images")
def api_media_images():
    return jsonify(_list_media(IMAGES_DIR, "/media/images", {".jpg", ".jpeg", ".png"}))


@app.route("/api/media/videos")
def api_media_videos():
    # só .mp4 com conteúdo; não lista o que ainda grava nem o que finaliza
    active = {os.path.basename(r.path) for r in recorders.values() if r.running}
    with finalizing_lock:
        active |= set(finalizing)
    return jsonify([v for v in _list_media(VIDEOS_DIR, "/media/videos", {".mp4"}, min_size=1024)
                    if v["name"] not in active])


@app.route("/api/video/trim", methods=["POST"])
def api_video_trim():
    """Recorta um trecho do vídeo (início→fim) e salva um novo .mp4."""
    d = request.get_json(force=True, silent=True) or {}
    name = os.path.basename(d.get("name", ""))
    src = os.path.join(VIDEOS_DIR, name)
    if not name or not os.path.isfile(src):
        return jsonify({"ok": False, "error": "vídeo não encontrado"}), 404
    try:
        start = max(0.0, float(d.get("start", 0)))
        end = float(d.get("end", 0))
    except Exception:
        return jsonify({"ok": False, "error": "tempos inválidos"}), 400
    if end <= start:
        return jsonify({"ok": False, "error": "o fim deve ser depois do início"}), 400
    if not FFMPEG_BIN:
        return jsonify({"ok": False, "error": "ffmpeg indisponível"}), 500
    overwrite = bool(d.get("overwrite"))
    base = os.path.splitext(name)[0]

    if overwrite:
        # grava num temporário e substitui o original (ffmpeg não grava no mesmo arquivo que lê)
        tmp = os.path.join(VIDEOS_DIR, f"{base}__tmp_{int(time.time())}.mp4")
        args = [FFMPEG_BIN, "-y", "-ss", str(start), "-to", str(end), "-i", src,
                "-c", "copy", "-movflags", "+faststart", tmp]
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if not (os.path.isfile(tmp) and os.path.getsize(tmp) > 1024):
            return jsonify({"ok": False, "error": "falha ao recortar"}), 500
        try:
            os.replace(tmp, src)
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return jsonify({"ok": False, "error": f"não foi possível substituir: {e}"}), 500
        return jsonify({"ok": True, "file": name, "url": f"/media/videos/{name}", "overwrote": True})

    out = f"{base}_corte_{time.strftime('%H-%M-%S')}.mp4"
    outp = os.path.join(VIDEOS_DIR, out)
    args = [FFMPEG_BIN, "-y", "-ss", str(start), "-to", str(end),
            "-i", src, "-c", "copy", "-movflags", "+faststart", outp]
    try:
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if os.path.isfile(outp) and os.path.getsize(outp) > 1024:
        return jsonify({"ok": True, "file": out, "url": f"/media/videos/{out}"})
    return jsonify({"ok": False, "error": "falha ao recortar"}), 500


@app.route("/api/save_image", methods=["POST"])
def api_save_image():
    """Salva uma imagem enviada pelo navegador (quadro com desenho) na galeria."""
    d = request.get_json(force=True, silent=True) or {}
    data = d.get("dataUrl", "")
    m = re.match(r"data:image/(\w+);base64,(.+)", data, re.S)
    if not m:
        return jsonify({"ok": False, "error": "imagem inválida"}), 400
    ext = "png" if m.group(1).lower() == "png" else "jpg"
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return jsonify({"ok": False, "error": "base64 inválido"}), 400
    base = safe_name(d.get("name", "")) or ("anotacao_" + time.strftime("%Y-%m-%d_%H-%M-%S"))
    fname = f"{base}.{ext}"
    with open(os.path.join(IMAGES_DIR, fname), "wb") as f:
        f.write(raw)
    return jsonify({"ok": True, "file": fname, "url": f"/media/images/{fname}"})


@app.route("/media/images/<path:fname>")
def serve_image(fname):
    return send_from_directory(IMAGES_DIR, fname)


@app.route("/media/videos/<path:fname>")
def serve_video(fname):
    return send_from_directory(VIDEOS_DIR, fname)


@app.route("/api/media/images/<path:fname>", methods=["DELETE"])
def del_image(fname):
    p = os.path.join(IMAGES_DIR, os.path.basename(fname))
    if os.path.isfile(p):
        os.remove(p)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/api/media/videos/<path:fname>", methods=["DELETE"])
def del_video(fname):
    p = os.path.join(VIDEOS_DIR, os.path.basename(fname))
    if os.path.isfile(p):
        os.remove(p)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


# ---------------- PTZ ----------------
VEL = 0.6


@app.route("/ptz/<cam_id>/move", methods=["POST"])
def ptz_move(cam_id):
    ctrl = PTZ.get(cam_id)
    if not ctrl:
        return jsonify({"ok": False, "error": "sem PTZ"}), 400
    d = request.get_json(force=True, silent=True) or {}
    PTZ_ACTIVITY[cam_id] = time.time() + 30   # enquanto o botão estiver pressionado
    try:
        ctrl.continuous_move(float(d.get("pan", 0)) * VEL,
                             float(d.get("tilt", 0)) * VEL,
                             float(d.get("zoom", 0)) * VEL)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/ptz/<cam_id>/stop", methods=["POST"])
def ptz_stop(cam_id):
    ctrl = PTZ.get(cam_id)
    if not ctrl:
        return jsonify({"ok": False, "error": "sem PTZ"}), 400
    PTZ_ACTIVITY[cam_id] = time.time()
    try:
        ctrl.stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- Scanner / cadastro ----------------
# ---------------- Teste de senhas na varredura ----------------
# Senhas de fábrica/genéricas das principais marcas de câmera IP (Hikvision,
# Dahua, Intelbras, Foscam, Amcrest, Reolink, Axis, Bosch, ACTi, FLIR, Mobotix,
# Ubiquiti, etc.), levantadas de listas públicas de credenciais padrão. Testadas só
# contra as câmeras do próprio usuário, na rede local dele, para já deixar a
# câmera pronta sem ele precisar lembrar a senha. A que funcionou é mostrada.
COMMON_CREDS = [
    ("admin", ""), ("admin", "admin"), ("admin", "123456"),
    ("admin", "12345"), ("admin", "1234"), ("admin", "123456789"),
    ("admin", "12345678"), ("admin", "admin123"), ("admin", "password"),
    ("admin", "admin1234"), ("admin", "111111"), ("admin", "1111"),
    ("admin", "000000"), ("admin", "123123"), ("admin", "54321"),
    ("admin", "4321"), ("admin", "abc123"), ("admin", "pass"),
    ("admin", "camera"), ("admin", "supervisor"), ("admin", "system"),
    ("admin", "9999"), ("admin", "888888"), ("admin", "666666"),
    ("888888", "888888"), ("666666", "666666"), ("admin", "fliradmin"),
    ("admin", "meinsm"), ("admin", "wbox123"), ("Admin", "123456"),
    ("Admin", "admin"), ("Admin", "12345"), ("administrator", "1234"),
    ("administrator", "admin"), ("service", "service"), ("root", "root"),
    ("root", "admin"), ("root", "123456"), ("root", "pass"),
    ("root", "system"), ("root", ""), ("ubnt", "ubnt"),
    ("supervisor", "supervisor"), ("user", "user"), ("admin", "Aa123456"),
    ("admin", "1234567890"), ("admin", "Pass@123"), ("admin", "1234567"),
    ("admin", "11111"), ("admin", "12345678910"), ("admin", "P@ssw0rd"),
    ("admin", "Password"), ("admin", "admintelecom"), ("admin", "Admin@123"),
    ("admin", "112233"), ("admin", "102030"), ("admin", "skibidi"),
    ("admin", "pakistan123"), ("admin", "assword"), ("", ""),
]
# caminhos RTSP mais comuns, usados só quando a câmera não fala ONVIF
RTSP_PATHS_COMUNS = [
    "/profile0", "/profile1", "/stream0", "/stream1", "/live", "/live/0",
    "/live/main", "/live/ch0", "/h264", "/h264/ch1/main/av_stream",
    "/11", "/12", "/0", "/1", "/ch0", "/ch0_0.264", "/ch01.264",
    "/cam/realmonitor?channel=1&subtype=0", "/Streaming/Channels/101",
    "/Streaming/Channels/1", "/media/video1", "/videoMain", "/av0_0",
]
# portas ONVIF mais usadas (além da configurada); só as abertas são testadas
ONVIF_PORTAS = [6688, 8899, 5000, 8000, 80, 8080, 2020]


def _porta_aberta(host, port, ms=600):
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        k.settimeout(ms / 1000.0)
        k.connect((host, int(port)))
        k.close()
        return True
    except Exception:
        return False


def _rtsp_status(host, port, path, user, pwd, timeout=3.0):
    """DESCRIBE com Basic auth. Devolve o código RTSP (200 ok, 401 precisa senha,
    404 caminho errado) ou None se nem conectou."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        url = "rtsp://%s:%d%s" % (host, port, path)
        req = "DESCRIBE %s RTSP/1.0\r\nCSeq: 1\r\n" % url
        if user or pwd:
            tok = base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()
            req += "Authorization: Basic %s\r\n" % tok
        req += "Accept: application/sdp\r\nUser-Agent: Argos\r\n\r\n"
        s.sendall(req.encode())
        data = s.recv(2048).decode("latin1", "ignore")
        s.close()
        m = re.match(r"RTSP/1\.0 (\d+)", data)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _testar_rtsp(host, port, creds):
    """Acha um caminho RTSP válido e tenta as credenciais nele."""
    caminho = None
    for path in RTSP_PATHS_COMUNS:
        st = _rtsp_status(host, port, path, "", "")
        if st == 200:
            return {"found": True, "method": "rtsp", "user": "", "password": "",
                    "rtsp_port": port, "hd_path": path, "sd_path": path,
                    "ptz_token": "PROFILE_000"}
        if st == 401:
            caminho = path
            break
    if caminho is None:
        return None
    for (u, p) in creds:
        if _rtsp_status(host, port, caminho, u, p) == 200:
            return {"found": True, "method": "rtsp", "user": u, "password": p,
                    "rtsp_port": port, "hd_path": caminho, "sd_path": caminho,
                    "ptz_token": "PROFILE_000"}
    return None


def testar_senhas(dev, extra=None):
    """Testa senhas comuns numa câmera encontrada, em várias portas ONVIF e,
    se preciso, por RTSP direto. Retorna o que funcionou."""
    host = dev.get("ip") or dev.get("host", "")
    creds = list(COMMON_CREDS)
    if extra and (extra.get("user") or extra.get("password")):
        creds = [(extra.get("user", "admin"), extra.get("password", ""))] + creds
    tried = 0

    # 1) ONVIF: monta as URLs candidatas (porta configurada + portas comuns),
    #    testando só as portas que estão abertas.
    urls, vistos = [], set()
    cfg = dev.get("onvif_url", "")
    if cfg:
        urls.append(cfg)
        m = re.search(r"://[^:/]+:(\d+)", cfg)
        if m:
            vistos.add(int(m.group(1)))
    for porta in ONVIF_PORTAS:
        if porta in vistos:
            continue
        if _porta_aberta(host, porta):
            urls.append("http://%s:%d/onvif/device_service" % (host, porta))
    for onvif_url in urls:
        morto = False
        for (u, p) in creds:
            tried += 1
            res = probe_stream(onvif_url, u, p)
            if res.get("ok"):
                return {"found": True, "method": "onvif", "user": u, "password": p,
                        "onvif_url": onvif_url, "hd_path": res.get("hd_path"),
                        "sd_path": res.get("sd_path"), "ptz_token": res.get("ptz_token"),
                        "has_ptz": bool(res.get("has_ptz")), "tried": tried}
            err = (res.get("error") or "").lower()
            if "sem resposta" in err or "inesperada" in err:
                morto = True
                break  # essa porta ONVIF não responde: pula para a próxima
        if morto:
            continue

    # 2) RTSP direto (câmeras sem ONVIF utilizável)
    portas_rtsp = list(dev.get("rtsp_ports") or [])
    for extra_p in (554, 8554):
        if extra_p not in portas_rtsp and _porta_aberta(host, extra_p):
            portas_rtsp.append(extra_p)
    for port in portas_rtsp:
        r = _testar_rtsp(host, port, creds)
        if r:
            r["onvif_url"] = cfg or ("http://%s:6688/onvif/device_service" % host)
            r["has_ptz"] = bool(dev.get("has_ptz"))
            r["tried"] = tried + 1
            return r
    return {"found": False, "tried": tried}


_scan_cache = {"running": False, "devices": []}


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if _scan_cache["running"]:
        return jsonify({"running": True, "devices": _scan_cache["devices"]})
    _scan_cache["running"] = True
    try:
        res = scanner.scan_network()
        devices = res.get("devices", [])
        subnets = res.get("subnets", [])
        known = {c["host"] for c in CONFIG["cameras"]}
        for d in devices:
            d["already_added"] = d["ip"] in known
        # testa senhas comuns em cada câmera ainda não adicionada (em paralelo)
        pendentes = [d for d in devices if not d["already_added"]]
        if pendentes:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for d, cred in zip(pendentes, ex.map(testar_senhas, pendentes)):
                    d["cred"] = cred
        _scan_cache["devices"] = devices
        achou = sum(1 for d in devices if d.get("cred", {}).get("found"))
        log(f"Varredura: {len(devices)} dispositivo(s) em {', '.join(subnets) or '—'}"
            f"; senha encontrada em {achou}")
        return jsonify({"running": False, "devices": devices, "subnets": subnets})
    except Exception as e:
        return jsonify({"running": False, "error": str(e), "devices": []}), 500
    finally:
        _scan_cache["running"] = False


@app.route("/api/probe", methods=["POST"])
def api_probe():
    """Testa credenciais via ONVIF e devolve os caminhos de vídeo descobertos."""
    d = request.get_json(force=True, silent=True) or {}
    onvif_url = d.get("onvif_url", "").strip()
    host = d.get("host", "").strip()
    port = int(d.get("onvif_port", 6688) or 6688)
    if not onvif_url and host:
        onvif_url = f"http://{host}:{port}/onvif/device_service"
    if not onvif_url:
        return jsonify({"ok": False, "error": "informe o host ou a URL ONVIF"}), 400
    res = probe_stream(onvif_url, d.get("user", ""), d.get("password", ""))
    res["onvif_url"] = onvif_url
    return jsonify(res)


@app.route("/api/scan/senha", methods=["POST"])
def api_testar_senha():
    """Testa uma senha informada à mão (e também a lista comum) numa câmera."""
    d = request.get_json(force=True, silent=True) or {}
    host = (d.get("host") or d.get("ip") or "").strip()
    if not host:
        return jsonify({"found": False, "error": "informe o IP"}), 400
    dev = {
        "ip": host,
        "onvif_url": (d.get("onvif_url") or "").strip(),
        "rtsp_ports": d.get("rtsp_ports") or ([int(d["rtsp_port"])] if d.get("rtsp_port") else []),
        "has_ptz": bool(d.get("has_ptz")),
    }
    extra = {"user": (d.get("user") or "admin").strip(), "password": d.get("password", "")}
    return jsonify(testar_senhas(dev, extra))


def _next_id():
    n = 1
    existing = set(CAMERAS.keys())
    while f"cam{n}" in existing:
        n += 1
    return f"cam{n}"


@app.route("/api/cameras", methods=["POST"])
def api_add_camera():
    d = request.get_json(force=True, silent=True) or {}
    required = ["name", "host"]
    for r in required:
        if not str(d.get(r, "")).strip():
            return jsonify({"ok": False, "error": f"campo obrigatório: {r}"}), 400

    cam = {
        "id": d.get("id") or _next_id(),
        "name": d["name"].strip(),
        "host": d["host"].strip(),
        "rtsp_port": int(d.get("rtsp_port", 8554) or 8554),
        "onvif_url": d.get("onvif_url", "").strip()
                     or f"http://{d['host'].strip()}:{int(d.get('onvif_port',6688) or 6688)}/onvif/device_service",
        "user": d.get("user", "").strip(),
        "password": d.get("password", ""),
        "hd_path": d.get("hd_path", "/profile0").strip() or "/profile0",
        "sd_path": d.get("sd_path", "/profile1").strip() or "/profile1",
        "ptz_token": d.get("ptz_token", "PROFILE_000").strip() or "PROFILE_000",
        "ptz": bool(d.get("ptz", False)),
    }
    if cam["id"] in CAMERAS:
        return jsonify({"ok": False, "error": "id já existe"}), 400

    CONFIG["cameras"].append(cam)
    CAMERAS[cam["id"]] = cam
    if cam["ptz"]:
        PTZ[cam["id"]] = PtzController(cam)
    save_config(CONFIG)
    manager.get(cam["id"], "sd")  # pré-aquece
    start_detector(cam["id"])
    log(f"Câmera adicionada: {cam['name']} ({cam['host']})")
    return jsonify({"ok": True, "camera": {"id": cam["id"], "name": cam["name"],
                                           "host": cam["host"], "ptz": cam["ptz"]}})


@app.route("/api/cameras/order", methods=["POST"])
def api_order_cameras():
    d = request.get_json(force=True, silent=True) or {}
    order = d.get("order", [])
    if not isinstance(order, list) or not order:
        return jsonify({"ok": False, "error": "ordem inválida"}), 400
    index = {cid: i for i, cid in enumerate(order)}
    # ordena pelas posições informadas; quem não estiver na lista vai para o fim
    CONFIG["cameras"].sort(key=lambda c: index.get(c["id"], 1_000_000))
    save_config(CONFIG)
    return jsonify({"ok": True})


@app.route("/api/cameras/<cam_id>", methods=["PATCH"])
def api_rename_camera(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "não existe"}), 404
    d = request.get_json(force=True, silent=True) or {}
    name = str(d.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "nome vazio"}), 400
    CAMERAS[cam_id]["name"] = name
    for c in CONFIG["cameras"]:
        if c["id"] == cam_id:
            c["name"] = name
    save_config(CONFIG)
    return jsonify({"ok": True, "name": name})


@app.route("/api/cameras/<cam_id>/zone", methods=["GET"])
def api_get_zone(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"error": "não existe"}), 404
    return jsonify({"zone": CAMERAS[cam_id].get("alert_zone", [])})


@app.route("/api/cameras/<cam_id>/zone", methods=["POST"])
def api_set_zone(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "não existe"}), 404
    d = request.get_json(force=True, silent=True) or {}
    zone = d.get("zone", [])
    if not isinstance(zone, list):
        return jsonify({"ok": False, "error": "área inválida"}), 400
    # normaliza para pares [x, y] entre 0 e 1
    clean = []
    for p in zone:
        try:
            x, y = float(p[0]), float(p[1])
            clean.append([max(0.0, min(1.0, x)), max(0.0, min(1.0, y))])
        except Exception:
            continue
    CAMERAS[cam_id]["alert_zone"] = clean
    for c in CONFIG["cameras"]:
        if c["id"] == cam_id:
            c["alert_zone"] = clean
    save_config(CONFIG)
    return jsonify({"ok": True, "zone": clean})


@app.route("/api/cameras/<cam_id>/detect", methods=["POST"])
def api_set_detect(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "não existe"}), 404
    d = request.get_json(force=True, silent=True) or {}
    en = bool(d.get("enabled", True))
    CAMERAS[cam_id]["detect"] = en
    for c in CONFIG["cameras"]:
        if c["id"] == cam_id:
            c["detect"] = en
    if not en:
        _detections.pop(cam_id, None)
    save_config(CONFIG)
    return jsonify({"ok": True, "enabled": en})


def _masked_alerts():
    """Configurações de alerta sem expor o token do bot do Telegram."""
    a = json.loads(json.dumps(alerts.merged_settings(CONFIG)))
    if a.get("telegram_token"):
        a["telegram_token"] = TOKEN_MASK
    return a


def _settings_payload():
    s = CONFIG.get("settings", {})
    return {
        "alert_phone": s.get("alert_phone", ""),
        "alerts": _masked_alerts(),
        "labels": alerts.LABELS,
        "cameras": [{"id": c["id"], "name": c["name"], "ptz": bool(c.get("ptz")),
                     "follow": bool(c.get("follow")), "follow_invert": bool(c.get("follow_invert")),
                     "has_zone": len(c.get("alert_zone") or []) >= 3,
                     "plates": bool(c.get("plates"))}
                    for c in CONFIG["cameras"]],
        "sender": {"last_error": ENGINE.sender.last_error, "last_ok": ENGINE.sender.last_ok},
        "placas": LEITOR.status(),
        "cores": alerts.CORES,
    }


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(_settings_payload())


def _clean_alerts(d):
    """Valida o que vem da tela de configurações."""
    cur = alerts.merged_settings(CONFIG)
    out = json.loads(json.dumps(cur))      # cópia (o retorno de merged_settings é compartilhado)
    for k in ("enabled", "send_image", "zone_only", "loiter_enabled", "tamper_enabled", "offline_enabled"):
        if k in d:
            out[k] = bool(d[k])
    if d.get("channel") in ("whatsapp", "telegram"):
        out["channel"] = d["channel"]
    for k in ("telegram_token", "telegram_chat_id"):
        if k in d:
            v = str(d[k] or "").strip()
            if k == "telegram_token" and v == TOKEN_MASK:
                continue   # a tela devolveu a máscara: mantém o token salvo
            out[k] = v
    def num(k, lo, hi):
        if k in d:
            try:
                out[k] = max(lo, min(hi, float(d[k])))
            except Exception:
                pass
    num("cooldown_s", 0, 86400)
    num("loiter_minutes", 0.5, 1440)
    num("offline_minutes", 0.5, 1440)
    num("keep_days", 1, 365)
    num("sys_down_minutes", 0.5, 1440)
    num("disk_percent", 50, 99)
    for k in ("plates_alert_unknown", "sys_down_enabled", "disk_enabled"):
        if k in d:
            out[k] = bool(d[k])
    for k in ("plates_known", "plates_watch"):
        if k in d:
            out[k] = str(d[k] or "")[:5000]
    if d.get("clips_mode") in ("todas", "alertas", "desligado"):
        out["clips_mode"] = d["clips_mode"]
    if isinstance(d.get("types"), dict):
        out["types"] = {l: bool(d["types"].get(l, cur["types"].get(l))) for l in alerts.LABELS}
    if isinstance(d.get("follow_types"), list):
        out["follow_types"] = [l for l in d["follow_types"] if l in alerts.LABELS]
    if isinstance(d.get("rules"), list):
        rules = []
        hhmm = re.compile(r"^\d{1,2}:\d{2}$")
        for i, r in enumerate(d["rules"][:50]):
            if not isinstance(r, dict):
                continue
            label = r.get("label", "*")
            if label != "*" and label not in alerts.LABELS:
                continue
            start, end = str(r.get("start", "00:00")), str(r.get("end", "00:00"))
            if not hhmm.match(start) or not hhmm.match(end):
                continue
            cam = r.get("cam", "*")
            rules.append({"id": str(r.get("id") or f"r{i}"), "enabled": bool(r.get("enabled", True)),
                          "mode": "ignore" if r.get("mode") == "ignore" else "only",
                          "label": label, "start": start, "end": end,
                          "cam": cam if (cam == "*" or cam in CAMERAS) else "*"})
        out["rules"] = rules
    return out


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    d = request.get_json(force=True, silent=True) or {}
    s = CONFIG.setdefault("settings", {})
    if "alert_phone" in d:
        s["alert_phone"] = str(d.get("alert_phone", "")).strip()
    if isinstance(d.get("alerts"), dict):
        s["alerts"] = _clean_alerts(d["alerts"])
    if isinstance(d.get("follow"), dict):
        for c in CONFIG["cameras"]:
            if c["id"] in d["follow"]:
                c["follow"] = bool(d["follow"][c["id"]]) and bool(c.get("ptz"))
    if isinstance(d.get("follow_invert"), dict):
        for c in CONFIG["cameras"]:
            if c["id"] in d["follow_invert"]:
                c["follow_invert"] = bool(d["follow_invert"][c["id"]])
    if isinstance(d.get("plates"), dict):
        for c in CONFIG["cameras"]:
            if c["id"] in d["plates"]:
                c["plates"] = bool(d["plates"][c["id"]])
                CAMERAS[c["id"]]["plates"] = c["plates"]
        if any(c.get("plates") for c in CONFIG["cameras"]):
            LEITOR.pronto()          # já começa a baixar/carregar os modelos
    save_config(CONFIG)
    return jsonify({"ok": True, "settings": _settings_payload()})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Envia uma mensagem de teste agora (com imagem de uma câmera, se houver)."""
    img = None
    for cid in CAMERAS:
        w = manager.workers.get((cid, "sd"))
        if w is not None and w.raw is not None:
            ok, buf = cv2.imencode(".jpg", w.raw, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            img = buf.tobytes() if ok else None
            break
    s = alerts.merged_settings(CONFIG)
    ok, info = ENGINE.sender.send_now("✅ Teste do Argos: os alertas estão chegando. "
                                      + time.strftime("%d/%m %H:%M:%S"),
                                      img if s.get("send_image", True) else None)
    ENGINE.store.add_event(time.time(), "", "teste", None, "Mensagem de teste", None,
                           info if ok else "falhou: " + info)
    return jsonify({"ok": ok, "info": info})


@app.route("/api/whatsapp/status")
def api_whatsapp_status():
    """Estado da conexão do WhatsApp do sistema (a tela consulta enquanto está aberta)."""
    st = ENGINE.wa.status()
    dests = alerts.wa_chat_ids((CONFIG.get("settings") or {}).get("alert_phone", ""))
    me = st.get("me") or ""
    st["self_send"] = bool(me) and any(me[-8:] == d[-8:] for d in dests if "@" not in d)
    return jsonify(st)


@app.route("/api/whatsapp/qr.svg")
def api_whatsapp_qr():
    svg = ENGINE.wa.qr_svg()
    if not svg:
        return Response(status=404)
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.route("/api/whatsapp/logout", methods=["POST"])
def api_whatsapp_logout():
    ok, info = ENGINE.wa.logout()
    return jsonify({"ok": ok, "info": info})


@app.route("/api/alerts/telegram_chat", methods=["POST"])
def api_telegram_chat():
    """Descobre o chat ID: o usuário manda qualquer mensagem ao bot e clica no botão."""
    d = request.get_json(force=True, silent=True) or {}
    tok = str(d.get("token", "")).strip()
    if tok == TOKEN_MASK:   # a tela mostra o token mascarado: usa o salvo
        tok = str(alerts.merged_settings(CONFIG).get("telegram_token", "")).strip()
    if not tok:
        return jsonify({"ok": False, "error": "informe o token do bot"}), 400
    try:
        import requests as _rq
        r = _rq.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15).json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"sem resposta do Telegram: {e}"}), 502
    if not r.get("ok"):
        return jsonify({"ok": False, "error": r.get("description", "token inválido")}), 400
    for upd in reversed(r.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return jsonify({"ok": True, "chat_id": str(chat["id"]),
                            "name": chat.get("first_name") or chat.get("title") or ""})
    return jsonify({"ok": False, "error": "nenhuma mensagem encontrada: mande um \"oi\" para o seu bot no Telegram e tente de novo"}), 404


# ---------------- Histórico, busca, estatísticas, mapa de calor ----------------
def _day_ts(date_str):
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return time.mktime(d.timetuple())


def _row_out(r):
    dt = datetime.datetime.fromtimestamp(r["t_start"])
    return {
        "id": r["id"], "cam": r["cam"], "cam_name": (CAMERAS.get(r["cam"]) or {}).get("name", r["cam"]),
        "label": r["label"], "ts": r["t_start"], "date": dt.strftime("%d/%m/%Y"), "time": dt.strftime("%H:%M:%S"),
        "dur": max(0, round((r.get("t_end") or r["t_start"]) - r["t_start"])),
        "in_zone": r.get("in_zone"),
        "thumb": f"/media/events/{r['thumb']}" if r.get("thumb") else None,
        "color": r.get("color"), "plate": r.get("plate"),
        "clip": f"/media/events/{r['clip']}" if r.get("clip") else None,
        "clip_off": r.get("clip_off") or 0,
        "clip_live": bool(r.get("clip")) and r["clip"] in CLIPS.gravando_agora(),
    }


@app.route("/api/search")
def api_search():
    a = request.args
    try:
        t_from = _day_ts(a["date_from"]) if a.get("date_from") else None
        t_to = _day_ts(a["date_to"]) + 86400 if a.get("date_to") else None
    except Exception:
        return jsonify({"ok": False, "error": "data inválida"}), 400
    labels = [x for x in (a.get("label") or "").split(",") if x in alerts.LABELS]
    color = a.get("color") if a.get("color") in alerts.CORES else None
    plate = placas.normaliza(a.get("plate")) or None
    rows, total = ENGINE.store.search(
        label=labels or None, cam=a.get("cam") or None, t_from=t_from, t_to=t_to,
        h_from=a.get("h_from") or None, h_to=a.get("h_to") or None,
        zone_only=a.get("zone") == "1", limit=int(a.get("limit", 300) or 300),
        color=color, plate=plate)
    return jsonify({"ok": True, "total": total, "rows": [_row_out(r) for r in rows]})


@app.route("/api/stats")
def api_stats():
    a = request.args
    date = a.get("date") or datetime.date.today().isoformat()
    days = max(1, min(31, int(a.get("days", 1) or 1)))
    try:
        t_to = _day_ts(date) + 86400
    except Exception:
        return jsonify({"ok": False, "error": "data inválida"}), 400
    t_from = t_to - days * 86400
    hours, by_cam, cam_hours, per_day = ENGINE.store.stats(t_from, t_to, a.get("cam") or None)
    names = {cid: c.get("name", cid) for cid, c in CAMERAS.items()}
    return jsonify({"ok": True, "date": date, "days": days, "hours": hours, "by_cam": by_cam,
                    "cam_hours": cam_hours, "per_day": per_day, "names": names})


@app.route("/api/heatmap/<cam_id>")
def api_heatmap(cam_id):
    if cam_id not in CAMERAS:
        return "camera desconhecida", 404
    date = request.args.get("date") or datetime.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return "data inválida", 400
    w = manager.workers.get((cam_id, "sd"))
    jpg = ENGINE.heat.render(cam_id, date, w.raw if w is not None else None)
    resp = Response(jpg, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/events")
def api_events():
    rows = ENGINE.store.events(limit=int(request.args.get("limit", 200) or 200),
                               cam=request.args.get("cam") or None)
    out = []
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["ts"])
        out.append({**r, "cam_name": (CAMERAS.get(r["cam"]) or {}).get("name", r["cam"] or "—"),
                    "when": dt.strftime("%d/%m/%Y %H:%M:%S"),
                    "image": f"/media/events/{r['image']}" if r.get("image") else None,
                    "clip": f"/media/events/{r['clip']}" if r.get("clip") else None,
                    "clip_off": r.get("clip_off") or 0,
                    "clip_live": bool(r.get("clip")) and r["clip"] in CLIPS.gravando_agora()})
    return jsonify({"ok": True, "rows": out})


@app.route("/api/timeline")
def api_timeline():
    cam = request.args.get("cam") or ""
    if cam not in CAMERAS:
        return jsonify({"ok": False, "error": "câmera desconhecida"}), 404
    date = request.args.get("date") or datetime.date.today().isoformat()
    try:
        t0 = _day_ts(date)
    except Exception:
        return jsonify({"ok": False, "error": "data inválida"}), 400
    sights, evs = ENGINE.store.timeline(cam, t0, t0 + 86400)
    med = lambda x: f"/media/events/{x}" if x else None
    vivos = CLIPS.gravando_agora()
    return jsonify({"ok": True, "date": date, "t0": t0,
                    "items": [{"id": r["id"], "label": r["label"], "ts": r["t_start"],
                               "dur": max(0, round((r["t_end"] or r["t_start"]) - r["t_start"])),
                               "in_zone": r["in_zone"], "thumb": med(r["thumb"]), "color": r["color"],
                               "plate": r["plate"], "clip": med(r["clip"]), "clip_off": r["clip_off"] or 0,
                               "clip_live": r["clip"] in vivos}
                              for r in sights],
                    "events": [{"id": e["id"], "ts": e["ts"], "kind": e["kind"], "label": e["label"],
                                "msg": e["msg"], "image": med(e["image"]), "clip": med(e["clip"]),
                                "clip_off": e["clip_off"] or 0, "clip_live": e["clip"] in vivos} for e in evs]})


@app.route("/api/diag")
def api_diag():
    now = time.time()
    cams = []
    for c in CONFIG["cameras"]:
        cid = c["id"]
        q = {}
        for qual in ("sd", "hd"):
            w = manager.workers.get((cid, qual))
            if w is None:
                q[qual] = None
                continue
            with w.lock:
                raw, rt = w.raw, w.raw_t
            q[qual] = {"connected": bool(w.connected), "fps": round(DIAG.fps.get((cid, qual), 0.0), 1),
                       "res": f"{raw.shape[1]}×{raw.shape[0]}" if raw is not None else None,
                       "age": round(now - rt, 1) if rt else None}
        d = detectors.get(cid)
        cams.append({"id": cid, "name": c["name"], "sd": q["sd"], "hd": q["hd"],
                     "detect": c.get("detect", True), "det_fps": round(DIAG.det_fps.get(cid, 0.0), 1),
                     "objetos": len(d.tracks) if d else 0, "plates": bool(c.get("plates")),
                     "ronda": PATROL.estado(cid) if c.get("ptz") else None})
    try:
        pct, livre, raiz = WATCH.disco()
        disco = {"pct": round(pct, 1), "livre_gb": round(livre, 1), "raiz": raiz}
    except Exception:
        disco = None
    mem = None
    if psutil:
        try:
            vm = psutil.virtual_memory()
            mem = {"proc_mb": round(DIAG.proc.memory_info().rss / 1024 ** 2), "sys_pct": vm.percent,
                   "sys_gb": round(vm.total / 1024 ** 3, 1)}
        except Exception:
            pass
    ms = list(DETECT["ms"])
    vers = {"python": sys.version.split()[0], "opencv": cv2.__version__}
    for mod in ("torch", "ultralytics", "onnxruntime"):
        m = sys.modules.get(mod)
        if m is not None:
            vers[mod] = getattr(m, "__version__", "?")
    parada = WATCH.ultima_parada
    return jsonify({
        "ok": True, "uptime_s": round(now - STARTED), "cpu_proc": DIAG.cpu_proc, "cpu_sys": DIAG.cpu_sys,
        "ncpu": (psutil.cpu_count() if psutil else os.cpu_count()), "mem": mem, "disco": disco,
        "media_gb": round(DIAG.media_gb, 2) if DIAG.media_gb is not None else None,
        "yolo_ms": round(sum(ms) / len(ms)) if ms else None,
        "yolo": "carregado" if DETECT["model"] is not None else ("indisponível" if not DETECT["available"] else "carregando"),
        "cameras": cams, "placas": {**LEITOR.status(), "lidas": PLATES.lidas},
        "clipes": CLIPS.status(), "whatsapp": ENGINE.wa.status(touch=False),
        "envio": {"ultimo_ok": ENGINE.sender.last_ok, "erro": ENGINE.sender.last_error},
        "ultima_parada": {"de": parada[0], "ate": parada[1]} if parada else None,
        "threads": threading.active_count(), "versoes": vers,
    })


# ---------------- PTZ: posições salvas e ronda ----------------
def _ptz_ou_erro(cam_id):
    if cam_id not in CAMERAS:
        return None, (jsonify({"ok": False, "error": "câmera desconhecida"}), 404)
    if cam_id not in PTZ:
        return None, (jsonify({"ok": False, "error": "esta câmera não tem PTZ"}), 400)
    return PTZ[cam_id], None


def _salva_cam(cam_id, **campos):
    for c in CONFIG["cameras"]:
        if c["id"] == cam_id:
            c.update(campos)
    CAMERAS[cam_id].update(campos)
    save_config(CONFIG)


@app.route("/api/ptz/<cam_id>/presets")
def api_presets(cam_id):
    ctrl, err = _ptz_ou_erro(cam_id)
    if err:
        return err
    try:
        lista = presets_de(cam_id, fresco=True)
        erro = None
    except Exception as e:
        lista, erro = [], f"a câmera não respondeu às posições salvas ({e})"
    p = CAMERAS[cam_id].get("patrol") or {}
    return jsonify({"ok": erro is None, "error": erro, "presets": lista,
                    "patrol": {"enabled": bool(p.get("enabled")), "dwell_s": int(p.get("dwell_s", 20) or 20),
                               "tokens": p.get("tokens") or []},
                    "estado": PATROL.estado(cam_id)})


@app.route("/api/ptz/<cam_id>/presets", methods=["POST"])
def api_preset_save(cam_id):
    ctrl, err = _ptz_ou_erro(cam_id)
    if err:
        return err
    d = request.get_json(force=True, silent=True) or {}
    nome = str(d.get("name", "")).strip()[:40]
    if not nome:
        return jsonify({"ok": False, "error": "dê um nome à posição"}), 400
    try:
        token = ctrl.set_preset(nome)
    except Exception as e:
        return jsonify({"ok": False, "error": f"a câmera não salvou a posição: {e}"}), 500
    if token:
        nomes = dict(CAMERAS[cam_id].get("preset_names") or {})
        nomes[token] = nome
        _salva_cam(cam_id, preset_names=nomes)
    PRESETS.pop(cam_id, None)
    log(f"[{cam_id}] posição salva: {nome}")
    return jsonify({"ok": True, "token": token})


@app.route("/api/ptz/<cam_id>/presets/<token>/goto", methods=["POST"])
def api_preset_goto(cam_id, token):
    ctrl, err = _ptz_ou_erro(cam_id)
    if err:
        return err
    try:
        PTZ_ACTIVITY[cam_id] = time.time() + 4
        ctrl.goto_preset(token)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ptz/<cam_id>/presets/<token>", methods=["DELETE"])
def api_preset_del(cam_id, token):
    ctrl, err = _ptz_ou_erro(cam_id)
    if err:
        return err
    try:
        ctrl.remove_preset(token)
    except Exception as e:
        return jsonify({"ok": False, "error": f"a câmera não apagou a posição: {e}"}), 500
    nomes = dict(CAMERAS[cam_id].get("preset_names") or {})
    nomes.pop(token, None)
    p = dict(CAMERAS[cam_id].get("patrol") or {})
    if token in (p.get("tokens") or []):
        p["tokens"] = [t for t in p["tokens"] if t != token]
    _salva_cam(cam_id, preset_names=nomes, patrol=p)
    PRESETS.pop(cam_id, None)
    return jsonify({"ok": True})


@app.route("/api/ptz/<cam_id>/patrol", methods=["POST"])
def api_patrol(cam_id):
    ctrl, err = _ptz_ou_erro(cam_id)
    if err:
        return err
    d = request.get_json(force=True, silent=True) or {}
    p = dict(CAMERAS[cam_id].get("patrol") or {})
    if "enabled" in d:
        p["enabled"] = bool(d["enabled"])
    if "dwell_s" in d:
        try:
            p["dwell_s"] = max(5, min(600, int(d["dwell_s"])))
        except Exception:
            pass
    if isinstance(d.get("tokens"), list):
        p["tokens"] = [str(t) for t in d["tokens"]][:30]
    _salva_cam(cam_id, patrol=p)
    log(f"[{cam_id}] ronda {'ligada' if p.get('enabled') else 'desligada'}")
    return jsonify({"ok": True, "patrol": p, "estado": PATROL.estado(cam_id)})


@app.route("/media/events/<path:fname>")
def serve_event_media(fname):
    resp = send_from_directory(ENGINE.events_dir, fname)
    if fname.endswith(".mp4") and fname in CLIPS.gravando_agora():
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
def api_delete_camera(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "error": "não existe"}), 404
    CONFIG["cameras"] = [c for c in CONFIG["cameras"] if c["id"] != cam_id]
    CAMERAS.pop(cam_id, None)
    PTZ.pop(cam_id, None)
    stop_detector(cam_id)
    manager.drop_camera(cam_id)
    save_config(CONFIG)
    return jsonify({"ok": True})


@app.route("/api/_version")
def api_version():
    return jsonify({"boot_id": BOOT_ID})


@app.route("/api/logs")
def api_logs():
    # a tela manda o "count" que já tem: se nada mudou, não reenvia as 2000 linhas
    if request.args.get("since") == str(LOG_SEQ["n"]):
        return jsonify({"same": True, "count": LOG_SEQ["n"]})
    return jsonify({"lines": list(LOG_BUF), "count": LOG_SEQ["n"]})


@app.route("/api/logs", methods=["DELETE"])
def api_logs_clear():
    LOG_BUF.clear()
    log("Log limpo pelo usuário")
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = SERVER.get("host", "0.0.0.0")
    port = int(SERVER.get("port", 8080))
    log(f"Servidor iniciado em http://localhost:{port}")
    app.run(host=host, port=port, threaded=True)
