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

"""Classes for items that are added to the scene by the user (images,
text).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from functools import cached_property
import copy
import logging
import math
import os.path
import time
import uuid
from typing import Any, cast

import mistune
from PIL import Image
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from zeeref import commands
from zeeref.config import ZeeSettings
from zeeref.constants import COLORS
from zeeref.fileio.tilecache import get_tile_cache
from zeeref.fileio.tiling import TILE_SIZE
from zeeref.types.tile import TileKey
from zeeref.types.snapshot import ErrorItemSnapshot, ItemSnapshot, PixmapItemSnapshot
from zeeref.selection import SelectableMixin

logger = logging.getLogger(__name__)

item_registry: dict[str, type[ZeeItemMixin]] = {}


class _GifLoader(QtCore.QObject):
    """Singleton QObject used to marshal GIF bytes to the main thread."""

    gif_blob_ready = QtCore.pyqtSignal(object, bytes)  # (ZeePixmapItem, raw_bytes)

    _inst: "_GifLoader | None" = None

    @classmethod
    def instance(cls) -> "_GifLoader":
        if cls._inst is None:
            cls._inst = cls()
            cls._inst.gif_blob_ready.connect(cls._inst._dispatch)
        return cls._inst

    @QtCore.pyqtSlot(object, bytes)
    def _dispatch(self, item: object, raw: bytes) -> None:
        from zeeref.items import ZeePixmapItem
        if isinstance(item, ZeePixmapItem):
            item._on_gif_blob_loaded(raw)

def register_item(cls: type[ZeeItemMixin]) -> type[ZeeItemMixin]:
    item_registry[cls.TYPE] = cls
    return cls


def create_item_from_snapshot(snap: ItemSnapshot) -> ZeeItemMixin:
    """Create a scene item from a snapshot. Dispatches by type.

    If the factory raises, returns a ZeeErrorItem preserving the
    item's position and save_id for future recovery.
    """
    cls = item_registry.get(snap.type)
    if cls is None:
        err = ZeeErrorItem(f"Item of unknown type: {snap.type}")
        err.save_id = snap.save_id
        err.setPos(snap.x, snap.y)
        err.setZValue(snap.z)
        return err

    try:
        return cls.from_snapshot(snap)
    except Exception as e:
        logger.exception(f"Failed to create {snap.type} from snapshot")
        filename = snap.data.get("filename", "unknown")
        err = ZeeErrorItem(f"Failed to load {snap.type}: {filename}\n{e}")
        err.save_id = snap.save_id
        err.setPos(snap.x, snap.y)
        err.setZValue(snap.z)
        return err


def sort_by_filename(items: list[ZeeItemMixin]) -> list[ZeeItemMixin]:
    """Order items by filename.

    Items with a filename (ordered by filename) first, then remaining
    items ordered by creation time.
    """

    items_by_filename: list[ZeeItemMixin] = []
    items_remaining: list[ZeeItemMixin] = []

    for item in items:
        if getattr(item, "filename", None):
            items_by_filename.append(item)
        else:
            items_remaining.append(item)

    items_by_filename.sort(key=lambda x: x.filename)
    items_remaining.sort(key=lambda x: x.created_at)
    return items_by_filename + items_remaining


class ZeeItemMixin(SelectableMixin):
    """Base for all items added by the user."""

    TYPE: str
    save_id: str
    created_at: float
    filename: str | None
    is_image: bool

    def get_extra_save_data(self) -> dict[str, Any]:
        """Return type-specific data for JSON serialization. Override in subclasses."""
        return {}

    def create_copy(self) -> ZeeItemMixin:
        """Create a copy of this item. Override in subclasses."""
        raise NotImplementedError

    def copy_to_clipboard(self, clipboard: QtGui.QClipboard) -> None:
        """Copy this item to the system clipboard. Override in subclasses."""
        raise NotImplementedError

    @classmethod
    def from_snapshot(cls, snap: ItemSnapshot) -> ZeeItemMixin:
        """Create an item from a snapshot. Override in subclasses."""
        raise NotImplementedError

    def set_pos_center(self, pos: QtCore.QPointF) -> None:
        """Sets the position using the item's center as the origin point."""

        self.setPos(pos - self.center_scene_coords)

    def has_selection_outline(self) -> bool:
        return self.isSelected()

    def has_selection_handles(self) -> bool:
        scene = self.zee_scene()
        return self.isSelected() and scene is not None and scene.has_single_selection()

    def selection_action_items(self) -> list[Any]:
        """The items affected by selection actions like scaling and rotating."""
        return [self]

    def snapshot(self) -> ItemSnapshot:
        """Create an immutable snapshot of this item for thread-safe saving."""
        return ItemSnapshot(
            save_id=self.save_id,
            type=self.TYPE,
            x=self.pos().x(),
            y=self.pos().y(),
            z=self.zValue(),
            scale=self.scale(),
            rotation=self.rotation(),
            flip=self.flip(),
            data=self.get_extra_save_data(),
            created_at=self.created_at,
        )

    def on_selected_change(self, value: Any) -> None:
        scene = self.zee_scene()
        if (
            value
            and scene
            and not scene.has_selection()
            and scene.active_mode is not None
        ):
            self.bring_to_front()

    def update_from_data(self, **kwargs: Any) -> None:
        self.save_id = kwargs.get("save_id", self.save_id)
        self.created_at = kwargs.get("created_at", self.created_at)
        self.setPos(kwargs.get("x", self.pos().x()), kwargs.get("y", self.pos().y()))
        self.setZValue(kwargs.get("z", self.zValue()))
        self.setScale(kwargs.get("scale", self.scale()))
        self.setRotation(kwargs.get("rotation", self.rotation()))
        if kwargs.get("flip", 1) != self.flip():
            self.do_flip()


@register_item
class ZeePixmapItem(ZeeItemMixin, QtWidgets.QGraphicsPixmapItem):
    """Class for images added by the user."""

    TYPE = "pixmap"
    CROP_HANDLE_SIZE: int = 15

    crop_temp: QtCore.QRectF | None
    crop_mode_move: Callable[[], QtCore.QRectF] | QtCore.QRectF | None
    crop_mode_event_start: QtCore.QPointF | None

    def __init__(
        self, image: QtGui.QImage, filename: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(QtGui.QPixmap.fromImage(image))
        self.save_id: str = uuid.uuid4().hex
        self.created_at: float = time.time()
        self.filename = filename
        self.is_image = True
        self.crop_mode: bool = False
        self._subscribed: bool = False
        self._tile_children: dict[TileKey, QtWidgets.QGraphicsPixmapItem] = {}
        self._stale_tile_children: dict[TileKey, QtWidgets.QGraphicsPixmapItem] = {}
        self._current_level: int = 0
        pm = self.pixmap()
        self._image_width: int = pm.width()
        self._image_height: int = pm.height()
        self.image_id: str = uuid.uuid4().hex
        self.title: str | None = None
        self.caption: str | None = None
        # GIF animation state
        self._is_gif: bool = False
        self._gif_bytes: bytes | None = None
        self._movie: QtGui.QMovie | None = None
        self._movie_buffer: QtCore.QBuffer | None = None
        self._movie_ba: QtCore.QByteArray | None = None  # must outlive _movie_buffer
        self._gif_reversed: bool = False
        self._gif_frames: list[tuple[QtGui.QPixmap, int]] = []  # (pixmap, delay_ms)
        self._gif_timer: QtCore.QTimer | None = None
        self._gif_frame_idx: int = 0
        # Invisible clip item parents all tile children so they are
        # clipped to the image/crop rect without affecting shape().
        self._clip_item = QtWidgets.QGraphicsRectItem(self)
        self._clip_item.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape,
            True,
        )
        self._clip_item.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False
        )
        self._clip_item.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False
        )
        self._clip_item.setPen(QtGui.QPen(Qt.PenStyle.NoPen))
        self._clip_item.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent, True
        )
        self.reset_crop()
        self.init_selectable()
        self.settings = ZeeSettings()

    def snapshot(self) -> PixmapItemSnapshot:
        """Create an immutable snapshot. Tile data lives in the .swp."""
        return PixmapItemSnapshot(
            save_id=self.save_id,
            type=self.TYPE,
            x=self.pos().x(),
            y=self.pos().y(),
            z=self.zValue(),
            scale=self.scale(),
            rotation=self.rotation(),
            flip=self.flip(),
            data=self.get_extra_save_data(),
            created_at=self.created_at,
            image_id=self.image_id,
            width=self._image_width,
            height=self._image_height,
            format="gif" if self._is_gif else "png",
        )

    @classmethod
    def create_from_data(cls, **kwargs: Any) -> ZeePixmapItem:
        item: ZeePixmapItem = kwargs.pop("item")
        data: dict[str, Any] = kwargs.pop("data", {})
        item.filename = item.filename or data.get("filename")
        if "crop" in data:
            item.crop = QtCore.QRectF(*data["crop"])
        item.setOpacity(data.get("opacity", 1))
        item.title = data.get("title")
        item.caption = data.get("caption")
        return item

    @classmethod
    def from_snapshot(cls, snap: PixmapItemSnapshot) -> ZeePixmapItem:
        """Create a placeholder ZeePixmapItem from a loaded snapshot.

        Tile data is loaded on demand via the TileCache.
        For GIF format, blob is loaded directly via _load_gif_async.
        """
        item = cls(QtGui.QImage())
        item._image_width = snap.width
        item._image_height = snap.height
        item.crop = QtCore.QRectF(0, 0, snap.width, snap.height)
        item.save_id = snap.save_id
        item.created_at = snap.created_at
        item.image_id = snap.image_id
        item.filename = snap.data.get("filename")
        item.title = snap.data.get("title")
        item.caption = snap.data.get("caption")
        item._gif_reversed = snap.data.get("gif_reversed", False)
        if "crop" in snap.data:
            item.crop = QtCore.QRectF(*snap.data["crop"])
        item.setOpacity(snap.data.get("opacity", 1))
        item.setPos(snap.x, snap.y)
        item.setZValue(snap.z)
        item.setScale(snap.scale)
        item.setRotation(snap.rotation)
        if snap.flip != item.flip():
            item.do_flip()
        if snap.format == "gif":
            item._is_gif = True
            item._load_gif_async()
        return item

    def __str__(self) -> str:
        return f'Image "{self.filename}" {self._image_width} x {self._image_height}'

    @cached_property
    def color_gamut(self) -> dict[tuple[int, int], int]:
        from zeeref.fileio.tilecache import get_tile_cache
        from zeeref.fileio.tiling import TILE_SIZE

        logger.debug(f"Calculating color gamut for {self}")
        gamut: defaultdict[tuple[int, int], int] = defaultdict(int)

        if self._is_gif or not self.pixmap().isNull():
            # Image or GIF frame is fully loaded in memory
            img = self.pixmap().toImage()
        else:
            # For tiled images, stitch at an appropriate resolution (around 1000px max)
            L = 0
            while L < self._max_level and max(self._image_width >> L, self._image_height >> L) > 1000:
                L += 1

            level_w = max(1, self._image_width >> L)
            level_h = max(1, self._image_height >> L)

            from math import ceil
            from zeeref.types.tile import TileKey

            num_cols = ceil(level_w / TILE_SIZE)
            num_rows = ceil(level_h / TILE_SIZE)

            keys = set()
            for row in range(num_rows):
                for col in range(num_cols):
                    keys.add(TileKey(self.image_id, L, col, row))

            try:
                tile_cache = get_tile_cache()
                tiles = tile_cache.request_blocking(keys)
            except AssertionError:
                # TileCache not initialized (e.g. in tests/fallback)
                tiles = {}

            img = QtGui.QImage(level_w, level_h, QtGui.QImage.Format.Format_ARGB32)
            img.fill(QtGui.QColor(0, 0, 0, 0))
            painter = QtGui.QPainter(img)
            for key, pixmap in tiles.items():
                painter.drawPixmap(key.col * TILE_SIZE, key.row * TILE_SIZE, pixmap)
            painter.end()

        # Don't evaluate every pixel for larger images:
        step = max(1, int(max(img.width(), img.height()) / 1000))
        logger.debug(f"Considering every {step}. row/column")

        for i in range(0, img.width(), step):
            for j in range(0, img.height(), step):
                rgb = img.pixelColor(i, j)
                rgbtuple = (rgb.red(), rgb.blue(), rgb.green())
                if (5 < rgb.alpha()
                        and min(rgbtuple) < 250 and max(rgbtuple) > 5):
                    # Only consider pixels that aren't close to
                    # transparent, white or black
                    gamut[(rgb.hue(), rgb.saturation())] += 1

        logger.debug(f"Got {len(gamut)} color gamut values")
        return dict(gamut)

    @property
    def crop(self) -> QtCore.QRectF:
        return self._crop

    @crop.setter
    def crop(self, value: QtCore.QRectF) -> None:
        logger.debug(f"Setting crop for {self} to {value}")
        self.prepareGeometryChange()
        self._crop = value
        self._clip_item.setRect(value)
        self.update()

    def sample_color_at(self, pos: QtCore.QPointF) -> QtGui.QColor | None:
        local = self.mapFromScene(pos)
        scale = 1 << self._current_level
        col = int(local.x()) // (TILE_SIZE * scale)
        row = int(local.y()) // (TILE_SIZE * scale)
        key = TileKey(self.image_id, self._current_level, col, row)
        child = self._tile_children.get(key)
        if child is None:
            return None
        px = (int(local.x()) // scale) % TILE_SIZE
        py = (int(local.y()) // scale) % TILE_SIZE
        color = child.pixmap().toImage().pixelColor(px, py)
        if color.alpha() == 0:
            return None
        return color

    def _text_height(self) -> float:
        """Height of one line of label text in item coordinates."""
        fm = QtGui.QFontMetricsF(QtGui.QFont())
        return fm.height() + 4  # small padding

    def _image_rect(self) -> QtCore.QRectF:
        """The image rect (crop or full) without text expansion."""
        if self.crop_mode:
            return QtCore.QRectF(0, 0, self._image_width, self._image_height)
        return self.crop

    def bounding_rect_unselected(self) -> QtCore.QRectF:
        rect = self._image_rect()
        h = self._text_height()
        if self.title:
            rect = rect.adjusted(0, -h, 0, 0)
        if self.caption:
            rect = rect.adjusted(0, 0, 0, h)
        return rect

    def get_extra_save_data(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "filename": self.filename,
            "opacity": self.opacity(),
            "crop": [
                self.crop.topLeft().x(),
                self.crop.topLeft().y(),
                self.crop.width(),
                self.crop.height(),
            ],
        }
        if self.title:
            d["title"] = self.title
        if self.caption:
            d["caption"] = self.caption
        if self._gif_reversed:
            d["gif_reversed"] = True
        return d

    def get_filename_for_export(
        self, imgformat: str, save_id_default: str | None = None
    ) -> str:
        save_id = self.save_id or save_id_default
        assert save_id is not None

        short_id = save_id[:8]
        if self.filename:
            basename = os.path.splitext(os.path.basename(self.filename))[0]
            return f"{short_id}-{basename}.{imgformat}"
        else:
            return f"{short_id}.{imgformat}"

    def get_imgformat(self, img: QtGui.QImage) -> str:
        """Determines the format for storing this image."""

        formt = self.settings.valueOrDefault("Items/image_storage_format")

        if formt == "best":
            # Images with alpha channel and small images are stored as png
            if img.hasAlphaChannel() or (img.height() < 500 and img.width() < 500):
                formt = "png"
            else:
                formt = "jpg"

        logger.debug(f"Found format {formt} for {self}")
        return formt

    def pixmap_to_bytes(self, apply_crop: bool = False) -> tuple[bytes, str]:
        """Convert the pixmap data to PNG bytestring."""
        barray = QtCore.QByteArray()
        buffer = QtCore.QBuffer(barray)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        pm = self.pixmap()

        if apply_crop:
            pm = pm.copy(self.crop.toRect())

        img = pm.toImage()
        imgformat = self.get_imgformat(img)
        img.save(buffer, imgformat.upper(), quality=90)
        return (barray.data(), imgformat)

    def _qpixmap_to_pil(self, pixmap: QtGui.QPixmap) -> Image.Image:
        """Convert a QPixmap to a PIL Image."""
        img = pixmap.toImage()
        if img.hasAlphaChannel():
            img = img.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            mode = "RGBA"
        else:
            img = img.convertToFormat(QtGui.QImage.Format.Format_RGB888)
            mode = "RGB"
        ptr = img.constBits()
        assert ptr is not None
        ptr.setsize(img.sizeInBytes())
        raw_bytes: bytes = bytes(cast(Any, ptr))
        return Image.frombytes(
            mode,
            (img.width(), img.height()),
            raw_bytes,
            "raw",
            mode,
            img.bytesPerLine(),
        )

    def _pil_to_qpixmap(self, pil_img: Image.Image) -> QtGui.QPixmap:
        """Convert a PIL Image to a QPixmap."""
        if pil_img.mode == "RGBA":
            fmt = QtGui.QImage.Format.Format_RGBA8888
            channels = 4
        else:
            fmt = QtGui.QImage.Format.Format_RGB888
            channels = 3
        data = pil_img.tobytes()
        stride = channels * pil_img.width
        qimg = QtGui.QImage(data, pil_img.width, pil_img.height, stride, fmt)
        return QtGui.QPixmap.fromImage(qimg.copy())

    @property
    def _max_level(self) -> int:
        if self._image_width == 0 or self._image_height == 0:
            return 0
        from math import floor, log2

        return max(
            0, floor(log2(max(self._image_width, self._image_height) / TILE_SIZE))
        )

    def _ensure_subscribed(self) -> None:
        """Lazily subscribe to tile cache on first visibility check."""
        if not self._subscribed:
            get_tile_cache().subscribe(self.image_id, self)
            self._subscribed = True

    def unsubscribe_tile_cache(self) -> None:
        """Unsubscribe from tile cache. Called on removal from scene."""
        if self._subscribed:
            get_tile_cache().unsubscribe(self.image_id, self)
            self._subscribed = False
        self._stop_gif()

    def _stop_gif(self) -> None:
        """Stop and clean up any active GIF playback."""
        if self._gif_timer is not None:
            self._gif_timer.stop()
            self._gif_timer = None
        if self._movie is not None:
            self._movie.stop()
            self._movie.setDevice(None)  # detach before destroying buffer
            self._movie = None
        if self._movie_buffer is not None:
            self._movie_buffer.close()
            self._movie_buffer = None
        self._movie_ba = None  # release the byte array last

    def _load_gif_async(self) -> None:
        """Load the raw GIF blob from the scratch DB in a background thread."""
        from zeeref.fileio.sql import SQLiteIO
        import threading

        image_id = self.image_id
        loader = _GifLoader.instance()
        item_ref = self
        logger.debug(f"[_load_gif_async] Starting for image_id={image_id[:8]}")

        def _fetch() -> None:
            try:
                cache = get_tile_cache()
                swp = cache._loader._swp_path
                logger.debug(f"[_load_gif_async] Using swp={swp}")
                io = SQLiteIO(swp, readonly=True)
                row = io.fetchone(
                    "SELECT data FROM tiles WHERE image_id=? AND level=0 AND col=0 AND row=0",
                    (image_id,),
                )
                io._close_connection()
                if row and row[0]:
                    raw: bytes = bytes(row[0])
                    logger.debug(f"[_load_gif_async] Found GIF blob of size={len(raw)} bytes")
                    loader.gif_blob_ready.emit(item_ref, raw)
                else:
                    logger.debug(f"[_load_gif_async] No GIF blob found in database for image_id={image_id[:8]}")
            except Exception as e:
                logger.exception(f"[_load_gif_async] Failed to load GIF blob for {image_id[:8]}: {e}")

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_gif_blob_loaded(self, raw: bytes) -> None:
        """Called on the main thread when GIF bytes are ready."""
        logger.debug(f"[_on_gif_blob_loaded] GIF bytes received: size={len(raw)}")
        self._gif_bytes = raw
        self._setup_movie()

    def _extract_gif_frames(self) -> None:
        """Extract all GIF frames using PIL for reverse playback.

        PIL decodes frames synchronously without needing a Qt event loop,
        avoiding the freeze that QMovie.jumpToFrame() causes on the main thread.
        """
        if not self._gif_bytes or self._gif_frames:
            return  # Already extracted or nothing to extract
        from io import BytesIO
        try:
            pil_gif = Image.open(BytesIO(self._gif_bytes))
            frames: list[tuple[QtGui.QPixmap, int]] = []
            while True:
                duration = pil_gif.info.get("duration", 100)
                frame_rgba = pil_gif.convert("RGBA")
                data = frame_rgba.tobytes()
                qimg = QtGui.QImage(
                    data,
                    frame_rgba.width,
                    frame_rgba.height,
                    frame_rgba.width * 4,
                    QtGui.QImage.Format.Format_RGBA8888,
                )
                frames.append((QtGui.QPixmap.fromImage(qimg.copy()), int(duration)))
                pil_gif.seek(pil_gif.tell() + 1)
        except EOFError:
            pass
        except Exception:
            logger.exception("Failed to extract GIF frames for reverse playback")
        self._gif_frames = frames

    def _setup_movie(self) -> None:
        """Set up QMovie or reverse timer for playback."""
        self._stop_gif()
        if not self._gif_bytes:
            logger.debug("[_setup_movie] No GIF bytes found")
            return
        if self._gif_reversed and self._gif_frames:
            logger.debug("[_setup_movie] Playing reversed")
            self._play_reversed()
        else:
            logger.debug("[_setup_movie] Setting up QMovie")
            # Keep QByteArray alive for the lifetime of the movie.
            # If _movie_ba goes out of scope, QBuffer reads freed memory and freezes.
            self._movie_ba = QtCore.QByteArray(self._gif_bytes)
            self._movie_buffer = QtCore.QBuffer(self._movie_ba)
            self._movie_buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
            self._movie = QtGui.QMovie()
            self._movie.setDevice(self._movie_buffer)
            self._movie.frameChanged.connect(self._on_frame_changed)
            logger.debug(f"[_setup_movie] QMovie is valid: {self._movie.isValid()}")
            self._movie.start()
            logger.debug(f"[_setup_movie] QMovie started: state={self._movie.state()}")

    def _on_frame_changed(self, _frame: int) -> None:
        """Update pixmap when QMovie advances a frame."""
        if self._movie:
            pm = self._movie.currentPixmap()
            logger.debug(f"[_on_frame_changed] Frame {_frame} changed, pixmap size={pm.size()}")
            self.setPixmap(pm)
            self.update()

    def _play_reversed(self) -> None:
        """Extract frames (lazily) then step through in reverse using a QTimer."""
        self._extract_gif_frames()  # No-op if already done
        if not self._gif_frames:
            return
        self._gif_frame_idx = len(self._gif_frames) - 1
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        self._gif_timer = timer

        def _next() -> None:
            if not self._gif_frames or self._gif_timer is None:
                return
            pixmap, delay = self._gif_frames[self._gif_frame_idx]
            self.setPixmap(pixmap)
            self.update()
            self._gif_frame_idx = (self._gif_frame_idx - 1) % len(self._gif_frames)
            self._gif_timer.setInterval(max(10, delay))
            self._gif_timer.start()

        timer.timeout.connect(_next)
        _next()

    def toggle_gif_reverse(self) -> None:
        """Toggle reversed playback for this GIF."""
        self._gif_reversed = not self._gif_reversed
        self._setup_movie()

    def update_visible_tiles(self, viewport_rect: QtCore.QRectF) -> None:
        """Compute and request needed tiles for the current viewport.

        Called by the view for each visible item during viewport checks.
        viewport_rect is in scene coordinates.
        """
        if self._is_gif:
            # GIFs are animated via QMovie/QTimer — no tile loading needed
            return

        from math import ceil, floor, log2

        self._ensure_subscribed()

        # Compute effective scale (view zoom × item scale)
        scene = self.scene()
        if scene is None:
            return
        views = scene.views()
        if not views:
            return
        view_scale = abs(views[0].transform().m11())
        effective_scale = view_scale * self.scale()

        # Pick level
        if effective_scale > 0:
            level = max(0, floor(-log2(effective_scale)))
        else:
            level = 0
        level = min(level, self._max_level)

        # If level changed, move old tiles to stale set
        if level != self._current_level:
            logger.info(
                f"Level change {self._current_level} -> {level} for {self.image_id[:8]} "
                f"(effective_scale={effective_scale:.4f}, view_scale={view_scale:.4f}, "
                f"item_scale={self.scale():.4f}, max_level={self._max_level})"
            )
            self._remove_stale_tile_children()
            self._stale_tile_children = self._tile_children
            self._tile_children = {}
            self._current_level = level

        # Convert viewport rect to item-local coords
        local_rect = self.mapRectFromScene(viewport_rect)

        # Tile size in image coords at this level
        tile_extent = TILE_SIZE * (1 << level)

        # Compute which tiles intersect the viewport
        # Pad by one tile to avoid edge flickering from rounding
        col_min = max(0, int(local_rect.left() / tile_extent) - 1)
        col_max = int(ceil(local_rect.right() / tile_extent)) + 1
        row_min = max(0, int(local_rect.top() / tile_extent) - 1)
        row_max = int(ceil(local_rect.bottom() / tile_extent)) + 1

        level_w = max(1, self._image_width >> level)
        level_h = max(1, self._image_height >> level)
        max_col = ceil(level_w / TILE_SIZE)
        max_row = ceil(level_h / TILE_SIZE)

        keys: set[TileKey] = set()
        for row in range(row_min, min(row_max, max_row)):
            for col in range(col_min, min(col_max, max_col)):
                keys.add(TileKey(self.image_id, level, col, row))
        hits = get_tile_cache().request(keys)
        for key, pixmap in hits.items():
            self.on_tile_loaded(key, pixmap)

        # All new tiles loaded — stale tiles no longer needed
        if self._stale_tile_children and len(hits) == len(keys):
            self._remove_stale_tile_children()

    def _remove_stale_tile_children(self) -> None:
        """Remove stale (previous-level) tile children from the scene."""
        for child in self._stale_tile_children.values():
            scene = child.scene()
            if scene is not None:
                scene.removeItem(child)
        self._stale_tile_children.clear()

    def _remove_all_tile_children(self) -> None:
        """Remove all tile child items from the scene."""
        self._remove_stale_tile_children()
        for child in self._tile_children.values():
            scene = child.scene()
            if scene is not None:
                scene.removeItem(child)
        self._tile_children.clear()

        self.update()

    def on_tile_loaded(self, key: TileKey, pixmap: QtGui.QPixmap) -> None:
        # Ignore tiles for a different level than what we're currently showing
        if key.level != self._current_level:
            return
        # Already have this tile
        if key in self._tile_children:
            return
        child = QtWidgets.QGraphicsPixmapItem(pixmap, self._clip_item)
        # Default is FastTransformation (nearest-neighbor); QGraphicsPixmapItem
        # forces the painter's SmoothPixmapTransform hint to match its own mode,
        # overriding the view-level hint. Tiles land at an on-screen scale in
        # (0.5, 1.0], so without smooth scaling the final downscale aliases.
        child.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        child.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        child.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        child.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent, True
        )
        child.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # Position in image coords: tile covers TILE_SIZE pixels at the
        # level resolution, which maps to TILE_SIZE * 2^level in full-res
        scale_factor = 1 << key.level
        child.setPos(
            key.col * TILE_SIZE * scale_factor, key.row * TILE_SIZE * scale_factor
        )
        child.setScale(scale_factor)
        self._tile_children[key] = child
        logger.debug(f"Tile child added: {key}")

    def on_tile_unloaded(self, key: TileKey) -> None:
        child = self._tile_children.pop(key, None) or self._stale_tile_children.pop(
            key, None
        )
        if child is not None:
            scene = child.scene()
            if scene is not None:
                scene.removeItem(child)
            logger.debug(f"Tile child removed: {key}")
        if not self._tile_children and not self._stale_tile_children:
            self.update()

    def create_copy(self) -> ZeePixmapItem:
        item = ZeePixmapItem(QtGui.QImage())
        item.image_id = self.image_id
        item._image_width = self._image_width
        item._image_height = self._image_height
        item._crop = QtCore.QRectF(0, 0, self._image_width, self._image_height)
        item.filename = self.filename
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        item.setOpacity(self.opacity())
        if self.flip() == -1:
            item.do_flip()
        item.crop = self.crop
        if self._is_gif:
            item._is_gif = True
            item._gif_reversed = self._gif_reversed
            item._load_gif_async()
        return item

    def reset_crop(self) -> None:
        self.crop = QtCore.QRectF(0, 0, self._image_width, self._image_height)

    @property
    def crop_handle_size(self) -> float:
        return self.fixed_length_for_viewport(self.CROP_HANDLE_SIZE)

    def crop_handle_topleft(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x(), topleft.y(), self.crop_handle_size, self.crop_handle_size
        )

    def crop_handle_bottomleft(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        bottomleft = self.crop_temp.bottomLeft()
        return QtCore.QRectF(
            bottomleft.x(),
            bottomleft.y() - self.crop_handle_size,
            self.crop_handle_size,
            self.crop_handle_size,
        )

    def crop_handle_bottomright(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        bottomright = self.crop_temp.bottomRight()
        return QtCore.QRectF(
            bottomright.x() - self.crop_handle_size,
            bottomright.y() - self.crop_handle_size,
            self.crop_handle_size,
            self.crop_handle_size,
        )

    def crop_handle_topright(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        topright = self.crop_temp.topRight()
        return QtCore.QRectF(
            topright.x() - self.crop_handle_size,
            topright.y(),
            self.crop_handle_size,
            self.crop_handle_size,
        )

    def crop_handles(
        self,
    ) -> tuple[
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
    ]:
        return (
            self.crop_handle_topleft,
            self.crop_handle_bottomleft,
            self.crop_handle_bottomright,
            self.crop_handle_topright,
        )

    def crop_edge_top(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x() + self.crop_handle_size,
            topleft.y(),
            self.crop_temp.width() - 2 * self.crop_handle_size,
            self.crop_handle_size,
        )

    def crop_edge_left(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        topleft = self.crop_temp.topLeft()
        return QtCore.QRectF(
            topleft.x(),
            topleft.y() + self.crop_handle_size,
            self.crop_handle_size,
            self.crop_temp.height() - 2 * self.crop_handle_size,
        )

    def crop_edge_bottom(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        bottomleft = self.crop_temp.bottomLeft()
        return QtCore.QRectF(
            bottomleft.x() + self.crop_handle_size,
            bottomleft.y() - self.crop_handle_size,
            self.crop_temp.width() - 2 * self.crop_handle_size,
            self.crop_handle_size,
        )

    def crop_edge_right(self) -> QtCore.QRectF:
        assert self.crop_temp is not None
        topright = self.crop_temp.topRight()
        return QtCore.QRectF(
            topright.x() - self.crop_handle_size,
            topright.y() + self.crop_handle_size,
            self.crop_handle_size,
            self.crop_temp.height() - 2 * self.crop_handle_size,
        )

    def crop_edges(
        self,
    ) -> tuple[
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
        Callable[[], QtCore.QRectF],
    ]:
        return (
            self.crop_edge_top,
            self.crop_edge_left,
            self.crop_edge_bottom,
            self.crop_edge_right,
        )

    def get_crop_handle_cursor(
        self, handle: Callable[[], QtCore.QRectF]
    ) -> Qt.CursorShape:
        """Gets the crop cursor for the given handle."""

        is_topleft_or_bottomright = handle in (
            self.crop_handle_topleft,
            self.crop_handle_bottomright,
        )
        return self.get_diag_cursor(is_topleft_or_bottomright)

    def get_crop_edge_cursor(self, edge: Callable[[], QtCore.QRectF]) -> Qt.CursorShape:
        """Gets the crop edge cursor for the given edge."""

        top_or_bottom = edge in (self.crop_edge_top, self.crop_edge_bottom)
        sideways = 45 < self.rotation() < 135 or 225 < self.rotation() < 315

        if top_or_bottom is sideways:
            return Qt.CursorShape.SizeHorCursor
        else:
            return Qt.CursorShape.SizeVerCursor

    def _paint_labels(self, painter: QtGui.QPainter) -> None:
        """Draw title above and caption below the image."""
        img_rect = self._image_rect()
        h = self._text_height()
        text_color = QtGui.QColor(*COLORS["Scene:Text"])
        bg_color = QtGui.QColor(0, 0, 0, 140)

        painter.setFont(QtGui.QFont())
        painter.setPen(text_color)

        if self.title:
            title_rect = QtCore.QRectF(
                img_rect.left(), img_rect.top() - h, img_rect.width(), h
            )
            painter.fillRect(title_rect, bg_color)
            painter.drawText(
                title_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                f"  {self.title}",
            )

        if self.caption:
            caption_rect = QtCore.QRectF(
                img_rect.left(), img_rect.bottom(), img_rect.width(), h
            )
            painter.fillRect(caption_rect, bg_color)
            painter.drawText(
                caption_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                f"  {self.caption}",
            )

    def draw_crop_rect(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        """Paint a dotted rectangle for the cropping UI."""
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255))
        pen.setWidth(2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(rect)
        pen.setColor(QtGui.QColor(0, 0, 0))
        pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawRect(rect)

    def paint(
        self,
        painter: QtGui.QPainter | None,
        option: QtWidgets.QStyleOptionGraphicsItem | None,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        assert painter is not None
        if self._is_gif:
            painter.save()
            painter.setClipRect(self.crop)
            super().paint(painter, option, widget)
            painter.restore()

        # Tile children paint themselves behind the parent
        # (ItemStacksBehindParent), so everything below here renders on top.

        if self.title or self.caption:
            self._paint_labels(painter)

        if self.crop_mode:
            assert self.crop_temp is not None
            self.paint_debug(painter, option, widget)
            self.draw_crop_rect(painter, self.crop_temp)
            for handle in self.crop_handles():
                self.draw_crop_rect(painter, handle())

        self.paint_selectable(painter, option, widget)

    def enter_crop_mode(self) -> None:
        logger.debug(f"Entering crop mode on {self}")
        self.prepareGeometryChange()
        self.crop_mode = True
        self.crop_temp = QtCore.QRectF(self.crop)
        self.crop_mode_move: Callable[[], QtCore.QRectF] | QtCore.QRectF | None = None
        self.crop_mode_event_start: QtCore.QPointF | None = None
        self.grabKeyboard()
        self.update()
        self.require_scene().crop_item = self

    def exit_crop_mode(self, confirm: bool) -> None:
        logger.debug(f"Exiting crop mode with {confirm} on {self}")
        scene = self.require_scene()
        if confirm and self.crop != self.crop_temp:
            scene.undo_stack.push(commands.CropItem(self, self.crop_temp))
        self.prepareGeometryChange()
        self.crop_mode = False
        self.crop_temp = None
        self.crop_mode_move = None
        self.crop_mode_event_start = None
        self.ungrabKeyboard()
        self.update()
        scene.crop_item = None

    def keyPressEvent(self, event: QtGui.QKeyEvent | None) -> None:
        assert event is not None
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.exit_crop_mode(confirm=True)
        elif event.key() == Qt.Key.Key_Escape:
            self.exit_crop_mode(confirm=False)
        else:
            super().keyPressEvent(event)

    def hoverMoveEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent | None) -> None:
        assert event is not None
        if not self.crop_mode:
            return super().hoverMoveEvent(event)

        for handle in self.crop_handles():
            if handle().contains(event.pos()):
                self.set_cursor(self.get_crop_handle_cursor(handle))
                return
        for edge in self.crop_edges():
            if edge().contains(event.pos()):
                self.set_cursor(self.get_crop_edge_cursor(edge))
                return
        assert self.crop_temp is not None
        if self.crop_temp.contains(event.pos()):
            self.set_cursor(Qt.CursorShape.SizeAllCursor)
            return
        self.unset_cursor()

    def mousePressEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent | None) -> None:
        assert event is not None
        if not self.crop_mode:
            return super().mousePressEvent(event)

        event.accept()
        for handle in self.crop_handles():
            # Click into a handle?
            if handle().contains(event.pos()):
                self.crop_mode_event_start = event.pos()
                self.crop_mode_move = handle
                return
        for edge in self.crop_edges():
            # Click into an edge handle?
            if edge().contains(event.pos()):
                self.crop_mode_event_start = event.pos()
                self.crop_mode_move = edge
                return
        assert self.crop_temp is not None
        if self.crop_temp.contains(event.pos()):
            self.crop_mode_event_start = event.pos()
            self.crop_mode_move = self.crop_temp
            return
        # Click not in handle, end cropping mode:
        self.exit_crop_mode(confirm=True)

    def mouseDoubleClickEvent(
        self, event: QtWidgets.QGraphicsSceneMouseEvent | None
    ) -> None:
        assert event is not None
        if not self.crop_mode:
            return super().mouseDoubleClickEvent(event)

        event.accept()
        assert self.crop_temp is not None
        if self.crop_temp.contains(event.pos()):
            self.exit_crop_mode(confirm=True)

    def ensure_crop_box_is_inside(self, point: QtCore.QPointF) -> QtCore.QPointF:
        """Returns the modified point that ensures that the crop rectangle is
        still within the pixmap.

        The point passed is assumed to be the top
        left crop rectangle position.
        """
        assert self.crop_temp is not None
        max_x_pos = self._image_width - self.crop_temp.width()
        max_y_pos = self._image_height - self.crop_temp.height()

        if point.x() < 0:
            point.setX(0.0)
        elif point.x() > max_x_pos:
            point.setX(max_x_pos)

        if point.y() < 0:
            point.setY(0.0)
        elif point.y() > max_y_pos:
            point.setY(max_y_pos)
        return point

    def ensure_point_within_crop_bounds(
        self, point: QtCore.QPointF, handle: Callable[[], QtCore.QRectF] | QtCore.QRectF | None
    ) -> QtCore.QPointF:
        """Returns the point, or the nearest point within the pixmap."""
        assert self.crop_temp is not None

        if handle == self.crop_handle_topleft:
            topleft = QtCore.QPointF(0, 0)
            bottomright = self.crop_temp.bottomRight()
        if handle == self.crop_handle_bottomleft:
            topleft = QtCore.QPointF(0, self.crop_temp.top())
            bottomright = QtCore.QPointF(self.crop_temp.right(), self._image_height)
        if handle == self.crop_handle_bottomright:
            topleft = self.crop_temp.topLeft()
            bottomright = QtCore.QPointF(self._image_width, self._image_height)
        if handle == self.crop_handle_topright:
            topleft = QtCore.QPointF(self.crop_temp.left(), 0)
            bottomright = QtCore.QPointF(self._image_width, self.crop_temp.bottom())
        if handle == self.crop_edge_top:
            topleft = QtCore.QPointF(0, 0)
            bottomright = QtCore.QPointF(self._image_width, self.crop_temp.bottom())
        if handle == self.crop_edge_bottom:
            topleft = QtCore.QPointF(0, self.crop_temp.top())
            bottomright = QtCore.QPointF(self._image_width, self._image_height)
        if handle == self.crop_edge_left:
            topleft = QtCore.QPointF(0, 0)
            bottomright = QtCore.QPointF(self.crop_temp.right(), self._image_height)
        if handle == self.crop_edge_right:
            topleft = QtCore.QPointF(self.crop_temp.left(), 0)
            bottomright = QtCore.QPointF(self._image_width, self._image_height)

        point.setX(min(bottomright.x(), max(topleft.x(), point.x())))
        point.setY(min(bottomright.y(), max(topleft.y(), point.y())))

        return point

    def mouseMoveEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent | None) -> None:
        assert event is not None
        if self.crop_mode:
            if self.crop_mode_event_start is None:
                event.accept()
                return
            assert self.crop_temp is not None
            diff = event.pos() - self.crop_mode_event_start
            if self.crop_mode_move == self.crop_temp:
                new = self.ensure_crop_box_is_inside(
                    self.crop_temp.topLeft() + diff
                )
                self.crop_temp.moveTo(new)
            elif self.crop_mode_move == self.crop_handle_topleft:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move
                )
                self.crop_temp.setTopLeft(new)
            elif self.crop_mode_move == self.crop_handle_bottomleft:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomLeft() + diff, self.crop_mode_move
                )
                self.crop_temp.setBottomLeft(new)
            elif self.crop_mode_move == self.crop_handle_bottomright:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomRight() + diff, self.crop_mode_move
                )
                self.crop_temp.setBottomRight(new)
            elif self.crop_mode_move == self.crop_handle_topright:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topRight() + diff, self.crop_mode_move
                )
                self.crop_temp.setTopRight(new)
            elif self.crop_mode_move == self.crop_edge_top:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move
                )
                self.crop_temp.setTop(new.y())
            elif self.crop_mode_move == self.crop_edge_left:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topLeft() + diff, self.crop_mode_move
                )
                self.crop_temp.setLeft(new.x())
            elif self.crop_mode_move == self.crop_edge_bottom:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.bottomLeft() + diff, self.crop_mode_move
                )
                self.crop_temp.setBottom(new.y())
            elif self.crop_mode_move == self.crop_edge_right:
                new = self.ensure_point_within_crop_bounds(
                    self.crop_temp.topRight() + diff, self.crop_mode_move
                )
                self.crop_temp.setRight(new.x())
            self.update()
            self.crop_mode_event_start = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(
        self, event: QtWidgets.QGraphicsSceneMouseEvent | None
    ) -> None:
        assert event is not None
        if self.crop_mode:
            self.crop_mode_move = None
            self.crop_mode_event_start = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)


@register_item
class ZeeTextItem(ZeeItemMixin, QtWidgets.QGraphicsTextItem):
    """Class for markdown text added by the user."""

    TYPE = "text"

    STYLESHEET = """
        body { color: %s; }
        h1, h2, h3, h4, h5, h6 { margin: 4px 0; }
        code { background: rgba(255,255,255,0.1); padding: 1px 3px; }
        pre { background: rgba(255,255,255,0.1); padding: 4px; }
        a { color: #6aeae7; }
    """

    def __init__(self, text: str | None = None, **kwargs: Any) -> None:
        super().__init__()
        self.save_id: str = uuid.uuid4().hex
        self.created_at: float = time.time()
        self.is_image = False
        self.init_selectable()
        self.edit_mode: bool = False
        self._markdown: str = text or "Text"
        self._render_markdown()
        logger.debug(f"Initialized {self}")

    def _render_markdown(self) -> None:
        """Render stored markdown to HTML for display."""
        text_color = "rgb(%d,%d,%d)" % COLORS["Scene:Text"]
        css = self.STYLESHEET % text_color
        html = mistune.html(self._markdown)
        self.setHtml(f"<style>{css}</style>{html}")

    def set_markdown(self, text: str) -> None:
        """Set markdown source and re-render."""
        self._markdown = text
        self._render_markdown()

    @classmethod
    def create_from_data(cls, **kwargs: Any) -> ZeeTextItem:
        data: dict[str, Any] = kwargs.get("data", {})
        item = cls(**data)
        return item

    @classmethod
    def from_snapshot(cls, snap: ItemSnapshot) -> ZeeTextItem:
        """Create a ZeeTextItem from a loaded snapshot."""
        item = cls(snap.data.get("text"))
        item.save_id = snap.save_id
        item.created_at = snap.created_at
        item.setPos(snap.x, snap.y)
        item.setZValue(snap.z)
        item.setScale(snap.scale)
        item.setRotation(snap.rotation)
        if snap.flip != item.flip():
            item.do_flip()
        return item

    def __str__(self) -> str:
        txt = self._markdown[:40]
        return f'Text "{txt}"'

    def get_extra_save_data(self) -> dict[str, Any]:
        return {"text": self._markdown}

    def contains(self, point: QtCore.QPointF) -> bool:
        return self.boundingRect().contains(point)

    def paint(
        self,
        painter: QtGui.QPainter | None,
        option: QtWidgets.QStyleOptionGraphicsItem | None,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        assert painter is not None
        painter.setPen(Qt.PenStyle.NoPen)
        color = QtGui.QColor(0, 0, 0)
        color.setAlpha(40)
        brush = QtGui.QBrush(color)
        painter.setBrush(brush)
        painter.drawRect(QtWidgets.QGraphicsTextItem.boundingRect(self))
        if option is not None:
            option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        super().paint(painter, option, widget)
        self.paint_selectable(painter, option, widget)

    def create_copy(self) -> ZeeTextItem:
        item = ZeeTextItem(self._markdown)
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        if self.flip() == -1:
            item.do_flip()
        return item

    def enter_edit_mode(self) -> None:
        logger.debug(f"Entering edit mode on {self}")
        self.edit_mode = True
        self.old_text = self._markdown
        self.setPlainText(self._markdown)
        self.setDefaultTextColor(QtGui.QColor(*COLORS["Scene:Text"]))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.require_scene().edit_item = self

    def exit_edit_mode(self, commit: bool = True) -> None:
        logger.debug(f"Exiting edit mode on {self}")
        self.edit_mode = False
        # reset selection:
        self.setTextCursor(QtGui.QTextCursor(self.document()))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        scene = self.require_scene()
        scene.edit_item = None
        if commit:
            new_text = self.toPlainText()
            self._markdown = new_text
            self._render_markdown()
            scene.undo_stack.push(commands.ChangeText(self, new_text, self.old_text))
            if not new_text.strip():
                logger.debug("Removing empty text item")
                scene.undo_stack.push(commands.DeleteItems(scene, [self]))
        else:
            self._markdown = self.old_text
            self._render_markdown()

    def has_selection_handles(self) -> bool:
        return super().has_selection_handles() and not self.edit_mode

    def keyPressEvent(self, event: QtGui.QKeyEvent | None) -> None:
        assert event is not None
        if (
            event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
            and event.modifiers() == Qt.KeyboardModifier.ShiftModifier
        ):
            self.exit_edit_mode()
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.exit_edit_mode(commit=False)
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_to_clipboard(self, clipboard: QtGui.QClipboard) -> None:
        clipboard.setText(self._markdown)


@register_item
class ZeeErrorItem(ZeeItemMixin, QtWidgets.QGraphicsTextItem):
    """Class for displaying error messages when an item can't be loaded
    from a zref file.

    This item will be displayed instead of the original item. It won't
    save to zref files. The original item will be preserved in the zref
    file, unless this item gets deleted by the user, or a new zref file
    is saved.
    """

    TYPE = "error"

    def __init__(self, text: str | None = None, **kwargs: Any) -> None:
        super().__init__(text or "Text")
        self.save_id: str = uuid.uuid4().hex
        self.created_at: float = time.time()
        logger.debug(f"Initialized {self}")
        self.is_image = False
        self.init_selectable()
        self.setDefaultTextColor(QtGui.QColor(*COLORS["Scene:Text"]))

    def snapshot(self) -> ErrorItemSnapshot:
        """Error items just preserve the original DB row."""
        return ErrorItemSnapshot(save_id=self.save_id)

    @classmethod
    def create_from_data(cls, **kwargs: Any) -> ZeeErrorItem:
        data: dict[str, Any] = kwargs.get("data", {})
        item = cls(**data)
        return item

    def __str__(self) -> str:
        txt = self.toPlainText()[:40]
        return f'Error "{txt}"'

    def contains(self, point: QtCore.QPointF) -> bool:
        return self.boundingRect().contains(point)

    def paint(
        self,
        painter: QtGui.QPainter | None,
        option: QtWidgets.QStyleOptionGraphicsItem | None,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        assert painter is not None
        painter.setPen(Qt.PenStyle.NoPen)
        color = QtGui.QColor(200, 0, 0)
        brush = QtGui.QBrush(color)
        painter.setBrush(brush)
        painter.drawRect(QtWidgets.QGraphicsTextItem.boundingRect(self))
        if option is not None:
            option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        super().paint(painter, option, widget)
        self.paint_selectable(painter, option, widget)

    def update_from_data(self, **kwargs: Any) -> None:
        self.save_id = kwargs.get("save_id", self.save_id)
        self.setPos(kwargs.get("x", self.pos().x()), kwargs.get("y", self.pos().y()))
        self.setZValue(kwargs.get("z", self.zValue()))
        self.setScale(kwargs.get("scale", self.scale()))
        self.setRotation(kwargs.get("rotation", self.rotation()))

    def create_copy(self) -> ZeeErrorItem:
        item = ZeeErrorItem(self.toPlainText())
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        return item

    def flip(self, *args: Any, **kwargs: Any) -> float:
        """Returns the flip value (1 or -1)"""
        # Never display error messages flipped
        return 1

    def do_flip(self, *args: Any, **kwargs: Any) -> None:
        """Flips the item."""
        # Never flip error messages
        pass

    def copy_to_clipboard(self, clipboard: QtGui.QClipboard) -> None:
        clipboard.setText(self.toPlainText())


@register_item
class ZeePathItem(ZeeItemMixin, QtWidgets.QGraphicsItem):
    """Class for freehand drawing/sketch strokes added by the user."""

    TYPE = "path"

    def __init__(self, strokes: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__()
        self.save_id = uuid.uuid4().hex
        self.created_at = time.time()
        self.filename = None
        self.is_image = False
        self.strokes = strokes or []
        self.temp_stroke: dict[str, Any] | None = None
        self._cached_rect = QtCore.QRectF(0, 0, 1, 1)
        self._cache_pixmap: QtGui.QPixmap | None = None
        self.init_selectable()
        self.setZValue(1e9)
        logger.debug(f"Initialized {self}")

    def setZValue(self, z: float) -> None:
        # Drawings should always be on top of images. We enforce this by
        # keeping their Z-values extremely high (>1e9), and we bypass
        # BaseItemMixin.setZValue so we don't skew the scene's max_z for images.
        if z < 1e9:
            z += 1e9
        QtWidgets.QGraphicsItem.setZValue(self, z)

    def bring_to_front(self) -> None:
        scene = self.zee_scene()
        if scene:
            max_path_z = 1e9
            for item in scene.items():
                if isinstance(item, ZeePathItem) and item is not self:
                    max_path_z = max(max_path_z, item.zValue())
            self.setZValue(max_path_z + scene.Z_STEP)

    @classmethod
    def from_snapshot(cls, snap: ItemSnapshot) -> ZeePathItem:
        item = cls(strokes=snap.data.get("strokes", []))
        item.save_id = snap.save_id
        item.created_at = snap.created_at
        item.setPos(snap.x, snap.y)
        item.setZValue(snap.z)
        item.setScale(snap.scale)
        item.setRotation(snap.rotation)
        if snap.flip != item.flip():
            item.do_flip()
        item._update_bounding_rect()
        item._invalidate_cache()
        return item

    def __str__(self) -> str:
        n = len(self.strokes)
        return f'Path ({n} stroke{"s" if n != 1 else ""})'

    def get_extra_save_data(self) -> dict[str, Any]:
        return {"strokes": self.strokes}

    def create_copy(self) -> ZeePathItem:
        item = ZeePathItem(strokes=copy.deepcopy(self.strokes))
        item.setPos(self.pos())
        item.setZValue(self.zValue())
        item.setScale(self.scale())
        item.setRotation(self.rotation())
        if self.flip() == -1:
            item.do_flip()
        item._update_bounding_rect()
        item._invalidate_cache()
        return item

    def contains(self, point: QtCore.QPointF) -> bool:
        return self.boundingRect().contains(point)

    def bounding_rect_unselected(self) -> QtCore.QRectF:
        rect = QtCore.QRectF(self._cached_rect)
        if self.temp_stroke:
            base_size = self.temp_stroke.get("base_size", 10)
            for pt in self.temp_stroke.get("points", []):
                r = base_size * pt.get("pressure", 1.0) / 2 + 1
                rect = rect.united(
                    QtCore.QRectF(pt["x"] - r, pt["y"] - r, 2 * r, 2 * r)
                )
        return rect

    def add_stroke(self, stroke: dict[str, Any]) -> None:
        self.prepareGeometryChange()
        self.strokes.append(stroke)
        self._update_bounding_rect()
        self._invalidate_cache()
        self.update()

    def undo_stroke(self) -> None:
        if self.strokes:
            old_rect = self.sceneBoundingRect()
            self.prepareGeometryChange()
            self.strokes.pop()
            self._update_bounding_rect()
            self._invalidate_cache()
            self.update()
            scene = self.scene()
            if scene:
                scene.update(old_rect)

    def _invalidate_cache(self) -> None:
        self._cache_pixmap = None

    def _update_bounding_rect(self) -> None:
        if not self.strokes:
            self._cached_rect = QtCore.QRectF(0, 0, 1, 1)
            return
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        max_r = 0.0
        for stroke in self.strokes:
            base_size = stroke.get("base_size", 10)
            for pt in stroke.get("points", []):
                r = base_size * pt.get("pressure", 1.0) / 2
                if r > max_r:
                    max_r = r
                x = pt["x"]
                y = pt["y"]
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
        pad = max_r + 1
        self._cached_rect = QtCore.QRectF(
            min_x - pad, min_y - pad, (max_x - min_x) + 2 * pad, (max_y - min_y) + 2 * pad
        )

    def _paint_stroke(self, painter: QtGui.QPainter, stroke: dict[str, Any]) -> None:
        color_data = stroke.get("color", [0, 0, 0, 255])
        color = QtGui.QColor(*color_data)
        base_size = stroke.get("base_size", 10)
        points = stroke.get("points", [])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(color))

        for i, pt in enumerate(points):
            pressure = pt.get("pressure", 1.0)
            radius = base_size * pressure / 2
            painter.drawEllipse(QtCore.QPointF(pt["x"], pt["y"]), radius, radius)
            if i > 0:
                prev = points[i - 1]
                dx = pt["x"] - prev["x"]
                dy = pt["y"] - prev["y"]
                dist = math.hypot(dx, dy)
                if dist > 1:
                    steps = int(dist)
                    for s in range(1, steps):
                        t = s / dist
                        ix = prev["x"] + dx * t
                        iy = prev["y"] + dy * t
                        prev_p = prev.get("pressure", 1.0)
                        ip = prev_p + (pressure - prev_p) * t
                        ir = base_size * ip / 2
                        painter.drawEllipse(QtCore.QPointF(ix, iy), ir, ir)

    def _ensure_cache(self) -> None:
        if self._cache_pixmap is not None:
            return
        if not self.strokes:
            return
        rect = self._cached_rect
        w = max(1, int(math.ceil(rect.width())))
        h = max(1, int(math.ceil(rect.height())))
        pixmap = QtGui.QPixmap(w, h)
        pixmap.fill(QtGui.QColor(0, 0, 0, 0))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.translate(-rect.x(), -rect.y())
        for stroke in self.strokes:
            self._paint_stroke(painter, stroke)
        painter.end()
        self._cache_pixmap = pixmap

    def paint(
        self,
        painter: QtGui.QPainter | None,
        option: QtWidgets.QStyleOptionGraphicsItem | None,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        assert painter is not None
        if self.strokes:
            self._ensure_cache()
            if self._cache_pixmap is not None:
                painter.drawPixmap(
                    QtCore.QPointF(self._cached_rect.x(), self._cached_rect.y()),
                    self._cache_pixmap,
                )

        if self.temp_stroke:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            self._paint_stroke(painter, self.temp_stroke)

        self.paint_selectable(painter, option, widget)

    def copy_to_clipboard(self, clipboard: QtGui.QClipboard) -> None:
        pass
