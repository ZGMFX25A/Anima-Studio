@echo off
rem Reset ports when ComfyUI (8188) / painter (8190) / frontend (8200) cannot bind: restarting WinNAT
rem releases the dynamic port ranges grabbed by Hyper-V / WSL / Docker. Nothing is reserved permanently.
rem NOTE: keep this file ASCII-only, cmd mis-parses multibyte text in .bat files.
title Anima Studio - reset ports

net session >nul 2>&1
if errorlevel 1 (
    echo [!] Please right-click this file and choose "Run as administrator".
    pause
    exit /b 1
)

echo.
echo Close WSL and Docker before resetting:
echo   - WSL:    run "wsl --shutdown" in a terminal
echo   - Docker: right-click the Docker Desktop tray icon and choose "Quit Docker Desktop"
echo.
set /p "_=Press Enter to reset ports and close this window..."

net stop winnat /y
net start winnat
if errorlevel 1 (
    echo [!] Failed to restart WinNAT, see the message above.
    pause
    exit /b 1
)
exit /b 0
