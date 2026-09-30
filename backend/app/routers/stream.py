"""A streamed response shaped like Delegate's chat answer, to test the whole path
between the browser and the API: proxy buffering, compression and read timeouts.

The events, as in Delegate (`fastapi.sse`, JSON data):
1. `status`, at once;
2. `STREAM_SILENCE_S` seconds of silence, like the agent's search before the
   first word. FastAPI writes a `: ping` comment after every 15 idle seconds, as
   it does in Delegate, so the longest gap a proxy sees is 15 s;
3. 40 `delta` events, 150 ms apart. A buffering proxy delivers them all at once,
   at the end, which the page shows by the time of the first delta;
4. `done`.
"""

import asyncio
import time
from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db

router = APIRouter(tags=["stream"])

DELTAS = 40
# A module constant so tests can set it to 0.
DELTA_INTERVAL_S = 0.15


@router.post("/stream", response_class=EventSourceResponse)
async def stream(db: Session = Depends(get_db)) -> AsyncIterable[ServerSentEvent]:
    # The same session the router's session check used. Closed now, or its pooled
    # connection would stay checked out for the whole stream; Delegate's pool
    # filled up that way at ~15 concurrent chats.
    db.close()

    started = time.monotonic()
    yield ServerSentEvent(
        event="status", data={"phase": "silence", "seconds": settings.stream_silence_s}
    )
    await asyncio.sleep(settings.stream_silence_s)

    for n in range(1, DELTAS + 1):
        yield ServerSentEvent(event="delta", data={"n": n, "text": f"word{n} "})
        await asyncio.sleep(DELTA_INTERVAL_S)

    yield ServerSentEvent(
        event="done", data={"deltas": DELTAS, "seconds": round(time.monotonic() - started, 1)}
    )
