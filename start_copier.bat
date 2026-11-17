@echo off
title [XAUUSD] Telegram-to-MT5 Copier Pro
color 0E
cd /d "%~dp0"

:loop
cls
echo.
echo  ==============================================================================
echo     X   X    A    U   U U   U  SSSS  DDDD 
echo      X X    A A   U   U U   U S      D   D
echo       X    AAAAA  U   U U   U  SSS   D   D
echo      X X   A   A  U   U U   U     S  D   D
echo     X   X  A   A   UUU   UUU  SSSS   DDDD 
echo.
echo  ==============================================================================
echo        TELEGRAM TO MT5 AUTONOMOUS XAUUSD COPIER PRO v2.0 (PU PRIME LIVE)
echo  ==============================================================================
echo    * Mode:        LIVE BROKER
echo    * Symbol:      XAUUSD.s (PU Prime)
echo    * Lots:        0.01 / 0.01 / 0.02 (0.04 Max Exposure)
echo    * Protection:  20-Pip TP1 Proximity Guard + Shortfall Detector
echo    * Strategy:    Deepest-Layer Runner Trailing Stop + Soft Floor
echo    * Remote:      Telegram Saved Messages Phone Controls Active
echo  ==============================================================================
echo.
echo  Starting copier engine in LIVE mode...
echo.
python main.py
if %ERRORLEVEL% EQU 88 (
    echo.
    echo  ==============================================================================
    echo    DAILY WIN TARGET REACHED - SESSION TERMINATED FOR TODAY!
    echo    All trades have been banked and a summary was sent to Saved Messages.
    echo    No further trades will be placed until you relaunch the copier.
    echo  ==============================================================================
    pause
    exit /b 0
)
if %ERRORLEVEL% EQU 89 (
    echo.
    echo  ==============================================================================
    echo    DAILY LOSS LIMIT WAS HIT - SESSION TERMINATED FOR TODAY!
    echo    Capital protected: no more trading until relaunch on next day or override.
    echo    A summary has been sent to Saved Messages.
    echo  ==============================================================================
    pause
    exit /b 0
)
if %ERRORLEVEL% EQU 0 (
    echo.
    echo  ==============================================================================
    echo    COPIER SESSION HALTED OR EXITED CLEANLY - NO AUTO-RESTART.
    echo    Check Saved Messages for daily limit status or press any key to close.
    echo  ==============================================================================
    pause
    exit /b 0
)

echo.
echo  ==============================================================================
echo    WARNING: Copier engine stopped or exited.
echo    Auto-Restarting in 5 seconds (Press Ctrl+C to abort)...
echo  ==============================================================================
timeout /t 5 /nobreak > nul
goto loop