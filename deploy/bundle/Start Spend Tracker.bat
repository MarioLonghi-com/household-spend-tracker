@echo off
rem Start Spend Tracker @VERSION@ on this computer: Windows.
rem
rem UNTESTED. No Windows machine has run this launcher yet (design decision
rem D2): it ships so that one can, and it does what the macOS and Linux
rem launcher does, step for step. If it fails, the commands it would have run
rem are in README.txt, and an issue saying where it stopped is welcome.
rem
rem Double-click it. It is safe to run again at any time: it is also how
rem Spend Tracker is started after reinstalling Docker or Podman, and how an
rem install whose updater no longer works is repaired -- a newer zip's
rem launcher keeps your release and replaces only the updater (C5).
rem
rem What it does: picks Docker or Podman -- the one Spend Tracker is already
rem in, else the one that is running; asks the
rem bundle's own updater image what to start (updater/launch.py writes the
rem pin and the engine's settings into .env); enables podman-restart in a
rem Podman machine; removes a leftover maintenance page; starts the project;
rem waits for it to answer; opens it in the browser.
rem
rem Built from deploy/bundle/Start Spend Tracker.bat by scripts/bundle.py.

setlocal EnableExtensions EnableDelayedExpansion

set "APP_IMAGE=@APP_IMAGE@"
set "UPDATER_IMAGE=@UPDATER_IMAGE@"
set "PROJECT=spend-tracker"
set "URL=http://localhost:8848"
set "PROBE=http://127.0.0.1:8848/api/health"
set "HEALTH_TIMEOUT=180"

rem Env-file paths are relative to where compose runs (S1): this folder.
cd /d "%~dp0" || (set "WHY=This launcher cannot open its own folder." & goto :stop)
set "HERE=%CD%"
echo Starting Spend Tracker @VERSION@ from %HERE%

rem ---------------------------------------------------------------------------
rem The engine
rem ---------------------------------------------------------------------------

rem The rule is updater/launch.py's `pick_engine` (#264), and
rem tests/test_launcher_engine.py holds this block to its sentences: an
rem engine that already holds the project, else one that answers, Docker
rem first in a tie. `docker compose version` reads only the client, so a
rem Docker CLI alone says nothing about whether Docker runs.
call :seen docker DOCKER_SEEN
call :seen podman PODMAN_SEEN

set "ENGINE="
if "%DOCKER_SEEN%"=="project" (
  set "ENGINE=docker"
  if "%PODMAN_SEEN%"=="project" echo Spend Tracker is installed in both Docker and Podman; this starts the one in Docker.
  goto :picked
)
if "%PODMAN_SEEN%"=="project" (
  set "ENGINE=podman"
  goto :picked
)
if "%DOCKER_SEEN%"=="answers" (
  set "ENGINE=docker"
  if "%PODMAN_SEEN%"=="installed" echo Podman is installed but not running, so whether Spend Tracker is already installed there was not checked; this starts it in Docker.
  goto :picked
)
if "%PODMAN_SEEN%"=="answers" (
  set "ENGINE=podman"
  if "%DOCKER_SEEN%"=="installed" echo Docker is installed but not running, so whether Spend Tracker is already installed there was not checked; this starts it in Podman.
  goto :picked
)
set "WHY=Spend Tracker runs in Docker Desktop or Podman Desktop, and neither was found. Install one, start it, then open this launcher again."
if not "%DOCKER_SEEN%"=="none" set "WHY=Docker is installed but not running. Start it, wait until it says it is running, then open this launcher again."
if not "%PODMAN_SEEN%"=="none" set "WHY=Podman is installed but not running. Start it, wait until it says it is running, then open this launcher again."
if not "%DOCKER_SEEN%"=="none" if not "%PODMAN_SEEN%"=="none" set "WHY=Docker and Podman are both installed, and neither is running. Start the one Spend Tracker uses, wait until it says it is running, then open this launcher again."
goto :stop

:picked
if "%ENGINE%"=="docker" (set "PRODUCT=Docker") else (set "PRODUCT=Podman")

rem Docker Desktop and a Podman machine both run the engine in a VM, where
rem /var/run/docker.sock is the right socket (S21).
set "SOCK=/var/run/docker.sock"

rem ---------------------------------------------------------------------------
rem The folder this project was started from, if not this one
rem ---------------------------------------------------------------------------

set "PREVIOUS="
for /f "delims=" %%i in ('%ENGINE% ps -aq --filter "label=com.docker.compose.project=%PROJECT%" --filter "label=com.docker.compose.service=updater" 2^>nul') do (
  if not defined PREVIOUS (
    for /f "delims=" %%d in ('%ENGINE% inspect --format "{{index .Config.Labels `com.docker.compose.project.working_dir`}}" %%i 2^>nul') do (
      if exist "%%d\" if /i not "%%d"=="%HERE%" set "PREVIOUS=%%d"
    )
  )
)
if defined PREVIOUS echo Spend Tracker was last started from %PREVIOUS%; its settings are carried over.

rem ---------------------------------------------------------------------------
rem What to start: asked of the bundle's own updater (updater/launch.py)
rem ---------------------------------------------------------------------------

set "MOUNTS=-v "%SOCK%:/run/engine.sock" -v "%HERE%:/project""
set "EXTRA="
if defined PREVIOUS (
  set "MOUNTS=!MOUNTS! -v "%PREVIOUS%:/previous:ro""
  set "EXTRA=--previous /previous"
)

echo Checking %PRODUCT% and this folder...
set "ANSWER=%TEMP%\spend-tracker-launch-%RANDOM%.txt"
%ENGINE% run --rm --network none --user 0:0 --security-opt label=disable !MOUNTS! --entrypoint python "%UPDATER_IMAGE%" -m updater.launch --bundle-app "%APP_IMAGE%" --bundle-updater "%UPDATER_IMAGE%" --host-dir "%HERE%" --engine-socket "%SOCK%" !EXTRA! > "%ANSWER%"
set "STATUS=%ERRORLEVEL%"

set "KIND=" & set "PODMAN_RESTART=" & set "APP=" & set "UPDATER=" & set "PLACARD=" & set "MADE_BY="
for /f "usebackq tokens=1,* delims==" %%a in ("%ANSWER%") do (
  if "%%a"=="ENGINE" set "KIND=%%b"
  if "%%a"=="PODMAN_RESTART" set "PODMAN_RESTART=%%b"
  if "%%a"=="APP" set "APP=%%b"
  if "%%a"=="UPDATER" set "UPDATER=%%b"
  if "%%a"=="PLACARD" set "PLACARD=%%b"
  if "%%a"=="COMPOSE" set "MADE_BY=%%b"
  if "%%a"=="SAY" echo %%b
)
del "%ANSWER%" >nul 2>&1

if not "%STATUS%"=="0" (
  set "WHY=Spend Tracker was not started."
  goto :stop
)
if not defined PLACARD set "KIND="
if not defined KIND (
  set "WHY=The updater image could not be run. Check the internet connection, then open this launcher again."
  goto :stop
)
echo Engine: %KIND%.

rem ---------------------------------------------------------------------------
rem Which compose (#247): the one that created the project, from its labels.
rem `podman compose` hands the work to docker-compose whenever that is
rem installed, and docker-compose refuses a stack podman-compose made; Podman
rem takes the provider from PODMAN_COMPOSE_PROVIDER.
rem ---------------------------------------------------------------------------

set "PROVIDER="
if "%MADE_BY%"=="podman-compose" (
  for /f "delims=" %%p in ('where podman-compose 2^>nul') do if not defined PROVIDER set "PROVIDER=%%p"
  if not defined PROVIDER (
    set "WHY=This Spend Tracker was created with podman-compose, which was not found. Install it, then open this launcher again."
    goto :stop
  )
)
if "%MADE_BY%"=="docker-compose" if "%ENGINE%"=="podman" (
  for /f "delims=" %%p in ('where docker-compose 2^>nul') do if not defined PROVIDER set "PROVIDER=%%p"
)
if defined PROVIDER (
  set "PODMAN_COMPOSE_PROVIDER=!PROVIDER!"
  echo Using %MADE_BY%, which created this Spend Tracker.
)

rem ---------------------------------------------------------------------------
rem Coming back after a restart (S2, S21): podman-restart inside the machine
rem ---------------------------------------------------------------------------

if defined PODMAN_RESTART (
  set "MACHINE="
  for /f "tokens=1,2" %%m in ('podman machine list --noheading --format "{{.Name}} {{.Running}}" 2^>nul') do (
    if not defined MACHINE if "%%n"=="true" set "MACHINE=%%m"
  )
  rem The default machine's name may carry a trailing "*".
  if defined MACHINE for /f "delims=*" %%x in ("!MACHINE!") do set "MACHINE=%%x"
  if not defined MACHINE (
    set "WHY=No running Podman machine was found. Start it in Podman Desktop, then open this launcher again."
    goto :stop
  )
  if "%PODMAN_RESTART%"=="system" (
    podman machine ssh "!MACHINE!" sudo systemctl enable podman-restart.service >nul 2>&1
  ) else (
    podman machine ssh "!MACHINE!" systemctl --user enable podman-restart.service >nul 2>&1
  )
  if errorlevel 1 (
    set "WHY=podman-restart could not be enabled in the machine !MACHINE!, so Spend Tracker would not come back after a restart."
    goto :stop
  )
)

rem ---------------------------------------------------------------------------
rem Start
rem ---------------------------------------------------------------------------

rem The updater's "ahead" page holds the app's port when compose once started
rem an older release than the pin (9.2, R30); `up` would collide with it.
for %%k in (com.docker.compose.project io.podman.compose.project) do (
  for /f "delims=" %%i in ('%ENGINE% ps -aq --filter "label=%%k=%PROJECT%" --filter "label=com.docker.compose.oneoff=True" --filter "label=%PLACARD%" 2^>nul') do (
    %ENGINE% rm -f %%i >nul 2>&1
  )
)

%ENGINE% compose --env-file .env up -d
if errorlevel 1 (
  set "WHY=%PRODUCT% could not start Spend Tracker; the lines above say why."
  goto :stop
)

echo Waiting for Spend Tracker to answer...
set /a "TRIES=HEALTH_TIMEOUT/2"
:wait
curl.exe -fsS -o NUL --max-time 5 "%PROBE%" >nul 2>&1 && goto :up
set /a "TRIES-=1"
if %TRIES% LEQ 0 (
  set "WHY=Spend Tracker has not answered after %HEALTH_TIMEOUT% seconds. In this folder, '%ENGINE% compose logs app' shows why."
  goto :stop
)
timeout /t 2 /nobreak >nul
goto :wait

:up
echo Spend Tracker is running at %URL%
start "" "%URL%"
endlocal
exit /b 0

:seen
rem What %1's CLI answers, into the variable %2: none, installed, answers
rem or project (updater/launch.py ENGINE_STATES; "denied" is Linux's).
set "%2=none"
%1 compose version >nul 2>&1 || exit /b 0
set "%2=installed"
%1 info >nul 2>&1 || exit /b 0
set "%2=answers"
for /f "delims=" %%i in ('%1 ps -aq --filter "label=com.docker.compose.project=%PROJECT%" 2^>nul') do set "%2=project"
exit /b 0

:stop
echo.
echo %WHY%
echo.
pause
endlocal
exit /b 1
