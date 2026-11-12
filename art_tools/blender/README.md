# Blender asset preparation

Run in a **new background Blender 5.2 process**. This script resets only that process's scene; it does not control or close an existing Blender window, invoke Tripo, or start UE.

```powershell
& 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe' --background --python-exit-code 1 --python 'art_tools/blender/prepare_batch.py' -- 'manifest.json' 'output-directory'
```

Input schema:

```json
{
  "schema_version": 1,
  "plot_size_m": 16,
  "max_texture_size": 2048,
  "assets": [
    {"asset_id": "house_l1", "level": "L1", "input_glb": "L1.glb", "target_height_m": 4.2, "max_footprint_m": [8, 6], "front_rotation_deg": 90, "source_normal_convention": "opengl", "orientation_verified": false},
    {"asset_id": "house_l4", "level": "L4", "input_glb": "L4.glb", "target_height_m": 5, "max_footprint_m": [10, 8], "front_rotation_deg": 90},
    {"asset_id": "house_l8", "level": "L8", "input_glb": "L8.glb", "target_height_m": 6.2, "max_footprint_m": [12, 10], "front_rotation_deg": 90}
  ]
}
```

Relative inputs resolve against the manifest. `front_rotation_deg=90` is calibrated from the actual first batch's doorway and rear views; the exported doorway faces -Y. Inspect each later model and adjust this value if needed; generation does not guarantee orientation. Orientation is applied once at GLB import; house instances have zero yaw. Scaling is **uniform** and is the smallest multiplier satisfying target height and maximum width/depth. Height can therefore fall below the target; this is reported. No small disconnected pieces are automatically deleted.

The current adjustable showcase uses three 16 m plots centred at X=-20/0/+20 m; these are not frozen gameplay footprints. The house, courtyard and life props stay separate. Thirteen reusable local assets comprise pottery with an open rim, woodpile, open plank crate, stool, drying rack with modelled cloth folds, hoe, earth tile, beaten-earth path, paving, separate L4/L8 complete enclosure modules, and short/long tiled service wings. The wings use timber posts, beams, diagonal braces, plaster back/side infill and an open front; the long version adds a bay without increasing height. Local procedural materials and meshes incur no Tripo cost.

The L1 workyard has no isolated rear wall. L4 has a continuous plaster enclosure on a coursed stone base, tiled coping and a timber gate; L8 adds thicker, higher walls with masonry piers, stone gate bases and a deeper straight-eaved entrance canopy. Both gates have individual planks, rails and straps, with two leaves open 90 degrees into the yard. Their clear openings exceed the unchanged 1.8/2.4 m paths. These enclosures are authored at full size, never made by stretching a shared gate frame. `courtyard_geometry.py` defines the level-specific dimensions and physical component plans; `assets[].courtyard_enclosure` and `plots[].enclosure` record the construction profile.

L1 has an open earth workyard; L4 has a stone route and short service wing; L8 has a broader doorstep and a two-bay service wing. Wings turn toward the courtyard and remain clear of the measured house bounds. A very wide house triggers a forecourt placement to keep the 16 m plot valid. Gate-to-door routes use ground-level centre-strip geometry to find the front stair; `entrance_local_m:[x,y]` can override that anchor in processed local metres. This geometric estimate still needs visual confirmation. `plots[].pedestrian_route` records the anchor and all route instances, and `service_wing` records wing placement.

Outputs:

- `assets_manifest.json`: `assets`, `materials`, `shared_props`, `instances`, `plots`, file hashes and review states.
- `meshes/*.fbx`: metres, Z-up, -Y front, material slots retained.
- `textures/<material>/`: original base colour, source images, original normal, DirectX normal, Blender/OpenGL normal, ORM and restrained A colour treatment. A retains 88% original colour detail and uses no spatial blur.
- `Courtyard_Batch_A.blend`: assembled plots and hidden source library.
- `processing_report.json`: source hashes, measurements, triangle counts, transforms, warnings and output hashes.

Each `assets` item has `asset_id`, `type`, `fbx`, `dimensions_m`, `material_slots:[{slot_index,slot_name,material}]`. Houses also contain `level`, source dimensions and uniform multiplier. Materials expose `material_name`, `base_color`, `normal`, `orm`, colour-space flags and provenance. Instances expose `asset_id`, `level`, `plot_id`, **`location_cm`**, **`rotation_deg:[X,Y,Z]`**, `scale`; Z is yaw. Paths are relative to the output directory.

`rotation_deg` is the UE placement value. The service wings additionally record `blender_rotation_deg` and a calibration reason: actual UE front/rear views showed the FBX local-Y reflection reverses their opening, so side wings use UE yaw -90 while the Blender preview uses +90. House instances remain at zero yaw. L4/L8 drying racks are angled away from a grazing view; cloth already has physical thickness.

Only the new complete enclosure meshes precompensate this local-Y reflection in a temporary FBX export copy, reversing face winding after the mirror. Their authored `.blend` mesh and UVs remain unchanged and their instance yaw remains zero. `fbx_local_y_precompensated` and `coordinate_export_audit` record both geometry fingerprints and pre-UE FBX bounds. A native FBX roundtrip verifies the export copy and recovered doorway clearance; actual UE import must still verify that the gate faces the existing path.

`ue/import_preview.py` imports only these enclosure meshes without autogenerated convex collision and applies `CTF_USE_COMPLEX_AS_SIMPLE` before rebuilding collision. They are static, non-simulating `BlockAll` actors. This preserves the hollow yard and open gate; other houses and props retain their existing collision setup. The import receipt records the policy and 17 line/sphere queries per enclosure, all with `trace_complex=False` and no ignored actors: the doorway must pass in both directions, and both front walls must block with the enclosure as the hit actor. A mismatch fails import. This is a static showcase collision policy, not a character movement/navigation integration or a final gameplay collision optimization.

**UE unit contract:** FBX records `UnitScaleFactor=100` because the scene is in metres. Use UE `convert_scene_unit=true` and `import_uniform_scale=1`, then compare UE bounds in centimetres with `dimensions_m * 100`. Applying another import scale of 100 would overscale these exports. This differs from exporters that omit physical FBX units.

Material extraction follows actual nodes and linked channels, not image names. Missing AO becomes 1. Metal and roughness remain separate when reconstructing ORM. Source OpenGL normal G is inverted once for UE, while the untouched source is retained. Explicit DirectX input is not inverted again. Nonstandard connected shader nodes and UV transforms raise an error instead of silently generating a plausible wrong texture. Transparency/cutout, multi-UV material transforms, animated/skeletal assets and displacement baking are outside this initial static-house implementation.

Free integration verification:

```powershell
& 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe' --background --python-exit-code 1 --python 'art_tools/blender/verify_fixture.py' -- 'C:/codex-dsh-work/art-assets/blender-fixture-20261106'
```

The fixture exports/imports three GLBs with two materials each and misleading image names, verifies actual ORM channel values and missing AO, normal G conversion, uniform scale and origin, local prop count, plot coordinates, prop/house clearance, continuous entrance routes, wing heights, texture hashes, FBX roundtrip dimensions and binary FBX unit metadata. It does not establish final artistic acceptance or UE rendering acceptance.

`verify_courtyard.py -- <existing production-input.json> <new independent output directory>` checks the three existing source GLB hashes, processes them without a service, verifies the enclosures and actual triangle clearance, roundtrips the new FBXs, and renders three courtyards plus two entrance closeups. It refuses a nonempty output directory or one inside the production artifact tree. Its output is local maintenance evidence, not a replacement for the current UE review package.
