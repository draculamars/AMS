@echo off
setlocal
set "ATLAS_ROOT=%~dp0"
if not exist "%ATLAS_ROOT%portable.json" (
  echo Atlas export is incomplete. See reports and export-state.json.
  exit /b 1
)
set "ATLAS_ARCHIVE=%ATLAS_ROOT%runtimes\Windows-AMD64.tar.gz"
set "ATLAS_CHECKSUM=%ATLAS_ROOT%runtimes\Windows-AMD64.sha256"
set "ATLAS_DIGEST="
for /f "delims=" %%H in ('powershell.exe -NoLogo -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; $h=(Get-FileHash -LiteralPath $env:ATLAS_ARCHIVE -Algorithm SHA256).Hash.ToLowerInvariant(); $e=[IO.File]::ReadAllText($env:ATLAS_CHECKSUM).Trim(); if($h -ne $e){throw 'Runtime checksum mismatch'}; $h"') do set "ATLAS_DIGEST=%%H"
if not defined ATLAS_DIGEST exit /b 1
set "ATLAS_RUNTIME_DIR=%ATLAS_ROOT%runtimes\expanded\Windows-AMD64-%ATLAS_DIGEST:~0,16%"
if exist "%ATLAS_RUNTIME_DIR%\.complete" goto ready
if not exist "%ATLAS_ROOT%runtimes\expanded" mkdir "%ATLAS_ROOT%runtimes\expanded"
mkdir "%ATLAS_RUNTIME_DIR%.extracting" 2>nul
if errorlevel 1 (
  echo Runtime extraction is active or was interrupted. Inspect its .extracting directory.
  exit /b 1
)
mkdir "%ATLAS_RUNTIME_DIR%.staging"
tar.exe -mxzf "%ATLAS_ARCHIVE%" -C "%ATLAS_RUNTIME_DIR%.staging"
if errorlevel 1 goto failure
"%ATLAS_RUNTIME_DIR%.staging\python\python.exe" -B -X utf8 -I -c "import sqlite3,ssl,fastapi,uvicorn,pydantic_core"
if errorlevel 1 goto failure
type nul > "%ATLAS_RUNTIME_DIR%.staging\.complete"
move "%ATLAS_RUNTIME_DIR%.staging" "%ATLAS_RUNTIME_DIR%" >nul
if errorlevel 1 goto failure
rmdir "%ATLAS_RUNTIME_DIR%.extracting"
:ready
set "PATH=%ATLAS_RUNTIME_DIR%\git\cmd;%ATLAS_RUNTIME_DIR%\git\mingw64\bin;%PATH%"
set "ATLAS_ACTION=%~1"
if not defined ATLAS_ACTION set "ATLAS_ACTION=start"
"%ATLAS_RUNTIME_DIR%\python\python.exe" -B -X utf8 -I "%ATLAS_ROOT%launcher.py" "%ATLAS_ROOT%." "%ATLAS_ACTION%" "%~2"
exit /b %errorlevel%
:failure
echo Runtime extraction failed. Partial files are retained for diagnosis.
exit /b 1
