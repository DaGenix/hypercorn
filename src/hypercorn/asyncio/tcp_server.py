import asyncio
from functools import partial
from typing import Any, Generator, Optional

from .spawn_app import spawn_app
from ..config import Config
from ..events import Closed, Event, RawData, Updated
from ..protocol import ProtocolWrapper
from ..typing import ASGIFramework
from ..utils import parse_socket_addr

MAX_RECV = 2 ** 16


class EventWrapper:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    async def clear(self) -> None:
        self._event.clear()

    async def wait(self) -> None:
        await self._event.wait()

    async def set(self) -> None:
        self._event.set()


class TCPServer:
    def __init__(
        self,
        app: ASGIFramework,
        loop: asyncio.AbstractEventLoop,
        config: Config,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.app = app
        self.config = config
        self.loop = loop
        self.protocol: ProtocolWrapper
        self.reader = reader
        self.writer = writer

        self._keep_alive_timeout_handle: Optional[asyncio.TimerHandle] = None

    def __await__(self) -> Generator[Any, None, None]:
        return self.run().__await__()

    async def run(self) -> None:
        socket = self.writer.get_extra_info("socket")
        client = parse_socket_addr(socket.family, socket.getpeername())
        server = parse_socket_addr(socket.family, socket.getsockname())
        ssl_object = self.writer.get_extra_info("ssl_object")
        if ssl_object is not None:
            ssl = True
            alpn_protocol = ssl_object.selected_alpn_protocol()
        else:
            ssl = False
            alpn_protocol = "http/1.1"

        self.protocol = ProtocolWrapper(
            self.config,
            ssl,
            client,
            server,
            self.protocol_send,
            partial(spawn_app, self.app, self.loop, self.config),
            EventWrapper,
            alpn_protocol,
        )
        await self.protocol.initiate()
        self.loop.create_task(self.protocol.send_task())
        self._update_keep_alive_timeout()
        await self._read_data()

    async def protocol_send(self, event: Event) -> None:
        if isinstance(event, RawData):
            try:
                self.writer.write(event.data)
                await self.writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self.protocol.handle(Closed())
        elif isinstance(event, Closed):
            await self._close()
            await self.protocol.handle(Closed())
        elif isinstance(event, Updated):
            pass  # Triggers the keep alive timeout update
        self._update_keep_alive_timeout()

    async def _read_data(self) -> None:
        while True:
            try:
                data = await self.reader.read(MAX_RECV)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                await self.protocol.handle(Closed())
                break
            else:
                if data == b"":
                    self._update_keep_alive_timeout()
                    break
                await self.protocol.handle(RawData(data))
                self._update_keep_alive_timeout()

    async def _close(self) -> None:
        try:
            self.writer.write_eof()
        except (NotImplementedError, OSError):
            pass  # Likely SSL connection

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Already closed

    def _update_keep_alive_timeout(self) -> None:
        if self._keep_alive_timeout_handle is not None:
            self._keep_alive_timeout_handle.cancel()
        self._keep_alive_timeout_handle = None
        if self.protocol.idle:
            self._keep_alive_timeout_handle = self.loop.call_later(
                self.config.keep_alive_timeout, self.writer.close
            )