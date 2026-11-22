@echo off
title MT5 Algo Trading Checker v3.9.0 (T3 + R-Lock)
cd /d "%~dp0"
echo ============================================================
echo   Checking MT5 Connection and Algo Trading Status...
echo   Copier v3.9.0 (T3 + R-Lock) - Active Protection Profile:
echo ============================================================
python -c "import config as c; from risk_controls import format_protection_profile_line as f; print('  '+f(c))"
python check_algo.py
echo.
pause
