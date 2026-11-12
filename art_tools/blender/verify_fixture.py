"""No-service integration check. Run inside an independent background Blender."""
import importlib.util
import json
from pathlib import Path
import sys

import bpy
import numpy as np


def main():
    root = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("prepare_batch", Path(__file__).with_name("prepare_batch.py"))
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    material = bpy.data.materials.new("Deliberately_Unrelated_Image_Names")
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    bsdf = nodes.get("Principled BSDF")

    def image_node(name, rgba, srgb=False):
        image = bpy.data.images.new(name, width=16, height=16, alpha=True)
        image.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
        image.pixels.foreach_set(np.tile(np.array(rgba, dtype=np.float32), 16 * 16))
        image.pack()
        node = nodes.new("ShaderNodeTexImage")
        node.image = image
        return node

    base = image_node("a_unrelated", (.4, .3, .2, 1), True)
    links.new(base.outputs["Color"], bsdf.inputs["Base Color"])
    mr = image_node("not_named_orm", (.33, .22, .67, 1))
    split = nodes.new("ShaderNodeSeparateColor")
    links.new(mr.outputs["Color"], split.inputs[0])
    links.new(split.outputs["Green"], bsdf.inputs["Roughness"])
    links.new(split.outputs["Blue"], bsdf.inputs["Metallic"])
    normal = image_node("also_unrelated", (.25, .8, 1, 1))
    normalmap = nodes.new("ShaderNodeNormalMap")
    links.new(normal.outputs["Color"], normalmap.inputs["Color"])
    links.new(normalmap.outputs["Normal"], bsdf.inputs["Normal"])
    group = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
    group.interface.new_socket(name="Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    groupnode = nodes.new("ShaderNodeGroup")
    groupnode.node_tree = group
    links.new(split.outputs["Red"], groupnode.inputs["Occlusion"])
    roofmat = bpy.data.materials.new("Second_Material_No_AO")
    roofmat.use_nodes = True
    roofmat.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (.15, .11, .08, 1)
    assets = []
    for level, target, maximum in [("L1", 4.2, [8, 6]), ("L4", 5, [10, 8]), ("L8", 6.2, [12, 10])]:
        body = prep.box("FixtureBody", (0, 0, 1), (3, 2, 2), material, 0)
        roof = prep.box("FixtureRoof", (0, 0, 2.2), (3.4, 2.4, .4), roofmat, 0)
        prep.select_only([body, roof])
        glb = root / f"fixture_{level}.glb"
        bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True)
        assets.append({"asset_id": f"fixture_{level}", "level": level, "input_glb": str(glb),
                       "target_height_m": target, "max_footprint_m": maximum, "front_rotation_deg": 0,
                       "orientation_verified": True})
        bpy.data.objects.remove(body, do_unlink=True)
        bpy.data.objects.remove(roof, do_unlink=True)
    manifest = root / "fixture_manifest.json"
    prep.write_json(manifest, {"schema_version": 1, "plot_size_m": 16, "assets": assets})
    output = root / "output"
    result = prep.run(manifest, output)
    houses = [a for a in result["assets"] if a["type"] == "building"]
    assert len(houses) == 3
    for house in houses:
        assert len(house["material_slots"]) == 2, house
        ratios = np.array(house["dimensions_m"]) / np.array(house["original_dimensions_m"])
        assert np.max(ratios) - np.min(ratios) < 1e-5, ratios
        assert abs(house["bounds_m"][0][2]) < 1e-5
        assert house["dimensions_m"][0] <= house["max_footprint_m"][0] + 1e-5
        assert house["dimensions_m"][1] <= house["max_footprint_m"][1] + 1e-5
    firstmat = next(m for m in result["materials"] if m["material_name"] == houses[0]["material_slots"][0]["material"])

    def read_pixel(path):
        image = bpy.data.images.load(str(output / path), check_existing=False)
        image.colorspace_settings.name = "Non-Color"
        return prep.pixel_array(image)[0, 0, :3]

    actual = read_pixel(firstmat["orm"])
    assert np.allclose(actual, [.33, .22 * .96 + .025, .67], atol=.01), actual
    normal = read_pixel(firstmat["normal"])
    assert abs(normal[1] - .2) < .01, normal
    fallback = next(m for m in result["materials"] if m["material_name"] == houses[0]["material_slots"][1]["material"])
    assert abs(read_pixel(fallback["orm"])[0] - 1) < .001
    assert len(result["shared_props"]) == 13
    assert {p["center_cm"][0] for p in result["plots"]} == {-2000, 0, 2000}
    assert all(i["level"] in {"L1", "L4", "L8"} for i in result["instances"])
    by_asset = {asset["asset_id"]: asset for asset in result["assets"]}
    by_instance = {item["instance_id"]: item for item in result["instances"]}

    def world_bounds(item):
        low, high = by_asset[item["asset_id"]]["bounds_m"]
        corners = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
        angle = np.deg2rad(item["rotation_deg"][2])
        rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        corners = (corners * np.array(item["scale"])) @ rotation.T + np.array(item["location_cm"]) / 100
        return corners.min(axis=0), corners.max(axis=0)

    for plot in result["plots"]:
        items = [i for i in result["instances"] if i["plot_id"] == plot["plot_id"]]
        house_item = next(i for i in items if i["layout_role"] == "building")
        assert house_item["rotation_deg"] == [0, 0, 0], "Orientation must be corrected once at GLB import"
        house_low, house_high = world_bounds(house_item)
        center = np.array(plot["center_cm"]) / 100
        for item in items:
            if item["layout_role"] != "prop":
                continue
            low, high = world_bounds(item)
            assert np.all(low[:2] >= center[:2] - 7.51), (item, low)
            assert np.all(high[:2] <= center[:2] + 7.51), (item, high)
            overlap = np.minimum(high[:2], house_high[:2]) - np.maximum(low[:2], house_low[:2])
            assert not np.all(overlap > .01), ("Life prop intersects house bounds", item, overlap)
        route = plot["pedestrian_route"]
        intervals = sorted((world_bounds(by_instance[key])[0][1], world_bounds(by_instance[key])[1][1]) for key in route["instance_ids"])
        assert intervals[0][0] <= route["start_y_cm"] / 100 + .02
        covered = intervals[0][1]
        for low, high in intervals[1:]:
            assert low - covered <= .04, ("Disconnected path", plot["level"], low, covered)
            covered = max(covered, high)
        assert covered >= route["end_y_cm"] / 100 - .02
    wings = [p["service_wing"] for p in result["plots"] if p["service_wing"]]
    assert [w["length_m"] for w in wings] == [2.86, 4.30]
    assert abs(by_asset[wings[0]["asset_id"]]["dimensions_m"][2] - by_asset[wings[1]["asset_id"]]["dimensions_m"][2]) < .001
    for item in result["instances"]:
        if "ServiceWing" in item["asset_id"]:
            assert item["rotation_deg"][2] == -item["blender_rotation_deg"][2]
    for material in result["materials"]:
        for key in ("base_color", "orm", "normal"):
            assert prep.sha256(output / material[key]) == material[key + "_sha256"]
    # FBX roundtrip proves serialized units; UE import must additionally check bounds.
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(output / houses[0]["fbx"]))
    imported = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    low, high = prep.bounds(imported)
    assert np.allclose(list(high - low), houses[0]["dimensions_m"], atol=.001), (high - low, houses[0]["dimensions_m"])
    # Read binary FBX unit declaration, without inferring from importer behaviour.
    from io_scene_fbx import parse_fbx
    tree, version = parse_fbx.parse(str(output / houses[0]["fbx"]))
    unit_values = {}
    def visit(element):
        if element.id == b"P" and element.props and element.props[0] in {b"UnitScaleFactor", b"OriginalUnitScaleFactor"}:
            unit_values[element.props[0].decode()] = element.props[-1]
        for child in element.elems:
            visit(child)
    visit(tree)
    assert unit_values["UnitScaleFactor"] == 100.0, unit_values
    report = {"status": "PASS", "checks": ["three_multimaterial_glb_imports", "uniform_scale_and_bottom_origin",
              "linked_orm_channel_extraction", "missing_ao_is_one", "normal_green_flipped_once", "thirteen_reusable_props",
              "three_16m_plots", "centimeter_instance_transforms", "fbx_dimension_roundtrip", "fbx_unit_metadata",
              "prop_plot_bounds_and_house_clearance", "continuous_gate_to_doorway_routes", "side_wings_keep_same_height",
              "orientation_applied_only_at_import", "material_texture_hashes", "side_wing_ue_orientation_calibration"],
              "fbx_version": version, "fbx_units": unit_values, "assets": len(result["assets"]), "instances": len(result["instances"])}
    prep.write_json(root / "verification.json", report)
    print("ART_FIXTURE_PASS " + json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
