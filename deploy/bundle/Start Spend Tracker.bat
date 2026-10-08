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
rem What it does: finds `docker compose` or `podman compose`; asks the
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
set "PLACARD_LABEL=com.github.mariolonghi-com.spend-tracker.updater-role=placard"

rem Env-file paths are relative to where compose runs (S1): this folder.
cd /d "%~dp0" || (set "WHY=This launcher cannot open its own folder." & goto :stop)
set "HERE=%CD%"
echo Starting Spend Tracker @VERSION@ from %HERE%

rem ---------------------------------------------------------------------------
rem The engine
rem ---------------------------------------------------------------------------

set "ENGINE="
docker compose version >nul 2>&1 && set "ENGINE=docker" && set "PRODUCT=Docker"
if not defined ENGINE (
  podman compose version >nul 2>&1 && set "ENGINE=podman" && set "PRODUCT=Podman"
)
if not defined ENGINE (
  set "WHY=Spend Tracker runs in Docker Desktop or Podman Desktop, and neither was found. Install one, start it, then open this launcher again."
  goto :stop
)
%ENGINE% info >nul 2>&1
if errorlevel 1 (
  set "WHY=%PRODUCT% is installed but not running. Start it, wait until it says it is running, then open this launcher again."
  goto :stop
)

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

set "KIND=" & set "PODMAN_RESTART=" & set "APP=" & set "UPDATER="
for /f "usebackq tokens=1,* delims==" %%a in ("%ANSWER%") do (
  if "%%a"=="ENGINE" set "KIND=%%b"
  if "%%a"=="PODMAN_RESTART" set "PODMAN_RESTART=%%b"
  if "%%a"=="APP" set "APP=%%b"
  if "%%a"=="UPDATER" set "UPDATER=%%b"
  if "%%a"=="SAY" echo %%b
)
del "%ANSWER%" >nul 2>&1

if not "%STATUS%"=="0" (
  set "WHY=Spend Tracker was not started."
  goto :stop
)
if not defined KIND (
  set "WHY=The updater image could not be run. Check the internet connection, then open this launcher again."
  goto :stop
)
echo Engine: %KIND%.

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
  for /f "delims=" %%i in ('%ENGINE% ps -aq --filter "label=%%k=%PROJECT%" --filter "label=com.docker.compose.oneoff=True" --filter "label=%PLACARD_LABEL%" 2^>nul') do (
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

:stop
echo.
echo %WHY%
echo.
pause
endlocal
exit /b 1
