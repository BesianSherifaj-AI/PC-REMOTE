@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-MAIC.ps1" -OpenDashboard
if errorlevel 1 pause
