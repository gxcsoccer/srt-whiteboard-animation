import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from parse_srt import group_scenes, parse_srt  # noqa: E402


class ParseSrtTests(unittest.TestCase):
    def test_parse_multiline_and_dot_milliseconds(self):
        cues = parse_srt(
            "\ufeff1\n00:00:00.000 --> 00:00:02,500\n第一行\n第二行\n\n"
            "2\n00:00:02,500 --> 00:00:04,000\n结束\n"
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "第一行 第二行")
        self.assertEqual(cues[0]["endMs"], 2500)

    def test_group_scenes_does_not_exceed_max_when_a_boundary_exists(self):
        cues = [
            {"index": 1, "startMs": 0, "endMs": 12000, "text": "一"},
            {"index": 2, "startMs": 12000, "endMs": 26000, "text": "二"},
            {"index": 3, "startMs": 26000, "endMs": 40000, "text": "三"},
        ]
        scenes = group_scenes(cues, target_sec=30, min_sec=25, max_sec=35)
        self.assertEqual([scene["cueRange"] for scene in scenes], [[1, 2], [3, 3]])


if __name__ == "__main__":
    unittest.main()
