Spend Tracker @VERSION@ -- on this computer
==========================================

1. Install Docker Desktop (https://www.docker.com/products/docker-desktop/)
   or Podman Desktop (https://podman-desktop.io/), and start it. With both
   installed, the launcher uses the one Spend Tracker is running in; else
   the one it is already in; else the one that is running. See "Installed
   in both Docker and Podman" below.
2. Unzip this folder somewhere you will keep it, such as your Documents.
   Keep the folder: it holds the settings Spend Tracker is started with.
3. Double-click the launcher for your system:

     macOS     Start Spend Tracker.command
     Windows   Start Spend Tracker.bat
     Linux     start-spend-tracker.sh       (or run ./start-spend-tracker.sh)

   The first time, your system stops it with a warning, because these files
   are not signed yet. That is expected:

     macOS     Close the warning. Open System Settings -> Privacy & Security,
               scroll down to the line about "Start Spend Tracker.command",
               click "Open Anyway" and enter your password. It starts by
               itself. (Right-click -> Open no longer does this.)
     Windows   SmartScreen says "Windows protected your PC". Click
               "More info", then "Run anyway".

   After that first time it opens without asking.

   A window opens and shows what it does; you do not type anything into it.
   The first start downloads Spend Tracker, which takes a few minutes. When it
   is ready, your browser opens http://localhost:8848, where the setup wizard
   creates the first account.
4. The wizard asks for a one-time setup token. It is in the app's log: in
   Docker Desktop, Containers -> spend-tracker -> app-1 -> Logs (in Podman
   Desktop, Containers -> spend-tracker -> spend-tracker-app-1 -> Logs).
   Look for the last line saying "Finish setup at /setup with this one-time
   token" and copy the token printed on its own line below it, after an
   empty line. Each start makes a new token until setup is finished.

After that, Spend Tracker starts with Docker or Podman, and updates are made
in the browser, under Application. Nothing else needs this folder's launcher.


Run the launcher again when
---------------------------

- Spend Tracker does not start after reinstalling Docker or Podman.
- The browser says the updater cannot reach the container engine, or a
  recovery page asks you to run the launcher.

It is safe to run at any time. It keeps the release your ledger is at; it
never moves you to an older or newer one. A newer zip's launcher, unzipped
anywhere, does the same and also replaces the updater when its own is newer:
that is the repair for an updater that no longer works.


Installed in both Docker and Podman
-----------------------------------

The launcher uses the Spend Tracker that is running. If neither is running,
it says which release each one's ledger is at and when each was last
started, and starts the one whose ledger is newer. If both are at the same
release, or one cannot be read, it stops and asks you to remove the one you
no longer use. Before it changes anything, it checks that nothing else is
using port 8848, and stops if something is, saying what.

To remove the one you no longer use, in Docker Desktop or Podman Desktop:
Containers -> spend-tracker -> Delete. From a terminal, in the folder that
install was last started from:

  docker compose --env-file .env down     (or: podman compose ...)

Either way removes its containers and keeps its ledger, in the engine's
volumes, so it can be started again. Delete those volumes (Volumes ->
spend-tracker_...) only once you are sure you no longer need that ledger.
If in doubt, start it once more and take a backup under Application ->
Backups first.


Windows
-------

"Start Spend Tracker.bat" has been run with Docker Desktop on Windows 11.
With Podman on Windows it has not been tried yet. If it stops, open a
terminal in this folder and run:

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
