"""
Clipes de vídeo das detecções e alertas.

Cada câmera mantém na memória os últimos segundos (em SD, quadros reduzidos).
Quando algo acontece, o clipe começa com esses segundos de antes e segue gravando
até alguns segundos depois. Se outra coisa acontece com o clipe ainda aberto, ele
só é estendido (até um limite), e as duas detecções apontam para o mesmo arquivo,
cada uma com o seu momento dentro dele.

Codifica com o ffmpeg (H.264, tocável no navegador) recebendo os quadros por pipe.
"""
import datetime
import os
import subprocess
import threading
import time
from collections import deque

import cv2

SEM_JANELA = 0x08000000 if os.name == "nt" else 0


class _Clipe:
    def __init__(self, proc, tmp, final, rel, inicio, fim, tam, pendentes):
        self.proc, self.tmp, self.final, self.rel = proc, tmp, final, rel
        self.inicio, self.fim, self.tam = inicio, fim, tam
        self.pendentes = pendentes      # quadros de antes, escritos pela thread dos clipes
        self.quadros = 0
        self.morto = False


class ClipManager:
    def __init__(self, events_dir, ffmpeg, get_frame, list_cams, log,
                 fps=8, antes=5.0, depois=10.0, maximo=90.0, largura=640):
        self.events_dir = events_dir
        self.ffmpeg = ffmpeg
        self.get_frame = get_frame        # cam -> (quadro BGR | None, seq, t)
        self.list_cams = list_cams        # () -> [cam]
        self.log = log
        self.fps, self.antes, self.depois, self.maximo = fps, antes, depois, maximo
        self.largura = largura
        self.lock = threading.Lock()
        self.buf = {}       # cam -> deque[(t, quadro)]
        self.ativo = {}     # cam -> _Clipe
        self.ultimo = {}    # cam -> (seq, quadro reduzido)
        self.encerrar = []  # clipes que a thread deve finalizar
        self.feitos = 0
        self.falhas = 0
        self.ligado = bool(ffmpeg)
        if not ffmpeg:
            log("Clipes: ffmpeg indisponível; os clipes de vídeo ficam desligados")
        self._limpa_restos()
        threading.Thread(target=self._loop, daemon=True, name="clipes").start()

    def _limpa_restos(self):
        """Clipes que estavam sendo gravados quando o Argos parou ficam incompletos: apaga."""
        try:
            for dia in sorted(os.listdir(self.events_dir))[-3:] if os.path.isdir(self.events_dir) else []:
                pasta = os.path.join(self.events_dir, dia)
                if not os.path.isdir(pasta):
                    continue
                for nome in os.listdir(pasta):
                    if nome.endswith(".gravando"):
                        try:
                            os.remove(os.path.join(pasta, nome))
                        except OSError:
                            pass
        except Exception:
            pass

    # ------------------------------------------------------------------ API
    def disparar(self, cam, t=None):
        """Garante um clipe cobrindo o instante t. Retorna (caminho relativo, segundos
        desde o início do clipe) ou (None, None)."""
        if not self.ligado:
            return None, None
        t = t or time.time()
        with self.lock:
            c = self.ativo.get(cam)
            if c is not None:
                c.fim = min(c.inicio + self.maximo, max(c.fim, t + self.depois))
                if t <= c.fim:
                    return c.rel, max(0.0, t - c.inicio)
                self.ativo.pop(cam, None)  # chegou no limite: encerra e abre outro
                self.encerrar.append(c)
            buf = self.buf.get(cam)
            if not buf:
                return None, None
            c = self._abrir(cam, list(buf), t)
            if c is None:
                return None, None
            return c.rel, max(0.0, t - c.inicio)

    def estender(self, cam, t=None):
        """O objeto continua se mexendo: mantém o clipe aberto um pouco mais."""
        t = t or time.time()
        with self.lock:
            c = self.ativo.get(cam)
            if c is not None:
                c.fim = min(c.inicio + self.maximo, max(c.fim, t + self.depois))

    def status(self):
        with self.lock:
            return {"ligado": self.ligado, "gravando": len(self.ativo),
                    "feitos": self.feitos, "falhas": self.falhas}

    # ------------------------------------------------------------------ interno
    def _reduz(self, q):
        h, w = q.shape[:2]
        if w > self.largura:
            nh = int(round(h * self.largura / w))
            q = cv2.resize(q, (self.largura, nh - nh % 2), interpolation=cv2.INTER_AREA)
        elif w % 2 or h % 2:
            q = q[: h - h % 2, : w - w % 2]
        return q

    def _abrir(self, cam, quadros, t):
        t0 = quadros[0][0]
        h, w = quadros[-1][1].shape[:2]
        dia = datetime.date.fromtimestamp(t0).isoformat()
        pasta = os.path.join(self.events_dir, dia)
        os.makedirs(pasta, exist_ok=True)
        nome = f"{cam}_{time.strftime('%H-%M-%S', time.localtime(t0))}_clipe.mp4"
        final = os.path.join(pasta, nome)
        n = 1
        while os.path.exists(final):
            n += 1
            nome = f"{cam}_{time.strftime('%H-%M-%S', time.localtime(t0))}_clipe{n}.mp4"
            final = os.path.join(pasta, nome)
        tmp = final + ".gravando"
        args = [self.ffmpeg, "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{w}x{h}", "-r", str(self.fps), "-i", "-", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-f", "mp4", "-y", tmp]
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=SEM_JANELA)
        except Exception as e:
            self.falhas += 1
            self.log(f"Clipes: não consegui iniciar o ffmpeg: {e}")
            return None
        c = _Clipe(proc, tmp, final, f"{dia}/{nome}", t0, t + self.depois, (w, h),
                   [q for _, q in quadros])
        self.ativo[cam] = c
        return c

    def _escrever(self, c, q):
        """Só a thread dos clipes escreve no ffmpeg (nunca com o lock preso)."""
        if c.morto:
            return
        if q.shape[1] != c.tam[0] or q.shape[0] != c.tam[1]:
            q = cv2.resize(q, c.tam, interpolation=cv2.INTER_AREA)
        try:
            c.proc.stdin.write(q.tobytes())
            c.quadros += 1
        except Exception:
            c.morto = True

    def _finalizar(self, c):
        def fim():
            try:
                c.proc.stdin.close()
            except Exception:
                pass
            try:
                c.proc.wait(timeout=60)
            except Exception:
                try:
                    c.proc.kill()
                except Exception:
                    pass
            if (not c.morto and c.proc.returncode == 0 and os.path.isfile(c.tmp)
                    and os.path.getsize(c.tmp) > 1024):
                try:
                    os.replace(c.tmp, c.final)
                    self.feitos += 1
                    return
                except Exception:
                    pass
            self.falhas += 1
            self.log(f"Clipes: falha ao gerar {os.path.basename(c.final)}")
            try:
                os.remove(c.tmp)
            except Exception:
                pass
        threading.Thread(target=fim, daemon=True).start()

    def _loop(self):
        intervalo = 1.0 / self.fps
        prox = time.time()
        while True:
            prox += intervalo
            espera = prox - time.time()
            if espera > 0:
                time.sleep(espera)
            else:
                prox = time.time()          # atrasou: não tenta compensar
            if not self.ligado:
                continue
            agora = time.time()
            cams = set(self.list_cams())
            for cam in cams:
                try:
                    q, seq, t = self.get_frame(cam)
                except Exception:
                    q, seq, t = None, None, 0
                if q is None or agora - (t or 0) > 3.0:
                    continue                # câmera parada: não grava imagem congelada
                ult = self.ultimo.get(cam)
                if ult is not None and ult[0] == seq:
                    pq = ult[1]             # mesmo quadro: repete (mantém o tempo certo)
                else:
                    pq = self._reduz(q)
                    self.ultimo[cam] = (seq, pq)
                lote, fechar = None, False
                with self.lock:
                    buf = self.buf.setdefault(cam, deque(maxlen=int(self.antes * self.fps)))
                    buf.append((agora, pq))
                    c = self.ativo.get(cam)
                    if c is not None:
                        lote = c.pendentes + [pq]
                        c.pendentes = []
                        if agora >= c.fim:
                            fechar = True
                            self.ativo.pop(cam, None)
                if c is not None:
                    for x in lote:
                        self._escrever(c, x)
                    if fechar:
                        self._finalizar(c)
            with self.lock:
                fim = self.encerrar
                self.encerrar = []
                for cam in list(self.buf):
                    if cam not in cams:
                        self.buf.pop(cam, None)
                        self.ultimo.pop(cam, None)
                        c = self.ativo.pop(cam, None)
                        if c is not None:
                            fim.append(c)
            for c in fim:
                for x in c.pendentes:
                    self._escrever(c, x)
                c.pendentes = []
                self._finalizar(c)
