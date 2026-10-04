"""Reading an upload without letting it decide how much memory to spend.

Shared rather than copied. `imports.upload` got this right first and receipts
need the identical thing with a different ceiling, and the failure mode of a
second copy is that one of them gets the fix and the other does not.
"""

from __future__ import annotations

from fastapi import Request, UploadFile

from ..errors import TooLarge

#: One megabyte at a time. The read stops one chunk past the limit, so an
#: oversized upload costs a megabyte rather than all of it.
CHUNK = 1024 * 1024


def refuse_declared_size(file: UploadFile | None, limit: int, message: str) -> None:
    """Check what the client says it is sending, before reading a byte.

    Reading the body and *then* measuring it spends exactly the memory the
    limit exists to save: a 200 MB upload cost ~930 MB of RSS before the
    refusal came back, a 4.6x amplification available to any member, and two of
    those OOM a small host.
    """
    declared = getattr(file, "size", None) if file is not None else None
    if declared is not None and declared > limit:
        raise TooLarge(message)


async def read_body_capped(request: Request, limit: int, message: str) -> bytes:
    """The same, for a request whose whole body *is* the file.

    `await request.body()` buffers everything before anything can object, which
    is precisely what `read_capped` exists not to do -- so this streams and
    stops one chunk past the limit, the same bargain one function down.

    `Content-Length` is checked first where the client sent one, for the reason
    `refuse_declared_size` gives: reading the body and then measuring it spends
    exactly the memory the ceiling is there to save.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TooLarge(message)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise TooLarge(message)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_capped(file: UploadFile, limit: int, message: str) -> bytes:
    """Read the upload, abandoning it one chunk past the limit.

    `await file.read()` with no argument buffers the whole body whatever its
    size, which is the thing this exists to not do.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise TooLarge(message)
        chunks.append(chunk)
    return b"".join(chunks)
