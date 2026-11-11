@echo off
setlocal
call "%~dp0start.cmd" backups
set /p "ATLAS_SNAPSHOT=Recovery point ID: "
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "if($env:ATLAS_SNAPSHOT -notmatch '^[a-f0-9]{32}$'){exit 1}"
if errorlevel 1 exit /b 1
call "%~dp0start.cmd" restore "%ATLAS_SNAPSHOT%"
if errorlevel 1 (
  pause
  exit /b 1
)
call "%~dp0start.cmd" start
