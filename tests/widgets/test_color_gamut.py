from unittest.mock import MagicMock

from PyQt6 import QtGui

from zeeref.items import ZeePixmapItem
from zeeref.widgets.color_gamut import (
    GamutDialog,
    GamutPainterThread,
    GamutWidget,
)


def test_gamut_painter_thread_generates_image(view, imgfilename3x3, qtbot):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    view.scene.addItem(item)
    dialog = GamutDialog(view, item)
    qtbot.addWidget(dialog)
    dialog.threshold_input.setValue(0)
    
    qtbot.waitUntil(lambda: dialog.gamut_widget.image is not None, timeout=5000)
    image = dialog.gamut_widget.image
    assert image.size().width() == 500
    assert image.size().height() == 500
    assert image.allGray() is False
    
    # Explicit cleanup
    dialog.gamut_widget.worker.quit()
    dialog.gamut_widget.worker.wait()
    dialog.accept()
    view.clear_scene()


def test_gamut_painter_thread_generates_image_below_threshold(
    view, imgfilename3x3, qtbot
):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    view.scene.addItem(item)
    dialog = GamutDialog(view, item)
    qtbot.addWidget(dialog)
    dialog.threshold_input.setValue(20)
    
    qtbot.waitUntil(lambda: dialog.gamut_widget.image is not None, timeout=5000)
    image = dialog.gamut_widget.image
    assert image.size().width() == 500
    assert image.size().height() == 500
    assert image.allGray() is True
    
    # Explicit cleanup
    dialog.gamut_widget.worker.quit()
    dialog.gamut_widget.worker.wait()
    dialog.accept()
    view.clear_scene()


def test_gamut_widget_generates_image(view, imgfilename3x3, qtbot):
    item = ZeePixmapItem(QtGui.QImage(imgfilename3x3))
    view.scene.addItem(item)
    dialog = GamutDialog(view, item)
    qtbot.addWidget(dialog)
    dialog.threshold_input.setValue(0)
    widget = dialog.gamut_widget
    
    qtbot.waitUntil(lambda: widget.image is not None, timeout=5000)
    assert widget.image.size().width() == 500
    assert widget.image.size().height() == 500
    assert widget.image.allGray() is False
    
    # Explicit cleanup
    dialog.gamut_widget.worker.quit()
    dialog.gamut_widget.worker.wait()
    dialog.accept()
    view.clear_scene()
