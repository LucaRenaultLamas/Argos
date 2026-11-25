"""
Leitura de placas de veículos (padrão brasileiro: ABC1234 e Mercosul ABC1D23).

Usa dois modelos ONNX (rodam com o onnxruntime, sem OpenCV extra):
  - detector de placas: YOLOv9-t 384 (open-image-models)
  - leitura dos caracteres: CCT-XS v2 global (fast-plate-ocr), que inclui o Brasil
Os modelos são baixados na primeira vez para a pasta "modelos" do Argos.
"""
import os
import re
import threading
import time
import urllib.request

import cv2
import numpy as np

MODELOS = {
    "det": ("https://github.com/ankandrew/open-image-models/releases/download/assets/"
            "yolo-v9-t-384-license-plates-end2end.onnx", "placa_detector.onnx"),
    "ocr": ("https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/"
            "cct_xs_v2_global.onnx", "placa_leitura.onnx"),
}
DET_TAM = 384
OCR_H, OCR_W = 64, 128
OCR_SLOTS = 10
ALFABETO = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
PAD = "_"
_LETRAS = [ALFABETO.index(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
_DIGITOS = [ALFABETO.index(c) for c in "0123456789"]
_IDX_PAD = ALFABETO.index(PAD)
# posição -> índices permitidos (ABC1D23 / ABC1234)
_PERMITIDOS = [_LETRAS, _LETRAS, _LETRAS, _DIGITOS, _LETRAS + _DIGITOS, _DIGITOS, _DIGITOS]
PLACA_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")


def normaliza(txt):
    """Texto digitado pelo usuário -> placa só com letras e números, maiúscula."""
    return re.sub(r"[^A-Z0-9]", "", str(txt or "").upper())


def parse_lista(texto):
    """Linhas "ABC1D23 Nome opcional" -> {placa: nome}."""
    out = {}
    for linha in str(texto or "").replace(";", "\n").replace(",", "\n").splitlines():
        partes = linha.strip().split(None, 1)
        if not partes:
            continue
        p = normaliza(partes[0])
        if len(p) >= 5:
            out[p] = partes[1].strip() if len(partes) > 1 else ""
    return out


def _letterbox(img, tam):
    h, w = img.shape[:2]
    r = min(tam / h, tam / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    dw, dh = (tam - nw) / 2, (tam - nh) / 2
    if (w, h) != (nw, nh):
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return img, r, (dw, dh)


class LeitorPlacas:
    def __init__(self, pasta, log):
        self.pasta = pasta
        self.log = log
        self.lock = threading.Lock()
        self.det = None
        self.ocr = None
        self.estado = "desligado"     # desligado | baixando | pronto | indisponivel | erro
        self.erro = ""
        self.tempos = []              # ms das últimas leituras (diagnóstico)
        self._tentou = 0.0

    # ------------------------------------------------------------ carga
    def status(self):
        return {"estado": self.estado, "erro": self.erro,
                "ms": round(sum(self.tempos) / len(self.tempos), 1) if self.tempos else None}

    def pronto(self):
        if self.det is not None:
            return True
        # tenta carregar (não mais que 1x a cada 10 min se deu erro)
        if self.estado in ("erro", "indisponivel") and time.time() - self._tentou < 600:
            return False
        if self.estado == "baixando":
            return False
        self._tentou = time.time()
        threading.Thread(target=self._carregar, daemon=True, name="placas-carga").start()
        return False

    def _baixar(self, url, destino):
        tmp = destino + ".baixando"
        req = urllib.request.Request(url, headers={"User-Agent": "Argos"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            while True:
                b = r.read(256 * 1024)
                if not b:
                    break
                f.write(b)
        if os.path.getsize(tmp) < 100_000:
            os.remove(tmp)
            raise RuntimeError("arquivo baixado incompleto")
        os.replace(tmp, destino)

    def _carregar(self):
        try:
            import onnxruntime as ort
        except Exception as e:
            self.estado, self.erro = "indisponivel", f"componente onnxruntime não instalado ({e})"
            self.log("Placas: " + self.erro)
            return
        try:
            os.makedirs(self.pasta, exist_ok=True)
            for chave, (url, nome) in MODELOS.items():
                destino = os.path.join(self.pasta, nome)
                if not os.path.isfile(destino):
                    self.estado = "baixando"
                    self.log(f"Placas: baixando o modelo {nome}…")
                    self._baixar(url, destino)
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2          # não disputa todos os núcleos com o YOLO
            opts.inter_op_num_threads = 1
            prov = ["CPUExecutionProvider"]
            det = ort.InferenceSession(os.path.join(self.pasta, MODELOS["det"][1]), opts, providers=prov)
            ocr = ort.InferenceSession(os.path.join(self.pasta, MODELOS["ocr"][1]), opts, providers=prov)
            with self.lock:
                self.det, self.ocr = det, ocr
            self.estado, self.erro = "pronto", ""
            self.log("Placas: leitura de placas pronta")
        except Exception as e:
            self.estado, self.erro = "erro", str(e)[:200]
            self.log(f"Placas: não consegui carregar os modelos: {self.erro}")

    # ------------------------------------------------------------ inferência
    def detectar(self, img, conf_min=0.35):
        """Placas na imagem (BGR). Retorna [(x1, y1, x2, y2, conf)]."""
        lb, r, (dw, dh) = _letterbox(img, DET_TAM)
        x = lb.transpose((2, 0, 1))[::-1].astype(np.float32) / 255.0
        x = np.ascontiguousarray(x[None])
        with self.lock:
            out = self.det.run(None, {self.det.get_inputs()[0].name: x})[0]
        res = []
        H, W = img.shape[:2]
        for p in out:
            conf = float(p[6])
            if conf < conf_min:
                continue
            x1 = max(0, int((p[1] - dw) / r)); y1 = max(0, int((p[2] - dh) / r))
            x2 = min(W, int((p[3] - dw) / r)); y2 = min(H, int((p[4] - dh) / r))
            if x2 - x1 >= 8 and y2 - y1 >= 4:
                res.append((x1, y1, x2, y2, conf))
        res.sort(key=lambda b: -b[4])
        return res

    def ler(self, recorte):
        """Recorte da placa (BGR) -> (placa, confiança) no padrão brasileiro, ou (None, 0)."""
        rgb = cv2.cvtColor(cv2.resize(recorte, (OCR_W, OCR_H), interpolation=cv2.INTER_LINEAR),
                           cv2.COLOR_BGR2RGB)
        with self.lock:
            out = self.ocr.run(["plate"], {"input": rgb[None].astype(np.uint8)})[0]
        prob = out.reshape(OCR_SLOTS, len(ALFABETO))
        # depois dos 7 caracteres o modelo precisa "ver" o fim da placa
        if prob[7:, _IDX_PAD].min() < 0.5:
            return None, 0.0
        txt, confs = "", []
        for i, perm in enumerate(_PERMITIDOS):
            linha = prob[i, perm]
            j = int(np.argmax(linha))
            txt += ALFABETO[perm[j]]
            confs.append(float(linha[j]))
        if not PLACA_RE.match(txt):
            return None, 0.0
        return txt, float(np.mean(confs)) * float(min(confs)) ** 0.5

    def ler_veiculo(self, img):
        """Procura e lê a placa num recorte de veículo. Retorna (placa, conf, caixa) ou None."""
        t0 = time.time()
        melhor = None
        try:
            for (x1, y1, x2, y2, c) in self.detectar(img)[:2]:
                mw, mh = int((x2 - x1) * 0.06), int((y2 - y1) * 0.12)
                rec = img[max(0, y1 - mh):y2 + mh, max(0, x1 - mw):x2 + mw]
                if rec.size == 0:
                    continue
                txt, conf = self.ler(rec)
                if txt and (melhor is None or conf * c > melhor[1]):
                    melhor = (txt, conf * c, (x1, y1, x2, y2))
        finally:
            self.tempos = (self.tempos + [(time.time() - t0) * 1000])[-30:]
        return melhor
