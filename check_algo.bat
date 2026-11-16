@echo off
title MT5 Algo Trading Checker
cd /d "%~dp0"
echo ============================================================
echo   Checking MT5 Connection and Algo Trading Status...
echo ============================================================
python check_algo.py
echo.
pause
