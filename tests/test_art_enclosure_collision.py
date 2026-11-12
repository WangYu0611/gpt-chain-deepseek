"""Offline policy checks complement the real UE 5.8 import/trace fixture."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "art_tools/ue/import_preview.py"


def load_functions(**globals_):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"json": json, **globals_}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace


class Actor:
    def __init__(self, name="Enclosure"):
        self.name = name
        self.static_mesh_component = SimpleNamespace(static_mesh=SimpleNamespace(get_num_sections=lambda lod: 6))

    def get_actor_location(self):
        return SimpleNamespace(x=2000, y=0, z=0)

    def get_actor_rotation(self):
        return SimpleNamespace(pitch=0, yaw=0, roll=0)

    def get_actor_scale3d(self):
        return SimpleNamespace(x=1, y=1, z=1)

    def get_actor_label(self):
        return self.name


class CollisionTest(unittest.TestCase):
    def setUp(self):
        self.actor = Actor()
        self.calls = []
        self.asset = {"asset_id": "Enclosure_L8", "courtyard_enclosure": {
            "gate_center_m": [0, -7.4, 0], "path_width_m": 2.4,
            "gate_clear_width_m": 2.64, "boundary_half_m": 7.5}}
        self.obstacle = None
        self.wall_actor = self.actor

        def trace(**kwargs):
            self.calls.append(kwargs)
            at_wall = abs(kwargs["start"].x - 2000) > 200
            hit_actor = self.wall_actor if at_wall else self.obstacle
            return None if hit_actor is None else SimpleNamespace(
                to_dict=lambda: {"blocking_hit": True, "hit_actor": hit_actor})

        unreal = SimpleNamespace(
            Vector=lambda x, y, z: SimpleNamespace(x=x, y=y, z=z),
            TraceTypeQuery=SimpleNamespace(TRACE_TYPE_QUERY1=1), DrawDebugTrace=SimpleNamespace(NONE=0),
            SystemLibrary=SimpleNamespace(line_trace_single=trace, sphere_trace_single=trace))
        self.verify = load_functions(unreal=unreal)["verify_enclosure_passage"]

    def test_gate_and_wall_checks_use_simple_queries_and_ignore_nothing(self):
        result = self.verify(object(), self.actor, self.asset)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["queries"]), 17)
        self.assertEqual(sum(q["expected_blocking"] for q in result["queries"]), 8)
        for call in self.calls:
            self.assertIs(call["trace_complex"], False)
            self.assertEqual(call["actors_to_ignore"], [])
            self.assertIs(call["ignore_self"], False)

    def test_real_obstacle_in_gate_is_not_ignored(self):
        self.obstacle = Actor("UnexpectedGateBlocker")
        with self.assertRaisesRegex(RuntimeError, "UnexpectedGateBlocker"):
            self.verify(object(), self.actor, self.asset)

    def test_other_prop_cannot_substitute_for_missing_wall_collision(self):
        self.wall_actor = Actor("UnrelatedProp")
        with self.assertRaisesRegex(RuntimeError, "UnrelatedProp"):
            self.verify(object(), self.actor, self.asset)

    def test_missing_wall_collision_fails(self):
        self.wall_actor = None
        with self.assertRaisesRegex(RuntimeError, "wall_left_line"):
            self.verify(object(), self.actor, self.asset)

    def test_scaled_or_rotated_enclosure_is_rejected(self):
        self.actor.get_actor_rotation = lambda: SimpleNamespace(pitch=0, yaw=180, roll=0)
        with self.assertRaisesRegex(RuntimeError, "authored orientation"):
            self.verify(object(), self.actor, self.asset)
        self.assertFalse(self.calls)

    def test_unready_mesh_cannot_be_mistaken_for_clear_passage(self):
        self.actor.static_mesh_component.static_mesh.get_num_sections = lambda lod: 0
        with self.assertRaisesRegex(RuntimeError, "completed LOD0"):
            self.verify(object(), self.actor, self.asset)
        self.assertFalse(self.calls)

    def test_collision_policy_does_not_touch_other_assets(self):
        configure = load_functions()["configure_enclosure_collision"]
        self.assertIsNone(configure(object(), {"asset_id": "House_L1", "type": "building"}))
        self.assertIsNone(configure(object(), {"asset_id": "Jar", "type": "prop"}))

    def test_collision_rebuild_occurs_after_flag_change(self):
        events = []
        flag = "COMPLEX_AS_SIMPLE"
        values = {"collision_trace_flag": "DEFAULT"}

        def set_property(name, value):
            events.append("set_flag")
            values[name] = value

        body = SimpleNamespace(set_editor_property=set_property, get_editor_property=values.__getitem__)
        mesh = SimpleNamespace(get_editor_property=lambda name: body)

        def rebuild(_mesh):
            self.assertEqual(values["collision_trace_flag"], flag)
            events.append("rebuild")
            return True

        namespace = load_functions(
            unreal=SimpleNamespace(CollisionTraceFlag=SimpleNamespace(CTF_USE_COMPLEX_AS_SIMPLE=flag)),
            STATIC=SimpleNamespace(remove_collisions=rebuild, get_collision_complexity=lambda mesh: flag),
            EAL=SimpleNamespace(save_loaded_asset=lambda mesh: events.append("save")))
        policy = namespace["configure_enclosure_collision"](mesh, self.asset)
        self.assertEqual(events, ["set_flag", "rebuild", "save"])
        self.assertFalse(policy["auto_generate_collision"])


if __name__ == "__main__":
    unittest.main()
