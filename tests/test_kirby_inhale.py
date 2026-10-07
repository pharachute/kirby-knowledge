"""Kirby inhale animation: the three frames must be a real opening sequence.

The frames are decoded with the standard library only (zlib + PNG unfiltering), so the test
proves the sequence from the actual shipped pixels: the mouth area grows from inhale 1 to 3,
and the frames really differ from the idle frame.
"""

from __future__ import annotations

import pathlib
import unittest
import urllib.error
import urllib.request
import zlib

from personal_memory.web import views as views_module

from .llm_fakes import response_for
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD

HIGH_VALUE = response_for(VALID_CAPTURE_PAYLOAD)


class KirbyInhaleSequenceTest(WebTestCase):
    """吸入帧必须是真实、逐级张开的序列，而不是同一张图重复。"""

    prefix = "pms-inhale-"

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(HIGH_VALUE))

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _rows(path: pathlib.Path) -> tuple[int, int, list[bytearray]]:
        """Decode an 8-bit RGBA PNG into unfiltered rows (no third-party dependency)."""
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", path
        position, width, height, idat = 8, 0, 0, bytearray()
        while position < len(data):
            length = int.from_bytes(data[position:position + 4], "big")
            kind = data[position + 4:position + 8]
            chunk = data[position + 8:position + 8 + length]
            if kind == b"IHDR":
                width = int.from_bytes(chunk[0:4], "big")
                height = int.from_bytes(chunk[4:8], "big")
                assert (chunk[8], chunk[9]) == (8, 6), "expected 8-bit RGBA"
            elif kind == b"IDAT":
                idat += chunk
            elif kind == b"IEND":
                break
            position += 12 + length
        raw = zlib.decompress(bytes(idat))
        stride = width * 4
        rows: list[bytearray] = []
        previous = bytearray(stride)
        offset = 0
        for _ in range(height):
            filter_type = raw[offset]
            offset += 1
            line = bytearray(raw[offset:offset + stride])
            offset += stride
            if filter_type == 1:
                for index in range(4, stride):
                    line[index] = (line[index] + line[index - 4]) & 0xFF
            elif filter_type == 2:
                for index in range(stride):
                    line[index] = (line[index] + previous[index]) & 0xFF
            elif filter_type == 3:
                for index in range(stride):
                    left = line[index - 4] if index >= 4 else 0
                    line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
            elif filter_type == 4:
                for index in range(stride):
                    a = line[index - 4] if index >= 4 else 0
                    b = previous[index]
                    c = previous[index - 4] if index >= 4 else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[index] = (line[index] + predictor) & 0xFF
            elif filter_type != 0:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            rows.append(line)
            previous = line
        return width, height, rows

    def _dark_mouth_pixels(self, name: str) -> int:
        """Dark pixels inside the mouth region only (right-lower part of the sprite).

        Counting the whole frame would be dominated by Kirby's black outline, which does not
        change between frames; the mouth region isolates the actual inhale opening.
        """
        width, height, rows = self._rows(views_module.KIRBY_ASSET_DIR / name)
        x_from, x_to = int(width * 0.50), int(width * 0.98)
        y_from, y_to = int(height * 0.30), int(height * 0.90)
        dark = 0
        for y in range(y_from, y_to):
            line = rows[y]
            for x in range(x_from, x_to):
                index = x * 4
                red, green, blue, alpha = line[index], line[index + 1], line[index + 2], line[index + 3]
                if alpha > 8 and red < 150 and green < 95 and blue < 115:
                    dark += 1
        return dark

    def _opaque_pixels(self, name: str) -> int:
        _, _, rows = self._rows(views_module.KIRBY_ASSET_DIR / name)
        return sum(1 for line in rows for index in range(3, len(line), 4) if line[index] > 8)

    # -- tests ------------------------------------------------------------
    def test_inhale_frames_open_progressively(self) -> None:
        counts = {
            name: self._dark_mouth_pixels(name)
            for name in (
                "kirby-idle.png",
                "kirby-open.png",
                "kirby-inhale-1.png",
                "kirby-inhale-2.png",
                "kirby-inhale-3.png",
            )
        }
        self.assertGreater(counts["kirby-inhale-1.png"], 0, counts)
        self.assertLess(counts["kirby-inhale-1.png"], counts["kirby-inhale-2.png"], counts)
        self.assertLess(counts["kirby-inhale-2.png"], counts["kirby-inhale-3.png"], counts)
        # a real, clearly bigger mouth than the idle frame's
        self.assertGreater(counts["kirby-inhale-3.png"], counts["kirby-idle.png"] * 2, counts)
        self.assertGreater(counts["kirby-inhale-1.png"], counts["kirby-idle.png"], counts)

    def test_frames_are_transparent_and_large_enough(self) -> None:
        for name in ("kirby-idle.png", "kirby-open.png", "kirby-inhale-1.png",
                     "kirby-inhale-2.png", "kirby-inhale-3.png"):
            width, height, rows = self._rows(views_module.KIRBY_ASSET_DIR / name)
            self.assertGreaterEqual(width, 300, name)
            self.assertGreaterEqual(height, 300, name)
            opaque = self._opaque_pixels(name)
            # the sprite occupies part of the canvas; a fully opaque frame would be a white box
            self.assertLess(opaque, width * height, name)
            self.assertGreater(opaque, 1000, name)

    def test_pages_and_assets_expose_the_sequence(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("kirby-inhale-1.png", body)
        self.assertIn("kirby-inhale-2.png", body)
        self.assertIn("kirby-inhale-3.png", body)
        for index in (1, 2, 3):
            request = urllib.request.Request(f"{self.base}/assets/kirby-inhale-{index}.png")
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
                self.assertEqual(response.status, 200, index)
                self.assertEqual(response.headers.get("Content-Type"), "image/png", index)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", index)
        # the removed hover frame is no longer served
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(f"{self.base}/assets/kirby-inhale.png", timeout=15)
        self.assertEqual(context.exception.code, 404)

    def test_animation_timing_and_state_wiring(self) -> None:
        _, body = self.get("/")
        self.assertEqual(views_module.KIRBY_INHALE_ORDER, ("inhale1", "inhale2", "inhale3"))
        self.assertLessEqual(views_module.KIRBY_INHALE_SECONDS, 0.6)   # 快，不拖沓
        self.assertIn("@keyframes kirby-inhale-a", body)
        self.assertIn("@keyframes kirby-inhale-c", body)
        for index in (1, 2, 3):
            self.assertIn(f'[data-kirby-state="inhale"] .kirby-frame-inhale{index}', body)
        self.assertIn("@keyframes kirby-intake", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
