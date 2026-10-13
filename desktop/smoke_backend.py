"""Smoke-test a frozen desktop backend (#319): start it from `/` on a fresh
ledger and check what only a complete build can do.

    python desktop/smoke_backend.py build/backend/spend-tracker-backend/spend-tracker-backend

- the start handshake, and how long it took;
- the build stamp (`/api/health` names a commit);
- the bundled common-password list: a common password is refused;
- the desktop setup: no token, one recovery code;
- a household whose accented name survives the pipe, with the bundled seed
  categories;
- a clean exit when stdin closes.

Exits 1 at the first thing missing, saying what.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.desktop.stdio_adapter import encode, read_message  # noqa: E402

NAME = "Café Ñandú — Göteborg"


class Backend:
    def __init__(self, executable: str, data: str) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SPENDTRACKER_") and k != "DATABASE_URL"}
        env.update(SPENDTRACKER_PROFILE="desktop", SPENDTRACKER_DATA_DIR=data)
        self.log = pathlib.Path(data) / "backend.log"
        self.child = subprocess.Popen(
            [executable], cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log.open("wb"), env=env,
        )
        self.next = 1
        self.cookies: dict[str, str] = {}

    def handshake(self) -> dict:
        said = read_message(self.child.stdout)
        if said is None:
            raise SystemExit(f"no handshake; the backend said:\n{self.log.read_text()[-2000:]}")
        return said[0]

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        headers = [["sec-fetch-site", "same-origin"]]
        if body is not None:
            headers.append(["content-type", "application/json"])
        if self.cookies:
            headers.append(["cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items())])
        head = {"id": self.next, "method": method, "path": path, "query": "", "headers": headers}
        self.next += 1
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        self.child.stdin.write(encode(head, raw))
        self.child.stdin.flush()
        answer, content = read_message(self.child.stdout)
        for key, value in answer["headers"]:
            if key == "set-cookie":
                name, _, rest = value.partition("=")
                self.cookies[name] = rest.split(";", 1)[0]
        try:
            return answer["status"], json.loads(content)
        except ValueError:
            return answer["status"], content

    def stop(self) -> int:
        self.child.stdin.close()
        return self.child.wait(timeout=60)


def check(what: str, ok: bool, said: object = "") -> None:
    sys.stderr.write(f"{'ok  ' if ok else 'FAIL'}  {what}{f'  ({said})' if said != '' else ''}\n")
    if not ok:
        raise SystemExit(1)


def main(executable: str) -> int:
    started = time.monotonic()
    backend = Backend(executable, tempfile.mkdtemp(prefix="st-smoke-"))
    head = backend.handshake()
    took = f"{time.monotonic() - started:.1f}s, {head.get('launch', {}).get('outcome')}"
    check("handshake", head.get("event") == "ready", took)

    status, health = backend.call("GET", "/api/health")
    check("build stamp", status == 200 and bool(health.get("commit")), health.get("commit"))

    who = {"token": "", "email": "jane@example.test", "display_name": "Jane"}
    status, refused = backend.call("POST", "/api/setup/begin", {**who, "password": "password1234"})
    check("common-password list", status == 422 and refused.get("code") == "password.common", refused.get("code"))

    status, begun = backend.call("POST", "/api/setup/begin", {**who, "password": "a sufficiently long password"})
    check("desktop setup", status == 200 and len(begun.get("recovery_codes", [])) == 1 and begun.get("secret") is None)
    status, _ = backend.call("POST", "/api/setup/complete", {"blob": begun["blob"], "codes_saved": True})
    check("signed in", status == 200 and backend.call("GET", "/api/me")[0] == 200)

    status, house = backend.call("POST", "/api/households", {"name": NAME})
    check("accented name across the pipe", status == 201 and house.get("name") == NAME, house.get("name"))
    status, categories = backend.call("GET", f"/api/households/{house['id']}/categories")
    seeded = isinstance(categories, list) and len(categories) > 0
    check("seed categories", status == 200 and seeded, len(categories) if seeded else categories)

    check("clean exit", backend.stop() == 0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
