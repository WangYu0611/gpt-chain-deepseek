"""Deterministic, service-free courtyard plans in local metres.

These are editable showcase enclosures, not gameplay footprints. Keeping the
plan independent of Blender makes doorway clearance and plot limits testable.
"""
from __future__ import annotations

import math


VERSION = "courtyard-crafted-v002"


def enclosure_plan(level, gate_x=0.0, plot_size=16.0):
    if level not in {"L1", "L4", "L8"}:
        raise ValueError("Only the three approved showcase levels are supported")
    if (type(gate_x) not in (int, float) or not math.isfinite(gate_x) or
            type(plot_size) not in (int, float) or not math.isfinite(plot_size) or abs(plot_size - 16.0) > 1e-6):
        raise ValueError("Courtyard requires a finite entrance and the 16 m plot")
    if level == "L1":
        return {"version": VERSION, "level": level, "style": "open_workyard",
                "parts": [], "gate_clear_width_m": None, "path_width_m": 1.15}
    high = level == "L8"
    half, front = 7.50, -7.40
    path_width = 2.4 if high else 1.8
    opening = path_width + .24
    post = .36 if high else .22
    gate_outer = opening / 2 + post
    if abs(gate_x) + gate_outer + .8 > half:
        raise ValueError("Gate must leave a connected front wall on both sides")
    wall_top, base_h = (1.78, .48) if high else (1.46, .30)
    thick = .40 if high else .30
    clear_h = 2.55 if high else 2.28
    parts = []

    def box(name, center, size, material, yaw=0, bevel=.012):
        parts.append({"kind": "box", "name": name, "center": list(center),
                      "size": list(size), "material": material, "yaw": yaw, "bevel": bevel})

    def roof(name, center, length, depth, ridge, eave, yaw=0):
        parts.append({"kind": "tiled_roof", "name": name, "center": list(center),
                      "length": length, "depth": depth, "ridge": ridge,
                      "eave": eave, "material": "roof", "yaw": yaw})

    # Continuous plaster infill and a real stone plinth. Small stone joints are
    # backed by the continuous foundation, rather than becoming holes in a wall.
    runs = [("West", -half, (front + half) / 2, half - front, 90),
            ("East", half, (front + half) / 2, half - front, 90),
            ("Rear", 0, half, 2 * half, 0)]
    for label, lo, hi in (("FrontLeft", -half, gate_x - gate_outer),
                          ("FrontRight", gate_x + gate_outer, half)):
        runs.append((label, (lo + hi) / 2, front, hi - lo, 0))
    for label, x, y, length, yaw in runs:
        box(label + "Foundation", (x, y, base_h / 2), (length, thick + .07, base_h), "stone", yaw)
        box(label + "Plaster", (x, y, (base_h + wall_top) / 2),
            (length, thick, wall_top - base_h), "plaster", yaw, .009)
        angle = math.radians(yaw)
        def local(u, v, z):
            return (x + u * math.cos(angle) - v * math.sin(angle),
                    y + u * math.sin(angle) + v * math.cos(angle), z)
        courses = 3 if high else 2
        course_h = base_h / courses
        for row in range(courses):
            width = .56 if high else .72
            count = math.ceil(length / width)
            # Different course counts stagger joints without clipping a corner.
            count += row % 2
            block_width = length / count
            for index in range(count):
                for side in (-1, 1):
                    box(label + "StoneCourse", local(-length / 2 + (index + .5) * block_width,
                        side * (thick / 2 + .036), (row + .5) * course_h),
                        (block_width - .008, .022, course_h - .008), "stone", yaw, .006)
        roof(label + "Coping", (x, y), length + .035, thick + .20,
             wall_top + .16, wall_top + .07, yaw)
        if high:
            # The upper level adds masonry piers, not a stretched low-level wall.
            bays = max(1, round(length / 2.5))
            for i in range(1, bays):
                box(label + "Pier", local(-length / 2 + i * length / bays, 0, wall_top / 2),
                    (.40, thick + .12, wall_top), "plaster", yaw)
                box(label + "PierPlinth", local(-length / 2 + i * length / bays, 0, .28),
                    (.45, thick + .16, .56), "stone", yaw)
    for x in (-half, half):
        for y in (front, half):
            width = .56 if high else .43
            box("CornerPier", (x, y, wall_top / 2), (width, width, wall_top), "plaster")
            box("CornerFoot", (x, y, base_h / 2), (width + .06, width + .06, base_h), "stone")
            roof("CornerCap", (x, y), width + .08, width + .08, wall_top + .17, wall_top + .07)

    for side in (-1, 1):
        x = gate_x + side * (opening / 2 + post / 2)
        box("GatePostFoot", (x, front, .18), (post + .13, .50 if high else .40, .36), "stone")
        box("GateMasonryJamb" if high else "GateTimberJamb", (x, front, clear_h / 2),
            (post, .43 if high else .31, clear_h), "plaster" if high else "wood")
        if high:
            box("GateInnerTimber", (x - side * (post / 2 - .035), front, clear_h / 2),
                (.07, .37, clear_h), "darkwood")
        # Two real plank leaves open 90 degrees into the yard. Their inner faces
        # stay outside the clear opening; no collision geometry crosses the path.
        leaf = opening / 2 - .025
        leaf_h = clear_h - .15
        leaf_x = gate_x + side * (opening / 2 + .035)
        boards = math.ceil(leaf / .17)
        for i in range(boards):
            box("OpenGatePlank", (leaf_x, front + (i + .5) * leaf / boards, .08 + leaf_h / 2),
                (.055, leaf / boards - .003, leaf_h), "wood", bevel=.004)
        for z in (.31, leaf_h / 2, leaf_h - .18):
            box("GateLeafRail", (leaf_x + side * .038, front + leaf / 2, z),
                (.055, leaf, .09), "darkwood", bevel=.006)
            box("GateIronStrap", (leaf_x - side * .031, front + .20, z),
                (.015, min(.36, leaf), .045), "iron", bevel=.002)
        for y in (front + .055, front + leaf - .055):
            box("GateLeafStile", (leaf_x + side * .031, y, .08 + leaf_h / 2),
                (.05, .065, leaf_h), "darkwood", bevel=.005)
    box("GateLintel", (gate_x, front, clear_h + .09), (opening + 2 * post + .12, .43, .18), "darkwood")
    if high:
        # A deeper but straight-eaved gate canopy, with its own timber support.
        for side in (-1, 1):
            box("GateCanopyBeam", (gate_x, front + side * .36, clear_h + .25),
                (opening + 2 * post + .24, .11, .14), "wood")
        box("GateHeaderPanel", (gate_x, front, clear_h + .31),
            (opening + .08, .12, .25), "wood")
    roof("GateRoof", (gate_x, front), opening + 2 * post + .34,
         1.04 if high else .80, clear_h + (.63 if high else .44), clear_h + (.34 if high else .23))
    return {"version": VERSION, "level": level,
            "style": "masonry_piers_and_roofed_gate" if high else "plastered_wall_and_timber_gate",
            "plot_size_m": [plot_size, plot_size], "parts": parts,
            "path_width_m": path_width, "gate_clear_width_m": opening,
            "gate_clear_height_m": clear_h, "gate_center_m": [gate_x, front, 0],
            "wall_top_m": wall_top, "wall_thickness_m": thick,
            "gate_leaves": "two_planked_leaves_open_inward_90deg",
            "boundary_half_m": half, "api_cost": 0}


def part_xy_bounds(part):
    """Conservative footprint for physical clearance tests, including rotated parts."""
    sx, sy = part["size"][:2] if part["kind"] == "box" else (part["length"], part["depth"])
    angle = math.radians(part["yaw"])
    dx = (abs(math.cos(angle)) * sx + abs(math.sin(angle)) * sy) / 2
    dy = (abs(math.sin(angle)) * sx + abs(math.cos(angle)) * sy) / 2
    x, y = part["center"][:2]
    return (x - dx, y - dy), (x + dx, y + dy)
