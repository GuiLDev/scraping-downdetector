@echo off
rem Sobe o dashboard do ambiente de leitura pontual e abre o navegador (so loopback).
rem Nao gera a extensao, nao le o token e nao abre o Downdetector. Veja o README.
setlocal
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Ambiente virtual nao encontrado em "%CD%\.venv". Veja o README.
    pause
    exit /b 1
)
if not exist "data\leitura-pontual\leitura.toml" (
    echo Ambiente de leitura pontual nao preparado: falta "%CD%\data\leitura-pontual\leitura.toml".
    echo Prepare o ambiente: veja o README ^(secao Leitura pontual^).
    pause
    exit /b 1
)
set "BANKMON_CONFIG=%CD%\data\leitura-pontual\leitura.toml"
set "BANKMON_DATA_DIR=%CD%\data\leitura-pontual\data"
echo.
echo ROTEIRO DA LEITURA PONTUAL
echo  1. No navegador, abra a aba https://downdetector.com.br/ e espere a pagina carregar.
echo  2. Clique no icone da extensao "BankMonitor - Leitura Pontual".
echo  3. A dashboard recarrega sozinha quando a leitura chegar.
echo  4. FECHE ESTA JANELA ao terminar: o servidor so deve ficar no ar durante a leitura ^(SEC-002^).
echo.
".venv\Scripts\python.exe" -P -m bankmon serve --navegador --abrir
pause
