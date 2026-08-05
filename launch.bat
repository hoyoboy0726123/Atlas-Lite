@echo off
setlocal
title Atlas-Lite Launcher
REM Atlas-Lite one-click launcher (backend 8020 / frontend 3020).
REM Runs alongside Atlas (8014/3012) and V5 (8004/3002) without clashing.
REM First run creates the venv and installs dependencies; later runs just start.
REM
REM NOTE: this file must stay pure ASCII. cmd.exe parses .bat byte-by-byte using
REM the system ANSI codepage (1252 here), which cannot represent Chinese - any
REM CJK character in a line corrupts that whole line into an invalid command.

echo ==================================================
echo  Atlas-Lite  (backend 8020 / frontend 3020)
echo ==================================================
echo.

REM ---- 1. Backend venv + dependencies ----------------
REM Pick the interpreter deliberately instead of taking whatever "python" is.
REM pydantic 2.9.2 ships no wheel for Python 3.14, so a bare "python" that
REM happens to be 3.14 falls back to compiling pydantic-core from Rust source
REM and fails with a wall of cargo errors. Prefer 3.13, then 3.12, then 3.11.
set "PY_CMD="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY_CMD (
        py -%%V -c "import sys" >NUL 2>&1
        if not errorlevel 1 set "PY_CMD=py -%%V"
    )
)
if not defined PY_CMD set "PY_CMD=python"

if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo [Setup] Creating backend virtual environment using: %PY_CMD%
    %PY_CMD% -m venv "%~dp0backend\.venv"
    if errorlevel 1 (
        echo [X] Failed. Install Python 3.11-3.13 and make sure it is on PATH.
        pause
        exit /b 1
    )
    echo [Setup] Installing backend dependencies ^(a few minutes on first run^)...
    "%~dp0backend\.venv\Scripts\python.exe" -m pip install -q -r "%~dp0backend\requirements.txt"
    if errorlevel 1 (
        echo [X] Backend dependency install failed. See the errors above.
        pause
        exit /b 1
    )
)

REM ---- 2. Optional .env ------------------------------
REM Atlas-Lite needs no API keys. .env is only for Telegram and paths.
if not exist "%~dp0backend\.env" (
    if exist "%~dp0backend\.env.example" (
        copy "%~dp0backend\.env.example" "%~dp0backend\.env" >NUL
    )
)

REM ---- 3. Frontend dependencies ----------------------
REM Skipped entirely when --no-frontend is given.
if /i "%~1"=="--no-frontend" goto backend_only

if not exist "%~dp0frontend\node_modules" (
    echo [Setup] Installing frontend dependencies ^(a few minutes on first run^)...
    pushd "%~dp0frontend"
    call npm install
    set "NPM_ERR=%errorlevel%"
    popd
    if not "%NPM_ERR%"=="0" (
        echo [X] Frontend install failed. Install Node.js 18+ first.
        pause
        exit /b 1
    )
)

:backend_only
echo.
REM Backend bind address. Default 127.0.0.1 = local only.
REM This backend runs arbitrary scripts and drives the desktop, so exposing it
REM to the network hands your machine to whoever can reach it. Change only if
REM you understand that:  set PO_HOST=0.0.0.0
if not defined PO_HOST set "PO_HOST=127.0.0.1"

echo [1/2] Starting backend  ^(port 8020, host %PO_HOST%^)...
REM /k keeps the window open if uvicorn crashes, so the error stays readable.
REM Clearing PYTHONUTF8 explicitly: an inherited empty value makes Python fatal.
start "AtlasLite_Backend" cmd /k "cd /d "%~dp0backend" && set "PYTHONUTF8=" && .venv\Scripts\uvicorn.exe main:app --host %PO_HOST% --port 8020"

if /i "%~1"=="--no-frontend" goto done_backend_only

echo [2/2] Starting frontend ^(port 3020^)...
REM BACKEND_PORT makes next.config.mjs proxy /api/backend to 8020.
start "AtlasLite_Frontend" cmd /k "cd /d "%~dp0frontend" && set "BACKEND_PORT=8020" && set "NEXT_PUBLIC_BACKEND_PORT=8020" && npx next dev --port 3020"

echo.
echo ==================================================
echo  Atlas-Lite is running
echo    UI      : http://localhost:3020
echo    Backend : http://localhost:8020
echo ==================================================
echo.
pause
exit /b 0

:done_backend_only
echo.
echo ==================================================
echo  Atlas-Lite backend only  (--no-frontend)
echo    Backend : http://localhost:8020
echo    API docs: http://localhost:8020/docs
echo.
echo  Use this for headless boxes, or when you drive
echo  workflows via the webhook / schedule / Telegram.
echo ==================================================
echo.
pause
