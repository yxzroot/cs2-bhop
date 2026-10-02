@echo off
title CS2 Bhop
echo Installing dependencies...
python -m pip install pymem >nul 2>&1
echo.
echo Starting CS2 Bhop...
echo.
python "%~dp0cs2_bhop.py"
echo.
echo Press any key to close...
pause >nul
