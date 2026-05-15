import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6 import QtNetwork

from zeeref.session import (
    AddMessage,
    ErrorMessage,
    NewMessage,
    OpenMessage,
    PingMessage,
    PROTOCOL_VERSION,
    SessionServer,
    StatusInfoMessage,
    StatusRequestMessage,
    parse_message,
    server_name,
)


@pytest.fixture
def session_name(tmp_path):
    """Use a unique session name based on tmp_path to avoid collisions."""
    return f"test-{tmp_path.name}"


def _mock_async_fn():
    """Mock that invokes its trailing callback with [] immediately."""
    fn = MagicMock()

    def side_effect(*args):
        on_done = args[-1]
        on_done([])

    fn.side_effect = side_effect
    return fn


@pytest.fixture
def mock_insert_fn():
    return _mock_async_fn()


@pytest.fixture
def mock_new_fn():
    return _mock_async_fn()


@pytest.fixture
def mock_open_fn():
    return _mock_async_fn()


@pytest.fixture
def mock_status_fn():
    fn = MagicMock(
        return_value=StatusInfoMessage(loaded_file=None, item_count=0, dirty=False)
    )
    return fn


@pytest.fixture
def server(
    qtbot, session_name, mock_insert_fn, mock_new_fn, mock_open_fn, mock_status_fn
):
    srv = SessionServer(
        session_name,
        mock_insert_fn,
        mock_new_fn,
        mock_open_fn,
        mock_status_fn,
    )
    assert srv.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def imgfile(tmp_path):
    """Create a small test image file."""
    p = tmp_path / "test.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return p


def make_msg(msg: dict) -> str:
    """Serialize a message dict to a protocol line."""
    return json.dumps(msg) + "\n"


def add_msg(items: list[dict]) -> str:
    """Build an add message line."""
    return make_msg({"type": "add", "payload": items})


class AsyncClient:
    """Sends a message to a session socket in a background thread.

    Consumes the server's ``hello`` greeting (stored as :attr:`hello`)
    before reading the actual reply to *message*.
    """

    def __init__(self, session_name: str, message: str):
        self.reply: dict | None = None
        self.hello: dict | None = None
        self._thread = threading.Thread(target=self._run, args=(session_name, message))
        self._thread.start()

    @staticmethod
    def _read_line(sock: QtNetwork.QLocalSocket) -> bytes:
        buf = b""
        while b"\n" not in buf:
            if not sock.waitForReadyRead(5000):
                break
            buf += bytes(sock.readAll())
        line, _, _ = buf.partition(b"\n")
        return line

    def _run(self, session_name: str, message: str):
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(server_name(session_name))
        if not sock.waitForConnected(5000):
            self.reply = {}
            return
        hello_line = self._read_line(sock)
        self.hello = json.loads(hello_line.decode()) if hello_line else {}
        sock.write(message.encode())
        sock.waitForBytesWritten(5000)
        reply_line = self._read_line(sock)
        sock.disconnectFromServer()
        self.reply = json.loads(reply_line.decode()) if reply_line else {}

    @property
    def done(self) -> bool:
        return self.reply is not None

    def join(self):
        self._thread.join(timeout=2)


# -- parse_message unit tests -----------------------------------------------


def test_parse_ping():
    result = parse_message('{"type": "ping"}')
    assert isinstance(result, PingMessage)


def test_parse_add(imgfile):
    result = parse_message(
        json.dumps(
            {
                "type": "add",
                "payload": [{"path": str(imgfile), "title": "t", "caption": "c"}],
            }
        )
    )
    assert isinstance(result, AddMessage)
    assert len(result.images) == 1
    assert result.images[0].title == "t"
    assert result.images[0].caption == "c"


def test_parse_invalid_json():
    result = parse_message("not json")
    assert isinstance(result, ErrorMessage)


def test_parse_missing_type():
    result = parse_message('{"payload": []}')
    assert isinstance(result, ErrorMessage)


def test_parse_unknown_type():
    result = parse_message('{"type": "explode"}')
    assert isinstance(result, ErrorMessage)


def test_parse_add_missing_path():
    result = parse_message(json.dumps({"type": "add", "payload": [{"title": "x"}]}))
    assert isinstance(result, ErrorMessage)


def test_parse_add_invalid_title_type(imgfile):
    result = parse_message(
        json.dumps({"type": "add", "payload": [{"path": str(imgfile), "title": 123}]})
    )
    assert isinstance(result, ErrorMessage)


# -- Server integration tests ------------------------------------------------


def _can_connect(session: str, timeout_ms: int = 500) -> bool:
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(server_name(session))
    ok = sock.waitForConnected(timeout_ms)
    sock.abort()
    return ok


def test_server_accepts_connections(server, session_name):
    assert _can_connect(session_name)


def _make_server(session_name, insert_fn):
    """Construct a SessionServer with no-op new/open/status mocks."""
    return SessionServer(
        session_name,
        insert_fn,
        _mock_async_fn(),
        _mock_async_fn(),
        MagicMock(return_value=StatusInfoMessage()),
    )


def test_server_shutdown_rejects_new_connections(qtbot, session_name, mock_insert_fn):
    srv = _make_server(session_name, mock_insert_fn)
    assert srv.start()
    assert _can_connect(session_name)
    srv.shutdown()
    assert not _can_connect(session_name)


def test_ping(qtbot, server, session_name):
    c = AsyncClient(session_name, make_msg({"type": "ping"}))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "pong"


def test_add_single_image(qtbot, server, session_name, mock_insert_fn, imgfile):
    c = AsyncClient(session_name, add_msg([{"path": str(imgfile)}]))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    mock_insert_fn.assert_called_once()
    inserts = mock_insert_fn.call_args[0][0]
    assert len(inserts) == 1
    assert inserts[0].path == str(imgfile)


def test_add_with_title_and_caption(
    qtbot, server, session_name, mock_insert_fn, imgfile
):
    c = AsyncClient(
        session_name,
        add_msg([{"path": str(imgfile), "title": "10x", "caption": "Chip 2"}]),
    )
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    inserts = mock_insert_fn.call_args[0][0]
    assert inserts[0].title == "10x"
    assert inserts[0].caption == "Chip 2"


def test_add_multiple_images(qtbot, server, session_name, mock_insert_fn, tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        files.append(p)

    c = AsyncClient(session_name, add_msg([{"path": str(f)} for f in files]))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    inserts = mock_insert_fn.call_args[0][0]
    assert [ins.path for ins in inserts] == [str(f) for f in files]


def test_add_nonexistent_file(qtbot, server, session_name, mock_insert_fn):
    c = AsyncClient(session_name, add_msg([{"path": "/nonexistent/file.png"}]))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "error"
    mock_insert_fn.assert_not_called()


def test_add_skips_nonexistent_keeps_valid(
    qtbot, server, session_name, mock_insert_fn, imgfile
):
    c = AsyncClient(
        session_name,
        add_msg([{"path": "/nonexistent/file.png"}, {"path": str(imgfile)}]),
    )
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    inserts = mock_insert_fn.call_args[0][0]
    assert [ins.path for ins in inserts] == [str(imgfile)]


def test_unknown_command(qtbot, server, session_name):
    c = AsyncClient(session_name, make_msg({"type": "explode"}))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "error"


def test_add_reports_insert_errors(qtbot, session_name, imgfile):
    """When the insert callback reports errors, the reply is error."""

    def insert_with_errors(inserts, on_done):
        on_done(["bad_file.png"])

    fn = MagicMock(side_effect=insert_with_errors)
    srv = _make_server(session_name, fn)
    assert srv.start()

    c = AsyncClient(session_name, add_msg([{"path": str(imgfile)}]))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "error"
    srv.shutdown()


def test_blocking_queues_requests(qtbot, session_name, imgfile):
    """Second add blocks until the first insert completes."""
    finish_callbacks: list = []

    def slow_insert(inserts, on_done):
        finish_callbacks.append(on_done)

    fn = MagicMock(side_effect=slow_insert)
    srv = _make_server(session_name, fn)
    assert srv.start()

    msg = add_msg([{"path": str(imgfile)}])

    c1 = AsyncClient(session_name, msg)
    qtbot.waitUntil(lambda: len(finish_callbacks) == 1, timeout=3000)

    c2 = AsyncClient(session_name, msg)
    qtbot.waitUntil(lambda: len(srv._queue) >= 1, timeout=3000)
    assert fn.call_count == 1

    finish_callbacks[0]([])
    qtbot.waitUntil(lambda: fn.call_count == 2, timeout=3000)

    qtbot.waitUntil(lambda: c1.done, timeout=3000)
    assert c1.reply["type"] == "ok"

    finish_callbacks[1]([])
    qtbot.waitUntil(lambda: c2.done, timeout=3000)
    assert c2.reply["type"] == "ok"

    srv.shutdown()


# -- parse_message: new/open/status -----------------------------------------


def test_parse_status_request():
    result = parse_message('{"type": "status"}')
    assert isinstance(result, StatusRequestMessage)


def test_parse_new_default_force():
    result = parse_message('{"type": "new"}')
    assert isinstance(result, NewMessage)
    assert result.force is False


def test_parse_new_with_force():
    result = parse_message('{"type": "new", "force": true}')
    assert isinstance(result, NewMessage)
    assert result.force is True


def test_parse_new_rejects_non_bool_force():
    result = parse_message('{"type": "new", "force": "yes"}')
    assert isinstance(result, ErrorMessage)


def test_parse_open_requires_path():
    result = parse_message('{"type": "open"}')
    assert isinstance(result, ErrorMessage)


def test_parse_open_with_path():
    result = parse_message('{"type": "open", "path": "/tmp/a.zref"}')
    assert isinstance(result, OpenMessage)
    assert result.path == "/tmp/a.zref"
    assert result.force is False


def test_parse_open_with_force():
    result = parse_message('{"type": "open", "path": "/tmp/a.zref", "force": true}')
    assert isinstance(result, OpenMessage)
    assert result.force is True


# -- Integration: hello, status, new, open ---------------------------------


def test_hello_sent_on_connect(qtbot, server, session_name):
    """Server pushes hello with protocol_version + app_version on connect."""
    c = AsyncClient(session_name, make_msg({"type": "ping"}))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.hello is not None
    assert c.hello["type"] == "hello"
    assert c.hello["protocol_version"] == PROTOCOL_VERSION
    assert isinstance(c.hello.get("app_version"), str)


def test_status_returns_status_info(qtbot, server, session_name, mock_status_fn):
    mock_status_fn.return_value = StatusInfoMessage(
        loaded_file="/tmp/x.zref", item_count=7, dirty=True
    )
    c = AsyncClient(session_name, make_msg({"type": "status"}))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "status_info"
    assert c.reply["loaded_file"] == "/tmp/x.zref"
    assert c.reply["item_count"] == 7
    assert c.reply["dirty"] is True
    mock_status_fn.assert_called_once()


def test_new_calls_new_fn(qtbot, server, session_name, mock_new_fn):
    c = AsyncClient(session_name, make_msg({"type": "new", "force": True}))
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    mock_new_fn.assert_called_once()
    assert mock_new_fn.call_args[0][0] is True  # force


def test_new_reports_errors_from_callback(qtbot, session_name):
    """When new_fn reports errors (e.g., dirty), reply is error."""
    new_fn = MagicMock()
    new_fn.side_effect = lambda force, on_done: on_done(
        ["session has unsaved changes; pass force=true to discard"]
    )
    srv = SessionServer(
        session_name,
        _mock_async_fn(),
        new_fn,
        _mock_async_fn(),
        MagicMock(return_value=StatusInfoMessage()),
    )
    assert srv.start()
    try:
        c = AsyncClient(session_name, make_msg({"type": "new"}))
        qtbot.waitUntil(lambda: c.done, timeout=3000)
        assert c.reply["type"] == "error"
        assert "unsaved" in c.reply["message"]
    finally:
        srv.shutdown()


def test_open_calls_open_fn(qtbot, server, session_name, mock_open_fn, tmp_path):
    target = tmp_path / "x.zref"
    target.write_bytes(b"stub")
    c = AsyncClient(
        session_name,
        make_msg({"type": "open", "path": str(target), "force": True}),
    )
    qtbot.waitUntil(lambda: c.done, timeout=3000)
    assert c.reply["type"] == "ok"
    mock_open_fn.assert_called_once()
    called_path, called_force, _ = mock_open_fn.call_args[0]
    assert called_path == Path(str(target))
    assert called_force is True


def test_open_reports_errors_from_callback(qtbot, session_name):
    """When open_fn reports errors, reply is error."""
    open_fn = MagicMock()
    open_fn.side_effect = lambda path, force, on_done: on_done(
        ["file not found: /missing/x.zref"]
    )
    srv = SessionServer(
        session_name,
        _mock_async_fn(),
        _mock_async_fn(),
        open_fn,
        MagicMock(return_value=StatusInfoMessage()),
    )
    assert srv.start()
    try:
        c = AsyncClient(
            session_name,
            make_msg({"type": "open", "path": "/missing/x.zref"}),
        )
        qtbot.waitUntil(lambda: c.done, timeout=3000)
        assert c.reply["type"] == "error"
    finally:
        srv.shutdown()


def test_writes_serialize_through_queue(qtbot, session_name, imgfile):
    """add → new → open arriving back-to-back run one at a time."""
    add_callbacks: list = []
    new_callbacks: list = []
    open_callbacks: list = []

    def slow_add(inserts, on_done):
        add_callbacks.append(on_done)

    def slow_new(force, on_done):
        new_callbacks.append(on_done)

    def slow_open(path, force, on_done):
        open_callbacks.append(on_done)

    srv = SessionServer(
        session_name,
        MagicMock(side_effect=slow_add),
        MagicMock(side_effect=slow_new),
        MagicMock(side_effect=slow_open),
        MagicMock(return_value=StatusInfoMessage()),
    )
    assert srv.start()
    try:
        c1 = AsyncClient(session_name, add_msg([{"path": str(imgfile)}]))
        qtbot.waitUntil(lambda: len(add_callbacks) == 1, timeout=3000)

        # While add is in flight, new and open should queue, not fire.
        c2 = AsyncClient(session_name, make_msg({"type": "new", "force": True}))
        c3 = AsyncClient(
            session_name,
            make_msg({"type": "open", "path": str(imgfile), "force": True}),
        )
        qtbot.waitUntil(lambda: len(srv._queue) >= 2, timeout=3000)
        assert len(new_callbacks) == 0
        assert len(open_callbacks) == 0

        # Drain in order.
        add_callbacks[0]([])
        qtbot.waitUntil(lambda: len(new_callbacks) == 1, timeout=3000)
        new_callbacks[0]([])
        qtbot.waitUntil(lambda: len(open_callbacks) == 1, timeout=3000)
        open_callbacks[0]([])

        qtbot.waitUntil(lambda: c1.done and c2.done and c3.done, timeout=3000)
        assert c1.reply["type"] == "ok"
        assert c2.reply["type"] == "ok"
        assert c3.reply["type"] == "ok"
    finally:
        srv.shutdown()
