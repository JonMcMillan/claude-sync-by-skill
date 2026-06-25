@echo off
rem Launcher: run the claude-sync-by-skill engine with the platform's Python 3.
rem Usage: run.cmd [--status | --direction up|down ...]
setlocal
set "DIR=%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%DIR%sync_engine.py" %*
) else (
    python "%DIR%sync_engine.py" %*
)
exit /b %errorlevel%
