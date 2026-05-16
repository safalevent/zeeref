import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6 import QtCore, QtGui

from zeeref import fileio
from zeeref.fileio.scratch import create_scratch_file
from zeeref.types.snapshot import PixmapItemSnapshot
from zeeref.items import ZeePixmapItem
from ..utils import queue2list


def test_save_zref_via_swp(scene, imgfilename3x3):
    from zeeref.fileio.scratch import create_scratch_file

    scene._scratch_file = create_scratch_file(None)
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    scene.addItem(item)
    snapshots = scene.snapshot_for_save()
    swp_path = scene._scratch_file
    assert swp_path is not None
    with tempfile.TemporaryDirectory() as dirname:
        fname = Path(dirname) / "test.zref"
        fileio.save_zref(fname, snapshots, swp_path)
        assert fname.exists()


@patch("zeeref.fileio.sql.SQLiteIO.read")
def test_load_zref(read_mock):
    with tempfile.TemporaryDirectory() as dirname:
        fname = Path(dirname) / "test.zref"
        fname.touch()
        fileio.load_zref(fname, MagicMock())
        read_mock.assert_called_once()


def _assert_finished_result(worker, *, errors, created_count):
    """Check the emitted IOResult's filename/errors and created_ids length."""
    worker.finished.emit.assert_called_once()
    result = worker.finished.emit.call_args[0][0]
    assert result.filename is None
    assert result.errors == errors
    assert len(result.created_ids) == created_count


def test_load_images_loads(scene, imgfilename3x3):
    scene._scratch_file = create_scratch_file(None)
    worker = MagicMock(canceled=False)
    fileio.insert_image_files([imgfilename3x3], QtCore.QPointF(5, 6), scene, worker)
    worker.begin_processing.emit.assert_called_once_with(1)
    worker.progress.emit.assert_called_once_with(0)
    _assert_finished_result(worker, errors=[], created_count=1)
    itemdata = queue2list(scene.items_to_add)
    assert len(itemdata) == 1
    snap, selected = itemdata[0]
    assert isinstance(snap, PixmapItemSnapshot)
    assert selected is True
    assert snap.x == 5 - snap.width / 2
    assert snap.y == 6 - snap.height / 2


def test_load_images_canceled(scene, imgfilename3x3):
    scene._scratch_file = create_scratch_file(None)
    worker = MagicMock(canceled=True)
    fileio.insert_image_files(
        [imgfilename3x3, imgfilename3x3], QtCore.QPointF(5, 6), scene, worker
    )
    worker.begin_processing.emit.assert_called_once_with(2)
    worker.progress.emit.assert_called_once_with(0)
    _assert_finished_result(worker, errors=[], created_count=1)
    itemdata = queue2list(scene.items_to_add)
    assert len(itemdata) == 1
    snap, selected = itemdata[0]
    assert isinstance(snap, PixmapItemSnapshot)
    assert selected is True
    assert snap.x == 5 - snap.width / 2
    assert snap.y == 6 - snap.height / 2


def test_load_images_error(scene, imgfilename3x3):
    scene._scratch_file = create_scratch_file(None)
    worker = MagicMock(canceled=False)
    fileio.insert_image_files(
        ["foo.jpg", imgfilename3x3], QtCore.QPointF(5, 6), scene, worker
    )
    worker.begin_processing.emit.assert_called_once_with(2)
    worker.progress.emit.assert_any_call(0)
    worker.progress.emit.assert_any_call(1)
    _assert_finished_result(worker, errors=["foo.jpg"], created_count=1)
    itemdata = queue2list(scene.items_to_add)
    assert len(itemdata) == 1
    snap, selected = itemdata[0]
    assert isinstance(snap, PixmapItemSnapshot)
    assert selected is True
    assert snap.x == 5 - snap.width / 2
    assert snap.y == 6 - snap.height / 2
