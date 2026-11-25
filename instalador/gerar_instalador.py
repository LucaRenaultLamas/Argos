# -*- coding: utf-8 -*-
"""
Gera o "Instalar Argos.exe" e o "Desinstalar Argos.exe" a partir da pasta do Argos
(a pasta acima desta). Rode pelo gerar-instalador.bat.

Saída: instalador\\dist\\Instalar Argos.exe  (o desinstalador vai dentro dele)
"""
import os
import sys
import json
import shutil
import zipfile
import fnmatch
import subprocess

AQUI = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(AQUI)
TRAB = os.path.join(AQUI, ".trabalho")
DIST = os.path.join(AQUI, "dist")
ICO = os.path.join(BASE, "icone", "argos.ico")
PNG = os.path.join(BASE, "icone", "argos-256.png")

# O que vai no pacote (pastas são copiadas inteiras, sem lixo)
PROGRAMA = ["app.py", "alerts.py", "onvif_ptz.py", "scanner.py", "whatsapp_local.py", "clipes.py", "placas.py",
            "requirements.txt", "iniciar.bat", "yolov8n.pt", "Argos.exe", "icone", "static"]
OPCIONAIS = ["README.md", "PRODUCT.md", "DESIGN.md"]
IGNORAR = ["__pycache__", "*.pyc", "*.bak", "*.log", "*.tmp", "*.novo"]


def ignorar(nome):
    return any(fnmatch.fnmatch(nome, p) for p in IGNORAR)


def montar_pacote(destino):
    falta = [p for p in PROGRAMA if not os.path.exists(os.path.join(BASE, p))]
    if falta:
        sys.exit("[ERRO] faltam na pasta %s: %s" % (BASE, ", ".join(falta)))
    n = 0
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in PROGRAMA + [o for o in OPCIONAIS if os.path.exists(os.path.join(BASE, o))]:
            src = os.path.join(BASE, item)
            if os.path.isfile(src):
                zf.write(src, item)
                n += 1
                continue
            for raiz, dirs, arqs in os.walk(src):
                dirs[:] = [d for d in dirs if not ignorar(d)]
                for a in arqs:
                    if ignorar(a):
                        continue
                    full = os.path.join(raiz, a)
                    zf.write(full, os.path.relpath(full, BASE).replace(os.sep, "/"))
                    n += 1
        # modelo de configuração: só o bloco "server" (sem senha) e nenhuma câmera
        server = {"host": "0.0.0.0", "port": 8080, "jpeg_quality": 70, "grid_fps": 15}
        try:
            with open(os.path.join(BASE, "cameras.json"), encoding="utf-8") as f:
                atual = json.load(f).get("server", {})
            server.update({k: v for k, v in atual.items() if k != "password"})
        except Exception:  # noqa: BLE001
            pass
        zf.writestr("cameras.modelo.json",
                    json.dumps({"server": server, "cameras": []}, ensure_ascii=False, indent=2))
        n += 1
    print("  pacote: %d arquivos, %.1f MB" % (n, os.path.getsize(destino) / 1024 ** 2))


def pyinstaller(nome, dados):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--windowed", "--name", nome, "--icon", ICO,
           "--distpath", DIST, "--workpath", os.path.join(TRAB, "build"),
           "--specpath", TRAB]
    for d in dados:
        cmd += ["--add-data", "%s%s." % (d, os.pathsep)]
    cmd.append(os.path.join(AQUI, "argos_instalador.py"))
    print("  > PyInstaller: %s" % nome)
    r = subprocess.run(cmd, cwd=AQUI)
    if r.returncode != 0:
        sys.exit("[ERRO] o PyInstaller falhou ao gerar %s" % nome)
    return os.path.join(DIST, nome + ".exe")


def main():
    print("Pasta do Argos: %s" % BASE)
    shutil.rmtree(TRAB, ignore_errors=True)
    os.makedirs(TRAB)
    os.makedirs(DIST, exist_ok=True)

    print("1/3 Gerando o desinstalador…")
    desi = pyinstaller("Desinstalar Argos", [PNG, ICO])

    print("2/3 Montando o pacote do programa…")
    pacote = os.path.join(TRAB, "payload.zip")
    montar_pacote(pacote)
    with zipfile.ZipFile(pacote, "a", zipfile.ZIP_STORED) as zf:
        zf.write(desi, "Desinstalar Argos.exe")

    print("3/3 Gerando o instalador…")
    inst = pyinstaller("Instalar Argos", [pacote, PNG, ICO])

    shutil.rmtree(TRAB, ignore_errors=True)
    print()
    print("Pronto:")
    print("  %s  (%.1f MB)" % (inst, os.path.getsize(inst) / 1024 ** 2))
    print("  %s  (%.1f MB, também vai dentro do instalador)" % (desi, os.path.getsize(desi) / 1024 ** 2))


if __name__ == "__main__":
    main()
