@echo off
rem Sobe o dashboard manualmente (sem o Agendador). Ctrl+C encerra.
rem Acesse http://127.0.0.1:8765/ (somente loopback).
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Ambiente virtual nao encontrado em "%CD%\.venv". Veja o README.
    exit /b 1
)
rem -P: nao poe a raiz do projeto em sys.path[0] (evita sequestro de modulos da stdlib).
".venv\Scripts\python.exe" -P -m bankmon serve
