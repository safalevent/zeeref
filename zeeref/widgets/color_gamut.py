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

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

if TYPE_CHECKING:
    from zeeref.items import ZeePixmapItem

logger = logging.getLogger(__name__)


class GamutPainterThread(QtCore.QThread):
    """Dedicated thread for drawing the gamut image."""

    finished = QtCore.pyqtSignal(QtGui.QImage)
    radius = 250

    def __init__(self, parent: GamutWidget, item: ZeePixmapItem) -> None:
        super().__init__()
        self.item = item
        self.parent = parent

    def run(self) -> None:
        logger.debug("Start drawing gamut image...")
        self.image = QtGui.QImage(
            QtCore.QSize(2 * self.radius, 2 * self.radius),
            QtGui.QImage.Format.Format_ARGB32,
        )
        self.image.fill(QtGui.QColor(0, 0, 0, 0))

        painter = QtGui.QPainter(self.image)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0)))
        painter.setPen(Qt.PenStyle.NoPen)
        center = QtCore.QPoint(self.radius, self.radius)
        painter.drawEllipse(center, self.radius, self.radius)
        logger.debug(f"Threshold: {self.parent.threshold}")

        for (hue, saturation), count in self.item.color_gamut.items():
            if count < self.parent.threshold:
                continue
            hypotenuse = saturation / 255 * self.radius
            angle = math.radians(-90 - hue)
            x = int(math.sin(angle) * hypotenuse) + center.x()
            y = int(math.cos(angle) * hypotenuse) + center.y()
            color = QtGui.QColor()
            color.setHsv(hue, saturation, 255)
            painter.setBrush(QtGui.QBrush(color))
            painter.drawEllipse(QtCore.QPoint(x, y), 3, 3)

        painter.end()
        logger.debug("Finished drawing gamut image.")
        self.finished.emit(self.image)


class GamutWidget(QtWidgets.QWidget):
    def __init__(self, parent: GamutDialog, item: ZeePixmapItem) -> None:
        super().__init__(parent)
        self.item = item
        self.image: QtGui.QImage | None = None
        self.worker = GamutPainterThread(self, item)
        self.worker.finished.connect(self.on_gamut_finished)
        self.worker.start()

    @property
    def threshold(self) -> int:
        parent = self.parent()
        assert isinstance(parent, GamutDialog)
        return parent.threshold_input.value()

    def on_gamut_finished(self, image: QtGui.QImage) -> None:
        logger.debug("Gamut image update received")
        self.image = image
        self.update()

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, 200)

    def update_values(self) -> None:
        if self.worker.isRunning():
            self.worker.quit()
            self.worker.wait()
        self.worker.start()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        if self.image:
            size = min(self.size().width(), self.size().height())
            x = max((self.size().width() - size) / 2, 0.0)
            y = max((self.size().height() - size) / 2, 0.0)
            painter.drawImage(QtCore.QRectF(x, y, float(size), float(size)), self.image)
        else:
            painter.drawText(10, 20, self.tr("Counting pixels..."))
        painter.end()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.worker.isRunning():
            self.worker.quit()
            self.worker.wait()
        super().closeEvent(event)


class GamutDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, item: ZeePixmapItem) -> None:
        super().__init__(parent)
        self.item = item
        self.setWindowTitle(self.tr("Color Gamut"))

        # The input controls on the right
        controls_layout = QtWidgets.QVBoxLayout()

        label = QtWidgets.QLabel(self.tr("Threshold:"), self)
        controls_layout.addWidget(label)
        self.threshold_input = QtWidgets.QSlider(self)
        self.threshold_input.setRange(0, 500)
        self.threshold_input.setValue(20)
        self.threshold_input.setTracking(False)
        self.threshold_input.valueChanged.connect(self.on_value_changed)
        controls_layout.addWidget(
            self.threshold_input, alignment=Qt.AlignmentFlag.AlignHCenter
        )

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(self.reject)

        controls_layout.addWidget(buttons)

        # The gamut display
        layout = QtWidgets.QHBoxLayout()
        self.setLayout(layout)
        self.gamut_widget = GamutWidget(self, item)
        layout.addWidget(self.gamut_widget, stretch=1)

        layout.addLayout(controls_layout, stretch=0)
        self.show()

    def on_value_changed(self, value: int) -> None:
        self.gamut_widget.update_values()

    def reject(self) -> None:
        if hasattr(self, "gamut_widget") and self.gamut_widget.worker.isRunning():
            self.gamut_widget.worker.quit()
            self.gamut_widget.worker.wait()
        super().reject()

    def accept(self) -> None:
        if hasattr(self, "gamut_widget") and self.gamut_widget.worker.isRunning():
            self.gamut_widget.worker.quit()
            self.gamut_widget.worker.wait()
        super().accept()

