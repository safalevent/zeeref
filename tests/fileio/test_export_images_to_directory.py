import os
import stat
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from PyQt6 import QtGui

from zeeref.items import ZeePixmapItem
from zeeref.fileio.errors import ZeeFileIOError
from zeeref.fileio.export import ImagesToDirectoryExporter
from zeeref.types.snapshot import IOResult


def test_images_to_directory_exporter_export_writes_images(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    view.scene.addItem(item1)
    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)
    
    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export()

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "rb") as f:
        assert f.read().startswith(b"\x89PNG")


def test_images_to_directory_exporter_export_file_exists_no_user_input(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)
    
    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")

    (tmp_path / filename2).write_text("foo")

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export()

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "r") as f:
        assert f.read() == "foo"

    assert exporter.start_from == 1


def test_images_to_directory_exporter_export_file_exists_skip(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    item3 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item3.filename = "image3.png"
    item3.save_id = "00000003" + "a" * 24
    view.scene.addItem(item3)

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")
    filename3 = item3.get_filename_for_export("png")

    (tmp_path / filename2).write_text("foo")
    (tmp_path / filename3).write_text("bar")

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "skip"
    exporter.export()

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "r") as f:
        assert f.read() == "foo"
    with open(tmp_path / filename3, "r") as f:
        assert f.read() == "bar"

    assert exporter.start_from == 2
    assert exporter.handle_existing is None


def test_images_to_directory_exporter_export_file_exists_skip_all(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    item3 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item3.filename = "image3.png"
    item3.save_id = "00000003" + "a" * 24
    view.scene.addItem(item3)

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")
    filename3 = item3.get_filename_for_export("png")

    (tmp_path / filename2).write_text("foo")
    (tmp_path / filename3).write_text("bar")

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "skip_all"
    exporter.export()

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "r") as f:
        assert f.read() == "foo"
    with open(tmp_path / filename3, "r") as f:
        assert f.read() == "bar"

    assert exporter.handle_existing == "skip_all"


def test_images_to_directory_exporter_export_file_exists_overwrite(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    item3 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item3.filename = "image3.png"
    item3.save_id = "00000003" + "a" * 24
    view.scene.addItem(item3)

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")
    filename3 = item3.get_filename_for_export("png")

    (tmp_path / filename2).write_text("foo")
    (tmp_path / filename3).write_text("bar")

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "overwrite"
    exporter.export()

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename3, "r") as f:
        assert f.read() == "bar"

    assert exporter.start_from == 2
    assert exporter.handle_existing is None


def test_images_to_directory_exporter_export_file_exists_overwrite_all(
    view, tmp_path, imgfilename3x3
):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    item3 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item3.filename = "image3.png"
    item3.save_id = "00000003" + "a" * 24
    view.scene.addItem(item3)

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")
    filename3 = item3.get_filename_for_export("png")

    (tmp_path / filename2).write_text("foo")
    (tmp_path / filename3).write_text("bar")

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "overwrite_all"
    exporter.export()

    with open(tmp_path / filename1, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename2, "rb") as f:
        assert f.read().startswith(b"\x89PNG")
    with open(tmp_path / filename3, "rb") as f:
        assert f.read().startswith(b"\x89PNG")

    assert exporter.handle_existing == "overwrite_all"


def test_images_to_directory_exporter_export_with_worker(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    view.scene.addItem(item)
    worker = MagicMock(canceled=False)
    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export(worker)

    filename = item.get_filename_for_export("png")
    with open(tmp_path / filename, "rb") as f:
        assert f.read().startswith(b"\x89PNG")

    worker.begin_processing.emit.assert_called_once_with(1)
    worker.progress.emit.assert_called_with(0)
    worker.finished.emit.assert_called_once_with(
        IOResult(filename=tmp_path, errors=[])
    )


def test_images_to_directory_exporter_export_with_worker_when_canceled(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    view.scene.addItem(item)
    worker = MagicMock(canceled=True)
    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export(worker)

    filename = item.get_filename_for_export("png")
    assert (tmp_path / filename).exists() is False

    worker.begin_processing.emit.assert_called_once_with(1)
    worker.progress.emit.assert_called_once_with(0)
    worker.finished.emit.assert_called_once_with(
        IOResult(filename=tmp_path, errors=[])
    )


def test_images_to_directory_exporter_export_with_worker_when_file_exists(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    item.save_id = "00000001" + "a" * 24
    view.scene.addItem(item)

    filename = item.get_filename_for_export("png")
    (tmp_path / filename).write_text("foo")

    worker = MagicMock(canceled=False)
    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export(worker)

    with open(tmp_path / filename, "r") as f:
        assert f.read() == "foo"

    worker.begin_processing.emit.assert_called_once_with(1)
    worker.progress.emit.assert_called_with(0)
    worker.user_input_required.emit.assert_called_once_with(str(tmp_path / filename))


@pytest.mark.skipif(os.name == "nt", reason="chmod on directories is ignored on Windows")
def test_images_to_directory_exporter_export_when_dir_not_writeable(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    view.scene.addItem(item)

    target_dir = tmp_path / "readonly"
    target_dir.mkdir()
    os.chmod(target_dir, stat.S_IREAD)
    
    exporter = ImagesToDirectoryExporter(view.scene, target_dir)

    with pytest.raises(ZeeFileIOError) as e:
        exporter.export()
    assert e.value.filename == target_dir

    os.chmod(target_dir, stat.S_IWRITE)


@pytest.mark.skipif(os.name == "nt", reason="chmod on directories is ignored on Windows")
def test_images_to_directory_exporter_export_when_dir_not_writeable_w_worker(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    view.scene.addItem(item)

    target_dir = tmp_path / "readonly_worker"
    target_dir.mkdir()
    os.chmod(target_dir, stat.S_IREAD)

    exporter = ImagesToDirectoryExporter(view.scene, target_dir)
    worker = MagicMock(canceled=False)

    exporter.export(worker)
    worker.begin_processing.emit.assert_called_once_with(1)
    worker.finished.emit.assert_called_once()
    args = worker.finished.emit.call_args.args[0]
    assert args.filename == target_dir
    assert len(args.errors) == 1

    os.chmod(target_dir, stat.S_IWRITE)


def test_images_to_directory_exporter_export_when_img_not_writeable(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    item.save_id = "00000001" + "a" * 24
    view.scene.addItem(item)

    filename = item.get_filename_for_export("png")
    (tmp_path / filename).write_text("foo")
    os.chmod(tmp_path / filename, stat.S_IREAD)

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "overwrite_all"

    with pytest.raises(ZeeFileIOError) as e:
        exporter.export()
    assert e.value.filename == tmp_path / filename
    
    os.chmod(tmp_path / filename, stat.S_IWRITE)


def test_images_to_directory_exporter_export_when_img_not_writeable_w_worker(
    view, tmp_path, imgfilename3x3
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item.filename = "image1.png"
    item.save_id = "00000001" + "a" * 24
    view.scene.addItem(item)

    filename = item.get_filename_for_export("png")
    (tmp_path / filename).write_text("foo")
    os.chmod(tmp_path / filename, stat.S_IREAD)

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.handle_existing = "overwrite_all"
    worker = MagicMock(canceled=False)

    exporter.export(worker)
    worker.begin_processing.emit.assert_called_once_with(1)
    worker.finished.emit.assert_called_once()
    args = worker.finished.emit.call_args.args[0]
    assert args.filename == tmp_path / filename
    assert len(args.errors) == 1

    os.chmod(tmp_path / filename, stat.S_IWRITE)


def test_images_to_directory_exporter_nameless_images(view, tmp_path, imgfilename3x3):
    import datetime
    date_str = datetime.date.today().strftime("%Y-%m-%d")

    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = None
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "has_name.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    item3 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item3.filename = None
    item3.save_id = "00000003" + "a" * 24
    view.scene.addItem(item3)

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export()

    assert (tmp_path / f"{date_str}-1.png").exists()
    assert (tmp_path / "00000002-has_name.png").exists()
    assert (tmp_path / f"{date_str}-2.png").exists()


def test_images_to_directory_exporter_selected_only(view, tmp_path, imgfilename3x3):
    item1 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item1.filename = "image1.png"
    item1.save_id = "00000001" + "a" * 24
    view.scene.addItem(item1)

    item2 = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    item2.filename = "image2.png"
    item2.save_id = "00000002" + "a" * 24
    view.scene.addItem(item2)

    # Select only item1
    item1.setSelected(True)
    item2.setSelected(False)

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export()

    filename1 = item1.get_filename_for_export("png")
    filename2 = item2.get_filename_for_export("png")

    assert (tmp_path / filename1).exists()
    assert not (tmp_path / filename2).exists()


def test_images_to_directory_exporter_gif(view, tmp_path):
    img = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    img.fill(QtGui.QColor(0, 0, 0, 0))
    item = ZeePixmapItem(img)
    item.filename = "anim.gif"
    item.save_id = "00000001" + "a" * 24
    item._gif_bytes = b"fake-gif-bytes"
    view.scene.addItem(item)

    from zeeref.fileio.sql import SQLiteIO
    io = SQLiteIO(view.scene._scratch_file)
    io.ex(
        "INSERT OR IGNORE INTO images (id, width, height, format) "
        "VALUES (?, ?, ?, ?)",
        (item.image_id, 10, 10, "gif"),
    )
    io.connection.commit()
    io._close_connection()

    exporter = ImagesToDirectoryExporter(view.scene, tmp_path)
    exporter.export()

    filename = item.get_filename_for_export("gif")
    assert filename == "00000001-anim.gif"
    assert (tmp_path / filename).exists()
    assert (tmp_path / filename).read_bytes() == b"fake-gif-bytes"
