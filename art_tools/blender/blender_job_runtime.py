"""Versioned Claude Blender-job contract; contains no asset construction rules.

Hash verification is provenance, not a Python sandbox. The launcher controls the
process permissions/environment; arbitrary job Python executes inside Blender.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re


JOB_KEYS = {"schema_version", "source_file", "sha256", "size_bytes", "snapshot"}
DERIVED_ASSET_KEYS = {"asset_id", "name", "type", "fbx", "fbx_sha256", "dimensions_m", "bounds_m",
                      "vertices", "triangles", "material_slots", "origin", "unit", "provenance",
                      "ue_import_uniform_scale", "fbx_unit_note", "front_axis",
                      "fbx_local_y_precompensated", "coordinate_export_note", "coordinate_export_audit"}


def load_job(metadata):
    if not isinstance(metadata, dict) or set(metadata) != JOB_KEYS or type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise ValueError("Invalid Blender job metadata")
    digest = metadata["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid Blender job SHA-256")
    if type(metadata["size_bytes"]) is not int or not 0 < metadata["size_bytes"] <= 1024 * 1024:
        raise ValueError("Blender job must contain at most 1 MiB of Python source")
    for key in ("source_file", "snapshot"):
        if not isinstance(metadata[key], str) or not Path(metadata[key]).is_absolute() or Path(metadata[key]).suffix.lower() != ".py":
            raise ValueError("Blender job paths must be absolute Python paths")
    path = Path(metadata["snapshot"])
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
        raise ValueError("Blender job snapshot may not cross a linked path")
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) != metadata["size_bytes"] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Blender job snapshot does not match its pinned hash/size")
    return compile(raw, str(path), "exec")


def execute_job(metadata, context):
    code = load_job(metadata)
    namespace = {"__name__": "claude_blender_job", "__file__": metadata["snapshot"]}
    exec(code, namespace)
    function = namespace.get("run_job")
    if not callable(function):
        raise ValueError("Blender job must define run_job(context)")
    return function(context)


class AssetExporter:
    """Replace exports by identity and keep imported-house source provenance."""
    def __init__(self, exporter, assets, objects, job):
        self.exporter, self.assets, self.objects = exporter, assets, objects
        self.sources = {a["asset_id"]: deepcopy(a) for a in assets}
        self.job = deepcopy(job)

    def __call__(self, obj, asset_type=None, provenance=None, extra=None, *, compensate_ue_local_y=False):
        identity = obj.name
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", identity):
            raise ValueError("Exported asset names must be safe stable identifiers")
        old = next((a for a in self.assets if a["asset_id"] == identity), None)
        source = self.sources.get(identity)
        extra = dict(extra or {})
        if DERIVED_ASSET_KEYS.intersection(extra):
            raise ValueError("Job metadata cannot override measured/exported asset fields")
        if source is not None:
            if provenance is not None and provenance != source["provenance"]:
                raise ValueError("A house re-export must preserve its original source provenance")
            if asset_type is not None and asset_type != source["type"]:
                raise ValueError("A house re-export cannot change its asset type")
            if "level" in extra and extra["level"] != source.get("level"):
                raise ValueError("A house re-export cannot change its approved level")
            asset_type, provenance = source["type"], deepcopy(source["provenance"])
        else:
            asset_type = asset_type or (old or {}).get("type", "prop")
            provenance = provenance or {"kind": "claude_blender_job", "job_sha256": self.job["sha256"], "api_cost": 0}
        preserved = {key: deepcopy(value) for key, value in (old or {}).items() if key not in DERIVED_ASSET_KEYS}
        preserved.update(extra)
        if source is not None:
            preserved["level"] = source["level"]
        record = self.exporter(obj, asset_type, provenance, preserved,
                               compensate_ue_local_y=compensate_ue_local_y)
        self.assets[:] = [a for a in self.assets if a["asset_id"] != identity] + [record]
        self.objects[identity] = obj
        return record


def _vector(value, size, label, *, positive=False):
    if (not isinstance(value, (list, tuple)) or len(value) != size or
            any(type(v) not in (int, float) or not math.isfinite(v) or positive and v <= 0 for v in value)):
        raise ValueError("Invalid Blender job " + label)


def validate_job_result(result, assets, materials, expected_houses, *, plot_size):
    """Check identity, units and references without deciding shapes or layout."""
    if not isinstance(result, dict) or set(result) != {"instances", "plots", "report"}:
        raise ValueError("Blender job must return instances, plots and report")
    if not isinstance(result["report"], dict):
        raise ValueError("Blender job report must be an object")
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Blender job result must be finite JSON data") from error
    if list(expected_houses) != ["L1", "L4", "L8"]:
        raise ValueError("Blender job must keep exactly the three approved levels")
    if not isinstance(assets, list) or not isinstance(materials, dict):
        raise ValueError("Invalid Blender job export registries")
    by_id = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("asset_id"), str) or asset["asset_id"] in by_id:
            raise ValueError("Duplicate or invalid Blender job asset identity")
        by_id[asset["asset_id"]] = asset
        slots = asset.get("material_slots")
        if not isinstance(slots, list) or not slots or any(not isinstance(s, dict) or s.get("material") not in materials for s in slots):
            raise ValueError("Blender job asset references an unregistered material")
    for level, identity in expected_houses.items():
        asset = by_id.get(identity, {})
        if asset.get("level") != level or asset.get("type") != "building":
            raise ValueError("Blender job changed an approved house identity or level")
    if {a["asset_id"] for a in assets if a.get("type") == "building"} != set(expected_houses.values()):
        raise ValueError("Blender job may not introduce additional house levels")
    plots = result["plots"]
    if not isinstance(plots, list) or [p.get("level") if isinstance(p, dict) else None for p in plots] != list(expected_houses):
        raise ValueError("Blender job plots must follow the approved level order")
    plot_by_id = {}
    for index, plot in enumerate(plots):
        identity = plot.get("plot_id")
        if identity != "Plot_" + plot["level"] or identity in plot_by_id or plot.get("house_asset_id") != expected_houses[plot["level"]]:
            raise ValueError("Invalid Blender job plot/house binding")
        _vector(plot.get("plot_size_m"), 2, "plot size", positive=True)
        if list(plot["plot_size_m"]) != [plot_size, plot_size]:
            raise ValueError("Blender job changed the approved plot dimensions")
        _vector(plot.get("center_cm"), 3, "plot center")
        if list(plot["center_cm"]) != [(index - 1) * 2000, 0, 0]:
            raise ValueError("Blender job plot centers must match the fixed capture stage")
        plot_by_id[identity] = plot
    instances, seen, placed = result["instances"], set(), set()
    if not isinstance(instances, list) or not instances:
        raise ValueError("Blender job must provide display instances")
    for instance in instances:
        if not isinstance(instance, dict):
            raise ValueError("Invalid Blender job instance")
        identity = instance.get("instance_id")
        plot = plot_by_id.get(instance.get("plot_id"))
        asset = by_id.get(instance.get("asset_id"))
        if not isinstance(identity, str) or not identity or identity in seen or plot is None or asset is None or instance.get("level") != plot["level"]:
            raise ValueError("Invalid Blender job instance references")
        if not identity.startswith(plot["plot_id"] + "_"):
            raise ValueError("Blender job instance names must match the capture plot prefix")
        seen.add(identity)
        if asset["asset_id"] in expected_houses.values():
            if asset["asset_id"] != plot["house_asset_id"] or plot["level"] in placed:
                raise ValueError("Each approved house must appear once in its own plot")
            placed.add(plot["level"])
        _vector(instance.get("location_cm"), 3, "instance location")
        _vector(instance.get("rotation_deg"), 3, "instance rotation")
        _vector(instance.get("scale"), 3, "instance scale", positive=True)
        if "blender_rotation_deg" in instance:
            _vector(instance["blender_rotation_deg"], 3, "Blender instance rotation")
    if placed != set(expected_houses):
        raise ValueError("Blender job must display all three approved houses")
    return deepcopy(result)
