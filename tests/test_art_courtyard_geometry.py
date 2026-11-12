"""Geometric contracts for free, deterministic courtyard maintenance."""
import math
import unittest

from art_tools.blender.courtyard_geometry import enclosure_plan, part_xy_bounds


class CourtyardGeometryTests(unittest.TestCase):
    def test_low_level_open_workyard_has_no_orphaned_enclosure(self):
        plan = enclosure_plan("L1")
        self.assertEqual(plan["parts"], [])
        self.assertEqual(plan["style"], "open_workyard")
        self.assertEqual(plan["path_width_m"], 1.15)

    def test_every_physical_component_fits_the_same_sixteen_metre_plot(self):
        for level in ("L4", "L8"):
            for gate_x in (-1.1, 0, 1.1):
                with self.subTest(level=level, gate_x=gate_x):
                    plan = enclosure_plan(level, gate_x)
                    for part in plan["parts"]:
                        low, high = part_xy_bounds(part)
                        self.assertTrue(all(-8 <= a < b <= 8 for a, b in zip(low, high)), part)
                        if part["kind"] == "box":
                            self.assertTrue(all(math.isfinite(n) and n > 0 for n in part["size"]))

    def test_open_door_leaves_and_jamb_feet_leave_full_pedestrian_clearance(self):
        for level, width in (("L4", 1.8), ("L8", 2.4)):
            for gate_x in (-1.1, 0, 1.1):
                plan = enclosure_plan(level, gate_x)
                self.assertGreater(plan["gate_clear_width_m"], width)
                self.assertGreaterEqual(plan["gate_clear_height_m"], 2.2)
                front = plan["gate_center_m"][1]
                for part in plan["parts"]:
                    if part["kind"] != "box":
                        continue
                    # The walkable passage from the street through the fully
                    # opened leaves must contain no geometry below person height.
                    z_low = part["center"][2] - part["size"][2] / 2
                    if z_low >= 2.0:
                        continue
                    low, high = part_xy_bounds(part)
                    overlap_x = min(high[0], gate_x + width / 2) - max(low[0], gate_x - width / 2)
                    overlap_y = min(high[1], front + 1.6) - max(low[1], front - .6)
                    self.assertFalse(overlap_x > .00001 and overlap_y > .00001, (level, part))

    def test_wall_bodies_form_connected_perimeter_except_for_the_gate(self):
        for level in ("L4", "L8"):
            plan = enclosure_plan(level)
            walls = [part for part in plan["parts"] if part["name"].endswith("Plaster")]
            self.assertEqual(len(walls), 5)
            by_name = {part["name"]: part_xy_bounds(part) for part in walls}
            for left, right in (("RearPlaster", "WestPlaster"), ("RearPlaster", "EastPlaster"),
                                ("FrontLeftPlaster", "WestPlaster"), ("FrontRightPlaster", "EastPlaster")):
                a, b = by_name[left], by_name[right]
                self.assertTrue(all(min(a[1][i], b[1][i]) >= max(a[0][i], b[0][i]) for i in (0, 1)))

    def test_levels_differ_in_construction_without_stretching_a_shared_frame(self):
        low, high = enclosure_plan("L4"), enclosure_plan("L8")
        self.assertNotEqual(low["style"], high["style"])
        self.assertGreater(high["wall_top_m"], low["wall_top_m"])
        self.assertGreater(high["wall_thickness_m"], low["wall_thickness_m"])
        self.assertTrue(any(part["name"] == "GateTimberJamb" for part in low["parts"]))
        self.assertTrue(any(part["name"] == "GateMasonryJamb" for part in high["parts"]))
        self.assertTrue(any(part["name"] == "GateCanopyBeam" for part in high["parts"]))
        for plan in (low, high):
            self.assertTrue(any(part["name"] == "OpenGatePlank" for part in plan["parts"]))
            self.assertTrue(any(part["name"].endswith("StoneCourse") for part in plan["parts"]))
            self.assertTrue(any(part["kind"] == "tiled_roof" for part in plan["parts"]))
            self.assertEqual(plan["api_cost"], 0)

    def test_invalid_plot_or_disconnected_gate_is_rejected(self):
        for args in (("L5",), ("L8", 7), ("L4", math.nan), ("L8", 0, 12),
                     ("L4", 0, math.nan), ("L4", True), ("L8", "0")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                enclosure_plan(*args)


if __name__ == "__main__":
    unittest.main()
