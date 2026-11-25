# -*- coding: utf-8 -*-
"""
Instalador / manutenção do Argos.

Um único programa que, conforme o caso, faz:
  - Instalar      (primeira vez)
  - Reparar       (restaura arquivos que faltam, conserta o ambiente do Python,
                   recria atalhos e o registro em "Apps instalados")
  - Reinstalar    (substitui todos os arquivos do programa e recria o ambiente
                   do Python do zero; câmeras e gravações são mantidas)
  - Desinstalar   (fecha o Argos, remove atalhos, registro e arquivos)

Gerado em dois executáveis por gerar_instalador.py:
  - "Instalar Argos.exe"    leva o pacote do programa (payload.zip)
  - "Desinstalar Argos.exe" sem pacote; só desinstala

Depois de instalar, os dois ficam dentro da pasta do Argos.

Parâmetros de linha de comando:
  --reparar | --reinstalar | --desinstalar   abre direto nessa opção
  --pasta "<pasta>"                          pasta da instalação
  --executar --manter 0|1                    (uso interno) desinstala sem perguntar
  --temp                                     (uso interno) cópia temporária; apaga-se no fim
"""
import os
import sys
import json
import time
import glob
import fnmatch
import shutil
import zipfile
import threading
import queue
import subprocess
import tempfile
import datetime
import base64
import traceback
import urllib.request

APP = "Argos"
VERSAO = "1.1.0"
CHAVE_UNINST = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Argos"
PASTA_PADRAO = r"C:\Argos"
EXE_PROGRAMA = "Argos.exe"
EXE_INSTALAR = "Instalar Argos.exe"
EXE_DESINSTALAR = "Desinstalar Argos.exe"
MANIFESTO = "instalado.txt"        # lista dos itens que vieram do pacote
MODELO_CAMERAS = "cameras.modelo.json"
PORTA_PADRAO = 8080
ESPACO_MIN_GB = 3.0                # torch + ultralytics + opencv ocupam ~2 GB

PY_VERSAO_DOWNLOAD = "3.13.7"
PY_URL = ("https://www.python.org/ftp/python/%s/python-%s-amd64.exe"
          % (PY_VERSAO_DOWNLOAD, PY_VERSAO_DOWNLOAD))

# Itens do programa caso a pasta não tenha o manifesto (instalações antigas).
PROGRAMA_PADRAO = [
    "app.py", "alerts.py", "onvif_ptz.py", "scanner.py", "whatsapp_local.py", "clipes.py", "placas.py",
    "modelos", "requirements.txt", "README.md", "PRODUCT.md", "DESIGN.md", "iniciar.bat",
    "yolov8n.pt", EXE_PROGRAMA, "icone", "static", MODELO_CAMERAS,
]
# Sempre removidos na desinstalação (gerados pelo programa / instalador).
EXTRAS = [
    ".venv", "__pycache__", "reiniciar.bat", ".restart", MANIFESTO,
    EXE_INSTALAR, EXE_DESINSTALAR, "instalacao.log",
]
EXTRAS_PADROES = ["*.bak", "*.log", "*.log.*"]
# Dados do usuário (mantidos se ele pedir).
DADOS = ["cameras.json", "media"]
DADOS_PADROES = ["cameras.antes*.json", "cameras.json.tmp"]
# Nunca apagados pelo desinstalador.
PROTEGIDOS = {"instalador"}

WIN = os.name == "nt"
SEM_JANELA = 0x08000000 if WIN else 0          # CREATE_NO_WINDOW
DESANEXADO = 0x00000008 | 0x00000200 if WIN else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP

if WIN:
    import winreg  # noqa: E402
    import ctypes  # noqa: E402


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def recurso(nome):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nome)


def caminho_pacote():
    p = recurso("payload.zip")
    return p if os.path.isfile(p) else None


def congelado():
    return bool(getattr(sys, "frozen", False))


def decodifica(b):
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("oem" if WIN else "latin-1", errors="replace")


def roda(cmd, timeout=None, cwd=None):
    """Executa e devolve (código, saída). Nunca levanta exceção."""
    try:
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout, creationflags=SEM_JANELA)
        return r.returncode, decodifica(r.stdout)
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def dentro(arquivo, pasta):
    try:
        a, p = norm(arquivo), norm(pasta)
        return a == p or a.startswith(p + os.sep)
    except Exception:  # noqa: BLE001
        return False


def e_instalacao(p):
    if not p or not os.path.isdir(p):
        return False
    return (os.path.isfile(os.path.join(p, EXE_PROGRAMA))
            or (os.path.isfile(os.path.join(p, "app.py"))
                and os.path.isfile(os.path.join(p, "static", "index.html"))))


def pasta_pode_ser_apagada(p):
    """Trava de segurança: nunca mexe em raiz de disco, pasta do usuário etc."""
    if not p:
        return False
    a = norm(p)
    drive, resto = os.path.splitdrive(a)
    if not resto.strip("\\/"):
        return False
    proibidas = [os.path.expanduser("~"), os.environ.get("SystemRoot", r"C:\Windows"),
                 os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("USERPROFILE", ""), os.environ.get("PUBLIC", ""),
                 os.path.join(drive + os.sep, "Users")]
    for x in proibidas:
        if x and norm(x) == a:
            return False
    return e_instalacao(p) or os.path.isfile(os.path.join(p, MANIFESTO))


def ler_porta(pasta):
    try:
        with open(os.path.join(pasta, "cameras.json"), "r", encoding="utf-8") as f:
            return int(json.load(f).get("server", {}).get("port", PORTA_PADRAO))
    except Exception:  # noqa: BLE001
        return PORTA_PADRAO


def tem_acento(p):
    try:
        p.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def apaga(caminho, tentativas=4):
    """Apaga arquivo ou pasta, tentando de novo (arquivos presos logo após fechar)."""
    def _destrava(func, path, _exc):
        try:
            os.chmod(path, 0o666)
            func(path)
        except Exception:  # noqa: BLE001
            pass
    for i in range(tentativas):
        try:
            if os.path.isdir(caminho) and not os.path.islink(caminho):
                shutil.rmtree(caminho, onerror=_destrava)
            elif os.path.lexists(caminho):
                try:
                    os.remove(caminho)
                except PermissionError:
                    os.chmod(caminho, 0o666)
                    os.remove(caminho)
        except Exception:  # noqa: BLE001
            pass
        if not os.path.lexists(caminho):
            return True
        time.sleep(1.0 + i)
    return not os.path.lexists(caminho)


def tamanho_kb(pasta):
    total = 0
    for raiz, dirs, arqs in os.walk(pasta):
        if norm(raiz) == norm(os.path.join(pasta, "media")):
            dirs[:] = []
            continue
        for a in arqs:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total // 1024


# ----------------------------------------------------------------------------
# Windows: pastas especiais, registro, processos
# ----------------------------------------------------------------------------
def pasta_especial(csidl):
    if not WIN:
        return None
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
        return buf.value
    return None


def pastas_atalhos():
    return {
        "desktop": pasta_especial(0x0010),   # CSIDL_DESKTOPDIRECTORY
        "programas": pasta_especial(0x0002),  # CSIDL_PROGRAMS (menu Iniciar)
        "inicializar": pasta_especial(0x0007),  # CSIDL_STARTUP
    }


def registro_ler():
    if not WIN:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_UNINST) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return None


def registro_gravar(pasta):
    if not WIN:
        return
    hoje = datetime.date.today().strftime("%Y%m%d")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CHAVE_UNINST) as k:
        def s(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)

        def d(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, int(v))
        s("DisplayName", APP)
        s("DisplayVersion", VERSAO)
        s("Publisher", APP)
        s("InstallLocation", pasta)
        s("InstallDate", hoje)
        s("DisplayIcon", os.path.join(pasta, "icone", "argos.ico"))
        s("UninstallString", '"%s"' % os.path.join(pasta, EXE_DESINSTALAR))
        s("QuietUninstallString", '"%s" --executar --manter 1' % os.path.join(pasta, EXE_DESINSTALAR))
        s("ModifyPath", '"%s"' % os.path.join(pasta, EXE_INSTALAR))
        d("NoModify", 0)
        d("NoRepair", 0)
        try:
            d("EstimatedSize", tamanho_kb(pasta))
        except Exception:  # noqa: BLE001
            pass


def registro_apagar():
    if not WIN:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, CHAVE_UNINST)
    except OSError:
        pass


def processo_argos_caminho():
    """Pasta do Argos.exe em execução, se houver."""
    if not WIN:
        return None
    rc, out = roda(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "$ProgressPreference='SilentlyContinue';"
                    "(Get-Process Argos -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
                   timeout=15)
    out = (out or "").strip().splitlines()
    if rc == 0 and out and out[-1].lower().endswith(".exe") and os.path.isfile(out[-1]):
        return os.path.dirname(out[-1])
    return None


def instalacao_existente():
    """Procura uma instalação: registro, perto do próprio exe, processo aberto, padrões."""
    cand = [registro_ler()]
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if congelado() else __file__))
    p = exe_dir
    for _ in range(4):   # a pasta do exe e até 3 acima (ex.: <Argos>\instalador\dist)
        cand.append(p)
        p = os.path.dirname(p)
    for c in cand:
        if e_instalacao(c):
            return os.path.abspath(c)
    rodando = processo_argos_caminho()
    if e_instalacao(rodando):
        return rodando
    for c in (PASTA_PADRAO, os.path.join(os.environ.get("LOCALAPPDATA", ""), APP)):
        if e_instalacao(c):
            return c
    return None


def fechar_argos(porta, log):
    if not WIN:
        return
    log("Fechando o Argos que estava aberto…")
    roda(["taskkill", "/IM", EXE_PROGRAMA, "/F", "/T"], timeout=20)
    for titulo in ("Argos - servidor*", "Sistema de Cameras*"):
        roda(["taskkill", "/FI", "IMAGENAME eq cmd.exe", "/FI",
              "WINDOWTITLE eq %s" % titulo, "/T", "/F"], timeout=20)
    # Python que ainda esteja ouvindo na porta do painel (só python, nunca outro programa)
    rc, out = roda(["netstat", "-ano", "-p", "TCP"], timeout=20)
    pids = set()
    for linha in out.splitlines():
        partes = linha.split()
        if len(partes) >= 5 and partes[1].endswith(":%d" % porta) and "LISTEN" in partes[3].upper():
            pids.add(partes[4])
    for pid in pids:
        rc, out = roda(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"], timeout=15)
        if "python" in out.lower():
            roda(["taskkill", "/F", "/T", "/PID", pid], timeout=15)
    time.sleep(1.5)


# ----------------------------------------------------------------------------
# atalhos
# ----------------------------------------------------------------------------
def _ps_str(s):
    return "'" + s.replace("'", "''") + "'"


def criar_atalhos(lista, log):
    """lista: [(arquivo.lnk, alvo, argumentos, ícone, pasta_de_trabalho)]"""
    if not WIN or not lista:
        return True
    for lnk, *_ in lista:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
    linhas = ["$ProgressPreference='SilentlyContinue'", "$ErrorActionPreference='Stop'",
              "$w = New-Object -ComObject WScript.Shell"]
    for lnk, alvo, args, ico, wd in lista:
        linhas += ["$s = $w.CreateShortcut(%s)" % _ps_str(lnk),
                   "$s.TargetPath = %s" % _ps_str(alvo),
                   "$s.Arguments = %s" % _ps_str(args or ""),
                   "$s.WorkingDirectory = %s" % _ps_str(wd),
                   "$s.IconLocation = %s" % _ps_str(ico + ",0"),
                   "$s.Save()"]
    script = "\n".join(linhas)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    roda(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-EncodedCommand", enc], timeout=90)
    faltando = [x for x in lista if not os.path.isfile(x[0])]
    if faltando:
        # plano B: VBScript (não depende de módulos do PowerShell)
        vbs = ['Set w = CreateObject("WScript.Shell")']
        for lnk, alvo, args, ico, wd in faltando:
            def q(t):
                return '"' + t.replace('"', '""') + '"'
            vbs += ["Set s = w.CreateShortcut(%s)" % q(lnk), "s.TargetPath = %s" % q(alvo),
                    "s.Arguments = %s" % q(args or ""), "s.WorkingDirectory = %s" % q(wd),
                    "s.IconLocation = %s" % q(ico + ",0"), "s.Save"]
        arq = os.path.join(tempfile.gettempdir(), "argos-atalhos.vbs")
        with open(arq, "w", encoding="utf-16") as f:
            f.write("\r\n".join(vbs))
        roda(["cscript", "//nologo", "//U", arq], timeout=60)
        apaga(arq, 1)
    faltando = [x for x in lista if not os.path.isfile(x[0])]
    for x in faltando:
        log("  aviso: não consegui criar o atalho %s" % x[0])
    return not faltando


def _lnk_aponta_para(lnk, pasta):
    try:
        with open(lnk, "rb") as f:
            dados = f.read()
    except OSError:
        return False
    alvo = norm(pasta).lower()
    for txt in (dados.decode("utf-16-le", errors="ignore"),
                dados.decode("mbcs" if WIN else "latin-1", errors="ignore")):
        if alvo in txt.lower().replace("/", "\\"):
            return True
    return False


def atalhos_existentes(pasta):
    """Quais atalhos opcionais a instalação já tem (para manter na reparação)."""
    pa = pastas_atalhos()
    res = {"desktop": False, "inicializar": False}
    for chave in res:
        d = pa.get(chave)
        if d and os.path.isdir(d):
            for lnk in glob.glob(os.path.join(d, "*.lnk")):
                if _lnk_aponta_para(lnk, pasta):
                    res[chave] = True
    return res


def remover_atalhos(pasta, log):
    pa = pastas_atalhos()
    removidos = 0
    for chave in ("desktop", "programas", "inicializar"):
        d = pa.get(chave)
        if not d or not os.path.isdir(d):
            continue
        for lnk in glob.glob(os.path.join(d, "*.lnk")):
            if _lnk_aponta_para(lnk, pasta):
                removidos += apaga(lnk, 2)
    grupo = os.path.join(pa.get("programas") or "", APP)
    if pa.get("programas") and os.path.isdir(grupo):
        for lnk in glob.glob(os.path.join(grupo, "*.lnk")):
            if _lnk_aponta_para(lnk, pasta):
                removidos += apaga(lnk, 2)
        try:
            if not os.listdir(grupo):
                os.rmdir(grupo)
        except OSError:
            pass
    log("  %d atalho(s) removido(s)" % removidos)


def montar_atalhos(pasta, desktop, inicializar):
    pa = pastas_atalhos()
    ico = os.path.join(pasta, "icone", "argos.ico")
    exe = os.path.join(pasta, EXE_PROGRAMA)
    inst = os.path.join(pasta, EXE_INSTALAR)
    desi = os.path.join(pasta, EXE_DESINSTALAR)
    lista = []
    if pa.get("programas"):
        g = os.path.join(pa["programas"], APP)
        lista.append((os.path.join(g, "Argos.lnk"), exe, "", ico, pasta))
        if os.path.isfile(inst):
            lista.append((os.path.join(g, "Reparar Argos.lnk"), inst, "--reparar", ico, pasta))
        if os.path.isfile(desi):
            lista.append((os.path.join(g, "Desinstalar Argos.lnk"), desi, "", ico, pasta))
    if desktop and pa.get("desktop"):
        lista.append((os.path.join(pa["desktop"], "Argos.lnk"), exe, "", ico, pasta))
    if inicializar and pa.get("inicializar"):
        lista.append((os.path.join(pa["inicializar"], "Argos.lnk"), exe, "--fundo", ico, pasta))
    return lista


# ----------------------------------------------------------------------------
# Python / ambiente
# ----------------------------------------------------------------------------
_TESTE_PY = "import sys,struct;print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P')*8));print(sys.executable)"


def python_ok(cmd):
    """cmd: lista para executar o Python. Devolve (versão, exe) se 3.10–3.13 64 bits."""
    rc, out = roda(cmd + ["-c", _TESTE_PY], timeout=30)
    if rc != 0:
        return None
    linhas = out.strip().splitlines()
    if len(linhas) < 2:
        return None
    try:
        ver, bits = linhas[-2].split()
        maj, mi = (int(x) for x in ver.split("."))
    except ValueError:
        return None
    if maj == 3 and 10 <= mi <= 13 and bits == "64":
        return (maj, mi), linhas[-1].strip()
    return None


def achar_python():
    cands = []
    for v in ("3.13", "3.12", "3.11", "3.10"):
        cands.append(["py", "-%s" % v])
        nome = "Python3" + v.split(".")[1]
        for base in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
                     os.environ.get("ProgramFiles", r"C:\Program Files")):
            exe = os.path.join(base, nome, "python.exe")
            if os.path.isfile(exe):
                cands.append([exe])
    rc, out = roda(["where", "python"], timeout=15)
    for linha in out.splitlines():
        linha = linha.strip()
        if linha.lower().endswith("python.exe") and "windowsapps" not in linha.lower():
            cands.append([linha])
    melhor = None
    for c in cands:
        r = python_ok(c)
        if r and (melhor is None or r[0] > melhor[0]):
            melhor = r
            if r[0] == (3, 13):
                break
    return melhor[1] if melhor else None


def baixar_python(log):
    destino = os.path.join(tempfile.gettempdir(), "python-%s-amd64.exe" % PY_VERSAO_DOWNLOAD)
    log("Baixando o Python %s…" % PY_VERSAO_DOWNLOAD)
    urllib.request.urlretrieve(PY_URL, destino)
    log("Instalando o Python %s (só para este usuário)…" % PY_VERSAO_DOWNLOAD)
    rc, out = roda([destino, "/quiet", "InstallAllUsers=0", "PrependPath=0",
                    "Include_launcher=1", "Include_test=0", "Include_tcltk=1"], timeout=900)
    apaga(destino, 1)
    if rc != 0:
        log(out[-2000:])
        raise RuntimeError("o instalador do Python terminou com erro (%s)" % rc)
    return achar_python()


def venv_python(pasta):
    return os.path.join(pasta, ".venv", "Scripts", "python.exe")


def venv_funciona(pasta):
    py = venv_python(pasta)
    return os.path.isfile(py) and python_ok([py]) is not None


def venv_completo(pasta, log):
    rc, out = roda([venv_python(pasta), "-c", "import flask, cv2, requests, ultralytics, neonize"],
                   timeout=300, cwd=pasta)
    if rc != 0:
        log("  faltam componentes: " + (out.strip().splitlines() or ["?"])[-1])
    return rc == 0


def criar_venv(pasta, py, log):
    log("Criando ambiente do Python…")
    rc, out = roda([py, "-m", "venv", os.path.join(pasta, ".venv")], timeout=600)
    if rc != 0 or not venv_funciona(pasta):
        log(out[-2000:])
        raise RuntimeError("não consegui criar o ambiente do Python (.venv)")
    v = python_ok([venv_python(pasta)])
    log("  Python %d.%d" % v[0])


def pip_instalar(pasta, log, status):
    log("Instalando componentes (OpenCV, YOLO, WhatsApp)… pode levar vários minutos")
    cmd = [venv_python(pasta), "-m", "pip", "install", "-r", "requirements.txt",
           "--disable-pip-version-check", "--no-input", "--progress-bar", "off"]
    p = subprocess.Popen(cmd, cwd=pasta, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=SEM_JANELA)
    ultimas = []
    for b in iter(p.stdout.readline, b""):
        linha = decodifica(b).rstrip()
        if not linha:
            continue
        ultimas = (ultimas + [linha])[-30:]
        if linha.startswith(("Collecting", "Downloading", "Installing", "Successfully")):
            status(linha[:110])
        if linha.startswith(("Collecting", "Successfully", "ERROR")):
            log("  " + linha)
    p.wait()
    if p.returncode != 0:
        log("\n".join(ultimas))
        raise RuntimeError("o pip não conseguiu instalar os componentes (sem internet?)")
    shutil.copyfile(os.path.join(pasta, "requirements.txt"),
                    os.path.join(pasta, ".venv", "requirements.instalado"))


# ----------------------------------------------------------------------------
# arquivos do programa
# ----------------------------------------------------------------------------
def itens_do_pacote(zf):
    tops = []
    for n in zf.namelist():
        t = n.replace("\\", "/").split("/")[0]
        if t and t not in tops:
            tops.append(t)
    return tops


def extrair_pacote(pacote, pasta, sobrescrever, log):
    """sobrescrever=False (reparar): só repõe o que falta.
    Os executáveis do instalador/desinstalador são sempre atualizados."""
    sempre = {EXE_DESINSTALAR, MODELO_CAMERAS, "iniciar.bat"}
    novos = 0
    with zipfile.ZipFile(pacote) as zf:
        tops = itens_do_pacote(zf)
        for info in zf.infolist():
            nome = info.filename.replace("\\", "/")
            if nome.endswith("/"):
                continue
            destino = os.path.join(pasta, *nome.split("/"))
            if not sobrescrever and os.path.exists(destino) and nome.split("/")[0] not in sempre:
                continue
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            tmp = destino + ".novo"
            with zf.open(info) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            try:
                os.replace(tmp, destino)
            except PermissionError:
                apaga(destino)
                os.replace(tmp, destino)
            novos += 1
    with open(os.path.join(pasta, MANIFESTO), "w", encoding="utf-8") as f:
        f.write("\n".join(tops) + "\n")
    log("  %d arquivo(s) %s" % (novos, "copiados" if sobrescrever else "repostos"))
    return tops


def garantir_cameras(pasta, log):
    cj = os.path.join(pasta, "cameras.json")
    if not os.path.isfile(cj) and os.path.isfile(os.path.join(pasta, MODELO_CAMERAS)):
        shutil.copyfile(os.path.join(pasta, MODELO_CAMERAS), cj)
        log("  cameras.json criado (sem câmeras)")
    os.makedirs(os.path.join(pasta, "media"), exist_ok=True)


def trazer_dados(origem, pasta, log):
    if not origem or norm(origem) == norm(pasta):
        return
    log("Copiando câmeras e gravações de %s…" % origem)
    n = 0
    cj = os.path.join(origem, "cameras.json")
    if os.path.isfile(cj):
        shutil.copy2(cj, os.path.join(pasta, "cameras.json"))
        n += 1
    m = os.path.join(origem, "media")
    if os.path.isdir(m):
        for raiz, _d, arqs in os.walk(m):
            rel = os.path.relpath(raiz, m)
            dst = os.path.join(pasta, "media", rel)
            os.makedirs(dst, exist_ok=True)
            for a in arqs:
                try:
                    shutil.copy2(os.path.join(raiz, a), os.path.join(dst, a))
                    n += 1
                except OSError as e:
                    log("  aviso: %s (%s)" % (a, e))
    log("  %d arquivo(s)" % n)


REINICIAR_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Reiniciar Argos
echo Encerrando o Argos...
taskkill /IM Argos.exe /F /T >nul 2>nul
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq Argos - servidor*" /T /F >nul 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :{porta} ^| findstr LISTENING') do taskkill /F /T /PID %%a >nul 2>nul
timeout /t 1 >nul
echo Abrindo o Argos de novo...
start "" "%~dp0Argos.exe"
"""


def escrever_reiniciar(pasta):
    with open(os.path.join(pasta, "reiniciar.bat"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(REINICIAR_BAT.replace("{porta}", str(ler_porta(pasta))))


def copiar_instalador(pasta, log):
    """Deixa uma cópia deste instalador dentro da pasta do Argos."""
    if not congelado() or not caminho_pacote():
        log("  (modo de desenvolvimento: instalador não copiado)")
        return
    destino = os.path.join(pasta, EXE_INSTALAR)
    try:
        if os.path.exists(destino) and os.path.samefile(sys.executable, destino):
            return
    except OSError:
        pass
    shutil.copy2(sys.executable, destino + ".novo")
    os.replace(destino + ".novo", destino)
    log("  instalador guardado em %s" % destino)


def remover_programa(pasta, manter_dados, log):
    """Apaga só o que é do Argos. Devolve o que ficou na pasta."""
    itens = list(PROGRAMA_PADRAO)
    man = os.path.join(pasta, MANIFESTO)
    if os.path.isfile(man):
        with open(man, encoding="utf-8") as f:
            itens += [x.strip() for x in f if x.strip()]
    itens += EXTRAS
    for pad in EXTRAS_PADROES:
        itens += [os.path.basename(x) for x in glob.glob(os.path.join(pasta, pad))]
    if not manter_dados:
        itens += DADOS
        for pad in DADOS_PADROES:
            itens += [os.path.basename(x) for x in glob.glob(os.path.join(pasta, pad))]
    falhas = []
    vistos = set()
    for nome in itens:
        if (not nome or nome in vistos or nome in PROTEGIDOS or "/" in nome or "\\" in nome
                or nome in (".", "..")):
            continue
        if manter_dados and (nome in DADOS or any(fnmatch.fnmatch(nome, p) for p in DADOS_PADROES)):
            continue
        vistos.add(nome)
        alvo = os.path.join(pasta, nome)
        if os.path.lexists(alvo):
            if congelado() and dentro(sys.executable, alvo):
                continue
            if not apaga(alvo):
                falhas.append(nome)
    for f in falhas:
        log("  não consegui apagar: %s" % f)
    try:
        ficou = sorted(os.listdir(pasta))
    except OSError:
        ficou = []
    if not ficou:
        try:
            os.rmdir(pasta)
            log("  pasta %s removida" % pasta)
        except OSError:
            pass
    return ficou, falhas


# ----------------------------------------------------------------------------
# operações (rodam numa thread)
# ----------------------------------------------------------------------------
class Cancelado(Exception):
    pass


def op_instalar(pasta, modo, opc, ui):
    """modo: instalar | reparar | reinstalar"""
    log, status, prog = ui.log, ui.status, ui.progresso
    pacote = caminho_pacote()
    if not pacote:
        raise RuntimeError("este executável não tem o pacote do programa (use o Instalar Argos.exe)")
    log("=== %s em %s — %s (instalador %s)" % (
        {"instalar": "Instalação", "reparar": "Reparação", "reinstalar": "Reinstalação"}[modo],
        pasta, datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), VERSAO))
    os.makedirs(pasta, exist_ok=True)
    ui.abrir_log(os.path.join(pasta, "instalacao.log"))

    prog(3)
    status("Fechando o Argos…")
    fechar_argos(ler_porta(pasta), log)

    prog(8)
    status("Copiando arquivos do programa…")
    log("Copiando arquivos do programa…")
    extrair_pacote(pacote, pasta, sobrescrever=(modo != "reparar"), log=log)

    prog(18)
    if opc.get("trazer"):
        status("Copiando câmeras e gravações…")
        trazer_dados(opc["trazer"], pasta, log)
    garantir_cameras(pasta, log)

    prog(25)
    status("Preparando o Python…")
    venv = os.path.join(pasta, ".venv")
    if modo == "reinstalar" and os.path.isdir(venv):
        log("Apagando o ambiente do Python antigo…")
        if not apaga(venv):
            raise RuntimeError("não consegui apagar a pasta .venv (algum programa está usando?)")
    if os.path.isdir(venv) and not venv_funciona(pasta):
        log("O ambiente do Python está quebrado; recriando…")
        apaga(venv)
    if not os.path.isdir(venv):
        py = achar_python()
        if not py:
            if not ui.perguntar("Não encontrei o Python 3.10–3.13 (64 bits) neste computador.\n\n"
                                "Baixar e instalar o Python %s agora? (precisa de internet)"
                                % PY_VERSAO_DOWNLOAD):
                raise RuntimeError("o Python é necessário para o Argos funcionar")
            status("Baixando o Python…")
            py = baixar_python(log)
            if not py:
                raise RuntimeError("instalei o Python mas não consegui encontrá-lo")
        criar_venv(pasta, py, log)

    prog(32)
    status("Instalando componentes… (pode levar vários minutos)")
    pip_instalar(pasta, log, status)
    if modo == "reparar":
        prog(80)
        status("Conferindo os componentes…")
        log("Conferindo os componentes…")
        if not venv_completo(pasta, log):
            log("Recriando o ambiente do Python do zero…")
            apaga(venv)
            py = achar_python()
            if not py:
                raise RuntimeError("não encontrei o Python para recriar o ambiente")
            criar_venv(pasta, py, log)
            pip_instalar(pasta, log, status)
            if not venv_completo(pasta, log):
                raise RuntimeError("os componentes continuam faltando mesmo depois de reinstalar")

    prog(88)
    status("Criando atalhos e registrando no Windows…")
    log("Criando o programa e os atalhos…")
    escrever_reiniciar(pasta)
    copiar_instalador(pasta, log)
    remover_atalhos(pasta, lambda *_: None)   # tira atalhos antigos/duplicados
    criar_atalhos(montar_atalhos(pasta, opc.get("desktop"), opc.get("inicializar")), log)
    try:
        registro_gravar(pasta)
        log("  registrado em Configurações → Apps instalados")
    except Exception as e:  # noqa: BLE001
        log("  aviso: não consegui registrar em Apps instalados (%s)" % e)
    prog(100)
    log("Pronto.")


def op_desinstalar(pasta, manter, ui):
    log, status, prog = ui.log, ui.status, ui.progresso
    ui.abrir_log(os.path.join(tempfile.gettempdir(), "argos-desinstalacao.log"))
    log("=== Desinstalação de %s — %s (manter dados: %s)" % (
        pasta, datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), "sim" if manter else "não"))
    if not pasta_pode_ser_apagada(pasta):
        raise RuntimeError("%s não parece ser uma pasta do Argos; nada foi apagado" % pasta)
    prog(5)
    status("Fechando o Argos…")
    fechar_argos(ler_porta(pasta), log)
    prog(25)
    status("Removendo atalhos…")
    log("Removendo atalhos…")
    remover_atalhos(pasta, log)
    prog(35)
    status("Removendo do Windows…")
    if registro_ler() is None or norm(registro_ler()) == norm(pasta):
        registro_apagar()
    prog(45)
    status("Apagando arquivos…")
    log("Apagando arquivos…")
    ficou, falhas = remover_programa(pasta, manter, log)
    prog(100)
    log("Pronto.")
    return ficou, falhas


# ----------------------------------------------------------------------------
# relançar a partir de uma cópia temporária (para poder apagar a pasta)
# ----------------------------------------------------------------------------
def relancar_do_temp(args):
    tmp = os.path.join(tempfile.gettempdir(), "argos-desinstalar-%d.exe" % os.getpid())
    shutil.copy2(sys.executable, tmp)
    subprocess.Popen([tmp] + args + ["--temp"], close_fds=True, creationflags=DESANEXADO,
                     cwd=tempfile.gettempdir())


def apagar_copia_temp():
    if not (WIN and congelado() and "--temp" in sys.argv):
        return
    exe = sys.executable
    cmd = 'ping -n 6 127.0.0.1 >nul & del /f /q "%s"' % exe
    subprocess.Popen(["cmd", "/c", cmd], creationflags=SEM_JANELA | DESANEXADO,
                     cwd=tempfile.gettempdir(), close_fds=True)


# ----------------------------------------------------------------------------
# interface
# ----------------------------------------------------------------------------
import tkinter as tk  # noqa: E402
from tkinter import ttk, filedialog, messagebox  # noqa: E402

COR_ESCURA = "#132a4d"
COR_AZUL = "#1d4b8f"
COR_FUNDO = "#fafbfc"
COR_TEXTO = "#1f2733"
COR_SUAVE = "#5b6575"
FONTE = "Segoe UI" if WIN else "DejaVu Sans"


class Assistente(tk.Tk):
    def __init__(self, inicio, pasta=None, manter=True, executar=False):
        super().__init__()
        self.title("Argos — " + ("Desinstalar" if inicio == "desinstalar" else "Instalação"))
        self.geometry("640x470")
        self.resizable(False, False)
        self.configure(bg=COR_FUNDO)
        try:
            if WIN and os.path.isfile(recurso("argos.ico")):
                self.iconbitmap(recurso("argos.ico"))
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.fechar)
        self.q = queue.Queue()
        self.ocupado = False
        self.arq_log = None
        self.linhas_log = []
        self.historico = []
        self.pasta = tk.StringVar(value=pasta or PASTA_PADRAO)
        self.modo = tk.StringVar(value="reparar")
        self.desktop = tk.BooleanVar(value=True)
        self.inicializar = tk.BooleanVar(value=True)
        self.abrir_fim = tk.BooleanVar(value=True)
        self.manter = tk.BooleanVar(value=manter)
        self.trazer_on = tk.BooleanVar(value=False)
        self.trazer = tk.StringVar(value="")
        self.existente = pasta if (pasta and e_instalacao(pasta)) else None

        self._estilo()
        self._montar()
        self._id_poll = self.after(100, self._poll)

        if inicio == "desinstalar":
            if executar and pasta:
                self.after(300, lambda: self.iniciar_desinstalacao(pasta, manter, ja_confirmado=True))
            else:
                self.tela_desinstalar()
        elif inicio in ("reparar", "reinstalar") and self.existente:
            self.modo.set(inicio)
            self.pasta.set(self.existente)
            self._prefill_atalhos()
            self.tela_opcoes()
        elif self.existente:
            self.pasta.set(self.existente)
            self.tela_manutencao()
        else:
            self.tela_boas_vindas()

    # --- estrutura -------------------------------------------------------
    def _estilo(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista" if WIN else "clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=COR_FUNDO)
        st.configure("TLabel", background=COR_FUNDO, foreground=COR_TEXTO, font=(FONTE, 10))
        st.configure("Suave.TLabel", foreground=COR_SUAVE, font=(FONTE, 9))
        st.configure("Titulo.TLabel", font=(FONTE, 15, "bold"), foreground=COR_ESCURA)
        st.configure("TCheckbutton", background=COR_FUNDO, font=(FONTE, 10))
        st.configure("TRadiobutton", background=COR_FUNDO, font=(FONTE, 10, "bold"))
        st.configure("TButton", font=(FONTE, 10), padding=(14, 4))
        st.configure("Horizontal.TProgressbar", troughcolor="#e6e9ef")

    def _montar(self):
        topo = tk.Frame(self, bg=COR_ESCURA, height=64)
        topo.pack(fill="x")
        topo.pack_propagate(False)
        self.img = None
        try:
            img = tk.PhotoImage(file=recurso("argos-256.png"))
            self.img = img.subsample(max(1, img.width() // 44))
            tk.Label(topo, image=self.img, bg=COR_ESCURA).pack(side="left", padx=(16, 10))
        except Exception:  # noqa: BLE001
            pass
        tk.Label(topo, text="Argos", fg="white", bg=COR_ESCURA,
                 font=(FONTE, 17, "bold")).pack(side="left")
        tk.Label(topo, text="versão " + VERSAO, fg="#b9c6dc", bg=COR_ESCURA,
                 font=(FONTE, 9)).pack(side="left", padx=10, pady=(8, 0))

        self.corpo = ttk.Frame(self, padding=(28, 18, 28, 8))
        self.corpo.pack(fill="both", expand=True)

        tk.Frame(self, bg="#dfe3ea", height=1).pack(fill="x")
        rodape = ttk.Frame(self, padding=(16, 10))
        rodape.pack(fill="x")
        self.b_cancelar = ttk.Button(rodape, text="Cancelar", command=self.fechar)
        self.b_cancelar.pack(side="right")
        self.b_avancar = ttk.Button(rodape, text="Avançar")
        self.b_avancar.pack(side="right", padx=8)
        self.b_voltar = ttk.Button(rodape, text="Voltar", command=self.voltar)
        self.b_voltar.pack(side="right")

    def _limpa(self, titulo, sub=None):
        for w in self.corpo.winfo_children():
            w.destroy()
        ttk.Label(self.corpo, text=titulo, style="Titulo.TLabel").pack(anchor="w")
        if sub:
            ttk.Label(self.corpo, text=sub, style="Suave.TLabel", wraplength=580,
                      justify="left").pack(anchor="w", pady=(4, 12))

    def _botoes(self, avancar_txt, avancar_cmd, voltar=True, cancelar=True):
        self.b_avancar.configure(text=avancar_txt, command=avancar_cmd,
                                 state="normal" if avancar_cmd else "disabled")
        if avancar_txt:
            self.b_avancar.pack(side="right", padx=8, before=self.b_voltar)
        else:
            self.b_avancar.pack_forget()
        self.b_voltar.configure(state="normal" if (voltar and self.historico) else "disabled")
        self.b_cancelar.configure(state="normal" if cancelar else "disabled")

    def ir(self, tela):
        self.historico.append(self._tela_atual)
        tela()

    def voltar(self):
        if self.historico:
            t = self.historico.pop()
            t()

    def fechar(self):
        if self.ocupado:
            messagebox.showinfo(APP, "Aguarde terminar. Interromper agora pode deixar o Argos incompleto.",
                                parent=self)
            return
        self.destroy()

    # --- telas ---------------------------------------------------------------
    def tela_boas_vindas(self):
        self._tela_atual = self.tela_boas_vindas
        self._limpa("Bem-vindo ao instalador do Argos",
                    "O Argos junta suas câmeras num painel no navegador, com detecção de "
                    "pessoas e veículos e alertas pelo WhatsApp. Tudo roda neste computador.")
        for t in ("• Instala o programa e o ambiente do Python (com OpenCV e YOLO)",
                  "• Cria atalhos e o ícone perto do relógio",
                  "• Deixa o instalador e o desinstalador dentro da pasta do Argos",
                  "• Registra em Configurações → Apps instalados (para desinstalar/reparar)",
                  "",
                  "Precisa de internet e cerca de %.0f GB livres. Leva de 5 a 20 minutos." % ESPACO_MIN_GB):
            ttk.Label(self.corpo, text=t, wraplength=580, justify="left").pack(anchor="w", pady=1)
        self._botoes("Avançar", lambda: self.ir(self.tela_pasta), voltar=False)

    def tela_manutencao(self):
        self._tela_atual = self.tela_manutencao
        self._limpa("O Argos já está instalado",
                    "Pasta: %s\nO que você quer fazer?" % self.pasta.get())
        opcoes = [
            ("reparar", "Reparar",
             "Repõe arquivos que estiverem faltando, conserta o ambiente do Python, recria os "
             "atalhos e o registro no Windows. Mantém câmeras, configurações e gravações."),
            ("reinstalar", "Reinstalar",
             "Substitui todos os arquivos do programa e baixa de novo os componentes do Python "
             "(demora mais). Mantém câmeras, configurações e gravações."),
            ("desinstalar", "Desinstalar",
             "Fecha o Argos e remove o programa, os atalhos e o registro. Você escolhe se quer "
             "guardar as câmeras e gravações."),
        ]
        for val, nome, desc in opcoes:
            ttk.Radiobutton(self.corpo, text=nome, value=val, variable=self.modo).pack(anchor="w", pady=(8, 0))
            ttk.Label(self.corpo, text=desc, style="Suave.TLabel", wraplength=550,
                      justify="left").pack(anchor="w", padx=(24, 0))

        def seguir():
            if self.modo.get() == "desinstalar":
                self.ir(self.tela_desinstalar)
            else:
                self._prefill_atalhos()
                self.ir(self.tela_opcoes)
        self._botoes("Avançar", seguir, voltar=False)

    def _prefill_atalhos(self):
        ex = atalhos_existentes(self.pasta.get())
        if ex["desktop"] or ex["inicializar"]:
            self.desktop.set(ex["desktop"])
            self.inicializar.set(ex["inicializar"])

    def tela_pasta(self):
        self._tela_atual = self.tela_pasta
        self._limpa("Onde instalar",
                    "Escolha a pasta do Argos. Prefira um caminho curto e sem acentos.")
        linha = ttk.Frame(self.corpo)
        linha.pack(fill="x", pady=6)
        e = ttk.Entry(linha, textvariable=self.pasta, font=(FONTE, 10))
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(linha, text="Procurar…", command=self._escolher_pasta).pack(side="left", padx=(8, 0))
        self.aviso_pasta = ttk.Label(self.corpo, text="", style="Suave.TLabel", wraplength=580, justify="left")
        self.aviso_pasta.pack(anchor="w", pady=(6, 0))
        self.pasta.trace_add("write", lambda *_: self._checar_pasta())
        self._checar_pasta()
        self._botoes("Avançar", self._pasta_ok)

    def _escolher_pasta(self):
        d = filedialog.askdirectory(parent=self, initialdir=os.path.dirname(self.pasta.get()) or "C:\\")
        if d:
            d = os.path.normpath(d)
            if os.listdir(d) and not e_instalacao(d):
                d = os.path.join(d, APP)
            self.pasta.set(d)

    def _checar_pasta(self):
        if not hasattr(self, "aviso_pasta") or not self.aviso_pasta.winfo_exists():
            return
        p = self.pasta.get().strip()
        msgs = []
        if tem_acento(p):
            msgs.append("Atenção: a pasta tem acento ou caractere especial; alguns componentes podem falhar.")
        if e_instalacao(p):
            msgs.append("Já existe um Argos nessa pasta: os arquivos do programa serão substituídos "
                        "e as câmeras e gravações mantidas.")
        elif os.path.isdir(p) and os.listdir(p):
            msgs.append("A pasta já tem outros arquivos. Eles não serão apagados.")
        try:
            drive = os.path.splitdrive(os.path.abspath(p))[0] + os.sep
            livre = shutil.disk_usage(drive).free / 1024 ** 3
            msgs.append("Espaço livre em %s %.1f GB%s" % (drive, livre,
                        "  — pouco! precisa de uns %.0f GB" % ESPACO_MIN_GB if livre < ESPACO_MIN_GB else ""))
        except Exception:  # noqa: BLE001
            pass
        self.aviso_pasta.configure(text="\n".join(msgs))

    def _pasta_ok(self):
        p = self.pasta.get().strip().strip('"')
        if not p or not os.path.isabs(p) or not os.path.splitdrive(os.path.abspath(p))[1].strip("\\/"):
            messagebox.showwarning(APP, "Escolha uma pasta completa, por exemplo C:\\Argos.", parent=self)
            return
        self.pasta.set(os.path.normpath(p))
        self.modo.set("reinstalar" if e_instalacao(p) else "instalar")
        if e_instalacao(p):
            self._prefill_atalhos()
        self.ir(self.tela_opcoes)

    def tela_opcoes(self):
        self._tela_atual = self.tela_opcoes
        self._limpa("Opções", "Pasta: %s" % self.pasta.get())
        ttk.Checkbutton(self.corpo, text="Criar atalho na área de trabalho",
                        variable=self.desktop).pack(anchor="w", pady=3)
        ttk.Checkbutton(self.corpo, text="Abrir o Argos junto com o Windows (fica perto do relógio)",
                        variable=self.inicializar).pack(anchor="w", pady=3)
        ttk.Label(self.corpo, text="O menu Iniciar sempre ganha Argos, Reparar Argos e Desinstalar Argos.",
                  style="Suave.TLabel").pack(anchor="w", pady=(0, 6))
        if self.modo.get() == "instalar":
            ttk.Checkbutton(self.corpo, text="Trazer câmeras e gravações de outra instalação:",
                            variable=self.trazer_on, command=self._toggle_trazer).pack(anchor="w", pady=(10, 2))
            linha = ttk.Frame(self.corpo)
            linha.pack(fill="x", padx=(24, 0))
            self.e_trazer = ttk.Entry(linha, textvariable=self.trazer)
            self.e_trazer.pack(side="left", fill="x", expand=True)
            self.b_trazer = ttk.Button(linha, text="Procurar…", command=self._escolher_origem)
            self.b_trazer.pack(side="left", padx=(8, 0))
            if not self.trazer.get():
                ant = registro_ler()
                if ant and e_instalacao(ant) and norm(ant) != norm(self.pasta.get()):
                    self.trazer.set(ant)
                    self.trazer_on.set(True)
            self._toggle_trazer()
        self._botoes("Avançar", self._opcoes_ok)

    def _toggle_trazer(self):
        st = "normal" if self.trazer_on.get() else "disabled"
        self.e_trazer.configure(state=st)
        self.b_trazer.configure(state=st)

    def _escolher_origem(self):
        d = filedialog.askdirectory(parent=self, title="Pasta com o cameras.json")
        if d:
            self.trazer.set(os.path.normpath(d))

    def _opcoes_ok(self):
        if self.modo.get() == "instalar" and self.trazer_on.get():
            if not os.path.isfile(os.path.join(self.trazer.get(), "cameras.json")):
                messagebox.showwarning(APP, "Não achei o cameras.json nessa pasta.", parent=self)
                return
        self.ir(self.tela_verificacao)

    def tela_verificacao(self):
        self._tela_atual = self.tela_verificacao
        modo = self.modo.get()
        nome = {"instalar": "Instalar", "reparar": "Reparar", "reinstalar": "Reinstalar"}[modo]
        self._limpa("Tudo pronto para %s" % nome.lower(), "Confira e clique em %s." % nome)
        itens = ["Pasta: %s" % self.pasta.get()]
        if modo == "reparar":
            itens.append("Repor arquivos que faltam e conferir o ambiente do Python")
        elif modo == "reinstalar":
            itens.append("Substituir os arquivos do programa e baixar de novo os componentes do Python")
        else:
            itens.append("Copiar o programa e instalar os componentes do Python")
        if modo != "instalar" or e_instalacao(self.pasta.get()):
            itens.append("Câmeras, configurações e gravações: mantidas")
        elif self.trazer_on.get():
            itens.append("Trazer câmeras e gravações de: %s" % self.trazer.get())
        at = ["menu Iniciar"]
        if self.desktop.get():
            at.append("área de trabalho")
        if self.inicializar.get():
            at.append("iniciar com o Windows")
        itens.append("Atalhos: " + ", ".join(at))
        itens.append("Instalador e desinstalador guardados dentro da pasta")
        itens.append("O Argos será fechado durante o processo, se estiver aberto")
        for t in itens:
            ttk.Label(self.corpo, text="• " + t, wraplength=580, justify="left").pack(anchor="w", pady=2)
        self._botoes(nome, self.iniciar_instalacao)

    def tela_progresso(self, titulo):
        self._tela_atual = None
        self._limpa(titulo)
        self.lb_status = ttk.Label(self.corpo, text="Começando…", wraplength=580)
        self.lb_status.pack(anchor="w", pady=(8, 6))
        self.barra = ttk.Progressbar(self.corpo, length=580, mode="determinate", maximum=100)
        self.barra.pack(fill="x")
        caixa = ttk.Frame(self.corpo)
        caixa.pack(fill="both", expand=True, pady=(12, 0))
        self.txt = tk.Text(caixa, height=12, font=("Consolas" if WIN else "DejaVu Sans Mono", 9),
                           bg="white", fg=COR_TEXTO, relief="solid", bd=1, wrap="word")
        sb = ttk.Scrollbar(caixa, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set, state="disabled")
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._botoes("", None, voltar=False, cancelar=False)

    def tela_desinstalar(self):
        self._tela_atual = self.tela_desinstalar
        achada = self.pasta.get() if e_instalacao(self.pasta.get()) else None
        self._limpa("Desinstalar o Argos",
                    "O Argos será fechado e removido deste computador." if achada else
                    "Não encontrei o Argos instalado. Indique a pasta dele.")
        linha = ttk.Frame(self.corpo)
        linha.pack(fill="x", pady=6)
        ttk.Label(linha, text="Pasta:").pack(side="left", padx=(0, 6))
        ttk.Entry(linha, textvariable=self.pasta).pack(side="left", fill="x", expand=True)
        ttk.Button(linha, text="Procurar…",
                   command=lambda: self.pasta.set(os.path.normpath(
                       filedialog.askdirectory(parent=self) or self.pasta.get()))).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(self.corpo, text="Guardar as câmeras, configurações e gravações",
                        variable=self.manter).pack(anchor="w", pady=(14, 2))
        ttk.Label(self.corpo, text="Ficam o cameras.json e a pasta media. Se instalar de novo na mesma "
                                   "pasta, tudo volta como estava. "
                                   "Desmarque para apagar tudo, inclusive o histórico e a conexão do WhatsApp.",
                  style="Suave.TLabel", wraplength=550, justify="left").pack(anchor="w", padx=(24, 0))
        self._botoes("Desinstalar", lambda: self.iniciar_desinstalacao(self.pasta.get(), self.manter.get()))

    def tela_concluido(self, ok, titulo, texto, abrir=False):
        self._tela_atual = None
        self._limpa(titulo)
        ttk.Label(self.corpo, text=texto, wraplength=580, justify="left").pack(anchor="w", pady=(8, 8))
        if ok and abrir:
            ttk.Checkbutton(self.corpo, text="Abrir o Argos agora", variable=self.abrir_fim).pack(anchor="w", pady=6)
        if self.arq_log:
            ttk.Label(self.corpo, text="Registro detalhado: %s" % self.arq_log,
                      style="Suave.TLabel", wraplength=580).pack(anchor="w", pady=(10, 0))
        if not ok and self.linhas_log:
            t = tk.Text(self.corpo, height=8, font=("Consolas" if WIN else "DejaVu Sans Mono", 9),
                        bg="white", relief="solid", bd=1, wrap="word")
            t.insert("end", "\n".join(self.linhas_log[-40:]))
            t.see("end")
            t.configure(state="disabled")
            t.pack(fill="both", expand=True, pady=(8, 0))

        def concluir():
            if ok and abrir and self.abrir_fim.get():
                exe = os.path.join(self.pasta.get(), EXE_PROGRAMA)
                if os.path.isfile(exe):
                    subprocess.Popen([exe], cwd=self.pasta.get(), creationflags=DESANEXADO, close_fds=True)
            self.destroy()
        self._botoes("Concluir", concluir, voltar=False, cancelar=False)

    # --- execução ----------------------------------------------------------
    def iniciar_instalacao(self):
        modo = self.modo.get()
        pasta = self.pasta.get()
        opc = {"desktop": self.desktop.get(), "inicializar": self.inicializar.get(),
               "trazer": self.trazer.get() if (modo == "instalar" and self.trazer_on.get()) else None}
        titulo = {"instalar": "Instalando o Argos…", "reparar": "Reparando o Argos…",
                  "reinstalar": "Reinstalando o Argos…"}[modo]
        self.tela_progresso(titulo)

        def fim(ok, erro):
            if ok:
                nome = {"instalar": "instalado", "reparar": "reparado", "reinstalar": "reinstalado"}[modo]
                self.tela_concluido(True, "Argos %s" % nome,
                                    "Pronto! O Argos está em %s.\n\nDentro da pasta ficaram o "
                                    "\"%s\" (para reparar ou reinstalar) e o \"%s\". Eles também "
                                    "aparecem no menu Iniciar e em Configurações → Apps instalados."
                                    % (pasta, EXE_INSTALAR, EXE_DESINSTALAR), abrir=True)
            else:
                self.tela_concluido(False, "Não deu certo", "Parou com este problema:\n%s\n\n"
                                    "Você pode tentar de novo com Reparar." % erro)
        self._rodar(lambda: op_instalar(pasta, modo, opc, self), fim)

    def iniciar_desinstalacao(self, pasta, manter, ja_confirmado=False):
        pasta = os.path.normpath(pasta.strip().strip('"')) if pasta else ""
        if not pasta_pode_ser_apagada(pasta):
            messagebox.showerror(APP, "Essa pasta não parece ser do Argos:\n%s" % pasta, parent=self)
            return
        if not ja_confirmado:
            msg = ("Desinstalar o Argos de %s?\n\n%s" % (pasta, "Câmeras e gravações serão guardadas."
                   if manter else "TUDO será apagado, inclusive câmeras, gravações e o WhatsApp."))
            if not messagebox.askyesno(APP, msg, parent=self, icon="warning"):
                return
            # este exe está dentro da pasta? então desinstala a partir de uma cópia temporária
            if congelado() and dentro(sys.executable, pasta):
                relancar_do_temp(["--desinstalar", "--pasta", pasta, "--executar",
                                  "--manter", "1" if manter else "0"])
                self.destroy()
                return
        else:
            time.sleep(2)   # o processo que nos chamou precisa terminar e soltar o exe
        self.pasta.set(pasta)
        self.tela_progresso("Desinstalando o Argos…")

        def fim(ok, res):
            if ok:
                ficou, falhas = res
                txt = "O Argos foi removido deste computador."
                if manter:
                    txt += ("\n\nSuas câmeras e gravações ficaram guardadas em %s. Para usá-las de novo, "
                            "instale o Argos nessa mesma pasta." % pasta)
                elif ficou:
                    txt += "\n\nFicaram na pasta %s (não são do Argos ou estavam em uso):\n  %s" % (
                        pasta, "\n  ".join(ficou[:12]))
                if falhas:
                    txt += "\n\nAlguns arquivos não puderam ser apagados agora; apague a pasta manualmente " \
                           "depois de reiniciar o computador."
                self.tela_concluido(True, "Argos desinstalado", txt)
            else:
                self.tela_concluido(False, "Não deu certo", "Parou com este problema:\n%s" % res)
        self._rodar(lambda: op_desinstalar(pasta, manter, self), fim)

    def _rodar(self, func, fim):
        self.ocupado = True

        def alvo():
            try:
                r = func()
                self.q.put(("fim", fim, True, r))
            except Exception as e:  # noqa: BLE001
                self.log("ERRO: %s" % e)
                self.log(traceback.format_exc())
                self.q.put(("fim", fim, False, str(e)))
        threading.Thread(target=alvo, daemon=True).start()

    # chamadas a partir da thread de trabalho
    def abrir_log(self, caminho):
        self.arq_log = caminho
        try:
            with open(caminho, "a", encoding="utf-8") as f:
                f.write("\n".join(self.linhas_log) + "\n")
        except OSError:
            self.arq_log = None

    def log(self, texto):
        self.linhas_log.append(texto)
        if self.arq_log:
            try:
                with open(self.arq_log, "a", encoding="utf-8") as f:
                    f.write(texto + "\n")
            except OSError:
                pass
        self.q.put(("log", texto))

    def status(self, texto):
        self.q.put(("status", texto))

    def progresso(self, pct):
        self.q.put(("prog", pct))

    def perguntar(self, texto):
        ev = threading.Event()
        caixa = {}
        self.q.put(("perg", texto, ev, caixa))
        ev.wait()
        return caixa.get("r", False)

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                tipo = m[0]
                if tipo == "log" and hasattr(self, "txt") and self.txt.winfo_exists():
                    self.txt.configure(state="normal")
                    self.txt.insert("end", m[1] + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif tipo == "status" and hasattr(self, "lb_status") and self.lb_status.winfo_exists():
                    self.lb_status.configure(text=m[1])
                elif tipo == "prog" and hasattr(self, "barra") and self.barra.winfo_exists():
                    self.barra.configure(value=m[1])
                elif tipo == "perg":
                    m[3]["r"] = messagebox.askyesno(APP, m[1], parent=self)
                    m[2].set()
                elif tipo == "fim":
                    self.ocupado = False
                    m[1](m[2], m[3])
        except queue.Empty:
            pass
        try:
            self._id_poll = self.after(100, self._poll)
        except tk.TclError:
            pass

    def destroy(self):
        try:
            self.after_cancel(self._id_poll)
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


# ----------------------------------------------------------------------------
def _arg(nome, padrao=None):
    if nome in sys.argv:
        i = sys.argv.index(nome)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return padrao


def main():
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    nome_exe = os.path.basename(sys.executable if congelado() else __file__).lower()
    pasta = _arg("--pasta")
    desinstalar = ("--desinstalar" in sys.argv or "desinstalar" in nome_exe
                   or caminho_pacote() is None)
    if not pasta or not e_instalacao(pasta):
        pasta = pasta if (pasta and desinstalar) else (instalacao_existente() or pasta)
    if desinstalar:
        app = Assistente("desinstalar", pasta=pasta,
                         manter=_arg("--manter", "1") != "0",
                         executar="--executar" in sys.argv)
    else:
        inicio = "reparar" if "--reparar" in sys.argv else (
            "reinstalar" if "--reinstalar" in sys.argv else "auto")
        app = Assistente(inicio, pasta=pasta)
    try:
        app.mainloop()
    finally:
        apagar_copia_temp()


if __name__ == "__main__":
    main()
