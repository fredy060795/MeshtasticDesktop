@echo off
REM ============================================================================
REM  Meshtastic Desktop - Build-Skript fuer Windows
REM  Erzeugt:
REM    dist\MeshtasticDesktop-Portable.exe      (eine Datei, ohne Installation)
REM    dist\MeshtasticDesktop\MeshtasticDesktop.exe  (Ordner-Version)
REM    Output\MeshtasticDesktop-Setup.exe       (Installer, falls Inno Setup 6 da ist)
REM  Voraussetzung: Python 3.10 - 3.13 von python.org (mit "Add to PATH")
REM  Internet wird NUR beim Bauen benoetigt (pip). Die fertige App ist offline.
REM ============================================================================
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo [FEHLER] Python wurde nicht gefunden. Bitte von https://www.python.org installieren.
  pause
  exit /b 1
)

echo === 1/4  Virtuelle Umgebung anlegen ===
if not exist .venv (
  %PY% -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err

echo === 2/4  Abhaengigkeiten installieren ===
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :err

echo === Selbsttest ===
python app.py --selftest || goto :err

set COMMON=--noconfirm --clean --windowed --icon icon.ico --add-data "icon.ico;." --add-data "worldmap.json.gz;." ^
  --collect-all meshtastic --collect-all bleak --collect-all winrt --collect-all bleak_winrt ^
  --collect-all pubsub --collect-submodules serial --hidden-import qrcode --hidden-import PIL.Image ^
  --exclude-module matplotlib --exclude-module numpy.tests

echo === 3/4  EXE bauen (Ordner + Portable) ===
pyinstaller %COMMON% --name MeshtasticDesktop app.py || goto :err
pyinstaller %COMMON% --onefile --name MeshtasticDesktop-Portable app.py || goto :err

echo === 4/4  Installer bauen (Inno Setup 6) ===
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC (
  %ISCC% installer.iss || goto :err
  echo.
  echo FERTIG:  Output\MeshtasticDesktop-Setup.exe
) else (
  echo.
  echo Inno Setup 6 nicht gefunden - Installer wird uebersprungen.
  echo Download: https://jrsoftware.org/isdl.php  ^(danach build.bat erneut starten^)
)
echo.
echo Portable EXE:  dist\MeshtasticDesktop-Portable.exe
echo Ordner-EXE:    dist\MeshtasticDesktop\MeshtasticDesktop.exe
pause
exit /b 0

:err
echo.
echo [FEHLER] Build abgebrochen.
pause
exit /b 1
