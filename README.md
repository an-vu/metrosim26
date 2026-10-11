# MetroSim 26

Build a worldwide 3D map in **Blender 5.2.2**, or explore a regional city-growth scenario.

- **3D map:** buildings, roads, parks, water, ocean and terrain.
- **Growth simulation:** Omaha / Council Bluffs employment inputs and possible future development.
- Works on Windows and macOS. Mac rendering currently falls back to CPU.

## Start in Blender

1. Keep the repository together. Open its saved **`metrosim.py`** in Blender's Text Editor.
2. Run it. MetroSim opens **Layout → Sidebar (N) → MetroSim 26**.
3. Choose a mode and enter coordinates. You can paste `22.29660° N, 113.95100° E` into either coordinate field.
4. Choose a radius: default **2 miles**, measured from center to edge.
5. Open **Include** to choose layers. All layers start enabled; the dropdown starts closed.
6. Open **Storage** to choose where files go.
7. Click **Download Area**. Completed downloads are reused.
8. After success, a **10-second countdown** starts the map build. Cancel the countdown to keep only the downloads, or disable **Auto-build after download**.

- **Preview Circle:** check your chosen boundary.
- **Build Map:** build from prepared local inputs.
- **Show build progress:** play an eight-second circular reveal once the map is ready. Roads follow the edge; buildings rise behind it.
- The reveal temporarily uses Material Preview in solid/wireframe views, then restores your shading. Finished geometry stays unchanged.
- Rerun `metrosim.py` after code changes. New Blender files do not require restarting Blender.

## Appearance and expansion

- **Palette:** Natural is the default. Apple Maps, Slate & Earth, Bright, Monochrome and custom colors are available.
- Colors update the current map immediately. Linked earlier maps may share materials.
- **Cameras:** Overview frames the whole area; Map detail gives a closer angled view. Manually adjusted framing is preserved.
- Increase the radius at the same center to choose **Expand existing** or **Rebuild**.
- Compatible expansion reuses inner feature geometry and creates outer fragments. Roads/buildings finish grouped by their OSM feature IDs.
- Land is regenerated across the full circle from cached elevation and finishes as one object. Ocean reuses existing water during expansion and finishes as one object.
- Earlier scenes remain available. Older maps without feature IDs regenerate their grouping once.

## Storage and cleanup

Defaults under your home folder:

| Folder | Contents |
| --- | --- |
| `Desktop/MetroSim 26 Data/Cache` | Shared downloads and location indexes |
| `Desktop/MetroSim 26 Data/Output` | Jobs, meshes, results and logs |
| `Documents/Blender_Logs` | Optional `live_logger.py` console log |

- Names label locations; coordinates and input versions determine cache reuse.
- Overlapping areas can reuse matching data, even with different location names.
- Changing a storage path does not move previous files.
- **Organize downloaded files:** consolidate validated downloads and make a readable saved-area index. Original files stay intact.
- **Clean current Blender project:** remove unused generated Blender data; older scenes are optional.
- **Archive failed jobs:** move eligible failed/cancelled jobs into `Output/_Archived_Jobs`.
- Cancel keeps completed downloads. Interrupted growth simulations do not resume from their last year.

## Growth mode

- Select **Growth simulation** for the supported Omaha / Council Bluffs region.
- Default future: **2026–2076**. Preview ends in **2035**.
- Development follows **infill → redevelopment → expansion** with demand and placement checks.
- Calculations run outside Blender. Scene import and optional saving/rendering run inside Blender.
- Growth import can overlap calculation; future objects have timeline visibility by year.
- **Pause visible build** pauses import/reveal, not calculation. Chunk-speed controls apply to growth import.
- Optional projects, infrastructure and historical evidence: [feature guide](docs/evolution.md).
- `REPLAY` opens compatible completed results. `EXTEND` adds years to a completed run.

## Limits to know

- Buildings and heights are simplified; missing heights may be estimated.
- Inland water follows terrain triangles with 30 cm clearance. This prevents clipping but is not measured river level or riverbed data.
- Coastlines and terrain are approximate. Bridges use estimated clearance; tunnels are omitted.
- Terrain is a map visualization layer; growth planning stays flat.
- Circles crossing the date line/poles, or centered at latitude ±85° or beyond, are unsupported.
- Time estimates need comparable local runs and are not guarantees.
- Expansion still reads/prepares the larger area. More than 400,000 terrain cells requires rebuilding at coarser spacing.

## Code map

```text
metrosim.py → blender_ui.py → runtime.py
                              ├─ datasets/ → cached baseline
                              ├─ growth/ → optional simulation
                              └─ scene.py + geometry/ → mesh packets
blender/scene_import.py → cameras + lighting + presentation + reveal
```

- `geometry/`: separate ground, water, terrain, coastline, buildings, roads and packet helpers.
- `datasets/`: separate cache, validation, downloads, parsing and baseline preparation.
- `growth/`: rules, parallel planning, yearly calculation and checkpoints.
- `blender/`: separate layout, fields, buttons, job scheduling, scene import and appearance.
- `blender_ui.py`: registration, session state and settings conversion.
- `config.py`: run identity. `storage.py`: saved-data organization. `regions/`: regional growth inputs.
- [Code guide](docs/wiki/Code-guide.md): where each responsibility lives and what to test.

## Developer checks

Use Python 3.11+ with NumPy installed:

```sh
python -m pip install numpy ruff==0.16.10
python -m ruff check .
python -m unittest discover -s tests -q
```

Native Blender smoke check (use your Blender executable):

```sh
blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py
```

- The launcher finds Blender's Python. A custom `WORKER_PYTHON` must match Blender's NumPy version.
- `scripts/prefetch_data.py --preview` or `--full` prepares the rectangular presets in `config.py`; use Blender's **Download Area** for your selected circle.
- Personal `.code-workspace` and `.vscode/` files are ignored by Git.

[Wiki](https://github.com/an-vu/metrosim26/wiki) · [FAQ](https://github.com/an-vu/metrosim26/wiki/FAQ)

## Extend a saved simulation

- Run `extend.py` in Blender and select a completed run folder.
- Choose a later end year; only additional years are calculated.
- Results open in a new scene; the original run stays unchanged.
- Manual scene edits are not copied into the new scene.
- A `.blend` alone is not enough; keep the simulation output folder.
- Check compatibility with `python scripts/extend_run.py PATH --end-year 2096 --check`.
- See the [roadmap](docs/wiki/Roadmap.md) for planned work.
