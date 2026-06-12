import os.path
from pathlib import Path

import httpretty
import pytest

from PyQt6 import QtCore

from zeeref.fileio.image import load_pil, load_pil_from_source


def test_load_pil_without_path(qapp):
    img = load_pil(Path("nonexistent"))
    assert img is None


def test_load_pil_not_a_file(qapp):
    img = load_pil(Path("foo"))
    assert img is None


@pytest.mark.parametrize(
    "path,expected",
    [
        ("test3x3.png", "test3x3.png"),
        ("test3x3_orientation1.jpg", "test3x3.jpg"),
        ("test3x3_orientation2.jpg", "test3x3.jpg"),
        ("test3x3_orientation3.jpg", "test3x3.jpg"),
        ("test3x3_orientation4.jpg", "test3x3.jpg"),
        ("test3x3_orientation5.jpg", "test3x3.jpg"),
        ("test3x3_orientation6.jpg", "test3x3.jpg"),
        ("test3x3_orientation7.jpg", "test3x3.jpg"),
        ("test3x3_orientation8.jpg", "test3x3.jpg"),
    ],
)
def test_load_pil_exif_orientation(path, expected, qapp):
    def get_fname(p):
        root = os.path.dirname(__file__)
        return Path(os.path.join(root, "..", "assets", p))

    img = load_pil(get_fname(path))
    assert img is not None
    expected_img = load_pil(get_fname(expected))
    assert expected_img is not None

    # Compare pixel values — JPEG isn't pixel-perfect
    img_rgb = img.convert("RGB")
    expected_rgb = expected_img.convert("RGB")
    for x in range(3):
        for y in range(3):
            p1: tuple[int, ...] = img_rgb.getpixel((x, y))  # type: ignore[assignment]
            p2: tuple[int, ...] = expected_rgb.getpixel((x, y))  # type: ignore[assignment]
            diff = sum((a - b) ** 2 for a, b in zip(p1, p2))
            assert diff < 9


def test_load_pil_from_source_loads_from_filename(view, imgfilename3x3):
    img, filename = load_pil_from_source(Path(imgfilename3x3))
    assert img is not None
    assert filename == imgfilename3x3


def test_load_pil_from_source_loads_from_nonexisting_filename(view):
    img, filename = load_pil_from_source(Path("foo.png"))
    assert img is None
    assert filename == "foo.png"


def test_load_pil_from_source_loads_from_existing_local_url(view, imgfilename3x3):
    url = QtCore.QUrl.fromLocalFile(imgfilename3x3)
    img, filename = load_pil_from_source(url)
    assert img is not None
    assert filename == imgfilename3x3


@httpretty.activate
def test_load_pil_from_source_loads_from_existing_web_url(view, imgdata3x3):
    url = "http://example.com/foo.png"
    httpretty.register_uri(
        httpretty.GET,
        url,
        body=imgdata3x3,
    )
    img, filename = load_pil_from_source(QtCore.QUrl(url))
    assert img is not None
    assert filename == url


@httpretty.activate
def test_load_pil_from_source_web_url_gif(view):
    from PIL import Image
    from io import BytesIO
    frames = [
        Image.new("RGB", (10, 10), (255, 0, 0)),
        Image.new("RGB", (10, 10), (0, 255, 0)),
    ]
    gif_buf = BytesIO()
    frames[0].save(
        gif_buf,
        save_all=True,
        append_images=frames[1:],
        format="GIF",
        loop=0,
        duration=100,
    )
    gif_data = gif_buf.getvalue()

    url = "http://example.com/anim.gif"
    httpretty.register_uri(
        httpretty.GET,
        url,
        body=gif_data,
    )
    img, filename = load_pil_from_source(QtCore.QUrl(url))
    assert img is not None
    assert img.format == "GIF"
    assert getattr(img, "is_animated", False) is True
    assert getattr(img, "n_frames", 1) == 2
    assert hasattr(img, "custom_raw_bytes")
    assert len(img.custom_raw_bytes) == len(gif_data)
    assert filename == url


@httpretty.activate
def test_load_pil_from_source_loads_from_existing_web_url_non_ascii(view, imgdata3x3):
    url = "http://example.com/föö.png"
    httpretty.register_uri(
        httpretty.GET,
        url,
        body=imgdata3x3,
    )
    img, filename = load_pil_from_source(QtCore.QUrl(url))
    assert img is not None
    assert filename == "http://example.com/f%C3%B6%C3%B6.png"


@httpretty.activate
def test_load_pil_from_source_loads_from_web_url_errors(view):
    url = "http://example.com/foo.png"
    httpretty.register_uri(
        httpretty.GET,
        url,
        status=500,
    )
    img, filename = load_pil_from_source(QtCore.QUrl(url))
    assert img is None
    assert filename == url


def test_load_pil_animated_gif(qapp, tmp_path):
    from PIL import Image
    frames = [
        Image.new("RGB", (10, 10), (255, 0, 0)),
        Image.new("RGB", (10, 10), (0, 255, 0)),
    ]
    gif_file = tmp_path / "test_anim.gif"
    frames[0].save(
        gif_file,
        save_all=True,
        append_images=frames[1:],
        format="GIF",
        loop=0,
        duration=100,
    )

    img = load_pil(gif_file)
    assert img is not None
    assert img.format == "GIF"
    assert getattr(img, "is_animated", False) is True
    assert getattr(img, "n_frames", 1) == 2
    assert hasattr(img, "custom_raw_bytes")
    assert len(img.custom_raw_bytes) > 0


def test_pureref_import_gif(qapp, tmp_path):
    from unittest.mock import MagicMock, patch
    from zeeref.fileio.pureref import PureRefIO

    pur_file_mock = MagicMock()
    pur_image = MagicMock()
    pur_image.pngBinary = b"fake_gif_binary"
    pur_image.transforms = [
        MagicMock(matrix=[1.0, 0.0, 0.0, 1.0], zLayer=1.0, x=100.0, y=200.0, source="dummy_src")
    ]
    pur_file_mock.images = [pur_image]
    pur_file_mock.unsupported_metadata = False

    pil_img_mock = MagicMock()
    pil_img_mock.size = (200, 100)
    pil_img_mock.format = "GIF"
    pil_img_mock.is_animated = True

    scene_mock = MagicMock()
    scene_mock._scratch_file = str(tmp_path / "scratch.db")

    with patch("zeeref.fileio.vendor.purformat.PurFile", return_value=pur_file_mock), \
         patch("PIL.Image.open", return_value=pil_img_mock) as mock_open, \
         patch("zeeref.fileio.pureref._insert_image") as mock_insert:

        reader = PureRefIO("dummy.pur", scene_mock)
        reader.read()

        mock_open.assert_called_once()
        assert pil_img_mock.custom_raw_bytes == b"fake_gif_binary"

        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args[1]
        assert call_kwargs.get("raw_bytes") == b"fake_gif_binary"
