#!/usr/bin/env python3

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

"""CLI client for interacting with a running ZeeRef session.

Uses :class:`QLocalSocket` so the same code works on Linux, macOS, and
Windows (named pipes).  Runs without a :class:`QCoreApplication`; the
synchronous ``waitFor*`` methods are sufficient for a one-shot CLI.

The CLI consumes the server's ``hello`` greeting on every connect and
validates that ``protocol_version`` matches the client's expectation.
Only ``add`` (and future ``add-text``) auto-spawn a session — other
subcommands error fast if the session is not running.  Use ``start``,
``new``, or ``open`` to launch a session explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PyQt6 import QtNetwork

from zeeref.session import PROTOCOL_VERSION, server_name


CONNECT_TIMEOUT_MS = 500
SPAWN_CONNECT_POLL_MS = 200
SPAWN_TIMEOUT_S = 10
WRITE_TIMEOUT_MS = 5000
HELLO_TIMEOUT_MS = 5000
REPLY_TIMEOUT_MS = 60000  # inserts/opens can take a while


# -- low-level socket helpers -----------------------------------------------


def _read_line(sock: QtNetwork.QLocalSocket, timeout_ms: int) -> bytes:
    """Read one \\n-terminated line from *sock*; raises on timeout."""
    buf = b""
    deadline = time.monotonic() + timeout_ms / 1000
    while b"\n" not in buf:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        if not sock.waitForReadyRead(remaining_ms):
            break
        buf += sock.readAll().data()
    if b"\n" not in buf:
        raise TimeoutError("timed out waiting for server reply")
    line, _, _ = buf.partition(b"\n")
    return line


def _read_reply(
    sock: QtNetwork.QLocalSocket, timeout_ms: int = REPLY_TIMEOUT_MS
) -> dict:
    line = _read_line(sock, timeout_ms)
    return json.loads(line.decode())


def _send(sock: QtNetwork.QLocalSocket, message: dict) -> None:
    data = (json.dumps(message) + "\n").encode()
    sock.write(data)
    if not sock.waitForBytesWritten(WRITE_TIMEOUT_MS):
        sys.exit(f"Error: write timed out: {sock.errorString()}")


def _read_hello(sock: QtNetwork.QLocalSocket) -> dict:
    """Read the server's hello greeting and validate protocol version."""
    try:
        line = _read_line(sock, HELLO_TIMEOUT_MS)
    except TimeoutError:
        sys.exit("Error: timed out waiting for server hello")
    try:
        hello = json.loads(line.decode())
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid hello from server: {e}")
    if hello.get("type") != "hello":
        sys.exit(f"Error: expected hello, got: {hello}")
    server_proto = hello.get("protocol_version")
    if server_proto != PROTOCOL_VERSION:
        sys.exit(
            f"Error: protocol mismatch (client={PROTOCOL_VERSION}, "
            f"server={server_proto})"
        )
    return hello


# -- connect / spawn --------------------------------------------------------


def _try_connect(session: str) -> QtNetwork.QLocalSocket | None:
    """Try to connect to *session*. Returns sock on success, None if down."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(server_name(session))
    if sock.waitForConnected(CONNECT_TIMEOUT_MS):
        return sock
    sock.abort()
    return None


def _spawn_zeeref(session: str, extra_args: list[str] | None = None) -> Path:
    """Spawn ``zeeref --session SESSION [extra_args...]``. Returns log path."""
    zeeref_bin = shutil.which("zeeref")
    if not zeeref_bin:
        sys.exit("Error: 'zeeref' not found in PATH")

    log = tempfile.NamedTemporaryFile(
        prefix=f"zeeref-{session}-spawn-",
        suffix=".log",
        delete=False,
    )
    argv = [zeeref_bin, "--session", session]
    if extra_args:
        argv.extend(extra_args)
    print(f"Starting session '{session}'...", file=sys.stderr)
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log.fileno())
    log.close()
    return Path(log.name)


def _wait_for_connect(session: str) -> QtNetwork.QLocalSocket:
    """Poll-connect to *session* until SPAWN_TIMEOUT_S, then sys.exit."""
    name = server_name(session)
    deadline = time.monotonic() + SPAWN_TIMEOUT_S
    while time.monotonic() < deadline:
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(name)
        if sock.waitForConnected(SPAWN_CONNECT_POLL_MS):
            return sock
        sock.abort()
    sys.exit(f"Error: timed out connecting to session '{session}'")


def _spawn_and_connect(
    session: str, extra_args: list[str] | None = None
) -> tuple[QtNetwork.QLocalSocket, Path]:
    """Spawn zeeref with *extra_args* and poll until connected."""
    log_path = _spawn_zeeref(session, extra_args)
    try:
        sock = _wait_for_connect(session)
    except SystemExit:
        tail = log_path.read_text(errors="replace")[-2000:]
        if tail.strip():
            print(
                f"--- zeeref stderr ---\n{tail}",
                file=sys.stderr,
            )
        raise
    return sock, log_path


def _cleanup_spawn_log(log_path: Path | None) -> None:
    if log_path is None:
        return
    try:
        os.unlink(log_path)
    except OSError:
        pass


def _connect_or_die(session: str) -> QtNetwork.QLocalSocket:
    """Connect to *session* or sys.exit. Reads hello. No spawning."""
    sock = _try_connect(session)
    if sock is None:
        sys.exit(f"Error: session '{session}' is not running")
    assert sock is not None
    _read_hello(sock)
    return sock


def _connect_or_spawn(
    session: str, extra_args: list[str] | None = None
) -> tuple[QtNetwork.QLocalSocket, Path | None]:
    """Connect to *session*; spawn with *extra_args* if down. Reads hello."""
    sock = _try_connect(session)
    if sock is not None:
        _read_hello(sock)
        return sock, None
    sock, log_path = _spawn_and_connect(session, extra_args)
    _read_hello(sock)
    return sock, log_path


# -- request/reply ----------------------------------------------------------


def _request(sock: QtNetwork.QLocalSocket, message: dict) -> dict:
    """Send *message* and read one reply. Exits on transport error."""
    _send(sock, message)
    try:
        return _read_reply(sock)
    except (TimeoutError, json.JSONDecodeError) as e:
        sys.exit(f"Error reading reply: {e}")


def _emit(obj: dict) -> None:
    """Print a JSON object on stdout."""
    print(json.dumps(obj))


def _exit_on_error_reply(reply: dict) -> None:
    if reply.get("type") == "error":
        sys.exit(f"Error: {reply.get('message', 'unknown')}")


# -- payload helpers --------------------------------------------------------


def _build_add_payload(args: argparse.Namespace) -> list[dict]:
    if args.stdin:
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            sys.exit(f"Error: invalid JSON on stdin: {e}")
        if not isinstance(payload, list) or not payload:
            sys.exit("Error: expected non-empty JSON array on stdin")
        return payload

    if not args.files:
        sys.exit("Error: no files provided")
    payload: list[dict] = []
    for f in args.files:
        p = Path(f).resolve()
        if not p.is_file():
            print(f"Warning: {f} does not exist, skipping", file=sys.stderr)
            continue
        entry: dict[str, str] = {"path": str(p)}
        if args.title:
            entry["title"] = args.title
        if args.caption:
            entry["caption"] = args.caption
        payload.append(entry)
    return payload


# -- subcommand handlers ---------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> None:
    payload = _build_add_payload(args)
    if not payload:
        sys.exit("Error: no valid files to send")

    sock, spawn_log = _connect_or_spawn(args.session)
    try:
        reply = _request(sock, {"type": "add", "payload": payload})
        _exit_on_error_reply(reply)
        _emit({"ok": True, "session": args.session, "added": len(payload)})
    finally:
        sock.disconnectFromServer()
        _cleanup_spawn_log(spawn_log)


def _cmd_start(args: argparse.Namespace) -> None:
    sock, spawn_log = _connect_or_spawn(args.session)
    try:
        reply = _request(sock, {"type": "status"})
        _exit_on_error_reply(reply)
        _emit({"ok": True, "session": args.session, "status": reply})
    finally:
        sock.disconnectFromServer()
        _cleanup_spawn_log(spawn_log)


def _cmd_new(args: argparse.Namespace) -> None:
    # When session is down, "new" is satisfied by spawning a fresh empty
    # session — no IPC new needed.  When running, send the new message.
    existing = _try_connect(args.session)
    if existing is None:
        sock, spawn_log = _spawn_and_connect(args.session)
        try:
            _read_hello(sock)
            _emit({"ok": True, "session": args.session, "spawned": True})
        finally:
            sock.disconnectFromServer()
            _cleanup_spawn_log(spawn_log)
        return

    _read_hello(existing)
    try:
        reply = _request(existing, {"type": "new", "force": args.force})
        _exit_on_error_reply(reply)
        _emit({"ok": True, "session": args.session, "spawned": False})
    finally:
        existing.disconnectFromServer()


def _cmd_open(args: argparse.Namespace) -> None:
    path = Path(args.path).resolve()
    if not path.is_file():
        sys.exit(f"Error: file not found: {args.path}")

    existing = _try_connect(args.session)
    if existing is None:
        sock, spawn_log = _spawn_and_connect(args.session, [str(path)])
        try:
            _read_hello(sock)
            _emit(
                {
                    "ok": True,
                    "session": args.session,
                    "path": str(path),
                    "spawned": True,
                }
            )
        finally:
            sock.disconnectFromServer()
            _cleanup_spawn_log(spawn_log)
        return

    _read_hello(existing)
    try:
        reply = _request(
            existing,
            {"type": "open", "path": str(path), "force": args.force},
        )
        _exit_on_error_reply(reply)
        _emit(
            {
                "ok": True,
                "session": args.session,
                "path": str(path),
                "spawned": False,
            }
        )
    finally:
        existing.disconnectFromServer()


def _cmd_ping(args: argparse.Namespace) -> None:
    sock = _connect_or_die(args.session)
    try:
        reply = _request(sock, {"type": "ping"})
        _exit_on_error_reply(reply)
        _emit({"ok": True, "session": args.session, "reply": reply})
    finally:
        sock.disconnectFromServer()


def _cmd_status(args: argparse.Namespace) -> None:
    sock = _connect_or_die(args.session)
    try:
        reply = _request(sock, {"type": "status"})
        _exit_on_error_reply(reply)
        _emit({"ok": True, "session": args.session, "status": reply})
    finally:
        sock.disconnectFromServer()


def _cmd_sessions(args: argparse.Namespace) -> None:
    sessions = _scan_sessions()
    _emit({"ok": True, "sessions": sessions})


def _scan_sessions() -> list[dict]:
    """Find candidate session sockets and ping each to confirm it's live.

    On Unix this scans ``$XDG_RUNTIME_DIR`` (or ``$TMPDIR``) for
    ``zeeref-*`` entries.  On Windows there's no equivalent enumeration,
    so we return an empty list and let callers fall back to ``ping``.
    """
    if sys.platform == "win32":
        return []
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    out: list[dict] = []
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    for entry in entries:
        if not entry.startswith("zeeref-"):
            continue
        name = entry[len("zeeref-") :]
        sock = _try_connect(name)
        if sock is None:
            continue
        try:
            _read_hello(sock)
        except SystemExit:
            sock.disconnectFromServer()
            continue
        try:
            reply = _request(sock, {"type": "status"})
            if reply.get("type") == "status_info":
                out.append({"name": name, "status": reply})
        finally:
            sock.disconnectFromServer()
    return out


# -- argparse wiring -------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zeeref-cli",
        description="Interact with a running ZeeRef session.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    add_p = sub.add_parser("add", help="Send images to a session (auto-spawns)")
    add_p.add_argument("session", help="Session name")
    add_p.add_argument("files", nargs="*", help="Image files to add")
    add_p.add_argument("--title", default=None, help="Title for the image(s)")
    add_p.add_argument("--caption", default=None, help="Caption for the image(s)")
    add_p.add_argument(
        "--stdin",
        action="store_true",
        help="Read JSON payload array from stdin (each entry: {path, title?, caption?})",
    )
    add_p.set_defaults(func=_cmd_add)

    # start
    start_p = sub.add_parser("start", help="Ensure a session is running (idempotent)")
    start_p.add_argument("session", help="Session name")
    start_p.set_defaults(func=_cmd_start)

    # new
    new_p = sub.add_parser(
        "new", help="Fresh empty scene in a session (spawns if needed)"
    )
    new_p.add_argument("session", help="Session name")
    new_p.add_argument(
        "--force",
        action="store_true",
        help="Discard unsaved changes if session is dirty",
    )
    new_p.set_defaults(func=_cmd_new)

    # open
    open_p = sub.add_parser(
        "open", help="Open a .zref file in a session (spawns if needed)"
    )
    open_p.add_argument("session", help="Session name")
    open_p.add_argument("path", help="Path to .zref file")
    open_p.add_argument(
        "--force",
        action="store_true",
        help="Discard unsaved changes if session is dirty",
    )
    open_p.set_defaults(func=_cmd_open)

    # ping
    ping_p = sub.add_parser("ping", help="Liveness check (no spawn)")
    ping_p.add_argument("session", help="Session name")
    ping_p.set_defaults(func=_cmd_ping)

    # status
    status_p = sub.add_parser("status", help="Session status (no spawn)")
    status_p.add_argument("session", help="Session name")
    status_p.set_defaults(func=_cmd_status)

    # sessions
    sessions_p = sub.add_parser("sessions", help="List running sessions (no spawn)")
    sessions_p.set_defaults(func=_cmd_sessions)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
