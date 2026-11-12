"""Validate courtyard maintenance using existing GLBs, without any service or UE."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def main():
    manifest_path, root = [Path(arg).resolve() for arg in sys.argv[sys.argv.index("--") + 1:]]
    if root.exists() and any(root.iterdir()):
        raise ValueError("Verification output must be a new or empty independent directory")
    if root == manifest_path.parent or root.is_relative_to(manifest_path.parent):
        raise ValueError("Verification must not write into the production artifact tree")
    root.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("prepare_batch", Path(__file__).with_name("prepare_batch.py"))
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    source = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    sources = {asset["level"]: {"path": asset["input_glb"], "sha256": prep.sha256(asset["input_glb"])}
               for asset in source["assets"]}
    assert set(sources) == {"L1", "L4", "L8"}
    for asset in source["assets"]:
        assert sources[asset["level"]]["sha256"] == asset["source_sha256"]
    output = root / "output"
    result = prep.run(manifest_path, output)
    by_asset = {asset["asset_id"]: asset for asset in result["assets"]}
    assert "SM_Court_Wall_A" not in by_asset and "SM_Court_Gate_A" not in by_asset
    for level in ("L1", "L4", "L8"):
        instances = [item for item in result["instances"] if item["level"] == level and item["layout_role"] == "enclosure"]
        if level == "L1":
            assert instances == [], "An isolated wall remains in the open workyard"
            continue
        assert len(instances) == 1
        instance = instances[0]
        assert instance["scale"] == [1, 1, 1] and instance["rotation_deg"] == [0, 0, 0]
        asset = by_asset[instance["asset_id"]]
        assert asset["type"] == "courtyard_module" and asset["provenance"]["api_cost"] == 0
        assert asset["fbx_local_y_precompensated"] is True
        axis = asset["coordinate_export_audit"]
        assert axis["authored_mesh_and_uv_preserved"] is True
        assert axis["authored_geometry_sha256"] != axis["export_geometry_sha256"]
        low, high = asset["bounds_m"]
        assert all(-8 <= a < b <= 8 for a, b in zip(low[:2], high[:2])), asset
        assert abs(low[2]) < .001
        assert len(asset["material_slots"]) >= 5, asset
        expected_width = 1.8 if level == "L4" else 2.4
        assert asset["courtyard_enclosure"]["gate_clear_width_m"] > expected_width
        # Serialized vertices must leave the street-to-yard passage clear below
        # head height. Use every triangle's XY extent, not just its vertices.
        obj = bpy.data.objects[asset["asset_id"]]
        obj.data.calc_loop_triangles()
        gate_x, front, _ = asset["courtyard_enclosure"]["gate_center_m"]
        for tri in obj.data.loop_triangles:
            points = [obj.data.vertices[index].co for index in tri.vertices]
            if min(point.z for point in points) >= 2.0:
                continue
            overlap_x = min(max(p.x for p in points), gate_x + expected_width / 2) - max(min(p.x for p in points), gate_x - expected_width / 2)
            overlap_y = min(max(p.y for p in points), front + 1.6) - max(min(p.y for p in points), front - .6)
            assert not (overlap_x > 1e-5 and overlap_y > 1e-5), (level, "Gate passage blocked", list(tri.vertices))
        # Native FBX roundtrip observes the deliberate local-Y export reflection.
        # UE applies its own reflection afterwards; the resulting opening must
        # recover the authored -Y layout without rotating the entire enclosure.
        before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(filepath=str(output / asset["fbx"]))
        imported = [item for item in bpy.data.objects if item not in before and item.type == "MESH"]
        assert imported
        fbx_low, fbx_high = prep.bounds(imported)
        expected_low, expected_high = axis["fbx_bounds_before_ue_m"]
        assert all(abs(fbx_low[i] - expected_low[i]) < .002 and abs(fbx_high[i] - expected_high[i]) < .002 for i in range(3))
        assert abs(fbx_low.y + high[1]) < .002 and abs(fbx_high.y + low[1]) < .002
        for imported_object in imported:
            imported_object.data.calc_loop_triangles()
            for tri in imported_object.data.loop_triangles:
                points = [imported_object.matrix_world @ imported_object.data.vertices[index].co for index in tri.vertices]
                points = [Vector((point.x, -point.y, point.z)) for point in points]
                if min(point.z for point in points) >= 2.0:
                    continue
                overlap_x = min(max(p.x for p in points), gate_x + expected_width / 2) - max(min(p.x for p in points), gate_x - expected_width / 2)
                overlap_y = min(max(p.y for p in points), front + 1.6) - max(min(p.y for p in points), front - .6)
                assert not (overlap_x > 1e-5 and overlap_y > 1e-5), (level, "FBX opening differs from authored walkway")
        for item in list(bpy.data.objects):
            if item not in before:
                bpy.data.objects.remove(item, do_unlink=True)
    for item in sources.values():
        assert prep.sha256(item["path"]) == item["sha256"], "Source GLB was modified"
    for asset in result["assets"]:
        assert prep.sha256(output / asset["fbx"]) == asset["fbx_sha256"]
    assert prep.sha256(output / result["blend"]) == result["blend_sha256"]

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 1100
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.53, .57, .62, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = .55
    light = bpy.data.lights.new("VerificationSun", "SUN")
    light.energy = 2.3
    light.angle = .12
    light_obj = bpy.data.objects.new("VerificationSun", light)
    scene.collection.objects.link(light_obj)
    light_obj.rotation_euler = (.48, -.55, -.35)
    camera = bpy.data.cameras.new("VerificationCamera")
    camera.type = "ORTHO"
    camera_obj = bpy.data.objects.new("VerificationCamera", camera)
    scene.collection.objects.link(camera_obj)
    scene.camera = camera_obj
    renders = []
    for plot in result["plots"]:
        level, ox = plot["level"], plot["center_cm"][0] / 100
        for obj in bpy.data.collections["Courtyard_Preview"].objects:
            obj.hide_render = not obj.name.startswith("Plot_" + level + "_")
        for view in (("courtyard", (ox + 16, -20, 19), (ox, 0, 1.3), 23),
                     ("entrance", (ox + 3.0, -12, 3.4), (ox, -7.3, 1.4), 6.0)):
            label, location, target, scale = view
            if level == "L1" and label == "entrance":
                continue
            camera.ortho_scale = scale
            camera_obj.location = location
            camera_obj.rotation_euler = (Vector(target) - camera_obj.location).to_track_quat("-Z", "Y").to_euler()
            target_path = root / (level + "_" + label + ".png")
            scene.render.filepath = str(target_path)
            bpy.ops.render.render(write_still=True)
            renders.append({"path": str(target_path), "sha256": prep.sha256(target_path)})
    verification = {"status": "PASS", "sources": sources, "api_calls": 0,
                    "production_written": False, "blender_version": bpy.app.version_string,
                    "checks": ["no_L1_orphan_wall", "level_specific_enclosures", "no_scaled_gate",
                               "sixteen_metre_bounds", "serialized_gate_walkway_clearance", "multi_material_construction",
                               "source_hashes_unchanged", "FBX_and_blend_hashes", "FBX_local_Y_compensation_roundtrip",
                               "authored_mesh_and_UV_preserved"],
                    "assets": len(result["assets"]), "instances": len(result["instances"]), "renders": renders}
    prep.write_json(root / "verification.json", verification)
    print("COURTYARD_MAINTENANCE_PASS " + json.dumps(verification), flush=True)


if __name__ == "__main__":
    main()
