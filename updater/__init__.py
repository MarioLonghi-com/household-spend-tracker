"""The self-updater: a separate container that holds the engine socket.

The app never gets the socket. It writes a request into the shared `update`
volume, and this package -- running in its own container -- validates it,
acts on it through a small engine client that can only make a listed set of
calls, and writes back what happened. Design notes, Parts 5 and 6.

This package is the core: the file contract (`contract`), the volume and its
atomic, group-shared writes (`volume`), the journal and what to do after a
crash (`journal`), deadlines that do not count sleep (`clock`), and the
restricted engine client (`engine`). The orchestration that strings them into
prepare, apply and rollback is built on top of it.

Standard library only. Nothing here imports `app`: the updater image carries
this package, not the application, and the less it carries the less there is
to trust beside a root-equivalent socket.
"""
