# PureRef-format library (MIT License)
# Source: https://github.com/FyorDev/PureRef-format
# Vendored for BuzzRef PureRef file import support

import struct
import colorsys
from .items import Item, PurImage, PurGraphicsImageItem, PurGraphicsTextItem

GRAPHICS_IMAGE_ITEM = 34
GRAPHICS_TEXT_ITEM = 32


def read_pur_file(pur_file, filepath: str):
    """Read a PureRef .pur file into a PurFile object."""
    pur_file.unsupported_metadata = False

    with open(filepath, "rb") as f:
        header = f.read(100)
        if b"SQLite format 3" in header:
            raise ValueError("PureRef 2.x format is not supported")
        f.seek(0)
        pur_bytes = f.read()
    
    offset = 0
    read_pin = 0
    image_items: list[PurGraphicsImageItem] = []

    def erase(length):
        """Advance the offset by length"""
        nonlocal offset, read_pin
        offset += length
        read_pin += length

    def unpack(typ: str, begin: int, stop: int):
        """Bytes to type"""
        return struct.unpack(typ, pur_bytes[offset + begin : offset + stop])[0]

    def unpack_erase(typ: str):
        """Unpack typ and remove from pur_bytes"""
        size = struct.calcsize(typ)
        val = struct.unpack(typ, pur_bytes[offset : offset + size])[0]
        erase(size)
        return val

    def unpack_matrix():
        """Unpack and delete a matrix"""
        matrix = [
            unpack(">d", 0, 8),
            unpack(">d", 8, 16),
            unpack(">d", 24, 32),
            unpack(">d", 32, 40)
        ]
        erase(48)
        return matrix

    def unpack_rgb():
        rgb = [
            unpack_erase(">H"),
            unpack_erase(">H"),
            unpack_erase(">H")
        ]
        return rgb

    def hsv_to_rgb(hsv):
        rgb = list(colorsys.hsv_to_rgb(
            hsv[0] / 35900, hsv[1] / 65535, hsv[2] / 65535))
        rgb = [int(i * 65535) for i in rgb]
        return rgb

    def unpack_string():
        length = unpack_erase(">I")
        string = pur_bytes[offset : offset + length].decode("utf-16-be", errors="replace")
        erase(length)
        return string

    def read_header():
        pur_file.canvas = [
            unpack('>d', 112, 120),
            unpack('>d', 120, 128),
            unpack('>d', 128, 136),
            unpack('>d', 136, 144)
        ]
        pur_file.zoom = unpack('>d', 144, 152)
        pur_file.xCanvas = unpack('>i', 216, 220)
        pur_file.yCanvas = unpack('>i', 220, 224)
        erase(224)

    def read_images():
        png_head = bytes([137, 80, 78, 71, 13, 10, 26, 10])
        png_foot = bytes([0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])

        while True:
            start_abs = pur_bytes.find(png_head, offset)
            if start_abs == -1:
                break
            
            start_rel = start_abs - offset

            if start_rel >= 4:
                num_chunks = start_rel // 4
                if num_chunks > 10000:
                    raise ValueError("Suspiciously large gap between images, aborting.")
                for i in range(num_chunks):
                    image_add = PurImage()
                    image_add.address = [read_pin + i*4, 4 + read_pin + i*4]
                    image_add.pngBinary = pur_bytes[offset + i*4 : offset + i*4 + 4]
                    pur_file.images.append(image_add)
                
                erase(num_chunks * 4)
                continue
            
            foot_idx = pur_bytes.find(png_foot, start_abs)
            if foot_idx == -1:
                end_abs = start_abs + len(png_head)
            else:
                end_abs = foot_idx + len(png_foot)
                
            end_rel = end_abs - offset
            
            image_add = PurImage()
            image_add.address = [start_rel + read_pin, end_rel + read_pin]
            image_add.pngBinary = pur_bytes[start_abs : end_abs]
            pur_file.images.append(image_add)
            erase(end_rel)

        while not (unpack(">I", 8, 12) == GRAPHICS_IMAGE_ITEM or
                   unpack(">I", 8, 12) == GRAPHICS_TEXT_ITEM):
            image_add = PurImage()
            image_add.address = [read_pin, 4 + read_pin]
            image_add.pngBinary = pur_bytes[offset : offset + 4]
            pur_file.images.append(image_add)
            erase(4)

    def read_items():
        def unpack_graphics_text_item():
            transform_end = unpack(">Q", 0, 8)
            text_transform = PurGraphicsTextItem()
            erase(12 + unpack(">I", 8, 12))

            text_transform.text = unpack_string()
            text_transform.matrix = unpack_matrix()
            text_transform.x = unpack_erase(">d")
            text_transform.y = unpack_erase(">d")
            erase(8)

            text_transform.id = unpack_erase(">I")
            text_transform.zLayer = unpack_erase(">d")

            is_hsv = unpack_erase('>b') == 2
            text_transform.opacity = unpack_erase(">H")
            text_transform.rgb = unpack_rgb()
            if is_hsv:
                text_transform.rgb = hsv_to_rgb(text_transform.rgb)

            erase(2)

            is_background_hsv = unpack_erase(">b") == 2
            text_transform.opacityBackground = unpack_erase(">H")
            text_transform.rgbBackground = unpack_rgb()
            if is_background_hsv:
                bg = text_transform.rgbBackground
                text_transform.rgbBackground = hsv_to_rgb(bg)

            number_of_children = unpack(">I", 2, 6)
            erase(transform_end - read_pin)

            if number_of_children > 0:
                add_text_children(text_transform, number_of_children)

            return text_transform

        def unpack_graphics_image_item():
            transform_end = unpack(">Q", 0, 8)
            transform = PurGraphicsImageItem()
            erase(12 + unpack(">I", 8, 12))

            brute_force_loaded = False
            if unpack(">I", 0, 4) == 0:
                brute_force_loaded = True
                erase(4)

            if unpack(">i", 0, 4) == -1:
                erase(4)
            else:
                transform.source = unpack_string()

            if not brute_force_loaded:
                if unpack(">i", 0, 4) == -1:
                    erase(4)
                else:
                    transform.name = unpack_string()

            erase(8)

            transform.matrix = unpack_matrix()
            transform.x = unpack_erase(">d")
            transform.y = unpack_erase(">d")
            erase(8)

            transform.id = unpack_erase(">I")
            transform.zLayer = unpack_erase(">d")
            transform.matrixBeforeCrop = unpack_matrix()
            transform.xCrop = unpack_erase(">d")
            transform.yCrop = unpack_erase(">d")
            transform.scaleCrop = unpack_erase(">d")

            point_count = unpack_erase(">I")
            transform.points = [[], []]

            for _ in range(point_count):
                erase(4)
                transform.points[0].append(unpack_erase(">d"))
                transform.points[1].append(unpack_erase(">d"))

            number_of_children = unpack(">I", 21, 25)
            erase(transform_end - read_pin)

            add_text_children(transform, number_of_children)

            return transform

        def add_text_children(parent: Item, number_of_children: int):
            for _ in range(number_of_children):
                text = unpack_graphics_text_item()
                parent.textChildren.append(text)

        while (unpack(">I", 8, 12) == GRAPHICS_IMAGE_ITEM or
               unpack(">I", 8, 12) == GRAPHICS_TEXT_ITEM):
            if unpack(">I", 8, 12) == GRAPHICS_IMAGE_ITEM:
                image_items.append(unpack_graphics_image_item())
            elif unpack(">I", 8, 12) == GRAPHICS_TEXT_ITEM:
                pur_file.text.append(unpack_graphics_text_item())
            else:
                break

    def is_duplicate(img):
        return (len(img.pngBinary) == 4 and
                img.pngBinary != b'\xFF\xFF\xFF\xFF')

    try:
        read_header()
        read_images()
        read_items()

        pur_file.folderLocation = unpack_string()

        items_by_id = {}
        for item in image_items:
            items_by_id.setdefault(item.id, []).append(item)

        images_by_address = {}
        for image in pur_file.images:
            images_by_address.setdefault(image.address[0], []).append(image)

        for _ in range(len(image_items)):
            ref_id = unpack(">I", 0, 4)
            ref_address = [unpack(">Q", 4, 12), unpack(">Q", 12, 20)]
            
            for item in items_by_id.get(ref_id, []):
                for image in images_by_address.get(ref_address[0], []):
                    image.transforms = [item]
            
            erase(20)

        other_images_by_first_tid = {}
        for other_image in pur_file.images:
            if len(other_image.transforms) > 0:
                tid = other_image.transforms[0].id
                other_images_by_first_tid.setdefault(tid, []).append(other_image)

        for image in pur_file.images:
            if is_duplicate(image):
                dup_id = struct.unpack('>I', image.pngBinary)[0]
                for other_image in other_images_by_first_tid.get(dup_id, []):
                    other_image.transforms += image.transforms

    except struct.error:
        raise ValueError("Unsupported PureRef file format or corrupted file.")

    pur_file.images = [
        image for image in pur_file.images
        if not is_duplicate(image)
    ]
