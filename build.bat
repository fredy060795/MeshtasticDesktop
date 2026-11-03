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

REM Kompatible Python-Version suchen (meshtastic unterstuetzt aktuell 3.10 - 3.14,
REM PyInstaller ebenfalls). Zuerst "python" aus PATH, dann der py-Launcher.
set PY=
set "VCHK=import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)"
python -c "%VCHK%" >nul 2>nul && set "PY=python"
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "%VCHK%" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  echo [FEHLER] Keine passende Python-Version ^(3.10 - 3.13^) gefunden.
  echo          Bitte Python 3.12 von https://www.python.org installieren ^(Haken bei "Add to PATH"^).
  pause
  exit /b 1
)
echo Verwende: %PY%

echo === 1/4  Virtuelle Umgebung anlegen ===
REM Vorhandene .venv mit falscher Python-Version verwerfen
if exist .venv (
  .venv\Scripts\python.exe -c "%VCHK%" >nul 2>nul || rmdir /s /q .venv
)
if not exist .venv (
  %PY% -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err

echo === 2/4  Abhaengigkeiten installieren ===
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :err

echo === Selbsttest ===
python app.py --selftest || goto :err
python flasher.py --selftest || goto :err

set COMMON=--noconfirm --clean --windowed --icon icon.ico --add-data "icon.ico;." --add-data "worldmap.json.gz;." ^
  --add-data "firmware_custom;firmware_custom" --collect-all esptool ^
  --collect-all meshtastic --collect-all bleak --collect-all winrt --collect-all bleak_winrt ^
  --collect-all pubsub --collect-submodules serial --hidden-import qrcode --hidden-import PIL.Image ^
  --exclude-module matplotlib --exclude-module numpy.tests

echo === 3/4  EXE bauen (Ordner + Portable) ===
pyinstaller %COMMON% --name MeshtasticDesktop app.py || goto :err
pyinstaller %COMMON% --onefile --name MeshtasticDesktop-Portable app.py || goto :err
pyinstaller --noconfirm --clean --windowed --onefile --icon icon.ico --add-data "icon.ico;." ^
  --add-data "firmware_custom;firmware_custom" --collect-all esptool --collect-submodules serial ^
  --name MeshtasticFlasher flasher.py || goto :err

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
echo Flasher-EXE:   dist\MeshtasticFlasher.exe
pause
exit /b 0

:err
echo.
echo [FEHLER] Build abgebrochen.
pause
exit /b 1
