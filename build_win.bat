@echo off
REM ============================================================================
REM  CynoType — Build standalone Windows executable (dist\CynoType.exe)
REM  Uses Microsoft Edge WebView2 (built into Windows 10/11) for the Apple UI.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo ==^> Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller

echo ==^> Building CynoType.exe...
if exist build\CynoType rmdir /s /q build\CynoType
if exist dist\CynoType.exe del /f /q dist\CynoType.exe

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "CynoType" ^
  --icon "assets\CynoType.ico" ^
  --add-data "assets;assets" ^
  --hidden-import cynotype.engine ^
  --hidden-import pynput.keyboard._win32 ^
  --hidden-import pynput.mouse._win32 ^
  --hidden-import pyperclip ^
  --hidden-import webview ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import clr ^
  --exclude-module mcp ^
  --exclude-module playwright ^
  auto_typer.py

if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Build failed.
  exit /b %ERRORLEVEL%
)

echo.
echo ============================================================
echo  Build complete: dist\CynoType.exe
echo ============================================================
endlocal
