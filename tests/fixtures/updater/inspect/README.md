# Recorded container inspect output

`GET /containers/{id}/json` for the app container, recorded by spike 3 (#157)
on each engine the design notes support, and read by U11
(`tests/test_updater_shapes.py`): the copy of the app (C10) is checked against
every one of them.

| File | Engine | Compose | Layout |
|---|---|---|---|
| `docker-engine-rootful/loopback-app.json` | Docker Engine 29.8, Debian 13 | Docker Compose 5.6 | loopback |
| `docker-engine-rootful/sidecar.json` | Docker Engine 29.8, Debian 13 | Docker Compose 5.6 | sidecar (the app, then the sidecar) |
| `docker-desktop/loopback-app.json` | Docker Desktop 4.93 (Engine 29.8), macOS | Docker Compose | loopback, with the spike's `/pin` bind |
| `podman-rootful-fedora/loopback-app.json` | Podman 5.8, Fedora 44 | podman-compose 1.6 (in a pod) | loopback |
| `podman-rootless-fedora/loopback-app.json` | Podman 5.8, Fedora 44, rootless | podman-compose 1.6 (in a pod) | loopback |
| `podman-machine/loopback-app-podman-compose.json` | Podman 6.1, `podman machine` | podman-compose (in a pod) | loopback |
| `podman-machine/loopback-app-compose-provider.json` | Podman 6.1, `podman machine` | `podman compose` with Docker Compose as provider | loopback |

**Scrubbed** before they were committed: every 64-hex id (containers, images,
networks, endpoints, sandboxes, pods) is replaced consistently within its
file, so a `container:<id>` network mode still names the sidecar's id in the
same file; host paths under `/home/<user>` are `/home/user/…`, and macOS
paths (a home directory, the spike's scratch directory) are `/Volumes/work/…`,
still a macOS path for detection's second signal (R25); addresses are
TEST-NET-1 (`192.0.2.0/24`)
and MACs one fixed value; the tailnet's name is `tailnet-example.ts.net`;
`TS_AUTHKEY` is `<redacted>`; `GraphDriver` is dropped.
