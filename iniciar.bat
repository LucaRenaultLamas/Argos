@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Argos - servidor

rem Usa sempre o Python do ambiente DESTA pasta (.venv). Nao usa o activate.bat,
rem porque ele guarda o caminho da pasta onde o ambiente foi criado e quebra
rem quando o projeto e copiado/movido para outro lugar.
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Instale em https://www.python.org/downloads/ ^(marque "Add Python to PATH"^).
    pause
    exit /b 1
  )
  echo Criando o ambiente do Python...
  python -m venv .venv
)

"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo O ambiente .venv esta quebrado. Recriando...
  rmdir /s /q .venv
  python -m venv .venv
)

set FIRST=1

:run
rem Instala/atualiza as dependencias so quando o requirements.txt mudou
fc /b requirements.txt ".venv\requirements.instalado" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Instalando dependencias. Na primeira vez pode levar alguns minutos...
  echo.
  "%PY%" -m pip install -r requirements.txt --disable-pip-version-check
  if errorlevel 1 (
    echo.
    echo [AVISO] Nao consegui instalar tudo ^(sem internet?^). O sistema vai abrir assim mesmo.
    echo [%date% %time%] Falha ao instalar dependencias do requirements.txt>>"erros.log"
    timeout /t 10 >nul
  ) else (
    copy /y requirements.txt ".venv\requirements.instalado" >nul
  )
)

if "%FIRST%"=="1" (
  start "" http://localhost:8080
  set FIRST=0
)
cls
echo ============================================
echo   Argos em execucao
echo   Pasta: %~dp0
echo   Acesse: http://localhost:8080
echo   Os registros ficam na aba "Log" do sistema.
echo   Para parar: feche esta janela.
echo ============================================
"%PY%" app.py >nul 2>>"erros.log"
if exist ".restart" (
  del ".restart" >nul 2>nul
  goto run
)
rem caiu sem pedir reinicio: registra e sobe de novo sozinho
echo [%date% %time%] O servidor parou inesperadamente. Reiniciando...>>"erros.log"
echo.
echo O servidor parou inesperadamente. Reiniciando em 5 segundos...
echo (para encerrar de vez, feche esta janela)
timeout /t 5 /nobreak >nul
goto run
