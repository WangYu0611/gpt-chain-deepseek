import base64
import copy
import hashlib
import json
import unittest
import zlib

from art_tools.blender import roof_repair as repair

try:
    import numpy as np
except ImportError:
    np = None


def blob(raw):
    return {"encoding": "zlib-base64", "sha256": hashlib.sha256(raw).hexdigest(),
            "data": base64.b64encode(zlib.compress(raw)).decode()}


class FrozenPlanTests(unittest.TestCase):
    def test_unknown_source_does_not_touch_object(self):
        self.assertIsNone(repair.apply_roof_repair(None, source_sha256="0" * 64, level="L1", output_dir="unused"))

    def test_known_source_wrong_level_is_rejected(self):
        source = next(iter(json.loads(repair.PLAN_PATH.read_bytes())["plans"]))
        with self.assertRaisesRegex(ValueError, "source/level"):
            repair.load_plan(source, "L8")

    def test_all_frozen_masks_and_reference_arrays_are_complete(self):
        for source, plan in json.loads(repair.PLAN_PATH.read_bytes())["plans"].items():
            self.assertEqual(len(source), 64)
            repair._decode(plan["reference_coordinates"], plan["vertex_count"] * 12)
            repair._decode(plan["downward_displacements"], plan["vertex_count"] * 4)
            self.assertEqual(sum(r["vertex_count"] for r in plan["regions"]), plan["selected_vertex_count"])
            for region in plan["regions"]:
                repair._decode(region["vertex_indices"], region["vertex_count"] * 4)

    def test_corrupt_array_rejected(self):
        value = blob(b"abcd")
        value["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            repair._decode(value, 4)

    def test_oversized_array_rejected(self):
        with self.assertRaisesRegex(ValueError, "length"):
            repair._decode(blob(b"a" * 1000), 4)

    def test_trailing_compressed_stream_rejected(self):
        value = blob(b"abcd")
        value["data"] = base64.b64encode(zlib.compress(b"abcd") + zlib.compress(b"abcd")).decode()
        with self.assertRaisesRegex(ValueError, "length"):
            repair._decode(value, 4)


@unittest.skipIf(np is None, "Blender's bundled Python supplies NumPy")
class ArrayRepairTests(unittest.TestCase):
    def setUp(self):
        self.co = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 1], [0, 1, 1]], dtype="<f4")
        self.tri = np.array([[0, 1, 2], [0, 2, 3]], dtype="<i4")
        self.uv = np.array([0, 0, 1, 0, 1, 1, 0, 1], dtype="<f4")
        self.plan = {"vertex_count": 4, "triangle_count": 2, "selected_vertex_count": 2,
                     "reference_coordinates": blob(self.co.tobytes()),
                     "downward_displacements": blob(np.array([0, 0, .1, .1], dtype="<f4").tobytes()),
                     "triangles_sha256": hashlib.sha256(self.tri.tobytes()).hexdigest(),
                     "uv_sha256": hashlib.sha256(self.uv.tobytes()).hexdigest(), "regions": []}

    def run_repair(self, co=None, tri=None, uv=None, plan=None):
        return repair.repair_arrays(self.co if co is None else co, self.tri if tri is None else tri,
                                    self.uv if uv is None else uv, self.plan if plan is None else plan)

    def test_only_masked_z_changes_and_inputs_are_untouched(self):
        before = self.co.copy()
        result, audit = self.run_repair()
        np.testing.assert_array_equal(self.co, before)
        np.testing.assert_array_equal(result[:2], before[:2])
        np.testing.assert_array_equal(result[:, :2], before[:, :2])
        self.assertEqual(audit["unselected_before_sha256"], audit["unselected_after_sha256"])
        self.assertEqual(audit["opposed_triangle_normal_count"], 0)

    def test_same_source_uniform_scale_supported(self):
        result, audit = self.run_repair(co=self.co * 2)
        self.assertAlmostEqual(float(result[2, 2]), 1.8, places=6)
        self.assertEqual(audit["uniform_scale_relative_to_plan"], 2)

    def test_double_application_fails(self):
        result, _ = self.run_repair()
        with self.assertRaisesRegex(ValueError, "reference coordinates"):
            self.run_repair(co=result)

    def test_nonuniform_scale_fails(self):
        modified = self.co.copy()
        modified[:, 2] *= 2
        with self.assertRaisesRegex(ValueError, "reference coordinates"):
            self.run_repair(co=modified)

    def test_topology_and_uv_changes_fail(self):
        with self.assertRaisesRegex(ValueError, "topology"):
            self.run_repair(tri=self.tri[::-1].copy())
        with self.assertRaisesRegex(ValueError, "UV"):
            self.run_repair(uv=self.uv + .01)

    def test_invalid_displacement_fails_before_mutation(self):
        plan = copy.deepcopy(self.plan)
        plan["downward_displacements"] = blob(np.array([0, 0, -.1, .1], dtype="<f4").tobytes())
        with self.assertRaisesRegex(ValueError, "negative"):
            self.run_repair(plan=plan)


if __name__ == "__main__":
    unittest.main()
