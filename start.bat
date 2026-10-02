@echo off
title Velocity V2
setlocal enableextensions

rem Always operate from the script's own directory so relative imports
rem (bhop_core, themes, updater) resolve no matter how the user launched us.
pushd "%~dp0"

where pythonw >NUL 2>&1
if errorlevel 1 (
    echo.
    echo   Velocity could not find "pythonw" on your PATH.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   and make sure "Add python.exe to PATH" is ticked during setup.
    echo.
    pause
    popd
    exit /b 1
)

start "Velocity V2" /b pythonw "%~dp0cs2_bhop.py"
popd
endlocal