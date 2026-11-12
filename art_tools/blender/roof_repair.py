"""Frozen, source-bound roof corrections. Run after metre normalization/join.

The plan contains explicit vertex displacements, not a runtime height selector.
Only the two inspected v003 source meshes are eligible. UVs, topology, material
slots and all vertices outside the mask are preserved. Unknown sources are a
deliberate no-op, so fixtures and future generations cannot inherit this repair.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import zlib

PLAN_PATH = Path(__file__).with_name("roof_repair_plans.json")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(blob: dict, expected_size: int) -> bytes:
    if not isinstance(blob, dict) or blob.get("encoding") != "zlib-base64":
        raise ValueError("Invalid roof-repair array encoding")
    packed = base64.b64decode(blob["data"], validate=True)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, expected_size + 1)
    if len(raw) != expected_size or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid roof-repair array length")
    if _sha(raw) != blob.get("sha256"):
        raise ValueError("Roof-repair array checksum mismatch")
    return raw


def load_plan(source_sha256: str, level: str | int):
    raw = PLAN_PATH.read_bytes()
    document = json.loads(raw)
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported roof-repair plan schema")
    plan = document["plans"].get(source_sha256)
    if plan is None:
        return None
    normalized_level = str(level).upper().removeprefix("L")
    if normalized_level != str(plan["level"]):
        raise ValueError("Roof-repair source/level mismatch")
    return plan, _sha(raw)


def repair_arrays(co, triangles, uv, plan):
    """Validate then compute a local correction without mutating caller arrays."""
    import numpy as np

    count = plan["vertex_count"]
    if co.shape != (count, 3) or triangles.shape != (plan["triangle_count"], 3):
        raise ValueError("Roof-repair topology count mismatch")
    if _sha(triangles.astype("<i4").tobytes()) != plan["triangles_sha256"]:
        raise ValueError("Roof-repair triangle order/topology mismatch")
    if _sha(uv.astype("<f4").tobytes()) != plan["uv_sha256"]:
        raise ValueError("Roof-repair UV mismatch")
    reference = np.frombuffer(_decode(plan["reference_coordinates"], count * 12), dtype="<f4").reshape(-1, 3)
    delta = np.frombuffer(_decode(plan["downward_displacements"], count * 4), dtype="<f4")
    mask = delta != 0
    if not np.isfinite(co).all() or not np.isfinite(delta).all() or np.any(delta < 0):
        raise ValueError("Non-finite/negative roof-repair geometry")
    if int(mask.sum()) != plan["selected_vertex_count"]:
        raise ValueError("Roof-repair mask count mismatch")
    # Permit only the same source orientation/origin at a different UNIFORM scale.
    # A second application, rotation, edited source or nonuniform scale fails closed.
    scale = float(np.ptp(co[:, 0]) / np.ptp(reference[:, 0]))
    if not .01 <= scale <= 100:
        raise ValueError("Invalid roof-repair uniform scale")
    if float(np.max(np.abs(co / scale - reference))) > 0.00003:
        raise ValueError("Roof-repair reference coordinates mismatch (orientation, scale or repeated repair)")
    corrected = co.copy()
    corrected[mask, 2] -= delta[mask] * scale
    if not np.array_equal(corrected[~mask], co[~mask]):
        raise AssertionError("Roof repair modified unselected vertices")
    old_cross = np.cross(co[triangles[:, 1]] - co[triangles[:, 0]], co[triangles[:, 2]] - co[triangles[:, 0]])
    new_cross = np.cross(corrected[triangles[:, 1]] - corrected[triangles[:, 0]], corrected[triangles[:, 2]] - corrected[triangles[:, 0]])
    old_area = np.linalg.norm(old_cross, axis=1)
    new_area = np.linalg.norm(new_cross, axis=1)
    valid = old_area > 1e-10 * scale * scale
    cosine = np.einsum("ij,ij->i", old_cross, new_cross) / (old_area * new_area + 1e-30)
    ratios = new_area[valid] / old_area[valid]
    turned = int(np.count_nonzero(cosine[valid] <= 0))
    if turned or np.min(ratios) < .10:
        raise ValueError("Roof repair would invert or collapse a source triangle")
    audit = {
        "changed_vertex_count": int(mask.sum()),
        "unchanged_vertex_count": int((~mask).sum()),
        "max_displacement_m": float(np.max(delta) * scale),
        "uniform_scale_relative_to_plan": scale,
        "coordinates_before_sha256": _sha(co.astype("<f4").tobytes()),
        "coordinates_after_sha256": _sha(corrected.astype("<f4").tobytes()),
        "unselected_before_sha256": _sha(co[~mask].astype("<f4").tobytes()),
        "unselected_after_sha256": _sha(corrected[~mask].astype("<f4").tobytes()),
        "unselected_coordinates_bitwise_unchanged": True,
        "xy_coordinates_bitwise_unchanged": bool(np.array_equal(co[:, :2], corrected[:, :2])),
        "triangles_sha256": plan["triangles_sha256"],
        "uv_sha256": plan["uv_sha256"],
        "vertex_count": count,
        "triangle_count": len(triangles),
        "minimum_triangle_area_ratio": float(np.min(ratios)),
        "minimum_normal_alignment": float(np.min(cosine[valid])),
        "opposed_triangle_normal_count": turned,
        "new_degenerate_triangle_count": int(np.count_nonzero(valid & (new_area <= 1e-10 * scale * scale))),
        "source_degenerate_triangle_count": int(np.count_nonzero(~valid)),
        "bounds_before_m": [co.min(0).tolist(), co.max(0).tolist()],
        "bounds_after_m": [corrected.min(0).tolist(), corrected.max(0).tolist()],
        "height_before_m": float(np.ptp(co[:, 2])),
        "height_after_m": float(np.ptp(corrected[:, 2])),
        "height_reduction_m": float(np.max(co[:, 2]) - np.max(corrected[:, 2])),
        "rescaled_after_repair": False,
        "regions": [{k: v for k, v in region.items() if k != "vertex_indices"}
                    | {"vertex_mask_sha256": region["vertex_indices"]["sha256"]}
                    for region in plan["regions"]],
    }
    return corrected, audit


def apply_roof_repair(house, *, source_sha256: str, level: str | int, output_dir):
    selected = load_plan(source_sha256, level)
    if selected is None:
        return None
    import numpy as np

    plan, plan_sha = selected
    mesh = house.data
    mesh.calc_loop_triangles()
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    triangles = np.array([list(t.vertices) for t in mesh.loop_triangles], dtype=np.int32)
    if mesh.uv_layers.active is None:
        raise ValueError("Roof-repair source is missing its UV map")
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    mesh.uv_layers.active.data.foreach_get("uv", uv)
    material_indices = [p.material_index for p in mesh.polygons]
    material_slots = [m.name if m else None for m in mesh.materials]
    corrected, audit = repair_arrays(co, triangles, uv, plan)
    mesh.vertices.foreach_set("co", corrected.ravel())
    mesh.update()
    checked_uv = np.empty_like(uv)
    mesh.uv_layers.active.data.foreach_get("uv", checked_uv)
    mesh.calc_loop_triangles()
    checked_tri = np.array([list(t.vertices) for t in mesh.loop_triangles], dtype=np.int32)
    if not np.array_equal(uv, checked_uv) or not np.array_equal(triangles, checked_tri):
        raise AssertionError("Roof repair altered source UV/topology")
    if material_indices != [p.material_index for p in mesh.polygons] or material_slots != [m.name if m else None for m in mesh.materials]:
        raise AssertionError("Roof repair altered material assignments")
    audit.update({"schema_version": 1, "method": "frozen_local_vertex_displacement_v1",
                  "asset": house.name, "level": plan["level"], "source_sha256": source_sha256,
                  "plan_id": plan["id"], "plan_sha256": plan_sha,
                  "mask_sha256": plan["downward_displacements"]["sha256"],
                  "uv_preserved": True, "topology_preserved": True, "material_assignments_preserved": True,
                  "material_slots": material_slots, "gable_policy": plan["gable_policy"]})
    destination = Path(output_dir) / "roof_repairs" / f"{house.name}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    destination.write_bytes(payload)
    return {**audit, "report_path": str(destination.resolve()), "report_sha256": _sha(payload)}
