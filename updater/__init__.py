"""The self-updater: a separate container that holds the engine socket.

The app never gets the socket. It writes a request into the shared `update`
volume, and this package -- running in its own container -- validates it,
acts on it through a small engine client that can only make a listed set of
calls, and writes back what happened. Design notes, Parts 5 and 6.

This package is the core: the file contract (`contract`), the volume and its
atomic, group-shared writes (`volume`), the journal and what to do after a
crash (`journal`), deadlines that do not count sleep (`clock`), and the
restricted engine client (`engine`).

On top of it, the orchestration (#161): `prepare` (4.4), `apply` (steps 0-10
of 4.2, the rollback of 4.3 and resuming after a crash, 5.6), the container
shapes it creates and the allowlist copy of the app (`shapes`), finding the
app and the sidecar (`survey`), one-offs (`oneoff`), health from where
requests arrive (`health`), the pin (`pin`), the handover interface
(`handover`, its mechanics are #162's), browser recovery behind the one-time
code (`recovery`, Part 11: the code checked here, never by the page), the
app rejoining its sidecar's network after the sidecar restarts (`rejoin`,
#275), and the request loop (`service`, `python -m updater`).

Standard library only, except `verify`, which uses sigstore. Nothing here
imports `app`: the updater image carries this package, not the application,
and the less it carries the less there is to trust beside a root-equivalent
socket.
"""
