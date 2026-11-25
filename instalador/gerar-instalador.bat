@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Gerar instalador do Argos

rem Ambiente separado (.build) so com o PyInstaller, para nao mexer no .venv do Argos.
set "PYB=%~dp0.build\Scripts\python.exe"
if exist "%PYB%" goto temvenv

set "PYS="
py -3.13 -c "import sys" >nul 2>nul && set "PYS=py -3.13"
if not defined PYS (py -3.12 -c "import sys" >nul 2>nul && set "PYS=py -3.12")
if not defined PYS (py -3.11 -c "import sys" >nul 2>nul && set "PYS=py -3.11")
if not defined PYS (py -3.10 -c "import sys" >nul 2>nul && set "PYS=py -3.10")
if not defined PYS (python -c "import sys" >nul 2>nul && set "PYS=python")
if not defined PYS goto sempython

echo Criando o ambiente de build (so na primeira vez)...
%PYS% -m venv .build
if errorlevel 1 goto falhou

:temvenv
echo Conferindo o PyInstaller...
"%PYB%" -m pip install --disable-pip-version-check -q --upgrade pyinstaller
if errorlevel 1 goto falhou

"%PYB%" gerar_instalador.py
if errorlevel 1 goto falhou
echo.
echo O instalador esta em: %~dp0dist\Instalar Argos.exe
pause
exit /b 0

:sempython
echo [ERRO] Python 3.10 a 3.13 nao encontrado.
pause
exit /b 1

:falhou
echo.
echo [ERRO] Nao consegui gerar o instalador. Veja as mensagens acima.
pause
exit /b 1
