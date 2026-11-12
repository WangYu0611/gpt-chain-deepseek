"""UE 5.8 editor script: import an immutable Blender manifest into its own preview project."""
import hashlib
import gc
import json
import math
import os
from pathlib import Path
import traceback
import unreal

MANIFEST = Path(os.environ["ZG_ART_MANIFEST"])
ROOT = Path(os.environ["ZG_ART_OUTPUT"])
RUN = os.environ["ZG_ART_RUN"]
DATA = json.loads(MANIFEST.read_text(encoding="utf-8"))
PREVIEW_ROOT = "/Game/ArtPreview"
MANIFEST_SHA = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
SCRIPT_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
VERSION_ID = hashlib.sha256((MANIFEST_SHA + SCRIPT_SHA).encode("ascii")).hexdigest()[:16]
BASE = PREVIEW_ROOT + "/Versions/V_" + VERSION_ID
MAP = PREVIEW_ROOT + "/Maps/HousingPreview"
AT = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
STATIC = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
REPORT = {"status": "started", "run": RUN, "engine": unreal.SystemLibrary.get_engine_version(),
          "manifest_sha256": MANIFEST_SHA, "script_sha256": SCRIPT_SHA,
          "asset_version_id": VERSION_ID, "asset_base": BASE,
          "meshes": [], "materials": [], "actors": [], "warnings": []}


def save_report():
    dest = ROOT / "receipts" / (RUN + "-import.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")


def local(relative):
    path = (MANIFEST.parent / relative).resolve()
    if not path.is_relative_to(MANIFEST.parent.resolve()) or not path.is_file():
        raise ValueError("Invalid manifest artifact: " + str(relative))
    return str(path)


def imported(source, folder, options=None):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", source)
    task.set_editor_property("destination_path", folder)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    if options:
        task.set_editor_property("options", options)
    AT.import_asset_tasks([task])
    paths = list(task.get_editor_property("imported_object_paths"))
    if not paths:
        raise RuntimeError("No imported object: " + Path(source).name)
    return unreal.load_asset(paths[0])


def expression(mat, cls, **props):
    node = MEL.create_material_expression(mat, cls)
    for key, value in props.items():
        node.set_editor_property(key, value)
    return node


def scalar(mat, name, default):
    return expression(mat, unreal.MaterialExpressionScalarParameter, parameter_name=name, default_value=default)


def connect(a, pin, b, slot):
    MEL.connect_material_expressions(a, pin, b, slot)


def master_material(defaults):
    path = BASE + "/Materials/M_A_Refined"
    mat = unreal.load_asset(path) if EAL.does_asset_exist(path) else AT.create_asset(
        "M_A_Refined", BASE + "/Materials", unreal.Material, unreal.MaterialFactoryNew())
    MEL.delete_all_material_expressions(mat)
    base = expression(mat, unreal.MaterialExpressionTextureSampleParameter2D,
                      parameter_name="BaseColor", texture=defaults["base_color"])
    orm = expression(mat, unreal.MaterialExpressionTextureSampleParameter2D,
                     parameter_name="ORM", texture=defaults["orm"], sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    normal = expression(mat, unreal.MaterialExpressionTextureSampleParameter2D,
                        parameter_name="Normal", texture=defaults["normal"],
                        sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    MEL.connect_material_property(base, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    rough = expression(mat, unreal.MaterialExpressionMax)
    connect(orm, "G", rough, "A")
    connect(scalar(mat, "RoughnessFloor", .58), "", rough, "B")
    MEL.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.connect_material_property(orm, "B", unreal.MaterialProperty.MP_METALLIC)
    MEL.connect_material_property(orm, "R", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)
    MEL.connect_material_property(scalar(mat, "Specular", .3), "", unreal.MaterialProperty.MP_SPECULAR)
    flat = expression(mat, unreal.MaterialExpressionConstant3Vector, constant=unreal.LinearColor(0, 0, 1, 1))
    blend = expression(mat, unreal.MaterialExpressionLinearInterpolate)
    connect(flat, "", blend, "A")
    connect(normal, "RGB", blend, "B")
    connect(scalar(mat, "NormalStrength", .72), "", blend, "Alpha")
    norm = expression(mat, unreal.MaterialExpressionNormalize)
    connect(blend, "", norm, "")
    MEL.connect_material_property(norm, "", unreal.MaterialProperty.MP_NORMAL)
    MEL.recompile_material(mat)
    EAL.save_asset(path)
    return mat


def solid_material(name, color, roughness=.86):
    path = BASE + "/Materials/" + name
    mat = unreal.load_asset(path) if EAL.does_asset_exist(path) else AT.create_asset(
        name, BASE + "/Materials", unreal.Material, unreal.MaterialFactoryNew())
    MEL.delete_all_material_expressions(mat)
    c = expression(mat, unreal.MaterialExpressionConstant3Vector, constant=unreal.LinearColor(*color, 1))
    r = expression(mat, unreal.MaterialExpressionConstant, r=roughness)
    MEL.connect_material_property(c, "", unreal.MaterialProperty.MP_BASE_COLOR)
    MEL.connect_material_property(r, "", unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.recompile_material(mat)
    EAL.save_asset(path)
    return mat


def spawn_mesh(name, mesh, position, rotation=(0, 0, 0), scale=(1, 1, 1), material=None):
    actor = ACTORS.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(*position),
                                         unreal.Rotator(pitch=rotation[1], yaw=rotation[2], roll=rotation[0]))
    actor.set_actor_label(name)
    actor.static_mesh_component.set_static_mesh(mesh)
    actor.set_actor_scale3d(unreal.Vector(*scale))
    if material:
        actor.static_mesh_component.set_material(0, material)
    return actor


def mesh_import_options(asset):
    options = unreal.FbxImportUI()
    for name, value in (("import_mesh", True), ("import_as_skeletal", False), ("import_animations", False),
                        ("import_materials", False), ("import_textures", False),
                        ("mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH)):
        options.set_editor_property(name, value)
    imp = options.get_editor_property("static_mesh_import_data")
    for name, value in (("combine_meshes", True), ("import_uniform_scale", 1.0), ("convert_scene", True),
                        ("convert_scene_unit", True),
                        ("auto_generate_collision", not bool(asset.get("courtyard_enclosure")))):
        imp.set_editor_property(name, value)
    return options


def configure_enclosure_collision(mesh, asset):
    """Preserve the hollow courtyard for ordinary pawn/visibility queries."""
    if not asset.get("courtyard_enclosure"):
        return None
    body = mesh.get_editor_property("body_setup")
    flag = unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE
    body.set_editor_property("collision_trace_flag", flag)
    # Apply after the flag: this invalidates/cooks collision and calls the mesh's
    # PostEditChange. A BodySetup property write alone does not rebuild its owner.
    if not STATIC.remove_collisions(mesh):
        raise RuntimeError("Could not clear enclosure convex collision: " + asset["asset_id"])
    if body.get_editor_property("collision_trace_flag") != flag or STATIC.get_collision_complexity(mesh) != flag:
        raise RuntimeError("Enclosure complex-as-simple collision was not applied")
    EAL.save_loaded_asset(mesh)
    return {"strategy": "complex_as_simple_static", "auto_generate_collision": False,
            "collision_trace_flag": "CTF_USE_COMPLEX_AS_SIMPLE", "simulates_physics": False,
            "query_channel": "Visibility", "validation_trace_complex": False,
            "scope": "courtyard_enclosure_only"}


def configure_enclosure_actor(actor):
    component = actor.static_mesh_component
    component.set_mobility(unreal.ComponentMobility.STATIC)
    component.set_simulate_physics(False)
    component.set_collision_profile_name("BlockAll")
    component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
    if not component.is_query_collision_enabled() or component.is_simulating_physics():
        raise RuntimeError("Enclosure must use static blocking query collision")


def verify_enclosure_passage(world, actor, asset):
    """Simple line/sphere queries must pass the gate and hit both front walls.

    Tests run in the assembled world with no ignored actors. Wall hits must belong
    to this enclosure, so a different prop cannot hide a missing wall collider.
    """
    meta = asset["courtyard_enclosure"]
    # UE loads static-mesh render/physics data asynchronously. GetNumSections
    # reaches GetRenderData/WaitUntilAsyncPropertyReleased before we trace.
    # Without this read barrier a freshly loaded map can falsely report no wall.
    section_count = actor.static_mesh_component.static_mesh.get_num_sections(0)
    if section_count <= 0:
        raise RuntimeError("Enclosure has no completed LOD0 mesh sections")
    position = actor.get_actor_location()
    rotation = actor.get_actor_rotation()
    scale = actor.get_actor_scale3d()
    if any(abs(float(v)) > .001 for v in (rotation.pitch, rotation.yaw, rotation.roll)) or any(
            abs(float(v) - 1) > .001 for v in (scale.x, scale.y, scale.z)):
        raise RuntimeError("Courtyard collision validation requires authored orientation and scale")
    gate = meta["gate_center_m"]
    gate_x, gate_y = position.x + gate[0] * 100, position.y + gate[1] * 100
    radius = 35.0
    edge_offset = meta["path_width_m"] * 50 - radius - 5
    if edge_offset <= 0 or meta["gate_clear_width_m"] < meta["path_width_m"]:
        raise RuntimeError("Invalid enclosure passage width")
    queries = []

    def query(name, x, z, kind, should_block, reverse=False):
        start = [float(x), float(gate_y - 180), float(position.z + z)]
        end = [float(x), float(gate_y + 180), float(position.z + z)]
        if reverse:
            start, end = end, start
        kwargs = dict(world_context_object=world, start=unreal.Vector(*start), end=unreal.Vector(*end),
                      trace_channel=unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, trace_complex=False,
                      actors_to_ignore=[], draw_debug_type=unreal.DrawDebugTrace.NONE, ignore_self=False)
        hit = (unreal.SystemLibrary.sphere_trace_single(radius=radius, **kwargs) if kind == "sphere"
               else unreal.SystemLibrary.line_trace_single(**kwargs))
        # UE5.8 HitResult fields are protected; to_dict uses its native BreakHitResult.
        hit_data = hit.to_dict() if hit is not None else {}
        hit_actor = hit_data.get("hit_actor")
        blocked = bool(hit_data.get("blocking_hit", False))
        record = {"name": name, "kind": kind, "trace_complex": False, "start_cm": start, "end_cm": end,
                  "radius_cm": radius if kind == "sphere" else 0, "expected_blocking": should_block,
                  "blocking_hit": blocked, "hit_actor": hit_actor.get_actor_label() if hit_actor else None,
                  "ignored_actor_count": 0}
        queries.append(record)
        if blocked != should_block or (should_block and hit_actor != actor):
            raise RuntimeError("Enclosure simple collision failed: " + json.dumps(record, ensure_ascii=False))

    for z in (30, 100, 180):
        query("gate_center_line_%s" % z, gate_x, z, "line", False)
    for label, dx in (("left", -edge_offset), ("center", 0), ("right", edge_offset)):
        query("gate_" + label + "_sphere", gate_x + dx, 100, "sphere", False)
        query("gate_" + label + "_sphere_reverse", gate_x + dx, 100, "sphere", False, True)
    for side, sign in (("left", -1), ("right", 1)):
        wall_x = position.x + sign * meta["boundary_half_m"] * 60
        for kind in ("line", "sphere"):
            query("wall_" + side + "_" + kind, wall_x, 100, kind, True)
            query("wall_" + side + "_" + kind + "_reverse", wall_x, 100, kind, True, True)
    return {"asset_id": asset["asset_id"], "actor": actor.get_actor_label(), "status": "passed",
            "policy": "complex_as_simple_static", "lod0_sections": section_count,
            "mesh_readiness_barrier": "get_num_sections(0)", "queries": queries}


try:
    if [p["level"] for p in DATA["plots"]] != ["L1", "L4", "L8"]:
        raise ValueError("Preview manifest must contain exactly the three approved plots")
    # StartupMap may still reference the meshes we are about to atomically reimport.
    # Release that world before replacing any asset (UE 5.8 otherwise asserts on
    # a referenced Nanite mesh). The scratch map belongs to this preview only.
    scratch = BASE + "/Maps/ImportScratch_" + "".join(c if c.isalnum() else "_" for c in RUN)
    if EAL.does_asset_exist(scratch):
        if not LEVEL.load_level(scratch):
            raise RuntimeError("Could not load empty import workspace")
    elif not LEVEL.new_level(scratch):
        raise RuntimeError("Could not create empty import workspace")
    gc.collect()
    unreal.SystemLibrary.collect_garbage()
    parent = None
    materials = {}
    for m in DATA["materials"]:
        textures = {}
        for key in ("base_color", "orm", "normal"):
            tx = imported(local(m[key]), BASE + "/Textures")
            is_color = key == "base_color"
            tx.set_editor_property("srgb", is_color)
            if not is_color:
                tx.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP
                                       if key == "normal" else unreal.TextureCompressionSettings.TC_MASKS)
            EAL.save_asset(tx.get_path_name())
            textures[key] = tx
        if parent is None:
            parent = master_material(textures)
        name = "MI_" + m["material_name"].removeprefix("M_")
        path = BASE + "/Materials/" + name
        mi = unreal.load_asset(path) if EAL.does_asset_exist(path) else AT.create_asset(
            name, BASE + "/Materials", unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        MEL.set_material_instance_parent(mi, parent)
        MEL.update_material_instance(mi)
        for param, key in (("BaseColor", "base_color"), ("ORM", "orm"), ("Normal", "normal")):
            MEL.set_material_instance_texture_parameter_value(mi, param, textures[key])
            actual_texture = MEL.get_material_instance_texture_parameter_value(mi, param)
            if actual_texture != textures[key]:
                raise RuntimeError("Material parameter failed: " + param)
        MEL.update_material_instance(mi)
        EAL.save_asset(path)
        materials[m["material_name"]] = mi
        REPORT["materials"].append(path)
    unreal.SystemLibrary.execute_console_command(None, "Interchange.FeatureFlags.Import.FBX 0")
    meshes = {}
    for a in DATA["assets"]:
        mesh = imported(local(a["fbx"]), BASE + "/Meshes", mesh_import_options(a))
        if not isinstance(mesh, unreal.StaticMesh):
            raise TypeError("Expected static mesh: " + a["asset_id"])
        imported_slots = mesh.get_editor_property("static_materials")
        slots_by_name = {}
        for index, imported_slot in enumerate(imported_slots):
            slots_by_name[str(imported_slot.get_editor_property("material_slot_name"))] = index
            slots_by_name[str(imported_slot.get_editor_property("imported_material_slot_name"))] = index
        for slot in a["material_slots"]:
            if slot["slot_name"] not in slots_by_name:
                raise RuntimeError("Missing FBX material slot: " + slot["slot_name"])
            mesh.set_material(slots_by_name[slot["slot_name"]], materials[slot["material"]])
        if a["type"] == "building":
            settings = STATIC.get_nanite_settings(mesh)
            settings.set_editor_property("enabled", True)
            STATIC.set_nanite_settings(mesh, settings, True)
        collision_policy = configure_enclosure_collision(mesh, a)
        bounds = mesh.get_bounds()
        actual = [float(bounds.box_extent.x) * 2 / 100, float(bounds.box_extent.y) * 2 / 100,
                  float(bounds.box_extent.z) * 2 / 100]
        expected = a["dimensions_m"]
        if any(abs(x-y) > max(.05, abs(y)*.025) for x, y in zip(actual, expected)):
            raise RuntimeError("FBX unit/bounds mismatch for %s: actual=%s expected=%s" % (a["asset_id"], actual, expected))
        EAL.save_asset(mesh.get_path_name())
        meshes[a["asset_id"]] = mesh
        REPORT["meshes"].append({"asset_id": a["asset_id"], "ue_path": mesh.get_path_name(),
                                 "dimensions_m": actual, "material_slots": len(a["material_slots"]),
                                 "material_slot_mapping": slots_by_name,
                                 "source_sha256": hashlib.sha256(Path(local(a["fbx"])).read_bytes()).hexdigest()})
        if collision_policy:
            REPORT["meshes"][-1]["collision_policy"] = collision_policy
    # This script only owns its dedicated preview map and /Game/ArtPreview subtree.
    if EAL.does_asset_exist(MAP):
        LEVEL.load_level(MAP)
        for actor in ACTORS.get_all_level_actors():
            ACTORS.destroy_actor(actor)
    else:
        if not LEVEL.new_level(MAP):
            raise RuntimeError("Could not create preview level")
    enclosure_actors = []
    assets_by_id = {a["asset_id"]: a for a in DATA["assets"]}
    for instance in DATA["instances"]:
        actor = spawn_mesh(instance["instance_id"], meshes[instance["asset_id"]], instance["location_cm"],
                           instance.get("rotation_deg", (0, 0, 0)), instance.get("scale", (1, 1, 1)))
        asset = assets_by_id[instance["asset_id"]]
        if asset.get("courtyard_enclosure"):
            configure_enclosure_actor(actor)
            enclosure_actors.append((actor, asset))
        REPORT["actors"].append(instance["instance_id"])
    ground_mat = solid_material("M_StageGround", (.34, .29, .22))
    ground = spawn_mesh("SurroundingGround", unreal.load_asset("/Engine/BasicShapes/Cube"),
                        (0, 0, -35), scale=(1000, 1000, .5), material=ground_mat)
    # Clear daylight: no miniature DOF or ink overlay. Asset detail remains visible.
    sun = ACTORS.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 3000),
                                       unreal.Rotator(pitch=-45, yaw=65, roll=0))
    sun.set_actor_label("Preview_Sun")
    sun.light_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    sun.light_component.set_editor_property("intensity", 5.0)
    sun.light_component.set_editor_property("light_source_angle", 4.0)
    sun.light_component.set_editor_property("atmosphere_sun_light", True)
    sun.light_component.set_editor_property("light_color", unreal.Color(255, 244, 226, 255))
    sky = ACTORS.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 1600))
    sky.light_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    sky.light_component.set_editor_property("intensity", 1.8)
    sky.light_component.set_editor_property("real_time_capture", True)
    ACTORS.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
    pp = ACTORS.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
    pp.set_editor_property("unbound", True)
    settings = pp.get_editor_property("settings")
    for name, value in (("override_auto_exposure_method", True), ("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL),
                        ("override_auto_exposure_apply_physical_camera_exposure", True),
                        ("auto_exposure_apply_physical_camera_exposure", False),
                        ("override_auto_exposure_bias", True), ("auto_exposure_bias", .4),
                        ("override_vignette_intensity", True), ("vignette_intensity", .12),
                        ("override_bloom_intensity", True), ("bloom_intensity", .15)):
        settings.set_editor_property(name, value)
    pp.set_editor_property("settings", settings)
    # A visible 1.8m figure makes scale review possible without a new paid character.
    human_mat = solid_material("M_ScaleFigure", (.27, .32, .30))
    for plot in DATA["plots"]:
        x = plot["center_cm"][0] + 300
        y = -610
        spawn_mesh("ScaleFigure_Body_"+plot["level"], unreal.load_asset("/Engine/BasicShapes/Cylinder"),
                   (x,y,97), scale=(.38,.26,.78), material=human_mat)
        spawn_mesh("ScaleFigure_Head_"+plot["level"], unreal.load_asset("/Engine/BasicShapes/Sphere"),
                   (x,y,167), scale=(.26,.25,.26), material=human_mat)
        for dx in (-10,10):
            spawn_mesh("ScaleFigure_Leg_"+plot["level"]+str(dx), unreal.load_asset("/Engine/BasicShapes/Cylinder"),
                       (x+dx,y,31), scale=(.13,.15,.62), material=human_mat)
    # GameModeBase's DefaultPawn can start here for a freely navigable -game preview.
    start_position = (500, -3500, 3000)
    start_target = (0, 0, 150)
    dx, dy, dz = (start_target[i] - start_position[i] for i in range(3))
    start_rotation = unreal.Rotator(pitch=math.degrees(math.atan2(dz, math.hypot(dx, dy))),
                                   yaw=math.degrees(math.atan2(dy, dx)), roll=0)
    player_start = ACTORS.spawn_actor_from_class(unreal.PlayerStart, unreal.Vector(*start_position), start_rotation)
    player_start.set_actor_label("Preview_PlayerStart")
    REPORT["player_start"] = {"location_cm": list(start_position), "target_cm": list(start_target),
                              "rotation": {"pitch": float(start_rotation.pitch), "yaw": float(start_rotation.yaw), "roll": 0},
                              "mode": "GameModeBase_DefaultPawn"}
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    REPORT["enclosure_collision_checks"] = [verify_enclosure_passage(world, actor, asset)
                                            for actor, asset in enclosure_actors]
    if not LEVEL.save_current_level():
        raise RuntimeError("Preview map was not saved")
    EAL.save_directory(BASE, only_if_is_dirty=False, recursive=True)
    project_root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir()))
    REPORT["ue_files"] = [{"path": str(p.relative_to(project_root)),
                           "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size}
                          for p in sorted((project_root / "Content/ArtPreview").rglob("*"))
                          if p.is_file() and p.suffix in (".uasset", ".umap")]
    REPORT.update(status="passed", map=MAP, plot_size_m=16, gameplay_integration="not_modified",
                  level_to_house={p["level"]: meshes[p["house_asset_id"]].get_path_name() for p in DATA["plots"]})
    save_report()
    unreal.log("ZG_ART_IMPORT_PASS")
except Exception:
    REPORT.update(status="failed", error=traceback.format_exc())
    save_report()
    unreal.log_error(REPORT["error"])
    raise
