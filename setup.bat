@echo off
setlocal EnableDelayedExpansion
rem One-click launcher for Windows. Double-click it, or run from a terminal:
rem   setup.bat          Docker if available, else local mode (Python + SQLite)
rem   setup.bat local    force local mode
rem   setup.bat stop     stop what this script started
cd /d "%~dp0"
set "URL=http://127.0.0.1:8000"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=auto"

echo.
echo  Social Automation Platform: setup
echo  ---------------------------------

rem ---- Python ---------------------------------------------------------------
set "PY="
for %%P in (py python3 python) do (
  if not defined PY (
    %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PY=%%P"
  )
)
if defined PY (echo   [ok] Python found: !PY!) else (echo   [!!] Python 3.12+ not found. Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH".)

rem ---- Docker ---------------------------------------------------------------
set "DOCKER=0"
docker info >nul 2>&1 && set "DOCKER=1"
if "%DOCKER%"=="1" (echo   [ok] Docker is running) else (echo   [!!] Docker not running. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ for the full stack.)

if /i "%MODE%"=="stop" goto :stop

rem ---- .env -----------------------------------------------------------------
if not exist .env (
  copy /y .env.example .env >nul
  echo   [ok] created .env from .env.example
) else (
  echo   [ok] .env already exists, keeping your values
)
rem Fill APP_SECRET_KEY, JWT_SECRET and POSTGRES_PASSWORD with secure random values
rem (PowerShell does the editing; batch has no safe way to handle these strings).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$lines = @(Get-Content .env);" ^
  "function Rand($n) { $b = New-Object byte[] $n; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b) }" ^
  "function SetVar([string]$name, [string]$value) {" ^
  "  $script:lines = @($script:lines | ForEach-Object { if ($_ -match ('^#?\s*' + $name + '=')) { $name + '=' + $value } else { $_ } });" ^
  "  if (-not ($script:lines | Where-Object { $_ -match ('^' + $name + '=') })) { $script:lines += ($name + '=' + $value) } }" ^
  "function IsPlaceholder([string]$name) { $l = $script:lines | Where-Object { $_ -match ('^' + $name + '=') } | Select-Object -First 1; if (-not $l) { return $true }; $v = $l.Substring($name.Length + 1); return ($v -eq '' -or $v.StartsWith('your_') -or $v.EndsWith('_here')) }" ^
  "foreach ($n in 'APP_SECRET_KEY','JWT_SECRET','POSTGRES_PASSWORD') { if (IsPlaceholder $n) { SetVar $n (Rand 32); Write-Host ('  [ok] generated ' + $n) } else { Write-Host ('  [ok] ' + $n + ' already set') } }" ^
  "Set-Content -Path .env -Value $lines -Encoding ASCII"
if errorlevel 1 (echo   [!!] could not update .env & goto :fail)

if /i "%MODE%"=="local" goto :local
if "%DOCKER%"=="1" goto :docker
echo   [!!] falling back to local mode (no Docker)
goto :local

:docker
echo.
echo  Docker mode: PostgreSQL + dashboard + worker
docker compose version >nul 2>&1 && (docker compose up --build -d) || (docker-compose up --build -d)
if errorlevel 1 goto :fail
call :wait 45
if errorlevel 1 (echo   [!!] the dashboard did not start. Check: docker compose logs web & goto :fail)
call :success Docker
goto :eof

:local
echo.
echo  Local mode: Python + SQLite
if not defined PY (echo   [!!] Python 3.12+ is required for local mode. & goto :fail)
where ffmpeg >nul 2>&1 && (echo   [ok] ffmpeg found) || (echo   [!!] ffmpeg not found: video posts will not render. Install from https://www.gyan.dev/ffmpeg/builds/ and add it to PATH.)
if not exist .venv\Scripts\python.exe (
  !PY! -m venv .venv || goto :fail
  echo   [ok] created virtualenv .venv
)
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :fail
echo   [ok] dependencies installed
.venv\Scripts\python.exe -m config.migrate upgrade >nul || goto :fail
echo   [ok] database ready (data\scheduler.db)
if not exist data\logs mkdir data\logs
start "Social dashboard" /min cmd /c ".venv\Scripts\python.exe -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000 > data\logs\web.log 2>&1"
start "Social worker" /min cmd /c ".venv\Scripts\python.exe -m src.main loop > data\logs\worker.log 2>&1"
echo   [ok] started the dashboard and the worker in two minimized windows (logs in data\logs\)
call :wait 20
if errorlevel 1 (echo   [!!] the dashboard did not start. See data\logs\web.log & goto :fail)
call :success local
goto :eof

:wait
set /a tries=%~1
echo   waiting for the dashboard...
:waitloop
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing %URL%/api/health -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 exit /b 0
set /a tries-=1
if %tries% leq 0 exit /b 1
timeout /t 2 /nobreak >nul
goto :waitloop

:success
echo.
echo  🚀 SUCCESS! Open %URL% to access your SaaS Dashboard.
echo     First time? Click "Create one" to make your account, then open API settings for the key wizard.
echo     Mode: %~1. Stop with: setup.bat stop
echo.
start "" %URL%
exit /b 0

:stop
taskkill /fi "WINDOWTITLE eq Social dashboard*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq Social worker*" /f >nul 2>&1
echo   [ok] stopped local processes (if any)
if "%DOCKER%"=="1" (docker compose down >nul 2>&1 || docker-compose down >nul 2>&1) && echo   [ok] stopped docker containers
goto :eof

:fail
echo.
echo  Setup did not finish. Read the messages above; the fix is usually installing Python, Docker or ffmpeg.
pause
exit /b 1
