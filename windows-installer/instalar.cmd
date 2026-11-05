@echo off
rem Abre o assistente de instalacao do Siteblock. De um duplo clique neste arquivo.
rem O assistente e feito em Python: se o computador nao tiver um Python que sirva,
rem este arquivo pede permissao para baixar e instalar o do python.org.
setlocal
chcp 65001 >nul
title Instalar o Siteblock

rem A versão que é instalada quando falta o Python. As disponíveis estão em https://www.python.org/downloads/windows/
set "PYVER=3.14.10"

rem Serve um Python 3.10 ou mais novo que não seja o da Microsoft Store, que as tarefas do Windows não conseguem usar.
set "CHECK=import sys; sys.exit(sys.version_info < (3, 10) or 'WindowsApps' in getattr(sys, '_base_executable', sys.executable))"

rem O siteblock.py fica na pasta deste arquivo ou na de cima.
set "SCRIPT=%~dp0siteblock.py"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0..\siteblock.py"
if not exist "%SCRIPT%" goto :no_script

echo Procurando o Python...
call :find
if defined PY goto :run

rem Daqui em diante, só o código de saída 0 vale como "deu certo". O teste "if errorlevel 1" deixaria
rem passar os códigos negativos de um programa que travou ou foi fechado à força, e isso contaria como um "sim".

set "SB_MSG=O Siteblock precisa do Python 3.10 ou mais novo, e este computador não tem um que sirva.\n\nBaixar o Python %PYVER% de python.org (cerca de 35 MB) e instalar agora?\n\nPara instalar, o Windows vai pedir a permissão de administrador."
powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; $answer = [System.Windows.MessageBox]::Show($env:SB_MSG.Replace('\n', [Environment]::NewLine), 'Instalar o Siteblock', 'YesNo', 'Question'); exit ([int]($answer -ne 'Yes'))"
if not "%errorlevel%"=="0" goto :declined

set "ARCH=-amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=-arm64"
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 set "ARCH="
set "SB_FILE=%TEMP%\python-%PYVER%%ARCH%.exe"

echo Baixando o Python %PYVER% de python.org...
curl.exe -fL --progress-bar -o "%SB_FILE%" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%%ARCH%.exe"
if not "%errorlevel%"=="0" goto :no_download

rem Só roda o instalador se ele estiver assinado pela Python Software Foundation.
echo Conferindo a assinatura do instalador...
powershell -NoProfile -Command "$s = Get-AuthenticodeSignature -LiteralPath $env:SB_FILE; exit ([int]($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Python Software Foundation*'))"
if not "%errorlevel%"=="0" goto :bad_signature

rem Para todos os usuários: uma das tarefas do Siteblock roda como SYSTEM com este Python.
echo Instalando o Python. Confirme o aviso do Windows e espere a janela do instalador fechar...
start "" /wait "%SB_FILE%" /passive InstallAllUsers=1 PrependPath=1 Include_launcher=1
set "CODE=%errorlevel%"
del "%SB_FILE%" >nul 2>&1
rem 3010: instalou, e o Windows pede para reiniciar depois.
if not "%CODE%"=="0" if not "%CODE%"=="3010" goto :not_installed
call :find
if not defined PY goto :not_installed
echo Python instalado.

:run
%PY% "%SCRIPT%" setup
if "%errorlevel%"=="0" exit /b 0
echo.
pause
exit /b 1

rem Põe em PY o comando de um Python que sirva, ou deixa PY vazio.
:find
set "PY="
py -c "%CHECK%" >nul 2>&1 && set "PY=py"
if not defined PY python -c "%CHECK%" >nul 2>&1 && set "PY=python"
rem Logo depois de instalar, esta janela ainda não enxerga o PATH novo: procura nas pastas do instalador.
for /d %%D in ("%ProgramFiles%\Python3*" "%LocalAppData%\Programs\Python\Python3*") do if not defined PY "%%D\python.exe" -c "%CHECK%" >nul 2>&1 && set PY="%%D\python.exe"
exit /b 0

:no_script
echo Não encontrei o siteblock.py. Ele precisa estar na pasta deste arquivo ou na pasta de cima.
echo.
pause
exit /b 1

:declined
echo.
echo Sem o Python não dá para instalar o Siteblock, então nada foi instalado.
goto :manual

:no_download
del "%SB_FILE%" >nul 2>&1
echo.
echo Não consegui baixar o Python. Confira a conexão com a internet e tente de novo.
goto :manual

:bad_signature
del "%SB_FILE%" >nul 2>&1
echo.
echo O arquivo baixado não está assinado pela Python Software Foundation, então não foi executado.
goto :manual

:not_installed
echo.
echo A instalação do Python não foi concluída.
goto :manual

:manual
echo.
echo Para instalar o Python por conta própria, baixe em https://www.python.org/downloads/
echo e, no instalador, marque "Add python.exe to PATH". Depois, dê um duplo clique neste arquivo de novo.
echo.
pause
exit /b 1
