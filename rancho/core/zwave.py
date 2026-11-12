"""One connection to zwave.service (Z-Wave JS Server on localhost).

Each Z-Wave worker keeps its own connection; the server accepts several clients.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zwave_js_server.model.controller import Controller

URL = "ws://127.0.0.1:3000"
RECONNECT_DELAY = 5


async def session(attach: Callable[[Controller], None]) -> None:
    """Connect, call `attach` once the server's state is loaded, and return when it closes."""
    import aiohttp
    from zwave_js_server.client import Client

    async with aiohttp.ClientSession() as http:
        client = Client(URL, http)
        await client.connect()
        ready = asyncio.Event()
        listening = asyncio.create_task(client.listen(ready))
        try:
            waiting = asyncio.create_task(ready.wait())
            await asyncio.wait({listening, waiting}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if ready.is_set():
                assert client.driver is not None
                attach(client.driver.controller)
            await listening
        finally:
            listening.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listening
            await client.disconnect()
