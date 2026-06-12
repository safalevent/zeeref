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

import base64
import io as python_io
import math
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from PIL import Image, ImageSequence

from PyQt6 import QtCore, QtGui, QtWidgets

from zeeref.config import ZeeSettings
from .errors import ZeeFileIOError
from zeeref.types.snapshot import IOResult
from zeeref import widgets
from zeeref.logging import getLogger
from zeeref.fileio.sql import SQLiteIO
from zeeref.fileio.tilecache import get_tile_cache
from zeeref.fileio.tiling import TILE_SIZE
from zeeref.types.tile import TileKey

if TYPE_CHECKING:
    from zeeref.fileio.thread import ThreadedIO
    from zeeref.scene import ZeeGraphicsScene
    from zeeref.items import ZeePixmapItem, ZeeTextItem


logger = getLogger(__name__)


class ExporterBase:
    def emit_begin_processing(self, worker: ThreadedIO | None, start: int) -> None:
        if worker:
            worker.begin_processing.emit(start)

    def emit_progress(self, worker: ThreadedIO | None, progress: int) -> None:
        if worker:
            worker.progress.emit(progress)

    def emit_finished(
        self, worker: ThreadedIO | None, filename: Path, errors: list[str]
    ) -> None:

        if worker:
            worker.finished.emit(IOResult(filename=filename, errors=errors))

    def emit_user_input_required(self, worker: ThreadedIO | None, msg: str) -> None:
        if worker:
            worker.user_input_required.emit(msg)

    def handle_export_error(
        self, filename: Path, error: Exception | str, worker: ThreadedIO | None
    ) -> None:

        logger.debug(f"Export failed: {error}")
        if worker:
            worker.finished.emit(IOResult(filename=filename, errors=[str(error)]))
            return
        else:
            e = error if isinstance(error, Exception) else None
            raise ZeeFileIOError(msg=str(error), filename=filename) from e


class SceneExporterBase(ExporterBase):
    """For exporting the scene to a single image."""

    def get_user_input(self, parent: QtWidgets.QWidget) -> bool:
        """Ask user for export parameters. Override in subclasses."""
        raise NotImplementedError

    def export(self, filename: Path, worker: ThreadedIO | None = None) -> None:
        """Export the scene. Override in subclasses."""
        raise NotImplementedError

    def __init__(self, scene: ZeeGraphicsScene) -> None:
        self.scene: ZeeGraphicsScene = scene
        self.scene.cancel_active_modes()
        self.scene.deselect_all_items()
        # Selection outlines/handles will be rendered to the exported
        # image, so deselect first. (Alternatively, pass an attribute
        # to paint functions to not paint them?)
        rect = self.scene.itemsBoundingRect()
        logger.trace(f"Items bounding rect: {rect}")
        size = QtCore.QSize(int(rect.width()), int(rect.height()))
        logger.trace(f"Export size without margins: {size}")
        self.margin: float = max(size.width(), size.height()) * 0.03
        self.default_size: QtCore.QSize = size.grownBy(
            QtCore.QMargins(*([int(self.margin)] * 4))
        )
        logger.debug(f"Default export margin: {self.margin}")
        logger.debug(f"Default export size with margins: {self.default_size}")


class ExporterRegistry(dict[str | int, type[SceneExporterBase]]):
    DEFAULT_TYPE = 0

    def __getitem__(self, key: str | int) -> type[SceneExporterBase]:
        if isinstance(key, str):
            key = key.removeprefix(".")
        exp = self.get(key, super().__getitem__(self.DEFAULT_TYPE))
        logger.debug(f"Exporter for type {key}: {exp}")
        return exp


exporter_registry = ExporterRegistry()


def register_exporter[T: type[SceneExporterBase]](cls: T) -> T:
    exporter_registry[cls.TYPE] = cls
    return cls


@register_exporter
class SceneToPixmapExporter(SceneExporterBase):
    TYPE = ExporterRegistry.DEFAULT_TYPE

    def get_user_input(self, parent: QtWidgets.QWidget) -> bool:
        """Ask user for final export size."""

        dialog = widgets.SceneToPixmapExporterDialog(
            parent=parent,
            default_size=self.default_size,
        )
        if dialog.exec():
            size = dialog.value()
            logger.debug(f"Got export size {size}")
            self.size = size
            return True
        else:
            return False

    def render_to_image(self) -> QtGui.QImage:
        logger.debug(f"Final export size: {self.size}")
        margin = self.margin * self.size.width() / self.default_size.width()
        logger.debug(f"Final export margin: {margin}")

        image = QtGui.QImage(self.size, QtGui.QImage.Format.Format_RGB32)
        canvas_color = ZeeSettings().valueOrDefault("View/canvas_color")
        image.fill(QtGui.QColor(canvas_color))
        painter = QtGui.QPainter(image)
        target_rect = QtCore.QRectF(
            margin,
            margin,
            self.size.width() - 2 * margin,
            self.size.height() - 2 * margin,
        )
        logger.trace(f"Final export target_rect: {target_rect}")
        self.scene.render(
            painter, source=self.scene.itemsBoundingRect(), target=target_rect
        )
        painter.end()
        return image

    def export(self, filename: Path, worker: ThreadedIO | None = None) -> None:
        logger.debug(f"Exporting scene to {filename}")
        self.emit_begin_processing(worker, 1)
        image = self.render_to_image()

        if worker and worker.canceled:
            logger.debug("Export canceled")
            self.emit_finished(worker, filename, [])
            return

        if not image.save(str(filename), quality=90):
            self.handle_export_error(filename, "Error writing file", worker)
            return

        logger.debug("Export finished")
        self.emit_progress(worker, 1)
        self.emit_finished(worker, filename, [])


@register_exporter
class SceneToSVGExporter(SceneExporterBase):
    TYPE = "svg"

    def get_user_input(self, parent: QtWidgets.QWidget) -> bool:
        self.size = self.default_size
        return True

    def _get_textstyles(self, item: ZeeTextItem) -> list[str]:
        fontstylemap = {
            QtGui.QFont.Style.StyleNormal: "normal",
            QtGui.QFont.Style.StyleItalic: "italic",
            QtGui.QFont.Style.StyleOblique: "oblique",
        }

        font = item.font()
        fontsize = font.pointSize() * item.scale()
        families = ", ".join(font.families()) if font.families() else font.family()
        fontstyle = fontstylemap.get(font.style(), "normal")

        return [
            "white-space:pre",
            f"font-size:{fontsize}pt",
            f"font-family:{families}",
            f"font-weight:{font.weight()}",
            f"font-stretch:{font.stretch()}",
            f"font-style:{fontstyle}",
        ]

    def render_to_svg(self, worker: ThreadedIO | None = None) -> ET.Element | None:
        svg = ET.Element(
            "svg",
            attrib={
                "width": str(self.size.width()),
                "height": str(self.size.height()),
                "xmlns": "http://www.w3.org/2000/svg",
                "xmlns:xlink": "http://www.w3.org/1999/xlink",
            },
        )

        rect = self.scene.itemsBoundingRect()
        offset = rect.topLeft() - QtCore.QPointF(self.margin, self.margin)

        for i, item in enumerate(sorted(self.scene.items(), key=lambda x: x.zValue())):
            if not hasattr(item, "TYPE"):
                continue

            pos = item.pos() - offset
            anchor = pos

            if item.TYPE == "text":
                styles = self._get_textstyles(item)
                element = ET.Element(
                    "text",
                    attrib={"style": ";".join(styles), "dominant-baseline": "hanging"},
                )
                element.text = item.toPlainText().strip()

            elif item.TYPE == "pixmap":
                width = item.crop.width() * item.scale()
                height = item.crop.height() * item.scale()

                # Retrieve format and read/stitch bytes
                if item._is_gif:
                    if item._gif_bytes:
                        pixmap_bytes = item._gif_bytes
                    else:
                        assert self.scene._scratch_file is not None
                        io = SQLiteIO(self.scene._scratch_file, readonly=True)
                        try:
                            row_tile = io.fetchone(
                                "SELECT data FROM tiles WHERE image_id=? AND level=0 AND col=0 AND row=0",
                                (item.image_id,),
                            )
                            pixmap_bytes = bytes(row_tile[0]) if row_tile else b""
                        finally:
                            io._close_connection()
                    imgformat = "gif"

                    # Crop GIF frames if cropped
                    if item.crop.width() < item._image_width or item.crop.height() < item._image_height:
                        try:
                            im = Image.open(python_io.BytesIO(pixmap_bytes))
                            durations = []
                            frames = []
                            for frame in ImageSequence.Iterator(im):
                                durations.append(frame.info.get("duration", 100))
                                frames.append(frame.crop((
                                    int(item.crop.left()),
                                    int(item.crop.top()),
                                    int(item.crop.right()),
                                    int(item.crop.bottom())
                                )))
                            out = python_io.BytesIO()
                            if len(frames) == 1:
                                frames[0].save(out, format="GIF")
                            else:
                                frames[0].save(
                                    out,
                                    format="GIF",
                                    save_all=True,
                                    append_images=frames[1:],
                                    duration=durations,
                                    loop=0
                                )
                            pixmap_bytes = out.getvalue()
                        except Exception as e:
                            logger.exception(f"Failed to crop GIF frames: {e}")
                else:
                    if not item.pixmap().isNull():
                        pm = item.pixmap()
                        pm_cropped = pm.copy(item.crop.toRect())
                        img = pm_cropped.toImage()
                        imgformat = item.get_imgformat(img)
                        barray = QtCore.QByteArray()
                        buffer = QtCore.QBuffer(barray)
                        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
                        img.save(buffer, imgformat.upper(), quality=90)
                        pixmap_bytes = barray.data()
                    else:
                        # Stitch tiled image
                        img_w = item._image_width
                        img_h = item._image_height
                        tile_cache = get_tile_cache()

                        keys = set()
                        num_cols = math.ceil(img_w / TILE_SIZE)
                        num_rows = math.ceil(img_h / TILE_SIZE)
                        for r in range(num_rows):
                            for c in range(num_cols):
                                keys.add(TileKey(item.image_id, 0, c, r))

                        tiles = tile_cache.request_blocking(keys)

                        img = QtGui.QImage(img_w, img_h, QtGui.QImage.Format.Format_ARGB32)
                        img.fill(QtGui.QColor(0, 0, 0, 0))
                        painter = QtGui.QPainter(img)
                        for key, pixmap in tiles.items():
                            painter.drawPixmap(key.col * TILE_SIZE, key.row * TILE_SIZE, pixmap)
                        painter.end()

                        # Crop the stitched image
                        img_cropped = img.copy(item.crop.toRect())
                        imgformat = item.get_imgformat(img_cropped)
                        barray = QtCore.QByteArray()
                        buffer = QtCore.QBuffer(barray)
                        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
                        img_cropped.save(buffer, imgformat.upper(), quality=90)
                        pixmap_bytes = barray.data()

                pixmap_b64 = base64.b64encode(pixmap_bytes).decode("ascii")
                element = ET.Element(
                    "image",
                    attrib={
                        "xlink:href": f"data:image/{imgformat};base64,{pixmap_b64}",
                        "width": str(width),
                        "height": str(height),
                        "image-rendering": (
                            "crisp-edges" if item.scale() > 2 else "optimizeQuality"
                        ),
                    },
                )
                pos = pos + item.crop.topLeft()
            else:
                continue

            transforms = []
            if item.flip() == -1:
                transforms.append(f"translate({anchor.x()} {anchor.y()})")
                transforms.append(f"scale({item.flip()} 1)")
                transforms.append(f"translate(-{anchor.x()} -{anchor.y()})")
            transforms.append(
                f"rotate({item.rotation()} {anchor.x()} {anchor.y()})"
            )

            element.set("transform", " ".join(transforms))
            element.set("x", str(pos.x()))
            element.set("y", str(pos.y()))
            element.set("opacity", str(item.opacity()))

            svg.append(element)
            self.emit_progress(worker, i)
            if worker and worker.canceled:
                return None

        return svg

    def export(self, filename: Path, worker: ThreadedIO | None = None) -> None:
        logger.debug(f"Exporting scene to {filename}")
        self.emit_begin_processing(worker, len(self.scene.items()))
        svg = self.render_to_svg(worker)

        if worker and worker.canceled:
            logger.debug("Export canceled")
            self.emit_finished(worker, filename, [])
            return

        if svg is None:
            self.handle_export_error(filename, "Export canceled or failed", worker)
            return

        tree = ET.ElementTree(svg)
        ET.indent(tree, space="  ")

        try:
            with open(filename, "w", encoding="utf-8") as f:
                tree.write(f, encoding="unicode", xml_declaration=True)
        except OSError as e:
            self.handle_export_error(filename, e, worker)
            return

        logger.debug("Export finished")
        self.emit_finished(worker, filename, [])


class ImagesToDirectoryExporter(ExporterBase):
    """Export all images to a folder.

    Not registered in the registry as it is accessed via its own menu entry,
    not auto-detected by file extension.
    """

    def __init__(self, scene: ZeeGraphicsScene, dirname: Path) -> None:
        self.scene: ZeeGraphicsScene = scene
        self.dirname: Path = dirname
        # If there are selected image items, export only them; otherwise export all.
        selected_items = [
            item for item in self.scene.selectedItems()
            if getattr(item, "is_image", False)
        ]
        if selected_items:
            # Keep original order if possible, or sort them?
            # scene.selectedItems() returns them in arbitrary order, but we can filter all items
            # in scene order to keep order consistent.
            all_pixmaps = self.scene.items_by_type("pixmap")
            self.items = [item for item in all_pixmaps if item in selected_items] # type: ignore
        else:
            self.items = list(self.scene.items_by_type("pixmap"))  # type: ignore

        self.num_total: int = len(self.items)
        self.start_from: int = 0
        self.handle_existing: str | None = None

    def export(self, worker: ThreadedIO | None = None) -> None:
        logger.debug(f"Exporting images to {self.dirname}")
        logger.debug(f"Starting at {self.start_from}")

        self.emit_begin_processing(worker, self.num_total)
        self.emit_progress(worker, self.start_from)

        # Precompute consecutive indices for nameless images
        import os.path
        import re
        nameless_indices = {}
        nameless_count = 0
        for idx, item in enumerate(self.items):
            has_valid_name = False
            if item.filename:
                basename = os.path.splitext(os.path.basename(item.filename))[0]
                basename = re.sub(r"[\<\>\:\"\/\\\|\?\*]", "_", basename)
                basename = re.sub(r"[\x00-\x1f]", "", basename)
                basename = basename.strip(" .")
                if basename:
                    has_valid_name = True
            if not has_valid_name:
                nameless_count += 1
                nameless_indices[idx] = nameless_count

        # Ensure directory exists
        try:
            self.dirname.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.handle_export_error(self.dirname, e, worker)
            return

        assert self.scene._scratch_file is not None
        io = SQLiteIO(self.scene._scratch_file, readonly=True)
        try:
            for i, item in enumerate(self.items[self.start_from :], start=self.start_from):
                if worker and worker.canceled:
                    logger.debug("Export canceled")
                    self.emit_finished(worker, self.dirname, [])
                    return

                # Determine format and read/stitch bytes
                row = io.fetchone("SELECT format FROM images WHERE id=?", (item.image_id,))
                imgformat = row[0] if row else "png"

                if imgformat == "gif":
                    if item._gif_bytes:
                        pixmap_bytes = item._gif_bytes
                    else:
                        row_tile = io.fetchone(
                            "SELECT data FROM tiles WHERE image_id=? AND level=0 AND col=0 AND row=0",
                            (item.image_id,),
                        )
                        pixmap_bytes = bytes(row_tile[0]) if row_tile else b""
                elif not item.pixmap().isNull():
                    pixmap_bytes, imgformat = item.pixmap_to_bytes()
                else:
                    # Stitch
                    width = item._image_width
                    height = item._image_height
                    tile_cache = get_tile_cache()

                    keys = set()
                    num_cols = math.ceil(width / TILE_SIZE)
                    num_rows = math.ceil(height / TILE_SIZE)
                    for r in range(num_rows):
                        for c in range(num_cols):
                            keys.add(TileKey(item.image_id, 0, c, r))

                    tiles = tile_cache.request_blocking(keys)

                    img = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
                    img.fill(QtGui.QColor(0, 0, 0, 0))
                    painter = QtGui.QPainter(img)
                    for key, pixmap in tiles.items():
                        painter.drawPixmap(key.col * TILE_SIZE, key.row * TILE_SIZE, pixmap)
                    painter.end()

                    imgformat = item.get_imgformat(img)
                    barray = QtCore.QByteArray()
                    buffer = QtCore.QBuffer(barray)
                    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
                    img.save(buffer, imgformat.upper(), quality=90)
                    pixmap_bytes = barray.data()

                no_filename_idx = nameless_indices.get(i)
                filename = item.get_filename_for_export(
                    imgformat, no_filename_idx=no_filename_idx
                )
                path = self.dirname / filename
                path_exists = path.exists()

                if path_exists:
                    logger.debug(f"File already exists: {path}")
                    if self.handle_existing is None:
                        self.start_from = i
                        self.emit_user_input_required(worker, str(path))
                        return
                    else:
                        if self.handle_existing == "skip":
                            self.handle_existing = None
                            logger.debug("Skipping file")
                            continue
                        elif self.handle_existing == "skip_all":
                            logger.debug("Skipping file")
                            continue
                        elif self.handle_existing == "overwrite":
                            self.handle_existing = None
                            logger.debug("Overwrite file")
                        elif self.handle_existing == "overwrite_all":
                            logger.debug("Overwrite file")

                logger.debug(f"Writing file: {path}")
                try:
                    path.write_bytes(pixmap_bytes)
                except OSError as e:
                    self.handle_export_error(path, e, worker)
                    return

                self.emit_progress(worker, i)
        except ZeeFileIOError:
            raise
        except Exception as e:
            self.handle_export_error(self.dirname, e, worker)
            return
        finally:
            io._close_connection()

        self.emit_finished(worker, self.dirname, [])
