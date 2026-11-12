"""Prepare auditable Tripo houses and reusable courtyard assets in Blender.

Run: blender --background --python prepare_batch.py -- manifest.json output_dir
This script runs only in its own Blender process. It never contacts a service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import bpy
import numpy as np
from mathutils import Matrix, Vector

# Fixed local helpers are part of the trusted processing-script fingerprint.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from courtyard_geometry import enclosure_plan
from blender_job_runtime import AssetExporter, execute_job, validate_job_result


VERSION = 1
MATERIALS = {}
ASSETS = []
WARNINGS = []
OUTPUT = None
TEXTURE_LIMIT = 2048


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "Asset"


def relative(path):
    return Path(path).relative_to(OUTPUT).as_posix()


def select_only(objects):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]


def pixel_array(image):
    width, height = image.size
    if not width or not height:
        raise ValueError(f"Image has no pixels: {image.name}")
    values = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(values)
    return values.reshape(height, width, 4)


def resize(array, width, height):
    """Deterministic nearest sampling; no hidden colour-space conversion."""
    if array.shape[:2] == (height, width):
        return array.copy()
    yy = np.minimum((np.arange(height) * array.shape[0] / height).astype(int), array.shape[0] - 1)
    xx = np.minimum((np.arange(width) * array.shape[1] / width).astype(int), array.shape[1] - 1)
    return array[yy[:, None], xx[None, :]].copy()


def save_pixels(path, pixels, srgb=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.ascontiguousarray(np.clip(pixels, 0, 1), dtype=np.float32)
    image = bpy.data.images.new(path.stem, width=pixels.shape[1], height=pixels.shape[0], alpha=True)
    image.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)
    return path


def upstream_images(socket, visited=None):
    visited = set() if visited is None else visited
    found = []
    for link in socket.links:
        node = link.from_node
        if node.as_pointer() in visited:
            continue
        visited.add(node.as_pointer())
        if node.type == "TEX_IMAGE" and node.image:
            found.append(node.image)
        for inp in node.inputs:
            found.extend(upstream_images(inp, visited))
    return list(dict.fromkeys(found))


def constant(value, width, height):
    vals = np.asarray(value if hasattr(value, "__len__") else [value], dtype=np.float32)
    if len(vals) == 1:
        vals = np.array([vals[0], vals[0], vals[0], 1], dtype=np.float32)
    elif len(vals) == 3:
        vals = np.append(vals, 1).astype(np.float32)
    return np.broadcast_to(vals, (height, width, 4)).copy()


def evaluate(socket, width, height, depth=0):
    """Resolve actual glTF material links, including channel split and factors.

    Unsupported connected nodes are rejected instead of silently picking an image
    by filename. The small subset covers Blender's standard glTF importer graph.
    """
    if depth > 24:
        raise ValueError("Material graph is cyclic or too deep")
    if not socket.is_linked:
        return constant(getattr(socket, "default_value", 0), width, height)
    link = socket.links[0]
    node, output = link.from_node, link.from_socket
    ev = lambda inp: evaluate(inp, width, height, depth + 1)
    if node.type == "TEX_IMAGE":
        if not node.image:
            raise ValueError(f"Missing linked image: {node.name}")
        if node.inputs["Vector"].is_linked:
            vector_node = node.inputs["Vector"].links[0].from_node
            if vector_node.type not in {"UVMAP", "TEX_COORD"}:
                raise ValueError(f"Texture transform must be baked first: {node.name}")
        result = resize(pixel_array(node.image), width, height)
        if output.name == "Alpha":
            result[:, :, :3] = result[:, :, 3:4]
        return result
    if node.type in {"SEPRGB", "SEPARATE_COLOR"}:
        result = ev(node.inputs[0])
        channel = {"R": 0, "Red": 0, "G": 1, "Green": 1, "B": 2, "Blue": 2, "Alpha": 3}.get(output.name)
        if channel is None:
            raise ValueError(f"Unsupported split channel: {output.name}")
        result[:, :, :3] = result[:, :, channel:channel + 1]
        result[:, :, 3] = 1
        return result
    if node.type == "MATH":
        a, b = ev(node.inputs[0]), ev(node.inputs[1])
        operation = node.operation
        if operation == "MULTIPLY":
            result = a * b
        elif operation == "ADD":
            result = a + b
        elif operation == "SUBTRACT":
            result = a - b
        elif operation == "MULTIPLY_ADD":
            result = a * b + ev(node.inputs[2])
        elif operation == "MINIMUM":
            result = np.minimum(a, b)
        elif operation == "MAXIMUM":
            result = np.maximum(a, b)
        else:
            raise ValueError(f"Unsupported material math: {operation}")
        return np.clip(result, 0, 1) if node.use_clamp else result
    if node.type == "MIX_RGB":
        factor, a, b = [ev(inp) for inp in node.inputs[:3]]
        mixed = a * b if node.blend_type == "MULTIPLY" else b if node.blend_type == "MIX" else None
        if mixed is None:
            raise ValueError(f"Unsupported material blend: {node.blend_type}")
        return a * (1 - factor) + mixed * factor
    if node.type == "MIX" and node.data_type == "RGBA":
        factor, a, b = ev(node.inputs[0]), ev(node.inputs[6]), ev(node.inputs[7])
        mixed = a * b if node.blend_type == "MULTIPLY" else b if node.blend_type == "MIX" else None
        if mixed is None:
            raise ValueError(f"Unsupported material blend: {node.blend_type}")
        return a * (1 - factor) + mixed * factor
    if node.type == "REROUTE":
        return ev(node.inputs[0])
    if node.type in {"RGB", "VALUE"}:
        return constant(output.default_value, width, height)
    raise ValueError(f"Unsupported material node {node.type}: {node.name}")


def material_socket(material):
    if material and material.use_nodes:
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
    return None


def texture_node(nodes, path, srgb=False):
    node = nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(str(path), check_existing=True)
    node.image.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
    return node


def make_material(name, base_path, orm_path, normal_gl_path, descriptor):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    base = texture_node(nodes, base_path, True)
    links.new(base.outputs["Color"], bsdf.inputs["Base Color"])
    orm = texture_node(nodes, orm_path)
    separate = nodes.new("ShaderNodeSeparateColor")
    links.new(orm.outputs["Color"], separate.inputs[0])
    links.new(separate.outputs["Green"], bsdf.inputs["Roughness"])
    links.new(separate.outputs["Blue"], bsdf.inputs["Metallic"])
    normal = texture_node(nodes, normal_gl_path)
    nmap = nodes.new("ShaderNodeNormalMap")
    links.new(normal.outputs["Color"], nmap.inputs["Color"])
    links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    MATERIALS[name] = descriptor
    return material


def process_material(source, name, source_normal_convention):
    bsdf = material_socket(source)
    base_socket = bsdf.inputs["Base Color"] if bsdf else None
    images = upstream_images(base_socket) if base_socket else []
    # Include other maps when the base is constant, so detailed normals are retained.
    if bsdf:
        for key in ("Roughness", "Metallic", "Normal"):
            images.extend(upstream_images(bsdf.inputs[key]))
    if source and source.use_nodes:
        for node in source.node_tree.nodes:
            if node.type == "GROUP" and "Occlusion" in node.inputs:
                images.extend(upstream_images(node.inputs["Occlusion"]))
    images = list(dict.fromkeys(images))
    width = max([int(img.size[0]) for img in images] + [16])
    height = max([int(img.size[1]) for img in images] + [16])
    ratio = min(1, TEXTURE_LIMIT / max(width, height))
    width, height = max(1, round(width * ratio)), max(1, round(height * ratio))
    folder = OUTPUT / "textures" / name
    originals = []
    for index, img in enumerate(images):
        path = folder / "source" / f"{index:02d}_{safe_name(img.name)}.png"
        save_pixels(path, pixel_array(img), img.colorspace_settings.name == "sRGB")
        originals.append({"path": relative(path), "sha256": sha256(path), "image_name": img.name,
                          "colorspace": img.colorspace_settings.name})
    base = evaluate(base_socket, width, height) if base_socket else constant(source.diffuse_color if source else (.5, .5, .5, 1), width, height)
    if bsdf and (bsdf.inputs["Alpha"].is_linked or bsdf.inputs["Alpha"].default_value < .999):
        alpha = evaluate(bsdf.inputs["Alpha"], width, height)[:, :, 0]
        if float(alpha.min()) < .999:
            raise ValueError(f"Transparent/cutout material needs explicit treatment before import: {name}")
    original_base_path = save_pixels(folder / f"T_{name}_BaseColor_Original.png", base, True)
    # A: restrained colour grouping; retain 88% source detail, no spatial blur.
    styled = base.copy()
    grouped = np.round(base[:, :, :3] * 28) / 28
    styled[:, :, :3] = np.clip((base[:, :, :3] * .88 + grouped * .12) * [1.015, 1.0, .985], 0, 1)
    base_path = save_pixels(folder / f"T_{name}_BaseColor_A.png", styled, True)
    rough = evaluate(bsdf.inputs["Roughness"], width, height)[:, :, 0] if bsdf else np.full((height, width), .7)
    metal = evaluate(bsdf.inputs["Metallic"], width, height)[:, :, 0] if bsdf else np.zeros((height, width))
    ao = np.ones((height, width), dtype=np.float32)
    ao_source = "missing_filled_one"
    if source and source.use_nodes:
        for node in source.node_tree.nodes:
            # Blender glTF Material Output stores the separately connected AO signal.
            if node.type == "GROUP" and "Occlusion" in node.inputs:
                ao = evaluate(node.inputs["Occlusion"], width, height)[:, :, 0]
                ao_source = "linked_gltf_occlusion" if node.inputs["Occlusion"].is_linked else "gltf_occlusion_constant"
                break
    orm = np.ones((height, width, 4), dtype=np.float32)
    orm[:, :, 0], orm[:, :, 1], orm[:, :, 2] = ao, np.clip(rough * .96 + .025, 0, 1), metal
    orm_path = save_pixels(folder / f"T_{name}_ORM.png", orm)
    normal = constant((.5, .5, 1, 1), width, height)
    normal_source = "flat_fallback"
    if bsdf and bsdf.inputs["Normal"].is_linked:
        nmap = bsdf.inputs["Normal"].links[0].from_node
        if nmap.type != "NORMAL_MAP":
            raise ValueError(f"Normal must use Normal Map node: {name}")
        normal = evaluate(nmap.inputs["Color"], width, height)
        normal_source = "linked_normal_map"
    original_normal_path = save_pixels(folder / f"T_{name}_Normal_Source.png", normal)
    dx, gl = normal.copy(), normal.copy()
    if source_normal_convention == "opengl":
        dx[:, :, 1] = 1 - dx[:, :, 1]
    elif source_normal_convention == "directx":
        gl[:, :, 1] = 1 - gl[:, :, 1]
    else:
        raise ValueError(f"Unknown normal convention: {source_normal_convention}")
    dx_path = save_pixels(folder / f"T_{name}_Normal_DX.png", dx)
    gl_path = save_pixels(folder / f"T_{name}_Normal_GL.png", gl)
    descriptor = {"material_name": name, "base_color": relative(base_path), "orm": relative(orm_path),
                  "normal": relative(dx_path), "normal_convention": "directx", "source_normal_convention": source_normal_convention,
                  "normal_green_flips_for_ue": int(source_normal_convention == "opengl"),
                  "normal_source": normal_source, "ao_source": ao_source,
                  "base_color_original": relative(original_base_path), "normal_original": relative(original_normal_path),
                  "source_images": originals, "texture_size": [width, height], "srgb": {"base_color": True, "orm": False, "normal": False},
                  "style": {"id": "A", "source_detail_weight": .88, "quantization_levels": 28, "spatial_blur": False}}
    return make_material(name, base_path, orm_path, gl_path, descriptor)


def bounds(objects):
    coords = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    minimum = Vector(tuple(min(v[axis] for v in coords) for axis in range(3)))
    maximum = Vector(tuple(max(v[axis] for v in coords) for axis in range(3)))
    return minimum, maximum


def join_meshes(objects, name):
    select_only(objects)
    bpy.ops.object.convert(target="MESH")
    if len(objects) > 1:
        bpy.ops.object.join()
    obj = bpy.context.object
    obj.name = name
    # Geometry becomes mesh-local world metres; exported origin is at the ground.
    obj.data.transform(obj.matrix_world)
    obj.matrix_world = Matrix.Identity(4)
    obj.data.update()
    return obj


def export_mesh(obj, asset_type, provenance, extra=None, *, compensate_ue_local_y=False):
    path = OUTPUT / "meshes" / f"{obj.name}.fbx"
    path.parent.mkdir(parents=True, exist_ok=True)
    exported = obj
    axis_audit = None
    if compensate_ue_local_y:
        # This compound module contains a directional opening, unlike a wall
        # segment placed by UE transforms. Precompensate UE's verified FBX local-Y
        # reflection only in an export copy; the authored Blender scene stays put.
        import bmesh
        bpy.context.view_layer.update()
        def geometry_fingerprint(mesh):
            positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", positions)
            digest = hashlib.sha256(positions.tobytes())
            for uv in mesh.uv_layers:
                coordinates = np.empty(len(uv.data) * 2, dtype=np.float32)
                uv.data.foreach_get("uv", coordinates)
                digest.update(coordinates.tobytes())
            return digest.hexdigest()
        original_fingerprint = geometry_fingerprint(obj.data)
        exported = obj.copy()
        exported.data = obj.data.copy()
        bpy.context.collection.objects.link(exported)
        exported.matrix_world = obj.matrix_world.copy()
        exported.data.transform(Matrix.Diagonal(Vector((1, -1, 1, 1))))
        mesh = bmesh.new()
        mesh.from_mesh(exported.data)
        bmesh.ops.reverse_faces(mesh, faces=list(mesh.faces))
        mesh.to_mesh(exported.data)
        mesh.free()
        exported.data.update()
        bpy.context.view_layer.update()
        low_export, high_export = bounds([exported])
        axis_audit = {"operation": "mirror_local_y_and_reverse_faces_on_export_copy",
                      "authored_geometry_sha256": original_fingerprint,
                      "export_geometry_sha256": geometry_fingerprint(exported.data),
                      "fbx_bounds_before_ue_m": [list(low_export), list(high_export)],
                      "vertex_count": len(obj.data.vertices)}
    try:
        select_only([exported])
        bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, object_types={"MESH"},
                                 global_scale=1.0, apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
                                 axis_forward="-Y", axis_up="Z", bake_space_transform=False,
                                 use_mesh_modifiers=True, mesh_smooth_type="FACE", use_tspace=True, use_triangles=True,
                                 add_leaf_bones=False, bake_anim=False, path_mode="STRIP", embed_textures=False)
    finally:
        if exported != obj:
            temporary_mesh = exported.data
            bpy.data.objects.remove(exported, do_unlink=True)
            bpy.data.meshes.remove(temporary_mesh)
            axis_audit["authored_mesh_and_uv_preserved"] = geometry_fingerprint(obj.data) == original_fingerprint
            if not axis_audit["authored_mesh_and_uv_preserved"]:
                raise RuntimeError("Export-axis compensation changed the authored courtyard mesh")
    obj.data.calc_loop_triangles()
    low, high = bounds([obj])
    record = {"asset_id": obj.name, "name": obj.name, "type": asset_type, "fbx": relative(path),
              "fbx_sha256": sha256(path), "dimensions_m": list(high - low), "bounds_m": [list(low), list(high)],
              "vertices": len(obj.data.vertices), "triangles": len(obj.data.loop_triangles),
              "material_slots": [{"slot_index": i, "slot_name": slot.name, "material": slot.name}
                                 for i, slot in enumerate(obj.material_slots)],
              "origin": "bottom_center" if abs(low.z) < 1e-5 else "authored_module_anchor",
              "unit": "meter", "ue_import_uniform_scale": 1.0,
              "fbx_unit_note": "Scene unit meter; FBX carries unit conversion. UE ConvertSceneUnit=true, import scale=1; verify measured bounds.",
              "front_axis": "-Y", "provenance": provenance}
    if extra:
        record.update(extra)
    if compensate_ue_local_y:
        record["fbx_local_y_precompensated"] = True
        record["coordinate_export_note"] = "UE FBX local-Y reflection restores authored doorway and inward-open leaves; instance yaw stays zero"
        record["coordinate_export_audit"] = axis_audit
    ASSETS.append(record)
    return record


def process_house(entry, manifest_dir, *, apply_model_repairs=True):
    path = Path(entry["input_glb"])
    path = path if path.is_absolute() else manifest_dir / path
    path = path.resolve(strict=True)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    imported = [o for o in bpy.data.objects if o not in before]
    imported_names = [o.name for o in imported]
    meshes = [o for o in imported if o.type == "MESH"]
    if not meshes:
        raise ValueError(f"No mesh imported from {path}")
    name = safe_name(entry.get("mesh_name", f"SM_House_{entry['level']}_A"))
    # Bake parent/world transforms before applying a shared orientation and scale.
    for obj in meshes:
        transform = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = transform
    rotation = Matrix.Rotation(math.radians(float(entry.get("front_rotation_deg", 90))), 4, "Z")
    for obj in meshes:
        obj.matrix_world = rotation @ obj.matrix_world
    bpy.context.view_layer.update()
    low, high = bounds(meshes)
    dims = high - low
    if min(dims) <= 1e-6:
        raise ValueError(f"Degenerate dimensions: {name}: {dims}")
    target = float(entry["target_height_m"])
    maxw, maxd = map(float, entry["max_footprint_m"])
    factor = min(target / dims.z, maxw / dims.x, maxd / dims.y)
    if factor <= 0:
        raise ValueError("Dimensions must be positive")
    center = Vector(((low.x + high.x) / 2, (low.y + high.y) / 2, low.z))
    normalization = Matrix.Diagonal(Vector((factor, factor, factor, 1))) @ Matrix.Translation(-center)
    material_map = {}
    for obj in meshes:
        obj.matrix_world = normalization @ obj.matrix_world
        if not obj.data.materials:
            obj.data.materials.append(bpy.data.materials.new(f"{name}_Fallback"))
        for slot in obj.material_slots:
            original = slot.material
            key = original.as_pointer() if original else 0
            if key not in material_map:
                matname = f"M_{name.removeprefix('SM_')}_{len(material_map):02d}"
                material_map[key] = process_material(original, matname, entry.get("source_normal_convention", "opengl"))
            slot.material = material_map[key]
    house = join_meshes(meshes, name)
    roof_audit = None
    if apply_model_repairs:
        from roof_repair import apply_roof_repair
        roof_audit = apply_roof_repair(house, source_sha256=sha256(path), level=entry["level"], output_dir=OUTPUT)
    for old_name in imported_names:
        obj = bpy.data.objects.get(old_name)
        if obj and obj != house:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.update()
    record = export_mesh(house, "building", {"kind": "tripo_glb", "input_path": str(path), "input_sha256": sha256(path)},
                         {"level": entry["level"], "original_dimensions_m": list(dims), "uniform_scale_applied": factor,
                          "target_height_m": target, "max_footprint_m": [maxw, maxd],
                          "front_rotation_deg": entry.get("front_rotation_deg", 90),
                          "orientation_verified": bool(entry.get("orientation_verified", False)),
                          "height_limited_by_footprint": factor < target / dims.z - 1e-8})
    if roof_audit is not None:
        record["roof_repair"] = roof_audit
    if not record["orientation_verified"]:
        WARNINGS.append(f"{name}: front orientation is a configured assumption; confirm doorway faces -Y in the contact sheet.")
    return house


def procedural_material(name, color, roughness, pattern="plain", metallic=0):
    size = 1024 if pattern == "packed_ground" else 256
    yy, xx = np.mgrid[0:size, 0:size]
    rng = np.random.default_rng(int(hashlib.sha256(name.encode()).hexdigest()[:8], 16))
    grain = rng.normal(0, .015, (size, size))
    if pattern == "wood":
        grain += .035 * np.sin(xx * .24 + np.sin(yy * .03) * 1.3) + .015 * np.sin(xx * 1.3)
    elif pattern == "cloth":
        grain += .018 * ((xx % 4 < 2) + (yy % 4 < 2))
    elif pattern == "earth":
        grain += .025 * np.sin(xx * .09) * np.cos(yy * .08)
    elif pattern == "plaster":
        # Mild irregular limewash variation, without the repeated sinusoidal
        # checker pattern that becomes conspicuous on a continuous long wall.
        grain = rng.normal(0, .004, (size, size))
        for cells, amplitude in ((8, .011), (24, .004)):
            grid = rng.normal(0, amplitude, (cells + 1, cells + 1))
            grid[-1, :] = grid[0, :]
            grid[:, -1] = grid[:, 0]
            knots = np.linspace(0, size, cells + 1)
            horizontal = np.array([np.interp(np.arange(size), knots, row) for row in grid])
            grain += np.array([np.interp(np.arange(size), knots, horizontal[:, x]) for x in range(size)]).T
    elif pattern == "packed_ground":
        grain = rng.normal(0, .009, (size, size))
        grain += .004 * np.sin(xx * 1.7) * np.cos(yy * 2.1)
        grain += .008 * np.sin(xx * .017) * np.cos(yy * .013)
    base = np.ones((size, size, 4), dtype=np.float32)
    base[:, :, :3] = np.clip(np.array(color)[None, None, :] + grain[:, :, None], 0, 1)
    orm = constant((1, roughness, metallic, 1), size, size)
    normal = constant((.5, .5, 1, 1), size, size)
    folder = OUTPUT / "textures" / name
    paths = {"base": save_pixels(folder / f"T_{name}_BaseColor.png", base, True),
             "orm": save_pixels(folder / f"T_{name}_ORM.png", orm),
             "normal": save_pixels(folder / f"T_{name}_Normal_DX.png", normal)}
    descriptor = {"material_name": name, "base_color": relative(paths["base"]), "orm": relative(paths["orm"]),
                  "normal": relative(paths["normal"]), "normal_convention": "directx", "normal_green_flips_for_ue": 0,
                  "ao_source": "procedural_one", "texture_size": [size, size], "provenance": "local_procedural_zero_api_cost",
                  "srgb": {"base_color": True, "orm": False, "normal": False}}
    return make_material(name, paths["base"], paths["orm"], paths["normal"], descriptor)


def box(name, center, size, material, bevel=.012):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(material)
    if bevel:
        modifier = obj.modifiers.new("CraftedEdges", "BEVEL")
        modifier.width = min(bevel, min(size) * .18)
        modifier.segments = 2
    return obj


def cylinder(name, center, radius, depth, material, rotation=(0, 0, 0), vertices=16):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth, end_fill_type="NGON", location=center, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    bevel = obj.modifiers.new("CutEdges", "BEVEL")
    bevel.width = .006
    bevel.segments = 2
    return obj


def timber_between(name, start, end, thickness, material):
    start, end = Vector(start), Vector(end)
    direction = end - start
    obj = box(name, (start + end) / 2, (thickness, thickness, direction.length), material, .008)
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = direction.to_track_quat("Z", "Y")
    return obj


def pottery(name, material):
    # Closed cross-section including interior wall and bottom; genuinely open rim.
    profile = [(.10, 0), (.15, .02), (.22, .10), (.27, .29), (.24, .43), (.17, .52),
               (.16, .58), (.18, .59), (.18, .62), (.145, .62), (.13, .57), (.14, .52),
               (.21, .42), (.24, .29), (.19, .11), (.12, .045), (0, .045), (0, 0)]
    segments = 48
    verts = [(r * math.cos(i * math.tau / segments), r * math.sin(i * math.tau / segments), z)
             for r, z in profile for i in range(segments)]
    faces = []
    for ring in range(len(profile)):
        next_ring = (ring + 1) % len(profile)
        for i in range(segments):
            faces.append((ring * segments + i, ring * segments + (i + 1) % segments,
                          next_ring * segments + (i + 1) % segments, next_ring * segments + i))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for poly in mesh.polygons:
        poly.use_smooth = True
        for loop_index in poly.loop_indices:
            vi = mesh.loops[loop_index].vertex_index
            uv.data[loop_index].uv = ((vi % segments) / segments, (vi // segments) / (len(profile) - 1))
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material)
    return obj


def finish_prop(name, parts, asset_type="prop"):
    obj = join_meshes(parts, name)
    export_mesh(obj, asset_type, {"kind": "local_procedural", "api_cost": 0})
    return obj


def tiled_courtyard_roof(part, material):
    """Closed, individually curved clay tiles on two straight roof slopes."""
    length, depth = part["length"], part["depth"]
    columns, rows = math.ceil(length / .18), max(1, math.ceil(depth / 2 / .24))
    width, course = length / columns, depth / 2 / rows
    vertices, faces = [], []
    for sign in (-1, 1):
        for column in range(columns):
            for row in range(rows):
                offset = len(vertices)
                for bottom in (False, True):
                    for distance in (max(0, row * course - .012), (row + 1) * course):
                        for j in range(7):
                            x = -length / 2 + (column + .5) * width + (j / 6 - .5) * (width - .002)
                            z = part["ridge"] + (part["eave"] - part["ridge"]) * distance / (depth / 2)
                            z += .012 * math.sin(j * math.pi / 6) + (rows - row - 1) * .009
                            vertices.append((x, sign * distance, z - (.025 if bottom else 0)))
                tile_faces = []
                for j in range(6):
                    tile_faces.extend([(j, j + 1, j + 8, j + 7), (j + 14, j + 21, j + 22, j + 15),
                                       (j, j + 14, j + 15, j + 1), (j + 7, j + 8, j + 22, j + 21)])
                tile_faces.extend([(0, 7, 21, 14), (6, 20, 27, 13)])
                if sign < 0:
                    tile_faces = [tuple(reversed(face)) for face in tile_faces]
                faces.extend(tuple(offset + index for index in face) for face in tile_faces)
    mesh = bpy.data.meshes.new(part["name"])
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for loop in mesh.loops:
        point = mesh.vertices[loop.vertex_index].co
        uv.data[loop.index].uv = (point.x / .18, point.y / .24)
    obj = bpy.data.objects.new(part["name"], mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material)
    obj.location = (*part["center"], 0)
    obj.rotation_euler.z = math.radians(part["yaw"])
    ridge = box(part["name"] + "StraightRidge", (*part["center"], part["ridge"] + .025),
                (length, .075, .075), material, .018)
    ridge.rotation_euler.z = math.radians(part["yaw"])
    return [obj, ridge]


def create_courtyard_enclosure(level, gate_x, materials):
    plan = enclosure_plan(level, gate_x)
    parts = []
    for part in plan["parts"]:
        material = materials[part["material"]]
        if part["kind"] == "tiled_roof":
            parts.extend(tiled_courtyard_roof(part, material))
        else:
            obj = box(part["name"], part["center"], part["size"], material, part["bevel"])
            # Long continuous walls retain metre-based grain density instead of
            # stretching one cube UV over a fifteen-metre plaster surface.
            uv = obj.data.uv_layers.active or obj.data.uv_layers.new(name="UVMap")
            for polygon in obj.data.polygons:
                axis = max(range(3), key=lambda i: abs(polygon.normal[i]))
                axes = [i for i in range(3) if i != axis]
                for loop_index in polygon.loop_indices:
                    point = obj.data.vertices[obj.data.loops[loop_index].vertex_index].co
                    uv.data[loop_index].uv = (point[axes[0]], point[axes[1]])
            obj.rotation_euler.z = math.radians(part["yaw"])
            parts.append(obj)
    obj = join_meshes(parts, f"SM_Court_Enclosure_{level}_A")
    metadata = {key: value for key, value in plan.items() if key != "parts"}
    metadata["authored_component_count"] = len(plan["parts"])
    export_mesh(obj, "courtyard_module", {"kind": "local_procedural", "api_cost": 0},
                {"courtyard_enclosure": metadata}, compensate_ue_local_y=True)
    return obj


def create_props(entries=None):
    wood = procedural_material("M_Life_Wood_A", (.31, .19, .09), .78, "wood")
    oldwood = procedural_material("M_Life_DarkWood_A", (.17, .105, .06), .84, "wood")
    ceramic = procedural_material("M_Life_Terracotta_A", (.42, .21, .115), .73, "earth")
    cloth = procedural_material("M_Life_Linen_A", (.60, .55, .41), .90, "cloth")
    metal = procedural_material("M_Life_Iron_A", (.16, .15, .13), .52, metallic=.8)
    earth = procedural_material("M_Court_Earth_A", (.42, .355, .25), .94, "packed_ground")
    stone = procedural_material("M_Court_Stone_A", (.38, .37, .32), .88, "earth")
    plaster = procedural_material("M_Court_Plaster_A", (.64, .59, .47), .88, "plaster")
    roof = procedural_material("M_Court_RoofTile_A", (.235, .26, .275), .83, "earth")
    path_earth = procedural_material("M_Court_BeatenEarth_A", (.455, .38, .265), .94, "packed_ground")
    props = {}
    props["jar"] = finish_prop("SM_Life_Pottery_A", [pottery("Pottery", ceramic)])
    parts = []
    for row in range(3):
        for col in range(5 - row):
            parts.append(cylinder("Firewood", ((col - (4 - row) / 2) * .20, 0, .105 + row * .17), .095, .72, wood, (math.pi / 2, 0, 0), 10))
    props["woodpile"] = finish_prop("SM_Life_Woodpile_A", parts)
    parts = [box("Bottom", (0, 0, .04), (.90, .65, .08), oldwood)]
    for z in (.14, .30, .46):
        for y in (-.31, .31):
            parts.append(box("CratePlank", (0, y, z), (.90, .055, .14), wood))
        for x in (-.425, .425):
            parts.append(box("CratePlank", (x, 0, z), (.055, .60, .14), wood))
    for x in (-.39, .39):
        for y in (-.27, .27):
            parts.append(box("Corner", (x, y, .30), (.07, .07, .60), oldwood))
    props["crate"] = finish_prop("SM_Life_Crate_A", parts)
    parts = [box("Seat", (0, 0, .44), (.80, .34, .07), wood)]
    for x in (-.30, .30):
        for y in (-.10, .10):
            parts.append(box("StoolLeg", (x, y, .22), (.055, .055, .44), oldwood))
    parts.append(box("Stretcher", (0, 0, .15), (.65, .045, .045), oldwood))
    props["stool"] = finish_prop("SM_Life_Stool_A", parts)
    parts = []
    for x in (-1.0, 1.0):
        parts.append(cylinder("DryingPost", (x, 0, .95), .045, 1.9, wood))
        parts.append(box("DryingFoot", (x, 0, .05), (.13, .80, .10), oldwood))
    parts.append(cylinder("DryingBar", (0, 0, 1.85), .035, 2.16, wood, (0, math.pi / 2, 0)))
    # Folded cloth strips have modelled gentle undulation, independent of the house.
    for idx in range(3):
        verts, faces = [], []
        for row in range(17):
            for col in range(9):
                u, v = col / 8, row / 16
                verts.append((-.83 + idx * .57 + u * .43, .035 * math.sin(u * math.tau * 3 + v), 1.82 - v * 1.05))
        for row in range(16):
            for col in range(8):
                a = row * 9 + col
                faces.append((a, a + 1, a + 10, a + 9))
        mesh = bpy.data.meshes.new("Linen")
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        obj = bpy.data.objects.new("Linen", mesh)
        bpy.context.collection.objects.link(obj)
        obj.data.materials.append(cloth)
        uv = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            uv.data[loop.index].uv = (loop.vertex_index % 9 / 8, loop.vertex_index // 9 / 16)
        solid = obj.modifiers.new("ClothThickness", "SOLIDIFY")
        solid.thickness = .004
        parts.append(obj)
    props["drying"] = finish_prop("SM_Life_DryingRack_A", parts)
    parts = [cylinder("HoeHandle", (0, 0, .72), .025, 1.44, wood), box("HoeBlade", (.11, 0, .07), (.27, .20, .025), metal)]
    props["tool"] = finish_prop("SM_Life_Hoe_A", parts)
    props["earth"] = finish_prop("SM_Court_EarthTile_A", [box("Earth", (0, 0, -.04), (1, 1, .08), earth, 0)], "courtyard_module")
    props["dirtpath"] = finish_prop("SM_Court_DirtPath_A", [box("DirtPath", (0, 0, .008), (1, 1, .016), path_earth, 0)], "courtyard_module")
    props["paving"] = finish_prop("SM_Court_StonePaving_A", [box("Paving", (0, 0, .025), (.97, .97, .05), stone, .008)], "courtyard_module")
    enclosure_materials = {"wood": wood, "darkwood": oldwood, "stone": stone,
                           "plaster": plaster, "roof": roof, "iron": metal}
    entrances = {entry["level"]: float(entry.get("entrance_local_m", [0])[0]) for entry in (entries or [])}
    for level in ("L4", "L8"):
        props["enclosure_" + level.lower()] = create_courtyard_enclosure(level, entrances.get(level, 0), enclosure_materials)
    # Reusable open-front service wings. Length changes by adding a structural bay,
    # not stretching the roof, timber thickness, or height of the building.
    def service_wing(name, length, bays):
        parts = []
        post_half = length / 2 - .35
        posts = [-post_half + i * 2 * post_half / bays for i in range(bays + 1)]
        for x in posts:
            for y in (-.65, .65):
                parts.append(box("WingPost", (x, y, 1), (.115, .115, 2), wood))
                brace_x = x + .38 if x < post_half - .01 else x - .38
                parts.append(timber_between("WingBraceX", (x, y, 1.48), (brace_x, y, 1.97), .065, oldwood))
                parts.append(timber_between("WingBraceY", (x, y, 1.53), (x, y - math.copysign(.34, y), 1.97), .065, oldwood))
            parts.append(box("WingTie", (x, 0, 2), (.13, 1.58, .14), wood))
            for sign in (-1, 1):
                parts.append(timber_between("WingRafter", (x, sign * .95, 2), (x, 0, 2.48), .08, oldwood))
        for y in (-.65, .65):
            parts.append(box("WingBeam", (0, y, 2), (length - .4, .13, .14), wood))
        for left, right in zip(posts, posts[1:]):
            parts.append(box("WingBackPlaster", ((left + right) / 2, .67, .96), (right - left - .10, .085, 1.78), plaster))
            parts.append(box("WingBackRail", ((left + right) / 2, .615, .40), (right - left, .055, .08), oldwood))
        for x in (-post_half, post_half):
            parts.append(box("WingSidePlaster", (x, 0, .63), (.085, 1.22, 1.15), plaster))
            parts.append(box("WingSideRail", (x, 0, 1.24), (.13, 1.35, .10), oldwood))
        slope_ratio = .48 / .95
        courses = math.ceil(length / .12)
        tile_width = length / courses
        for sign in (-1, 1):
            for index in range(courses):
                for row in range(4):
                    # Four overlapping curved ceramic tiles down each roof slope,
                    # rather than continuous strips resembling sheet metal.
                    verts = []
                    for bottom in (False, True):
                        for v in (-.15, .15):
                            distance = .12 + row * .24 + v
                            for j in range(7):
                                u = (j / 6 - .5) * (tile_width - .002)
                                curve = .014 * math.sin(j * math.pi / 6)
                                verts.append(((index - (courses - 1) / 2) * tile_width + u, sign * distance,
                                              2.56 - slope_ratio * distance + curve + (3 - row) * .008 - (.014 if bottom else 0)))
                    faces = []
                    for j in range(6):
                        faces.extend([(j, j + 1, j + 8, j + 7), (j + 14, j + 21, j + 22, j + 15)])
                    faces.extend([(j, j + 14, j + 15, j + 1) for j in range(6)])
                    faces.extend([(j + 7, j + 8, j + 22, j + 21) for j in range(6)])
                    faces.extend([(0, 7, 21, 14), (6, 20, 27, 13)])
                    if sign < 0:
                        faces = [tuple(reversed(face)) for face in faces]
                    mesh = bpy.data.meshes.new("OverlappingRoofTile")
                    mesh.from_pydata(verts, [], faces)
                    mesh.update()
                    uv = mesh.uv_layers.new(name="UVMap")
                    for loop in mesh.loops:
                        uv.data[loop.index].uv = ((loop.vertex_index % 7) / 6, (loop.vertex_index // 7) % 2)
                    obj = bpy.data.objects.new("OverlappingRoofTile", mesh)
                    bpy.context.collection.objects.link(obj)
                    obj.data.materials.append(roof)
                    parts.append(obj)
        parts.append(cylinder("WingRidge", (0, 0, 2.62), .06, length, roof, (0, math.pi / 2, 0)))
        parts.append(box("WorkCounter", (0, .37, .82), (length - 1.1, .52, .10), wood))
        for x in (-post_half + .20, post_half - .20):
            for y in (.20, .54):
                parts.append(box("CounterLeg", (x, y, .39), (.065, .065, .78), oldwood))
        return finish_prop(name, parts)
    props["wing_short"] = service_wing("SM_Life_ServiceWing_Short_A", 2.86, 1)
    props["wing_long"] = service_wing("SM_Life_ServiceWing_Long_A", 4.30, 2)
    return props


def assemble(houses, props, entries, plot_size):
    instances, plots = [], []
    display = bpy.data.collections.new("Courtyard_Preview")
    bpy.context.scene.collection.children.link(display)

    def instance(obj, plot_id, location, rotation=0, scale=(1, 1, 1), role="prop", ue_yaw=None):
        copy = obj.copy()
        copy.data = obj.data
        display.objects.link(copy)
        copy.name = f"{plot_id}_{obj.name}_{len(instances):04d}"
        copy.location = location
        copy.rotation_euler = (0, 0, math.radians(rotation))
        copy.scale = scale
        instances.append({"instance_id": copy.name, "plot_id": plot_id, "level": plot_id.removeprefix("Plot_"), "asset_id": obj.name,
                          "location_cm": [round(x * 100, 6) for x in location],
                          "rotation_deg": [0, 0, rotation if ue_yaw is None else ue_yaw], "rotation_convention": "XYZ_euler_degrees_Z_yaw",
                          "scale": list(scale), "layout_role": role})
        if ue_yaw is not None:
            instances[-1]["blender_rotation_deg"] = [0, 0, rotation]
            instances[-1]["ue_rotation_override_reason"] = "FBX_local_Y_handedness_calibrated_from_UE_front_and_rear_views"
        return copy.name

    def surface(obj, plot_id, ox, xmin, xmax, ymin, ymax, role):
        """Tile a specified walkable rectangle; small joints never disconnect a route."""
        if xmax <= xmin or ymax <= ymin:
            raise ValueError(f"Invalid courtyard surface: {plot_id}: {role}")
        nx, ny = math.ceil(xmax - xmin), math.ceil(ymax - ymin)
        cellx, celly = (xmax - xmin) / nx, (ymax - ymin) / ny
        module_width = .97 if obj == props["paving"] else 1.0
        gap = .018 if obj == props["paving"] else 0
        return [instance(obj, plot_id,
                         (ox + xmin + (x + .5) * cellx, ymin + (y + .5) * celly, .004),
                         scale=((cellx - gap) / module_width, (celly - gap) / module_width, 1), role=role)
                for x in range(nx) for y in range(ny)]

    for index, (house, entry) in enumerate(zip(houses, entries)):
        level = entry["level"]
        rank = {"L1": 1, "L4": 4, "L8": 8}.get(level, 4)
        ox = (index - (len(houses) - 1) / 2) * (plot_size + 4)
        plot_id = f"Plot_{level}"
        house_y = 2.0
        instance(house, plot_id, (ox, house_y, 0), role="building")
        instance(props["earth"], plot_id, (ox, 0, 0), scale=(plot_size, plot_size, 1), role="ground")
        # Ground-level centre-strip geometry locates a projecting door stair rather
        # than the roof overhang. The explicit override uses processed local metres.
        low, high = bounds([house])
        if "entrance_local_m" in entry:
            doorx, doory = map(float, entry["entrance_local_m"][:2])
            anchor_method = "configured_processed_local_m"
        else:
            candidates = [v.co.y for v in house.data.vertices if abs(v.co.x) <= .75 and -.01 <= v.co.z <= .70]
            doorx, doory = 0.0, min(candidates) if candidates else low.y
            anchor_method = "ground_level_center_strip" if candidates else "front_bounds_fallback"
        if not (low.x - .05 <= doorx <= high.x + .05 and low.y - .05 <= doory <= high.y + .05):
            raise ValueError(f"Doorway anchor outside house bounds: {plot_id}")
        doorway_y = house_y + doory
        path_start = -plot_size / 2 + .08
        if doorway_y < path_start + 1:
            raise ValueError(f"Insufficient forecourt depth for pedestrian route: {plot_id}")
        path_width = 1.15 if rank == 1 else 1.8 if rank == 4 else 2.4
        apron_depth = 0 if rank == 1 else 1.15 if rank == 4 else 1.50
        path_end = doorway_y - apron_depth
        path_obj = props["dirtpath"] if rank == 1 else props["paving"]
        route = surface(path_obj, plot_id, ox, doorx - path_width / 2, doorx + path_width / 2,
                        path_start, path_end + .015, "entrance_path")
        apron = []
        if apron_depth:
            apron_width = min(high.x - low.x, 3.8 if rank == 4 else 5.8)
            apron = surface(props["paving"], plot_id, ox, doorx - apron_width / 2, doorx + apron_width / 2,
                            path_end + .015, doorway_y + .025, "doorstep_apron")
        half = plot_size / 2 - .35
        enclosure = enclosure_plan(level, doorx, plot_size)
        if rank > 1:
            instance(props["enclosure_" + level.lower()], plot_id, (ox, 0, 0), role="enclosure")
        # L1 remains an open workyard. No disconnected demonstration wall is
        # left behind the house; the house and life props are unchanged.
        wing_layout = None
        if rank > 1:
            wing_length = 2.86 if rank == 4 else 4.30
            wing_x, wing_y, wing_yaw = low.x - 1.22, doorway_y - (.95 if rank == 4 else .55), 90
            if wing_x - 1.06 < -half + .12:
                # A maximum-width house may leave no side aisle on a 16 m plot.
                # Keep the service wing in the forecourt instead of clipping either.
                wing_x, wing_y, wing_yaw = -4.6, -5.7, 0
            wing_key = "wing_short" if rank == 4 else "wing_long"
            # UE's FBX local-Y reflection reverses the side wing's opening. Keep
            # the Blender preview facing the yard and serialize the calibrated UE yaw.
            instance(props[wing_key], plot_id, (ox + wing_x, wing_y, .05), wing_yaw, ue_yaw=-wing_yaw)
            wx, wy = (1.08, wing_length / 2 + .10) if wing_yaw else (wing_length / 2 + .10, 1.08)
            surface(props["paving"], plot_id, ox, wing_x - wx, wing_x + wx, wing_y - wy, wing_y + wy, "work_surface")
            if wing_yaw:
                link_y = min(wing_y, doorway_y - .55)
                surface(props["paving"], plot_id, ox, wing_x + wx, doorx - path_width / 2 + .03,
                        link_y - .35, link_y + .35, "work_path")
            wing_layout = {"asset_id": props[wing_key].name, "length_m": wing_length,
                           "location_cm": [(ox + wing_x) * 100, wing_y * 100, 5],
                           "rotation_yaw_deg": -wing_yaw, "blender_rotation_yaw_deg": wing_yaw,
                           "arrangement": "side_wing" if wing_yaw else "forecourt_wing"}
        # Different households have different work/storage groupings. All positions
        # clear the configured maximum house footprints, rather than filling a grid.
        if rank == 1:
            placements = [("jar", -2.4, -2.4, 0, 0, 1), ("jar", -3.0, -2.55, 0, 16, .72),
                          ("woodpile", 3.2, -2.4, 0, 90, 1), ("tool", 3.8, -2.85, 0, 18, 1),
                          ("drying", -4.7, -4.5, 0, -8, 1), ("stool", -2.9, -4.8, 0, 25, 1)]
            work, drying, storage = (-2.9, -4.8), (-4.7, -4.5), (3.2, -2.4)
            arrangement = "open_earth_workyard"
        elif rank == 4:
            placements = [("jar", -2.8, -4.0, 0, 0, 1), ("jar", -3.4, -4.1, 0, 22, .8),
                          ("stool", -3.0, -5.0, 0, -12, 1), ("crate", 5.7, -3.5, 0, -5, 1),
                          ("woodpile", 5.75, -5.3, 0, 90, 1), ("tool", 6.5, -5.55, 0, 18, 1),
                          ("drying", 6.35, -2.4, 0, 65, 1)]
            work, drying, storage = (wing_x, wing_y), (6.35, -2.4), (5.7, -3.5)
            arrangement = "stone_path_and_short_service_wing"
        else:
            placements = [("stool", -3.2, -5.3, 0, 10, 1), ("woodpile", -6.7, -6.7, 0, 0, 1),
                          ("crate", 5.55, -4.2, 0, 0, 1), ("crate", 6.6, -4.2, 0, 0, .85),
                          ("jar", 5.6, -5.35, 0, 0, 1), ("jar", 6.25, -5.4, 0, 13, .72),
                          ("tool", 6.85, -5.55, 0, -15, 1), ("drying", 6.6, -1.5, 0, 70, 1)]
            # Pottery sits on the authored counter, transformed with the wing.
            for localx, s in [(-.4, .85), (.2, .7)]:
                px, py = (-.37, localx) if wing_yaw else (localx, .37)
                placements.append(("jar", wing_x + px, wing_y + py, .92, 0, s))
            work, drying, storage = (wing_x, wing_y), (6.6, -1.5), (5.8, -4.5)
            arrangement = "broad_doorstep_and_long_service_wing"
        for key, x, y, z, yaw, s in placements:
            instance(props[key], plot_id, (ox + x, y, z), yaw, scale=(s, s, s))
        plots.append({"plot_id": plot_id, "level": level, "plot_size_m": [plot_size, plot_size],
                      "center_cm": [ox * 100, 0, 0], "front_axis": "-Y", "house_asset_id": house.name,
                      "courtyard_arrangement": arrangement,
                       "enclosure": {key: value for key, value in enclosure.items() if key != "parts"},
                      "service_wing": wing_layout,
                      "pedestrian_route": {"doorway_anchor_cm": [(ox + doorx) * 100, doorway_y * 100, 0],
                                           "doorway_anchor_method": anchor_method, "visual_confirmation_required": True,
                                           "start_y_cm": path_start * 100, "end_y_cm": doorway_y * 100,
                                           "width_cm": path_width * 100, "instance_ids": route + apron},
                      "activity_anchors_cm": {"work": [(ox + work[0]) * 100, work[1] * 100, 0],
                                              "drying": [(ox + drying[0]) * 100, drying[1] * 100, 0],
                                              "storage": [(ox + storage[0]) * 100, storage[1] * 100, 0],
                                               "entrance": [(ox + doorx) * 100,
                                                            enclosure.get("gate_center_m", [0, -half])[1] * 100, 0]},
                      "status": "adjustable_showcase_plot_not_gameplay_footprint"})
    return instances, plots


def run(manifest_path, output_dir):
    global OUTPUT, TEXTURE_LIMIT
    OUTPUT = Path(output_dir).resolve()
    MATERIALS.clear()
    ASSETS.clear()
    WARNINGS.clear()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != VERSION:
        raise ValueError("Only schema_version=1 is supported")
    entries = manifest.get("assets", [])
    if not entries:
        raise ValueError("At least one house asset is required")
    levels = [a["level"] for a in entries]
    if len(set(levels)) != len(levels):
        raise ValueError("House levels must be unique")
    TEXTURE_LIMIT = int(manifest.get("max_texture_size", 2048))
    if TEXTURE_LIMIT < 16 or TEXTURE_LIMIT > 8192:
        raise ValueError("max_texture_size must be 16..8192")
    plot_size = float(manifest.get("plot_size_m", 16))
    if abs(plot_size - 16) > 1e-6:
        raise ValueError("This initial courtyard layout requires plot_size_m=16; house dimensions remain configurable")
    # This is a dedicated background process, never the user's open Blender.
    # Fresh datablocks also prevent old glTF node-group schemas polluting import.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.scale_length = 1.0
    job = manifest.get("blender_job")
    houses = [process_house(entry, manifest_path.parent, apply_model_repairs=job is None) for entry in entries]
    job_report = None
    if job is None:
        props = create_props(entries)
        instances, plots = assemble(houses, props, entries, plot_size)
        source_objects = [*houses, *props.values()]
    else:
        # No built-in courtyard, prop placement or roof repair is used in this
        # lane. The versioned Claude job owns every artistic edit and placement.
        from copy import deepcopy
        expected_houses = {entry["level"]: obj.name for entry, obj in zip(entries, houses)}
        source_objects_by_id = {obj.name: obj for obj in houses}
        export_asset = AssetExporter(export_mesh, ASSETS, source_objects_by_id, job)
        api = {"bounds": bounds, "join_meshes": join_meshes, "export_asset": export_asset,
               "export_mesh": export_asset, "procedural_material": procedural_material,
               "process_material": process_material, "box": box, "cylinder": cylinder,
               "timber_between": timber_between, "pottery": pottery}
        context = {"houses": houses, "entries": deepcopy(entries), "manifest": deepcopy(manifest),
                   "output_dir": OUTPUT, "api": api, "assets": ASSETS, "materials": MATERIALS}
        job_result = execute_job(deepcopy(job), context)
        for entry, house in zip(entries, houses):
            if house.name != expected_houses[entry["level"]]:
                raise ValueError("Blender job must preserve each imported house object identity")
            # Persist actual final house geometry even when the job edited it in
            # place without an explicit re-export. Source provenance is retained.
            previous = next((a for a in ASSETS if a["asset_id"] == house.name), {})
            export_asset(house, compensate_ue_local_y=bool(previous.get("fbx_local_y_precompensated")))
        checked = validate_job_result(job_result, ASSETS, MATERIALS, expected_houses, plot_size=plot_size)
        instances, plots, job_report = checked["instances"], checked["plots"], checked["report"]
        # All authored objects are sources. Only the returned, validated instances
        # enter the display collection; stray working objects cannot leak into it.
        source_objects = list(bpy.context.scene.objects)
        display = bpy.data.collections.new("Claude_Job_Preview")
        bpy.context.scene.collection.children.link(display)
        for item in instances:
            original = source_objects_by_id.get(item["asset_id"])
            if original is None or original.type != "MESH":
                raise ValueError("Blender job display references an object that was not exported")
            copy = original.copy()
            copy.data = original.data
            display.objects.link(copy)
            copy.name = item["instance_id"]
            copy.matrix_world = Matrix.Identity(4)
            copy.location = [v / 100 for v in item["location_cm"]]
            copy.rotation_mode = "XYZ"
            copy.rotation_euler = [math.radians(v) for v in item.get("blender_rotation_deg", item["rotation_deg"])]
            copy.scale = item["scale"]
            copy.hide_render = False
            copy.hide_set(False)
    library = bpy.data.collections.new("Asset_Library_Hidden")
    bpy.context.scene.collection.children.link(library)
    for obj in source_objects:
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        library.objects.link(obj)
    library.hide_render = True
    library.hide_viewport = True
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("CourtyardWorld")
    bpy.context.scene.world.color = (.18, .18, .18)
    blend_path = OUTPUT / "Courtyard_Batch_A.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    for descriptor in MATERIALS.values():
        for key in ("base_color", "orm", "normal"):
            descriptor[f"{key}_sha256"] = sha256(OUTPUT / descriptor[key])
    result = {"schema_version": VERSION, "generator": "art_tools.blender.prepare_batch", "style_id": "A",
              "units": {"mesh_source": "meters", "instances": "centimeters", "up_axis": "Z", "front_axis": "-Y"},
              "input_manifest_sha256": sha256(manifest_path), "blender_version": bpy.app.version_string,
              "blend": relative(blend_path), "blend_sha256": sha256(blend_path),
              "assets": ASSETS, "shared_props": [a["asset_id"] for a in ASSETS if a["type"] != "building"],
              "materials": list(MATERIALS.values()), "instances": instances, "plots": plots,
              "warnings": WARNINGS,
              "acceptance": {"technical_processing": "complete", "visual_style": "pending_review", "ue_import": "not_run"}}
    if "blender_provenance" in manifest:
        result["blender_provenance"] = manifest["blender_provenance"]
    if job is not None:
        result["blender_job"] = deepcopy(job)
        result["blender_job_report"] = job_report
    # All deliverable hashes support immutable evidence; excludes the report itself.
    result["files"] = [{"path": relative(p), "size_bytes": p.stat().st_size, "sha256": sha256(p)}
                       for p in sorted(OUTPUT.rglob("*")) if p.is_file() and p.suffix in {".png", ".fbx", ".blend"}]
    write_json(OUTPUT / "assets_manifest.json", result)
    report_keys = ["schema_version", "input_manifest_sha256", "blender_version", "assets", "warnings", "acceptance", "files"]
    if "blender_provenance" in result:
        report_keys.append("blender_provenance")
    if job is not None:
        report_keys.extend(("blender_job", "blender_job_report"))
    write_json(OUTPUT / "processing_report.json", {key: result[key] for key in report_keys})
    print(f"ART_PREP_COMPLETE assets={len(ASSETS)} materials={len(MATERIALS)} instances={len(instances)} report={OUTPUT / 'assets_manifest.json'}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest_path")
    parser.add_argument("output_dir")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    run(args.manifest_path, args.output_dir)


if __name__ == "__main__":
    main()
