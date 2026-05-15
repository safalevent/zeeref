# This file is part of ZeeRef.
#
# ZeeRef is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ZeeRef is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ZeeRef.  If not, see <https://www.gnu.org/licenses/>.

"""Named session IPC server.

When ZeeRef is started with ``--session <name>``, a
:class:`SessionServer` listens for local IPC connections via
:class:`QLocalServer`.  A lightweight client (``zeeref-cli``) connects
with :class:`QLocalSocket` and exchanges JSON messages.

Transport differs by platform (Qt handles the translation):
  * Linux/macOS: AF_UNIX socket at ``$XDG_RUNTIME_DIR/zeeref-<name>``
    (falls back to ``$TMPDIR``).
  * Windows: named pipe at ``\\\\.\\pipe\\zeeref-<name>``.

Wire protocol (one JSON object per line, ``\\n``-terminated).  Server
sends a ``hello`` greeting immediately on connect; clients should read
it before sending::

    Server (on connect): {"type": "hello", "protocol_version": 1,
                          "app_version": "..."}

    Client: {"type": "ping"}
    Server: {"type": "pong"}

    Client: {"type": "add", "payload": [{"path": "...", ...}]}
    Server: {"type": "ok"} | {"type": "error", "message": "..."}

    Client: {"type": "new", "force": false}
    Server: {"type": "ok"} | {"type": "error", "message": "..."}

    Client: {"type": "open", "path": "...", "force": false}
    Server: {"type": "ok"} | {"type": "error", "message": "..."}

    Client: {"type": "status"}
    Server: {"type": "status_info", "loaded_file": "..." | null,
             "item_count": N, "dirty": bool}

Bump ``PROTOCOL_VERSION`` on any wire-incompatible change.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import cast

from PyQt6 import QtCore, QtNetwork

from zeeref import constants
from zeeref.fileio.io import ImageInsert

logger = logging.getLogger(__name__)


PROTOCOL_VERSION = 1


# -- Messages --------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ClientMessage:
    """Base class for messages sent by the client."""

    type: str


@dataclasses.dataclass(frozen=True)
class AddMessage(ClientMessage):
    """Request to insert images into the scene."""

    type: str = "add"
    images: tuple[ImageInsert, ...] = ()


@dataclasses.dataclass(frozen=True)
class PingMessage(ClientMessage):
    """Health check request."""

    type: str = "ping"


@dataclasses.dataclass(frozen=True)
class NewMessage(ClientMessage):
    """Reset scene to empty."""

    type: str = "new"
    force: bool = False


@dataclasses.dataclass(frozen=True)
class OpenMessage(ClientMessage):
    """Open a .zref file in the running session."""

    type: str = "open"
    path: str = ""
    force: bool = False


@dataclasses.dataclass(frozen=True)
class StatusRequestMessage(ClientMessage):
    """Request session status info."""

    type: str = "status"


@dataclasses.dataclass(frozen=True)
class ServerMessage:
    """Base class for messages sent by the server."""

    type: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))


@dataclasses.dataclass(frozen=True)
class OkMessage(ServerMessage):
    type: str = "ok"


@dataclasses.dataclass(frozen=True)
class ErrorMessage(ServerMessage):
    type: str = "error"
    message: str = ""


@dataclasses.dataclass(frozen=True)
class PongMessage(ServerMessage):
    type: str = "pong"


@dataclasses.dataclass(frozen=True)
class HelloMessage(ServerMessage):
    type: str = "hello"
    protocol_version: int = PROTOCOL_VERSION
    app_version: str = ""


@dataclasses.dataclass(frozen=True)
class StatusInfoMessage(ServerMessage):
    type: str = "status_info"
    loaded_file: str | None = None
    item_count: int = 0
    dirty: bool = False


# -- Parsing ----------------------------------------------------------------


def server_name(session_name: str) -> str:
    """Return the server identifier to pass to :class:`QLocalServer.listen`
    and :class:`QLocalSocket.connectToServer`.

    On Windows this is a bare name (Qt maps to ``\\\\.\\pipe\\zeeref-<name>``).
    On Unix it is an absolute path so we can honour ``$XDG_RUNTIME_DIR``
    for per-user cleanup on logout.
    """
    if sys.platform == "win32":
        return f"zeeref-{session_name}"
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(runtime, f"zeeref-{session_name}")


def _parse_insert_entry(raw: object, index: int) -> ImageInsert | str:
    """Parse a single JSON object into an ImageInsert.

    Returns an ImageInsert on success, or an error string on failure.
    """
    if not isinstance(raw, dict):
        return f"item {index}: expected object, got {type(raw).__name__}"
    # JSON objects always have string keys
    d = cast(dict[str, object], raw)
    path = d.get("path")
    if not isinstance(path, str) or not path:
        return f"item {index}: missing or invalid 'path'"
    title = d.get("title")
    if title is not None and not isinstance(title, str):
        return f"item {index}: 'title' must be a string"
    caption = d.get("caption")
    if caption is not None and not isinstance(caption, str):
        return f"item {index}: 'caption' must be a string"
    resolved = Path(path).resolve()
    if not resolved.is_file():
        logger.warning("Session: skipping non-existent path: %s", path)
        return f"item {index}: file not found: {path}"
    return ImageInsert(path=str(resolved), title=title, caption=caption)


def parse_message(line: str) -> ClientMessage | ErrorMessage:
    """Parse a JSON line into a typed ClientMessage.

    Returns an ErrorMessage if parsing or validation fails.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        return ErrorMessage(message=f"invalid JSON: {e}")

    if not isinstance(raw, dict):
        return ErrorMessage(message="expected JSON object")

    msg = cast(dict[str, object], raw)
    msg_type = msg.get("type")
    if not isinstance(msg_type, str):
        return ErrorMessage(message="missing or invalid 'type'")

    if msg_type == "ping":
        return PingMessage()

    if msg_type == "status":
        return StatusRequestMessage()

    if msg_type == "new":
        force = msg.get("force", False)
        if not isinstance(force, bool):
            return ErrorMessage(message="'force' must be a boolean")
        return NewMessage(force=force)

    if msg_type == "open":
        path = msg.get("path")
        if not isinstance(path, str) or not path:
            return ErrorMessage(message="'open' requires 'path' string")
        force = msg.get("force", False)
        if not isinstance(force, bool):
            return ErrorMessage(message="'force' must be a boolean")
        return OpenMessage(path=path, force=force)

    if msg_type == "add":
        payload = msg.get("payload")
        if not isinstance(payload, list) or not payload:
            return ErrorMessage(message="'add' requires non-empty 'payload' array")

        images: list[ImageInsert] = []
        for i, entry in enumerate(payload):
            parsed = _parse_insert_entry(entry, i)
            if isinstance(parsed, str):
                if "file not found" in parsed:
                    continue
                return ErrorMessage(message=parsed)
            images.append(parsed)

        if not images:
            return ErrorMessage(message="no valid files")
        return AddMessage(images=tuple(images))

    return ErrorMessage(message=f"unknown type: {msg_type}")


# -- Server -----------------------------------------------------------------


class SessionServer(QtCore.QObject):
    """QLocalServer that accepts JSON messages over a named Unix socket.

    Mutating operations (add/new/open) are serialized through a single
    queue so they don't race against each other on the scene.  Reads
    (status/ping) bypass the queue.
    """

    def __init__(
        self,
        session_name: str,
        insert_fn: Callable[[list[ImageInsert], Callable[[list[str]], None]], None],
        new_fn: Callable[[bool, Callable[[list[str]], None]], None],
        open_fn: Callable[[Path, bool, Callable[[list[str]], None]], None],
        status_fn: Callable[[], StatusInfoMessage],
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_name = session_name
        self._insert_fn = insert_fn
        self._new_fn = new_fn
        self._open_fn = open_fn
        self._status_fn = status_fn
        self._server = QtNetwork.QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: list[_SessionConnection] = []
        self._queue: deque[tuple[ClientMessage, _SessionConnection]] = deque()
        self._busy = False

    def start(self) -> bool:
        """Begin listening.  Returns False if the name is taken."""
        name = server_name(self._session_name)
        QtNetwork.QLocalServer.removeServer(name)
        if not self._server.listen(name):
            logger.error(
                "SessionServer: failed to listen on %s: %s",
                name,
                self._server.errorString(),
            )
            return False
        logger.info(
            "Session '%s' listening on %s",
            self._session_name,
            self._server.fullServerName(),
        )
        return True

    def shutdown(self) -> None:
        """Stop listening and clean up."""
        self._server.close()
        for conn in self._connections:
            conn.close()
        self._connections.clear()
        logger.info("Session '%s' shut down", self._session_name)

    # -- internal ----------------------------------------------------------

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            conn = _SessionConnection(socket, self._on_client_message, self)
            self._connections.append(conn)
            conn.reply(HelloMessage(app_version=constants.VERSION))

    def _on_client_message(self, msg: ClientMessage, conn: _SessionConnection) -> None:
        """Dispatch a parsed client message.

        Reads reply inline; mutations get queued for serial processing.
        """
        if isinstance(msg, StatusRequestMessage):
            conn.reply(self._status_fn())
            return
        if isinstance(msg, (AddMessage, NewMessage, OpenMessage)):
            self._queue.append((msg, conn))
            self._process_queue()
            return
        # Unknown — shouldn't reach here since parse_message guards.
        conn.reply(ErrorMessage(message=f"unhandled message type: {msg.type}"))

    def _process_queue(self) -> None:
        if self._busy or not self._queue:
            return
        self._busy = True
        msg, conn = self._queue[0]
        if isinstance(msg, AddMessage):
            logger.info("Session: inserting %d image(s)", len(msg.images))
            self._insert_fn(list(msg.images), self._on_op_finished)
        elif isinstance(msg, NewMessage):
            logger.info("Session: new scene (force=%s)", msg.force)
            self._new_fn(msg.force, self._on_op_finished)
        elif isinstance(msg, OpenMessage):
            logger.info("Session: opening %s (force=%s)", msg.path, msg.force)
            self._open_fn(Path(msg.path), msg.force, self._on_op_finished)
        else:
            # Defensive — should be unreachable.
            self._busy = False
            self._queue.popleft()
            conn.reply(ErrorMessage(message=f"unqueueable message: {msg.type}"))
            self._process_queue()

    def _on_op_finished(self, errors: list[str]) -> None:
        self._busy = False
        msg, conn = self._queue.popleft()
        if errors:
            conn.reply(ErrorMessage(message="; ".join(errors)))
        else:
            conn.reply(OkMessage())
        self._process_queue()

    def _remove_connection(self, conn: _SessionConnection) -> None:
        if conn in self._connections:
            self._connections.remove(conn)


class _SessionConnection(QtCore.QObject):
    """Handles one client connection, accumulating bytes and parsing lines."""

    def __init__(
        self,
        socket: QtNetwork.QLocalSocket,
        on_message: Callable[[ClientMessage, _SessionConnection], None],
        server: SessionServer,
    ) -> None:
        super().__init__(server)
        self._socket = socket
        self._on_message = on_message
        self._server = server
        self._buf = b""
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._on_disconnected)

    def reply(self, msg: ServerMessage) -> None:
        if (
            self._socket.state()
            == QtNetwork.QLocalSocket.LocalSocketState.ConnectedState
        ):
            self._socket.write((msg.to_json() + "\n").encode())
            self._socket.flush()

    def close(self) -> None:
        self._socket.disconnectFromServer()

    def _on_ready_read(self) -> None:
        raw = self._socket.readAll()
        if raw:
            self._buf += raw.data()
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._process_line(line.decode())

    def _process_line(self, line: str) -> None:
        result = parse_message(line)
        if isinstance(result, ErrorMessage):
            self.reply(result)
        elif isinstance(result, PingMessage):
            self.reply(PongMessage())
        else:
            self._on_message(result, self)

    def _on_disconnected(self) -> None:
        self._server._remove_connection(self)
