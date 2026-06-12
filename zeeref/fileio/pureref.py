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

"""PureRef (.pur) file import support.

Uses the vendored PureRef-format library (MIT License).
"""

import logging
import math
import uuid
from PIL import Image

from PyQt6 import QtCore

from zeeref import commands
from zeeref.fileio.io import SQLiteIO, _insert_image
from zeeref.types.snapshot import PixmapItemSnapshot

logger = logging.getLogger(__name__)

class PureRefIO:
    """Reader for PureRef .pur files."""

    def __init__(self, filename, scene, worker=None):
        self.filename = filename
        self.scene = scene
        self.worker = worker
        self.items = []

    def read(self):
        """Read a PureRef file and add images to the scene."""
        from .vendor.purformat import PurFile

        logger.info(f'Loading PureRef file: {self.filename}')

        pur = PurFile()
        try:
            pur.read(self.filename)
        except Exception as e:
            logger.error(f"Failed to read PureRef file: {e}")
            if self.worker:
                from zeeref.types.snapshot import LoadResult
                self.worker.finished.emit(LoadResult(filename=self.filename, snapshots=[], scratch_file=self.scene._scratch_file, errors=[str(e)]))
            return

        total_items = sum(len(img.transforms) for img in pur.images)
        logger.debug(f'Found {len(pur.images)} images, {total_items} items')

        if getattr(pur, 'unsupported_metadata', False):
            for image in pur.images:
                if not getattr(image, 'transforms', []) and image.pngBinary:
                    from zeeref.fileio.vendor.purformat.items import PurGraphicsImageItem
                    dummy = PurGraphicsImageItem()
                    dummy.source = "Recovered Image"
                    dummy.name = "Recovered Image"
                    # Default matrix for dummy transform
                    dummy.matrix = [1.0, 0.0, 0.0, 1.0]
                    dummy.x = 0.0
                    dummy.y = 0.0
                    dummy.zLayer = 0.0
                    image.transforms = [dummy]
            total_items = sum(len(img.transforms) for img in pur.images)

        if self.worker:
            self.worker.begin_processing.emit(total_items)

        item_count = 0
        io = SQLiteIO(self.scene._scratch_file)

        for image in pur.images:
            if not image.pngBinary:
                continue

            # Load image data using PIL (ZeeRef backend)
            import io as builtin_io
            try:
                raw_bytes = bytes(image.pngBinary)
                pil_img = Image.open(builtin_io.BytesIO(raw_bytes))
                if pil_img.format == "GIF" and getattr(pil_img, "is_animated", False):
                    pil_img.custom_raw_bytes = raw_bytes
                pil_img.load()
            except Exception as e:
                logger.warning(f'Failed to load image data: {e}')
                continue

            # Create an item for each transform
            for transform in getattr(image, 'transforms', []):
                if self.worker and self.worker.canceled:
                    logger.debug('Import canceled')
                    self.worker.finished.emit('', [])
                    return

                snap = self._create_item_snapshot(pil_img, transform, io)
                if snap:
                    self.items.append(snap)

                item_count += 1
                if self.worker:
                    self.worker.progress.emit(item_count)
                    self.worker.msleep(5)

        io._close_connection()

        logger.info(f'Imported {len(self.items)} items from PureRef file')
        
        errors = []
        if getattr(pur, 'unsupported_metadata', False):
            errors.append('FALLBACK_MODE')
            
        if self.worker:
            from zeeref.types.snapshot import LoadResult
            self.worker.finished.emit(LoadResult(filename=self.filename, snapshots=self.items, scratch_file=self.scene._scratch_file, errors=errors))

    def _create_item_snapshot(self, pil_img, transform, io):
        """Create a PixmapItemSnapshot from PureRef transform data."""
        m11, m12, m21, m22 = transform.matrix

        scale = math.sqrt(m11 * m11 + m21 * m21)
        rotation = math.atan2(m21, m11) * 180.0 / math.pi

        det = m11 * m22 - m12 * m21
        flip = -1 if det < 0 else 1

        # Use an ImageInsert-like dict for _insert_image args
        class TransformArgs:
            pass
        t = TransformArgs()
        t.scale = scale
        t.rotation = rotation
        t.z = transform.zLayer
        t.flip = flip
        t.opacity = 1.0
        t.x = transform.x
        t.y = transform.y
        
        pos = QtCore.QPointF(0, 0) # Used only if x/y not present
        raw_bytes = getattr(pil_img, "custom_raw_bytes", None)
        snap = _insert_image(
            pil_img,
            transform.source,
            pos,
            io,
            self.scene,
            transforms=t,
            raw_bytes=raw_bytes,
        )
        return snap
