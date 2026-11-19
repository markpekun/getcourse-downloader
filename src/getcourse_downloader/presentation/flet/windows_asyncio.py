from __future__ import annotations

import asyncio
import socket
from asyncio import ProactorEventLoop  # type: ignore[attr-defined]  # Windows only
from asyncio.proactor_events import _ProactorSocketTransport


class _FletSocketTransport(_ProactorSocketTransport):
    _called_connection_lost: bool
    _protocol: asyncio.BaseProtocol
    _sock: socket.socket | None
    _server: asyncio.Server | None

    def _call_connection_lost(self, exc: Exception | None) -> None:
        if self._called_connection_lost:
            return

        try:
            self._protocol.connection_lost(exc)
        finally:
            sock = self._sock
            assert sock is not None
            try:
                if sock.fileno() != -1:
                    sock.shutdown(socket.SHUT_RDWR)
            except ConnectionResetError as reset:
                # CPython gh-160429: finish closing a socket the peer has already reset.
                if getattr(reset, "winerror", None) != 10054:
                    raise
            finally:
                sock.close()
                self._sock = None
                if self._server is not None:
                    self._server._detach(self)  # type: ignore[attr-defined]
                    self._server = None
                self._called_connection_lost = True


class FletProactorEventLoop(ProactorEventLoop):
    def _make_socket_transport(self, sock, protocol, waiter=None, extra=None, server=None):
        if server is None:
            return super()._make_socket_transport(  # type: ignore[misc]
                sock, protocol, waiter, extra, server
            )
        return _FletSocketTransport(self, sock, protocol, waiter, extra, server)
