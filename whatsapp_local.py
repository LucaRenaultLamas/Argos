"""
WhatsApp direto do Argos, sem site e sem serviço de terceiros.

O sistema se conecta ao WhatsApp como um "aparelho conectado" (igual ao
WhatsApp Web): na tela de configurações aparece um QR code, você lê com o
celular do número que vai ENVIAR os alertas e pronto. A sessão fica salva em
media/whatsapp_sessao.sqlite3 e volta sozinha quando o sistema reinicia.

Usa a biblioteca neonize (whatsmeow), instalada pelo requirements.txt.
"""
import os
import threading
import time


def _digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


class LocalWhatsApp:
    QR_MAX_AGE = 70          # sem leitura nesse tempo, gera uma conexão nova (QR novo)
    IDLE_AFTER = 180         # tela fechada há esse tempo: para de gerar QR

    def __init__(self, base_dir, log):
        self.log = log
        self.db = os.path.join(base_dir, "media", "whatsapp_sessao.sqlite3")
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        self.lock = threading.Lock()
        self.state = "iniciando"     # iniciando | qr | conectando | conectado | desconectado | indisponivel
        self.qr = None               # texto do QR atual
        self.qr_t = 0.0
        self.me = ""                 # número conectado
        self.error = ""
        self.interest = time.time()  # última vez que a tela pediu o status
        self.client = None
        self._jid_cache = {}
        self._kick = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="whatsapp").start()
        threading.Thread(target=self._watchdog, daemon=True, name="whatsapp-qr").start()

    # ------------------------------------------------------------------ status
    def status(self, touch=True):
        if touch:
            self.interest = time.time()
            self._kick.set()
        with self.lock:
            return {"state": self.state, "me": self.me, "error": self.error,
                    "qr_t": self.qr_t if self.state == "qr" else 0}

    def qr_svg(self):
        with self.lock:
            data = self.qr if self.state == "qr" else None
        if not data:
            return None
        import segno
        import io
        buf = io.BytesIO()
        segno.make_qr(data, error="l").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff")
        return buf.getvalue()

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    # ------------------------------------------------------------------ conexão
    def _loop(self):
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, LoggedOutEv, PairStatusEv
            import logging
            logging.getLogger("whatsmeow").setLevel(logging.WARNING)
            logging.getLogger("neonize").setLevel(logging.WARNING)
        except Exception as e:
            self._set(state="indisponivel",
                      error=f"biblioteca do WhatsApp não instalada ({e}). Reinicie o sistema para instalar.")
            self.log(f"WhatsApp: {self.error}")
            return

        try:   # a biblioteca avisa que falta ffmpeg; só é usado para vídeo/áudio, não para foto
            import neonize.utils.ffmpeg as _ff
            _ff._ffmpeg_checked = True
        except Exception:
            pass
        # Espera o banco destravar em vez de falhar ("database is locked") e usa o modo WAL,
        # que deixa leitura e escrita acontecerem ao mesmo tempo. A biblioteca acrescenta
        # "?_foreign_keys=on" no fim; o "&_x=" final só absorve esse trecho.
        dsn = self.db + "?_busy_timeout=15000&_journal_mode=WAL&_foreign_keys=on&_x="
        c = NewClient(dsn)
        self.client = c

        def on_qr(_cl, data):
            txt = data.decode() if isinstance(data, (bytes, bytearray)) else str(data)
            self._set(state="qr", qr=txt, qr_t=time.time(), error="")

        def on_pair(_cl, ev):
            self._set(state="conectando", qr=None)

        def on_connected(cl, _ev):
            me = ""
            try:
                me = cl.get_me().JID.User
            except Exception:
                pass
            self._set(state="conectado", qr=None, me=me, error="")
            self.log(f"WhatsApp conectado ({me or 'número desconhecido'})")

        def on_logged_out(cl, _ev):
            self._set(state="desconectado", qr=None, me="")
            self._jid_cache.clear()
            self.log("WhatsApp desconectado pelo celular: leia o QR code de novo nas configurações")
            try:
                cl.stop()
            except Exception:
                pass

        c.event.qr(on_qr)
        c.event(PairStatusEv)(on_pair)
        c.event(ConnectedEv)(on_connected)
        c.event(LoggedOutEv)(on_logged_out)

        fails = 0
        while True:
            # sem sessão salva e ninguém olhando a tela: não fica gerando QR à toa
            if self.state in ("qr", "desconectado") and time.time() - self.interest > self.IDLE_AFTER:
                self._set(state="desconectado", qr=None)
                self._kick.clear()
                self._kick.wait()
            if self.state not in ("qr", "desconectado"):
                self._set(state="conectando")
            t0 = time.time()
            try:
                c.connect()                       # bloqueia enquanto a conexão estiver ativa
                err = ""
            except Exception as e:
                err = str(e)
            if self.state == "conectado":
                self._set(state="conectando")
            if err:
                self._set(error="sem conexão com o WhatsApp: " + err[:150])
            fails = fails + 1 if time.time() - t0 < 30 else 0
            time.sleep(min(60, 3 + fails * 5))

    def _watchdog(self):
        while True:
            time.sleep(5)
            c = self.client
            if c is not None and self.state == "qr" and time.time() - self.qr_t > self.QR_MAX_AGE:
                try:
                    c.stop()                      # o laço reconecta e gera um QR novo
                except Exception:
                    pass

    def logout(self):
        c = self.client
        if c is None:
            return False, "WhatsApp não está ativo"
        try:
            if self.state == "conectado":
                c.logout()
        except Exception as e:
            self.log(f"WhatsApp: erro ao sair: {e}")
        self._set(state="desconectado", qr=None, me="")
        self._jid_cache.clear()
        self.interest = time.time()
        try:
            c.stop()
        except Exception:
            pass
        return True, "desconectado"

    # ------------------------------------------------------------------ envio
    def _jid(self, dest):
        """Número -> JID certo no WhatsApp (resolve o 9º dígito); grupo (@g.us) passa direto."""
        from neonize.utils.jid import build_jid
        dest = str(dest).strip()
        if "@" in dest:
            user, srv = dest.rsplit("@", 1)
            return build_jid(user, "s.whatsapp.net" if srv == "c.us" else srv)
        num = _digits(dest)
        if num in self._jid_cache:
            return self._jid_cache[num]
        res = self.client.is_on_whatsapp("+" + num) or []
        for r in res:
            if r.IsIn:
                self._jid_cache[num] = r.JID
                return r.JID
        raise ValueError(f"o número {num} não tem WhatsApp")

    def send(self, dests, text, image=None):
        """Retorna (ok, detalhe)."""
        if self.state == "indisponivel":
            return False, self.error
        if self.state != "conectado" or self.client is None:
            return False, "WhatsApp do sistema não está conectado: leia o QR code nas configurações"
        errors = []
        for d in dests:
            try:
                jid = self._jid(d)
                if image:
                    self.client.send_image(jid, image, caption=text[:1000])
                else:
                    self.client.send_message(jid, text)
            except Exception as e:
                errors.append(f"{_digits(d) or d}: {str(e)[:120]}")
        if errors:
            return False, "; ".join(errors)
        return True, "enviado (WhatsApp)"
