# Code guide

## Follow a build

`metrosim.py` → `blender_ui.py` → `runtime.py` → data / optional growth → `scene.py` → Blender import → optional reveal.

- Entry files connect the steps; feature modules own the details.
- `data.py` keeps existing imports working; implementation lives in `datasets/`.
- `simulation.py` prepares/replays runs; yearly calculation lives in `growth/`.
- Keep the package together. Rerun `metrosim.py` after edits; it refreshes the modules.

## Geometry: `metrosim26/geometry/`

| File | Owns |
| --- | --- |
| `land.py` | Ground offsets, park surfaces, full-ground batch policy |
| `water.py` | Ocean batch policy and inland-water clearance |
| `terrain.py` | Elevation sampling and aligning geometry to terrain triangles |
| `coastline.py` | Classify shared surface cells as land or sea |
| `buildings.py` | Footprint extrusion, walls and courtyard roofs |
| `roads.py` | Road strips and corner joins |
| `polygons.py` | Shared winding and triangulation |
| `packets.py` | Circle clipping, vertex limits and mesh-file writing |
| `lifetimes.py` | Match persistent generated features across years |

- `geography.py` loads elevation/coastline inputs and connects the terrain tools.
- `scene.py` routes layers into packets and writes the manifest.
- Batch behavior is explicit: ground requests full-area welding; ocean requests welding without radial batches.
- Names label objects; they do not decide batching behavior.
- Inland water uses a shared terrain-alignment primitive, not the road-specific routine.

## Blender: `metrosim26/blender/`

| File | Owns |
| --- | --- |
| `panel.py` | Sidebar drawing only |
| `properties.py` | Field declarations and defaults |
| `coordinates.py` | Coordinate-pair parsing and field synchronization |
| `operators.py` | Buttons: preview, build, cameras and cleanup |
| `job.py` | Polling, countdown, cancellation and import scheduling |
| `scene_import.py` | Mesh creation, expansion reuse and final object joining |
| `cameras.py` | Overview/detail cameras and framing |
| `lighting.py` | Sun, fill, rim, world and exposure |
| `presentation.py` | Palettes, materials, viewport state, timeline visibility and optional output |
| `reveal.py` | Temporary circular sweep; restores the finished geometry |

- `blender_ui.py` keeps registration, session state and settings conversion together.
- Layout and button modules receive callbacks; they do not own the active job.
- `job.py` receives UI actions explicitly; it does not import the controller.
- Changing lighting does not require editing camera setup.

## Data: `metrosim26/datasets/`

| File | Owns |
| --- | --- |
| `files.py` | Input records, hashes and atomic writes |
| `inputs.py` | Which geographic cells/files are needed |
| `validation.py` | File validation and receipts |
| `cache.py` | Find and reuse existing downloads |
| `download.py` | HTTP attempts, deadlines, retries and fallback servers |
| `preparation.py` | Prefetch orchestration and cache reports |
| `osm.py` | Merge tiles and parse mapped features |
| `lodes.py` | Regional employment inputs |
| `baseline.py` | Derived masks/signals and baseline archives |
| `location.py` | Cached place-name lookup |

## Growth: `metrosim26/growth/`

- `capacity.py`: demand/capacity estimates, archetype rules and site accounting.
- `planning.py`: parallel planning cache and worker coordination.
- `engine.py`: annual infill → redevelopment → expansion decisions.
- `checkpoints.py`: saved states, summaries and replay validation.
- `continuation.py`: validate completed runs and restore state to calculate additional years.
- `extend.py`: open the saved-run extension dialog in Blender.
- `workers.py` remains the process entry point and footprint/collision helper API.
- `projects.py`, `infrastructure.py`, `history.py`: separate optional features.
- `regions/`: local inputs; worldwide map code stays universal.

## Check a change

- Geometry changes: run `python -m unittest discover -s tests -q`.
- `tests/test_layer_isolation.py` compares finished faces across ground, inland-water and reveal switches.
- Blender changes: run `blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py`.
- Native checks cover reload/New File, fresh maps, expansion, ocean grouping and reveal cleanup.
- Also run `python -m ruff check .`.
- Keep reveal changes out of final geometry code. Keep cache identity separate from location labels.
