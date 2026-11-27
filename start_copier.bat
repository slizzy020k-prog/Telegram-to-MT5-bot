@echo off
title [XAUUSD] Telegram-to-MT5 Copier Pro v3.9.0 (T3 + R-Lock)
color 0E
cd /d "%~dp0"

set "RETRY_DELAY=5"
set "MAX_RETRY_DELAY=60"
set "CRASH_COUNT=0"

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
echo        TELEGRAM TO MT5 AUTONOMOUS XAUUSD COPIER PRO v3.9.0 (T3 + R-Lock) (PU PRIME LIVE)
echo  ==============================================================================
echo    * Mode:        LIVE BROKER
echo    * Symbol:      XAUUSD.s (PU Prime)
echo    * Lots:        Live .env (layer count / base lots / lot cap)
echo    * Targets:     All Start @ TP1 - Dynamic Deepest Runner Scales TP1 to TP4
echo    * Protection:  Anti-Choke Runner FSM (10p Tight Zone, 12p x 2-Tick Guard)
echo    * Trailing:    T3 Profile (0.70x ATR Activation + $5.50 Clamp)
echo    * R-Lock:      Profit Staircase ON (R-Relative Floors)
echo    * Lot Cap:     High-Risk Lot Cap ON
echo    * Drawdown:    Daily-Loss Pause + Float-Loss + Emergency Cutoff (live .env)
echo    * Safety:      Single-Instance Mutex + Reconnect Catch-Up + JSONL Ledger
echo    * Remote:      Telegram Saved Messages Phone Controls Active (/health)
echo  ==============================================================================
echo.
echo  Starting copier engine v3.9.0 (T3 + R-Lock) in LIVE mode...
echo.
echo  Active Protection Profile (read from live config):
python -c "import config as c; from risk_controls import format_protection_profile_line as f; print('  '+f(c))"
python main.py
if %ERRORLEVEL% EQU 88 goto :exit_daily_win
if %ERRORLEVEL% EQU 89 goto :exit_daily_loss
if %ERRORLEVEL% EQU 90 goto :exit_duplicate
if %ERRORLEVEL% EQU 0 goto :exit_clean

set "EXIT_CODE=%ERRORLEVEL%"
set /a CRASH_COUNT+=1
echo.
echo  ==============================================================================
echo    WARNING: Copier engine stopped unexpectedly (Exit Code: %EXIT_CODE%, Crash #%CRASH_COUNT%).
echo    Auto-Restarting in %RETRY_DELAY% seconds (Capped Backoff, Max %MAX_RETRY_DELAY%s)...
echo    Press Ctrl+C to abort.
echo  ==============================================================================
timeout /t %RETRY_DELAY% /nobreak > nul
set /a RETRY_DELAY=RETRY_DELAY * 2
if %RETRY_DELAY% GTR %MAX_RETRY_DELAY% set "RETRY_DELAY=%MAX_RETRY_DELAY%"
goto loop

:exit_daily_win
echo.
echo  ==============================================================================
echo    DAILY WIN TARGET REACHED (Exit Code 88) - SESSION TERMINATED FOR TODAY!
echo    All trades have been banked and a summary was sent to Saved Messages.
echo    No further trades will be placed until you relaunch the copier.
echo  ==============================================================================
pause
exit /b 0

:exit_daily_loss
echo.
echo  ==============================================================================
echo    DAILY LOSS LIMIT HIT (Exit Code 89) - SESSION TERMINATED FOR TODAY!
echo    Capital protected: no more trading until relaunch on next day or override.
echo    A summary has been sent to Saved Messages.
echo  ==============================================================================
pause
exit /b 0

:exit_duplicate
echo.
echo  ==============================================================================
echo    DUPLICATE INSTANCE BLOCKED (Exit Code 90) - COPIER IS ALREADY RUNNING!
echo    Single-instance mutex prevented launching a second copier process.
echo    Close the existing copier window before starting a new session.
echo  ==============================================================================
pause
exit /b 90

:exit_clean
echo.
echo  ==============================================================================
echo    COPIER SESSION HALTED OR EXITED CLEANLY (Exit Code 0) - NO AUTO-RESTART.
echo    Check Saved Messages for daily limit status or press any key to close.
echo  ==============================================================================
pause
exit /b 0
