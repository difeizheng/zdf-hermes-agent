@echo off
REM Hermes Orchestrator - Windows service management.
REM Usage: orchestrator [start^|stop^|status^|logs]

setlocal

REM Resolve repo root (parent of scripts\)
set "REPO_ROOT=%~dp0.."

REM Activate venv if exists
if exist "%REPO_ROOT%\.venv\Scripts\activate.bat" (
    call "%REPO_ROOT%\.venv\Scripts\activate.bat"
)

if "%~1"=="" goto usage
if "%~1"=="start" goto start
if "%~1"=="stop" goto stop
if "%~1"=="status" goto status
if "%~1"=="logs" goto logs

echo Unknown command: %~1
goto usage

:start
echo Starting Hermes Orchestrator...
python "%REPO_ROOT%\scripts\supervisor.py" start --background
goto end

:stop
echo Stopping Hermes Orchestrator...
python "%REPO_ROOT%\scripts\supervisor.py" stop
goto end

:status
python "%REPO_ROOT%\scripts\supervisor.py" status
goto end

:logs
set "LOGFILE=%USERPROFILE%\.hermes\logs\coordinator.log"
if "%~2"=="-f" (
    powershell -Command "Get-Content -Wait '%LOGFILE%'"
) else (
    powershell -Command "Get-Content '%LOGFILE%' -Tail 100"
)
goto end

:usage
echo.
echo Usage: orchestrator [command]
echo.
echo Commands:
echo   start       Start coordinator + 6 agents (background)
echo   stop        Stop all processes
echo   status      Show process status
echo   logs [-f]   View coordinator log (-f to follow)
echo.

:end
