# Engine answers for the updater's tests

Each folder holds an engine's `GET /version` answer (`version.json`) and, where
detection needs it, its `GET /info` answer (`info.json`).

**Recorded** from a real engine, then scrubbed of ids, home paths, host names
and uids:

| Folder | Engine |
|---|---|
| `docker-desktop` | Docker Desktop 4.93 for Mac, Engine 29.8.1 |
| `podman-machine` | Podman 6.1.3 in `podman machine` (Fedora CoreOS, rootless, SELinux enforcing) |
| `docker-engine-rootful` | Docker Engine 29.8.2 on Debian 13, rootful (API 1.40-1.56) |
| `podman-rootful-fedora` | Podman 5.8.7 on Fedora 44, rootful, SELinux enforcing |
| `podman-rootless-fedora` | Podman 5.8.7 on Fedora 44, rootless, SELinux enforcing |

**Written by hand** from the engines' documented API shapes and published
version windows. They stand in until a run on that engine records the real
answer; when one does, replace the folder's files with the recorded ones and
move the row up. Every hand-written file written for engine detection also
carries a top-level `"_hand_written": true`, which no engine sends.

| Folder | Stands in for |
|---|---|
| `docker-28`, `docker-29.0` | Docker Engine 28 and 29.0 windows (negotiation only) |
| `docker-future` | an engine whose floor is above the tested window (`outdated`) |
| `podman-4.9` | Podman 4.9 window (negotiation only) |
| `docker-engine-rootless` | Docker Engine 29.8.2 on Debian 13, rootless: the version and window the daemon reported, the rest written |
| `docker-desktop-windows` | Docker Desktop on Windows in Windows containers mode (`OSType: windows`) |
| `podman-4.3` | Podman 4.3, below the 4.4 floor |
