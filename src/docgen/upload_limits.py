"""Shared byte-cap + timeout helpers for streamed request bodies and uploads.

Two entry points, one core loop:

  * :func:`read_capped_body` for a Starlette ``Request`` — used by the
    JSON intake route (``docgen.api.pap``) to bound the request body
    before Pydantic parses it.
  * :func:`read_capped_upload` for a FastAPI ``UploadFile`` — used by
    every template / static-doc / package upload route.

Both funnel through :func:`_consume_chunks_capped`, which reads the
async byte iterator with a running total and refuses to buffer past
``cap`` bytes. Per-chunk ``asyncio.wait_for`` timeouts bound a
slowloris trickle so a caller can't hold a worker slot open by
drip-feeding one byte every 60 seconds.

The two entry points differ only in how they build the byte iterator
(``request.stream()`` yields chunks directly; ``UploadFile.read(N)``
needs to be looped until empty). Keeping the size-cap + timeout logic
in one place means a future adjustment to either bound applies
identically to both paths.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, UploadFile

# Global defaults. Callers override the cap when a route has stricter
# needs (JSON intake wants 512 KB; template uploads want 20 MB).
DEFAULT_UPLOAD_CAP_BYTES = 20 * 1024 * 1024
DEFAULT_INTAKE_CAP_BYTES = 512 * 1024

# Per-chunk read deadline. A well-behaved client sends the whole body
# in bursts; 30 s between chunks is comfortably longer than any real
# network hiccup and short enough that a slowloris trickle gets
# hung up rather than pinning a worker slot indefinitely.
_CHUNK_READ_TIMEOUT_SECONDS = 30.0

# Chunk size for UploadFile.read() loops. 64 KB balances syscall
# overhead vs. worst-case wasted read when the cap is close.
_UPLOAD_CHUNK_SIZE = 65536


async def _consume_chunks_capped(
    chunks: AsyncIterator[bytes],
    cap: int,
    kind_label: str,
) -> bytes:
    """Buffer bytes from an async iterator, refusing to exceed ``cap``.

    Each ``__anext__`` call is wrapped in :func:`asyncio.wait_for` so a
    slowloris trickle raises :class:`asyncio.TimeoutError` (surfaced as
    a 408) rather than holding the coroutine open indefinitely.
    """
    buf: list[bytes] = []
    total = 0
    it = chunks.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=_CHUNK_READ_TIMEOUT_SECONDS)
        except StopAsyncIteration:
            break
        except TimeoutError as e:
            raise HTTPException(
                status_code=408,
                detail=f"{kind_label} read timed out after {_CHUNK_READ_TIMEOUT_SECONDS:.0f}s",
            ) from e
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail=f"{kind_label} exceeds {cap // 1024} KB cap",
            )
        buf.append(chunk)
    return b"".join(buf)


async def read_capped_body(request: Request, cap: int = DEFAULT_INTAKE_CAP_BYTES) -> bytes:
    """Stream a request body with an early Content-Length reject.

    Fast-paths on the ``Content-Length`` header so an honest oversize
    upload gets a 413 without buffering. Chunked encoding (no header)
    falls through to the streaming byte-count check.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                raise HTTPException(
                    status_code=413,
                    detail=f"intake body exceeds {cap // 1024} KB cap",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="malformed content-length") from None
    return await _consume_chunks_capped(request.stream(), cap, "intake body")


async def read_capped_upload(
    file: UploadFile,
    cap: int = DEFAULT_UPLOAD_CAP_BYTES,
) -> bytes:
    """Read an :class:`UploadFile` with a byte-count cap + slowloris deadline.

    Precondition: the caller must not have pre-read from ``file`` (no
    prior ``await file.read()`` / ``await file.seek()``). This helper
    reads from the current stream position; a partially-consumed file
    yields only the remaining bytes and the cap check passes on fewer
    bytes than the caller uploaded. Every route in this codebase either
    routes the raw ``UploadFile`` here or reads the whole body via this
    helper; if a future caller needs both patterns it must ``seek(0)``
    first.
    """

    async def _chunks() -> AsyncIterator[bytes]:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    return await _consume_chunks_capped(_chunks(), cap, "upload")


__all__ = [
    "DEFAULT_INTAKE_CAP_BYTES",
    "DEFAULT_UPLOAD_CAP_BYTES",
    "read_capped_body",
    "read_capped_upload",
]
