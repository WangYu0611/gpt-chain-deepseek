"""Generic job/provenance checks with abstract objects, never art construction."""
import ast
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blender_job_runtime", ROOT / "art_tools/blender/blender_job_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def payload():
    houses = {level: "SM_House_" + level + "_A" for level in ("L1", "L4", "L8")}
    assets = [{"asset_id": identity, "type": "building", "level": level,
               "provenance": {"kind": "tripo_glb", "input_sha256": level},
               "material_slots": [{"material": "Material"}]} for level, identity in houses.items()]
    result = {"plots": [{"plot_id": "Plot_" + level, "level": level, "house_asset_id": identity,
                         "plot_size_m": [16, 16], "center_cm": [(index - 1) * 2000, 0, 0]}
                        for index, (level, identity) in enumerate(houses.items())],
              "instances": [{"instance_id": "Plot_" + level + "_Instance", "plot_id": "Plot_" + level, "level": level,
                             "asset_id": identity, "location_cm": [0, 0, 0], "rotation_deg": [0, 0, 0],
                             "scale": [1, 1, 1]} for level, identity in houses.items()], "report": {"kind": "abstract_test"}}
    return houses, assets, result


class JobRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def job(self, text):
        raw = text.encode("utf-8")
        path = self.root / "snapshot.py"
        path.write_bytes(raw)
        return {"schema_version": 1, "source_file": str(self.root / "source.py"), "snapshot": str(path),
                "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def test_executes_exact_snapshot_not_mutable_source(self):
        metadata = self.job("def run_job(context):\n    return {'value':context['value']}\n")
        Path(metadata["source_file"]).write_text("raise RuntimeError('wrong source')", encoding="utf-8")
        self.assertEqual(runtime.execute_job(metadata, {"value": 7}), {"value": 7})

    def test_modified_snapshot_never_executes(self):
        metadata = self.job("def run_job(context):\n    context['executed']=True\n")
        Path(metadata["snapshot"]).write_text("raise AssertionError('must not execute')", encoding="utf-8")
        context = {}
        with self.assertRaisesRegex(ValueError, "pinned hash/size"):
            runtime.execute_job(metadata, context)
        self.assertFalse(context)

    def test_job_requires_entrypoint_and_strict_metadata(self):
        metadata = self.job("unused = 1\n")
        with self.assertRaisesRegex(ValueError, "run_job"):
            runtime.execute_job(metadata, {})
        for change in ({"size_bytes": True}, {"schema_version": True}, {"sha256": "0" * 63}, {"other": "value"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.load_job({**metadata, **change})

    def test_validates_result_without_adding_layout(self):
        houses, assets, result = payload()
        self.assertEqual(runtime.validate_job_result(result, assets, {"Material": {}}, houses, plot_size=16), result)

    def test_rejects_missing_house_duplicate_instance_unknown_material_and_nonfinite(self):
        houses, assets, original = payload()
        cases = []
        value = deepcopy(original); value["instances"].pop(); cases.append((value, assets))
        value = deepcopy(original); value["instances"].append(deepcopy(value["instances"][0])); cases.append((value, assets))
        value = deepcopy(original); value["instances"][0]["asset_id"] = "unknown"; cases.append((value, assets))
        value = deepcopy(original); value["instances"][0]["location_cm"][0] = float("nan"); cases.append((value, assets))
        value = deepcopy(assets); value[0]["material_slots"][0]["material"] = "unknown"; cases.append((original, value))
        value = deepcopy(original); value["plots"][0]["house_asset_id"] = houses["L8"]; cases.append((value, assets))
        value = deepcopy(original); value["plots"][0]["plot_size_m"] = [20, 16]; cases.append((value, assets))
        for index, (result, registered) in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                runtime.validate_job_result(result, registered, {"Material": {}}, houses, plot_size=16)

    def test_capture_contract_rejects_wrong_plot_names_centers_and_actor_prefixes(self):
        houses, assets, original = payload()
        for change in ("plot_name", "center", "instance_prefix"):
            result = deepcopy(original)
            if change == "plot_name": result["plots"][0]["plot_id"] = "Other"
            elif change == "center": result["plots"][0]["center_cm"] = [0, 0, 0]
            else: result["instances"][0]["instance_id"] = "UnscopedActor"
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.validate_job_result(result, assets, {"Material": {}}, houses, plot_size=16)

    def test_reexport_replaces_record_and_keeps_source_provenance(self):
        _, assets, _ = payload()
        original = deepcopy(assets[0])
        objects = {}

        def export(obj, asset_type, provenance, extra, **kwargs):
            record = {"asset_id": obj.name, "type": asset_type, "provenance": provenance, **extra}
            assets.append(record)
            return record

        exporter = runtime.AssetExporter(export, assets, objects, {"sha256": "a" * 64})
        obj = SimpleNamespace(name=original["asset_id"])
        record = exporter(obj, extra={"job_note": "test"})
        exporter(obj)
        self.assertEqual(len(assets), 3)
        self.assertEqual(record["provenance"], original["provenance"])
        self.assertIs(objects[obj.name], obj)
        for kwargs in ({"provenance": {"kind": "wrong"}}, {"asset_type": "prop"},
                       {"extra": {"level": "L2"}}, {"extra": {"dimensions_m": [1, 1, 1]}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                exporter(obj, **kwargs)

    def test_driver_job_lane_skips_all_preset_calls_and_reexports_houses(self):
        houses, records, result = payload()
        metadata = self.job("def run_job(context):\n    assert len(context['houses']) == 3\n    return " + repr(result) + "\n")
        manifest = {"schema_version": 1, "plot_size_m": 16, "assets": [{"level": level} for level in houses],
                    "blender_job": metadata, "blender_provenance": {"schema_version": 1, "files": []}}
        manifest_path = self.root / "input.json"; manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "output"
        exported = []
        assets, materials = [], {}
        scene_objects = []

        class Links:
            def link(self, value):
                pass

        class Object:
            type = "MESH"
            users_collection = []
            data = object()
            def __init__(self, name): self.name = name
            def copy(self): return Object(self.name)
            def hide_set(self, value): pass

        def process(entry, directory, *, apply_model_repairs):
            self.assertIs(apply_model_repairs, False)
            record = deepcopy(next(r for r in records if r["level"] == entry["level"]))
            assets.append(record)
            obj = Object(record["asset_id"]); scene_objects.append(obj)
            for key in ("base_color", "orm", "normal"):
                (output / (key + ".png")).write_bytes(b"abstract-fixture")
            materials["Material"] = {key: key + ".png" for key in ("base_color", "orm", "normal")}
            return obj

        def export(obj, asset_type, provenance, extra, **kwargs):
            exported.append(obj.name)
            record = {"asset_id": obj.name, "type": asset_type, "provenance": provenance,
                      "material_slots": [{"material": "Material"}], **extra}
            assets.append(record)
            return record

        collections = SimpleNamespace(new=lambda name: SimpleNamespace(objects=Links()))
        scene = SimpleNamespace(objects=scene_objects, unit_settings=SimpleNamespace(),
                                collection=SimpleNamespace(children=Links()), world=SimpleNamespace())
        bpy = SimpleNamespace(context=SimpleNamespace(scene=scene), data=SimpleNamespace(collections=collections),
                              app=SimpleNamespace(version_string="abstract"), ops=SimpleNamespace(wm=SimpleNamespace(
                                  read_factory_settings=lambda **kw: None,
                                  save_as_mainfile=lambda filepath: Path(filepath).write_bytes(b"abstract-fixture"))))
        def forbidden(*args, **kwargs): raise AssertionError("art preset must not execute")
        namespace = dict(Path=Path, json=json, math=__import__("math"), bpy=bpy,
                         Matrix=SimpleNamespace(Identity=lambda n: None), VERSION=1, ASSETS=assets,
                         MATERIALS=materials, WARNINGS=[], process_house=process, create_props=forbidden,
                         assemble=forbidden, export_mesh=export, AssetExporter=runtime.AssetExporter,
                         execute_job=runtime.execute_job, validate_job_result=runtime.validate_job_result,
                         sha256=lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest(),
                         relative=lambda p: Path(p).relative_to(output).as_posix(),
                         write_json=lambda p, data: Path(p).write_text(json.dumps(data), encoding="utf-8"))
        for key in ("bounds", "join_meshes", "procedural_material", "process_material", "box", "cylinder", "timber_between", "pottery"):
            namespace[key] = forbidden
        tree = ast.parse((ROOT / "art_tools/blender/prepare_batch.py").read_text(encoding="utf-8"))
        run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        exec(compile(ast.Module(body=[run], type_ignores=[]), "prepare_batch.py", "exec"), namespace)
        returned = namespace["run"](manifest_path, output)
        self.assertEqual(exported, list(houses.values()))
        self.assertEqual(returned["blender_job"], metadata)
        self.assertEqual(returned["blender_job_report"], result["report"])
        self.assertEqual(returned["instances"], result["instances"])
        self.assertEqual(json.loads((output / "processing_report.json").read_text())["blender_job"], metadata)


if __name__ == "__main__":
    unittest.main()
