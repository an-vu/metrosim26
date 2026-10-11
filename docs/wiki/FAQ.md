# FAQ

## Can I build anywhere?

- **3D map:** worldwide OpenStreetMap coverage, within the current projection limits.
- **Growth:** currently Omaha / Council Bluffs, using regional employment inputs.
- Polar/date-line crossing circles and centers at ±85° latitude or beyond are unsupported.
- Missing mapped features can limit the result. Download failure alone does not mean the location is unsupported.

## Why is a 2-mile radius about 13 square miles?

- Radius measures center to edge.
- Area measures everything inside the circle: π × 2² ≈ 12.6 square miles.

## Can I paste both coordinates together?

- Yes. Paste a decimal pair or `22.29660° N, 113.95100° E` into either field.
- The two fields update together. North/east are positive; south/west are negative.
- A cached place lookup suggests a detailed name. You can override it.
- Coordinates are sent to the configured lookup service. See [Data credits](Data-Credits) for the service policy.

## Will changing the name or radius download everything again?

- Names do not determine shared download identity.
- Matching geographic cells are reused across overlapping circles and different location names.
- Completed files survive cancellation; retries fetch missing or invalid inputs.
- Older tiles can be reused when they fully cover a requested cell with compatible query/snapshot settings.
- **Find existing data** checks known cache folders. **Organize downloaded files** consolidates validated inputs and writes a readable index.

## Where are my files?

- Downloads: `~/Desktop/MetroSim 26 Data/Cache`.
- Results and logs: `~/Desktop/MetroSim 26 Data/Output`.
- Choose different folders under Storage. Existing chosen paths are preserved.
- Each job has `progress.json` and `calculation.log`.
- Changing folders does not move old files.

## What does each cleanup button do?

- **Organize downloaded files:** copy reusable downloads into the selected cache and index saved areas. Source files remain intact.
- **Clean current Blender project:** remove unused generated Blender data. Removing older scenes is optional.
- **Archive failed jobs:** move eligible failed/cancelled jobs into `Output/_Archived_Jobs`; it does not free disk space by deleting them.

## What happens after Download Area?

- A successful map download starts a **10-second countdown**.
- Cancel stops that automatic build and keeps the downloads.
- Disable **Auto-build after download** to build manually.

## What is Show build progress?

- Maps: after preparation, an **eight-second circular reveal** shows terrain, then roads and rising buildings.
- Solid/wireframe views temporarily use Material Preview, then return to their original shading.
- The effect changes only the preview; finished geometry is unchanged.
- Expansion starts at the old radius.
- Growth: the starting city can import while calculation continues; future visibility follows construction years.
- Pause stops visible import/reveal, not the external calculation. Chunk speed applies to growth import.

## What does Expand existing reuse?

- At the same center with compatible settings, it reuses inner features and generates outer fragments.
- Land is regenerated over the full circle from cached elevation to avoid inherited seams, then joined into one object.
- Ocean reuses existing water during expansion and finishes as one object. Existing scenes stay available.
- Roads/buildings are grouped by source feature ID, not reveal rings. One named street may contain several OSM features.
- Older maps without feature IDs regenerate those features once. Changed settings can use the comparison/rebuild path.
- Preparing the larger area's baseline still takes time, even when downloads are cached.

## Why does the terrain or water look simplified?

- Terrain uses sampled elevation and a triangle grid. Growth planning remains flat.
- Inland water follows that surface with 30 cm clearance to prevent ground clipping through it.
- Water heights are approximate, not a hydraulic simulation or measured riverbed.
- Coastline classification is approximate. Bridges use estimated clearance; tunnels are omitted.
- Turn Terrain off under Include for a flat map. Turning layers off keeps their downloads.

## Can I change colors and cameras?

- Natural is the default palette. Apple Maps, Slate & Earth, Bright, Monochrome and Custom are available.
- Colors update immediately. Linked earlier maps may share the same materials.
- Overview frames the full map; Map detail provides a closer angled view.
- Manually adjusted camera framing is preserved.

## Is Blender stuck?

- Watch the phase, mesh count, elapsed time and job log in **N → MetroSim 26**.
- Preparation may take time before geometry is ready.
- A heartbeat means the reporter is alive; it does not prove forward progress.
- Native mesh operations, saving and rendering can briefly pause Blender.
- For optional console recording, run `live_logger.py` before the launcher. Its log is in `~/Documents/Blender_Logs`.

## Can I cancel or resume?

- Cancel stops the job and removes its partial imported scene; completed downloads and older scenes stay saved.
- Canceling a reveal cleans up its temporary objects/materials too.
- `REPLAY` opens compatible completed results. Interrupted simulations do not resume from their last year.
- `EXTEND` adds years to a completed compatible run; use `extend.py` in Blender.

## Why isn't the GPU or every CPU core busy?

- Preparation and some simulation work are serial; workers help with layout planning.
- The simulation is CPU-based. Rendering is separate.
- Rendering currently tries OptiX, then CPU fallback. Metal selection on Mac is still pending.
- Small workloads may not start a worker pool.

## Is growth a prediction?

- It is a possible scenario, not a forecast.
- Default growth assumptions: 0.8% households and 1% jobs per year.
- Demand and placement checks guide infill, redevelopment and expansion.
- Existing mapped buildings are protected. Missing heights and some uses are estimated.
- LODES home-location job counts are not population or household counts.
- Default baseline label is 2026; inputs have different observation dates, including default 2023 LODES.
- Zoning, ownership, prices, utilities and budgets are not modeled.

## Can I move the project or create a new Blender file?

- Keep the repository together and run its saved root `metrosim.py`.
- Rerun it after edits. New File should not require restarting Blender.
- Keep downloads separately or select their existing folder in Storage.
- A custom worker interpreter must use Blender-compatible NumPy.

## Can I extend a completed simulation?

- Run `extend.py`, choose the saved run folder and a later end year.
- Only new years are calculated; Blender imports the full timeline into a new scene.
- Original results stay unchanged; manual scene edits are not copied.
- This does not resume a cancelled partial run.
