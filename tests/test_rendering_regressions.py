import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_annotation_preview  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import stream_render as sr  # noqa: E402


class _MemoryWriter:
    def __init__(self, *_args, **_kwargs):
        self.frames = []
        self.released = False

    def isOpened(self):
        return True

    def write(self, frame):
        self.frames.append(frame.copy())

    def release(self):
        self.released = True


class RenderingRegressionTests(unittest.TestCase):
    def test_annotation_preview_uses_cross_platform_font_fallback(self):
        image = ROOT / "examples" / "scene-01-monkey-mountain-banana.png"
        annotation = ROOT / "examples" / "scene-01-monkey-mountain-banana.annotation.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "preview.png"
            with mock.patch.object(render_annotation_preview, "FONT_CANDIDATES", ()), mock.patch.dict(
                os.environ, {"SRT_WB_FONT": "/font/does/not/exist.ttf"}
            ):
                render_annotation_preview.main(str(image), str(annotation), str(output))
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    def test_blank_region_does_not_raise_type_error(self):
        image = np.full((100, 160, 3), 245, dtype=np.uint8)
        annotation = {
            "canvas": {"width": 160, "height": 100},
            "sceneDurationMs": 700,
            "elements": [
                {
                    "sequence": 1,
                    "region": {"x": 0, "y": 0, "width": 160, "height": 100},
                    "reveal": {
                        "direction": "top_to_bottom",
                        "startMs": 0,
                        "durationMs": 200,
                        "protectedRegions": [],
                    },
                }
            ],
        }
        cfg = sr.Config(fps=5, cap_long_edge=160, grid_edge=8, ink_path_mode="grid")
        renderer = render_stream_whiteboard.RegionStreamRenderer(image, annotation, cfg, None, True)
        writer = _MemoryWriter()
        with mock.patch.object(render_stream_whiteboard.cv2, "VideoWriter", return_value=writer):
            renderer.render_to(Path("unused.mp4"), 700)
        self.assertTrue(writer.released)
        self.assertGreater(len(writer.frames), 0)


if __name__ == "__main__":
    unittest.main()
