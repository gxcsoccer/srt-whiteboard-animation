import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from annotation_validation import annotation_errors, ordered_elements, validate_annotation  # noqa: E402


class AnnotationValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = json.loads(
            (ROOT / "examples" / "scene-01-monkey-mountain-banana.annotation.json").read_text(
                encoding="utf-8"
            )
        )

    def test_repository_sample_is_valid(self):
        validate_annotation(self.sample, image_size=(1672, 941))

    def test_order_uses_sequence_instead_of_array_position_or_start_time(self):
        elements = deepcopy(self.sample["elements"])
        elements.reverse()
        elements[0]["reveal"]["startMs"] = 0
        self.assertEqual([e["sequence"] for e in ordered_elements(elements)], [1, 2, 3])

    def test_overlap_and_out_of_bounds_are_reported(self):
        broken = deepcopy(self.sample)
        broken["elements"][1]["reveal"]["startMs"] = 100
        broken["elements"][2]["region"]["width"] = 1000
        errors = annotation_errors(broken, image_size=(1672, 941))
        self.assertTrue(any("时间重叠" in error for error in errors))
        self.assertTrue(any("右边界" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
