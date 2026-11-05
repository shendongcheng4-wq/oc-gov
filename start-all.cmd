@echo off
rem start-all.cmd - double-click wrapper that launches the one-click starter.
rem Kept ASCII-only; real logic lives in start-all.ps1.
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
