@echo off
REM ---------------------------------------------------------------------------
REM  Build the standalone Windows Companion using the canonical PyInstaller spec
REM
REM  One-time setup (installs runtime deps + PyInstaller):
REM    python -m pip install -r requirements.txt
REM    python -m pip install pyinstaller
REM
REM  Then double-click this file, or run it from a terminal in this folder.
REM  Output lands in:
REM    dist\pc_stats_monitor_v4.exe
REM
REM  Notes:
REM   * --windowed  -> no console window flashes at login (this is a background
REM                    app that lives in the system tray). print() output is
REM                    redirected to %APPDATA%\PCStatsMonitor\monitor.log.
REM   * The spec extracts onefile files under the current user's Local AppData,
REM     avoiding restricted TEMP paths and administrator requirements.
REM   * --add-data "webui;webui" bundles the web UI (index.html/portal.css/js).
REM     At runtime server.webui_dir() resolves it from sys._MEIPASS.
REM   * --collect-all webview / pythonnet pulls in the pywebview EdgeChromium
REM     (WebView2) backend, which PyInstaller can't see (loaded via clr).
REM   * The other hidden-imports cover lazily-imported wmi / pystray / PIL /
REM     pywin32 modules.
REM   * --collect-all soundcard / numpy bundles the optional audio-visualizer
REM     stream so it works from the .exe without a separate pip install
REM     (soundcard's native loopback backend is otherwise missed).
REM   * WebView2 runtime is required on the target PC (ships with Windows 11;
REM     on Windows 10 install the Evergreen runtime from Microsoft).
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM PyInstaller's wrapper exe is often on a per-user Scripts dir that's not on
REM PATH. Invoke it as a module instead. Try "python" first, then the "py" launcher.
set "PYI="
python -m PyInstaller --version >nul 2>&1 && set "PYI=python -m PyInstaller"
if not defined PYI py -m PyInstaller --version >nul 2>&1 && set "PYI=py -m PyInstaller"
if not defined PYI (
  echo ERROR: PyInstaller is not importable from "python" or "py".
  echo Install build dependencies first:
  echo     python -m pip install -r requirements.txt
  echo     python -m pip install pyinstaller
  pause
  exit /b 1
)
echo Using: %PYI%

REM Clean previous build artifacts so we don't ship stale bundles. The committed
REM .spec is the single source of PyInstaller build settings.
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

%PYI% --workpath build --distpath dist pc_stats_monitor_v4.spec

if errorlevel 1 (
  echo.
  echo BUILD FAILED.
  pause
  exit /b 1
)

echo.
echo ---------------------------------------------------------------
echo  BUILD OK
echo  Output: %CD%\dist\pc_stats_monitor_v4.exe
echo ---------------------------------------------------------------
echo.
echo Test it by double-clicking the .exe in dist\ .
pause
