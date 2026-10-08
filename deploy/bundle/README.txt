Spend Tracker @VERSION@ -- on this computer
==========================================

1. Install Docker Desktop (https://www.docker.com/products/docker-desktop/)
   or Podman Desktop (https://podman-desktop.io/), and start it.
2. Unzip this folder somewhere you will keep it, such as your Documents.
   Keep the folder: it holds the settings Spend Tracker is started with.
3. Double-click the launcher for your system:

     macOS     Start Spend Tracker.command
     Windows   Start Spend Tracker.bat      (untested -- see below)
     Linux     start-spend-tracker.sh       (or run ./start-spend-tracker.sh)

   A window opens and shows what it does; you do not type anything into it.
   The first start downloads Spend Tracker, which takes a few minutes. When it
   is ready, your browser opens http://localhost:8848, where the setup wizard
   creates the first account.
4. The wizard asks for a one-time setup token. It is in the app's log: in
   Docker Desktop, Containers -> spend-tracker -> app -> Logs (in Podman
   Desktop, Containers -> spend-tracker-app -> Logs). Look for the line
   "Finish setup at /setup with this one-time token" and copy the token
   printed just below it.

After that, Spend Tracker starts with Docker or Podman, and updates are made
in the browser, under Application. Nothing else needs this folder's launcher.


The first double-click: your system asks first
----------------------------------------------

These files are not signed yet, so the first open meets a warning.

macOS 15 and later: double-click the .command once and close the warning.
Then open System Settings -> Privacy & Security, scroll down to the line
about "Start Spend Tracker.command", click "Open Anyway" and enter your
password. Double-click it again. (Right-click -> Open no longer does this.)

Windows: SmartScreen says "Windows protected your PC". Click "More info",
then "Run anyway".


Run the launcher again when
---------------------------

- Spend Tracker does not start after reinstalling Docker or Podman.
- The browser says the updater cannot reach the container engine, or a
  recovery page asks you to run the launcher.

It is safe to run at any time. It keeps the release your ledger is at; it
never moves you to an older or newer one. A newer zip's launcher, unzipped
anywhere, does the same and also replaces the updater when its own is newer:
that is the repair for an updater that no longer works.


Windows is untested
-------------------

No Windows machine has run "Start Spend Tracker.bat" yet. If it stops,
open a terminal in this folder and run:

  docker compose --env-file .env up -d

then open http://localhost:8848. Under Podman, the same with
"podman compose", after enabling podman-restart in its machine:

  podman machine ssh <machine> sudo systemctl enable podman-restart.service


Without a launcher
------------------

  docker compose --env-file .env up -d

in this folder starts the same thing, once a launcher has run here once and
written the engine's settings into .env.

More: https://github.com/MarioLonghi-com/household-spend-tracker/blob/main/deploy/DOCKER.md
