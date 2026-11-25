@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Reiniciar Argos
echo Encerrando o Argos...
taskkill /IM Argos.exe /F /T >nul 2>nul
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq Argos - servidor*" /T /F >nul 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do taskkill /F /T /PID %%a >nul 2>nul
timeout /t 1 >nul
echo Abrindo o Argos de novo...
start "" "%~dp0Argos.exe"
