# Urban Generator - Omaha

A Blender 5.2.2 city-growth scenario using OpenStreetMap geometry and Census LODES data. A standalone Python process prepares local data and simulates growth; Blender monitors progress and imports the scene incrementally.

## Setup

Keep all Python files together. Edit **`omaha_config.py`** for settings.

Defaults: full Omaha/Council Bluffs area, 2026–2076, 16 simulation workers, and offline data loading in Blender.

## 1. Download the data outside Blender

Open **PowerShell** and run:

```powershell
& "C:\Program Files (x86)\Steam\steamapps\common\Blender\5.2\python\bin\python.exe" `
    "C:\Users\anvu1\Desktop\Blender Generator\prefetch_omaha_data.py" `
    --full
```

Adjust the folder paths if needed. Alternatively, with ordinary Python and NumPy installed:

```powershell
python .\prefetch_omaha_data.py --full
```

Wait for **`CACHE READY FOR BLENDER`**. The downloader rotates through the main,
Private Coffee, and Kumi Overpass endpoints automatically. If interrupted or a download
fails, rerun the same command; completed files are reused. The cache is stored in
`Documents\Blender_Omaha_V3_Cache`.

## 2. Run inside Blender

1. Run your existing **`live_logger.py`**.
2. Open the saved **`blender.py`** in Blender’s Text Editor and run it. The other modules import automatically.
3. Open the 3D Viewport sidebar (**N → Omaha**) to see phase, year, workers, elapsed time, and **Cancel Simulation**. Blender returns control immediately while calculation runs externally.
4. Wait for **Ready**, then scrub the timeline: frame **1 = 2026**, frame **25 = 2050**, frame **51 = 2076**.

Results are saved in `Documents\Blender_Omaha_V3_Output`. Blender reports missing data instead of downloading it.

The launcher finds Blender's standalone Python automatically. If needed, set
`WORKER_PYTHON` in `omaha_config.py` to a standalone Python with matching NumPy.
Keep `omaha_runtime.py` and `omaha_scene.py` alongside the other modules; neither
needs to be run manually.

## Useful settings

In `omaha_config.py`:

- **Preview:** set `PREVIEW_MODE = True`, then prefetch with `--preview` instead of `--full`. Preview ends in 2035.
- **Recalculate:** `RUN_MODE = "SIMULATE"`.
- **Replay saved results:** `RUN_MODE = "REPLAY"`.
- **Reuse results or calculate if absent:** `RUN_MODE = "AUTO"`.

Keep your logger unchanged. No hard-coded project path is needed in the scripts.

Each launch has a `<scenario>/jobs/<job-id>/` folder containing `progress.json`,
`calculation.log`, and prepared mesh chunks. Fresh simulation checkpoints are under
`result/simulation/`. `latest_completed.json` identifies the completed simulation
used by AUTO/REPLAY; older saved layouts remain readable. Cancellation stops the
calculation process tree and removes a partially imported scene incrementally,
preserving input caches, finished checkpoints, and prior complete runs. An interrupted
simulation is not automatically resumed from its last year. Job folders are retained
for diagnosis; remove unwanted jobs only when idle, keeping the job referenced by
`latest_completed.json`. Previously completed Blender scenes are retained too, so
remove those manually when no longer needed.

## FAQ

### What data and logic does the Omaha simulation use to decide where and what to generate?

The simulator does **not simply place random buildings around the map**. It builds a
baseline from real geography and employment data, estimates future demand, ranks
eligible locations, and generates buildings that satisfy capacity and placement rules.

**Real-world inputs.** [omaha_data.py](omaha_data.py) loads OpenStreetMap buildings,
roads, land use, water, parks/protected areas, amenities, and points of interest such
as shops, offices, schools, and hospitals. It reconstructs geometry from nodes,
ways, and relations, including multipolygons and their holes. Existing OSM building
footprints are protected. Missing heights and some use classifications are inferred;
those inferred properties are not surveyed facts.

Census **2023 LODES8** files for Nebraska and Iowa provide workplace employment
(WAC) and jobs associated with workers' residence locations (RAC). The loader assigns
census blocks to grid cells using crosswalk coordinates and aggregates total and
sector employment. RAC is a residential activity signal, **not population, unique
residents, or a household count**. The 2026 starting label does not mean every input
was observed in 2026; OSM reflects its cached snapshot and LODES uses its configured year.

**Derived signals.** The baseline uses 0.35 km grid cells and, by default, 8×8
subcell samples to estimate land coverage. It combines:

- Road accessibility and alignment from mapped roads; existing development from
  building footprints, land coverage, and employment activity.
- Housing activity from smoothed residence employment and residential land use;
  job activity from smoothed workplace employment.
- Industrial, retail, office, and institutional signals from relevant employment
  sectors, mapped land use, and selected POIs.
- Centrality from distance to the configured downtown Omaha anchor plus workplace
  concentration: a modeled accessibility/density indicator, not an observed district label.

These are heuristics derived from the inputs. Their weights, thresholds, smoothing,
and baseline archetype classifications are model choices. Most baseline signals
remain fixed during a run; the developed-cell map, neighbors, generated sites,
and demand/capacity accounting evolve annually.

**Demand and development rules.** [omaha_config.py](omaha_config.py) defaults to
annual household growth of **0.8%**, job growth of **1.0%**, and initial housing/job
vacancy of **4%** each. Initial households are estimated from OSM residential floor
space unless explicitly configured. Initial jobs use the WAC total unless configured;
if that total is nonpositive, the code labels and uses a nonresidential floor-space
fallback. Floor area per home/job, usable floor space, vacancy, and growth are scenario
assumptions. Existing spare capacity serves demand first, and unmet demand carries
forward rather than disappearing.

[omaha_simulation.py](omaha_simulation.py) then processes development in this order:

1. **Infill:** add buildings on unused parts of generated sites or in eligible existing
   developed cells, preserving real OSM buildings. Existing generated sites get priority.
2. **Redevelopment:** replace only generated sites old enough to qualify (12 years by
   default), following allowed archetype transitions and requiring useful capacity gains.
3. **Greenfield:** expand into eligible undeveloped, road-served cells neighboring
   existing development. This is contiguous growth, not scattered random placement.

Candidate scores combine road access, developed neighbors, centrality, and housing/job
signals weighted by remaining demand. Water/park thresholds and road-access requirements
filter cells; individual footprints also undergo geometry checks against real buildings,
protected polygons, road buffers, cell boundaries, and other buildings in the site.
Annual action/candidate limits also constrain how much demand can be served.

Archetype choice follows explicit rules: housing-dominated demand favors **suburban
residential** or denser **urban residential**, depending on centrality and neighbors;
combined demand in sufficiently central locations can favor **mixed use**. Employment
preferences compare **industrial**, **retail**, **office/institutional**, and
**commercial corridor** signals. New-site **high-rise** selection requires centrality,
job concentration, developed-neighbor thresholds, and substantial remaining demand for
both homes and jobs. Redevelopment uses its own allowed transitions, including the
location eligibility checks for high-rises.

**Procedural variation.** [omaha_workers.py](omaha_workers.py) derives a reproducible
random stream from `SEED`, cell coordinates, archetype, and redevelopment/layout epoch.
Current random draws vary floor counts for some archetypes; a seeded stable ordering
determines which valid candidate lots are filled first. Roads, parcels, setbacks,
building arrangements, and parking otherwise follow archetype templates aligned to
local roads. Randomness does not choose arbitrary map locations or annual growth rates.
It can affect capacity and later outcomes through building heights and lot selection,
so it is not purely cosmetic. With the same inputs, settings, and supported numerical
environment, output is deterministic; the tests compare serial and parallel results.

This is a **data-grounded scenario simulation, not a calibrated prediction of Omaha's
actual future**. It does not model actual future zoning decisions, ownership, land
prices, utility capacity, terrain, or infrastructure budgets. Generated parcels and
streets are procedural approximations, not cadastral or transportation forecasts.

### Why can Blender appear frozen while the Omaha simulation is running, and how can I tell whether it is still working?

Earlier versions ran long calculation stages on Blender's main Python thread,
preventing the interface from repainting even while the log advanced. The current
`blender.py` launches [omaha_runtime.py](omaha_runtime.py) as a standalone process
and polls atomic progress files with `bpy.app.timers`. Heavy calculation and waiting
for the planning broker now happen outside Blender. No calculation process imports
`bpy`, and no background thread mutates Blender data.

A fresh simulation goes through local cache validation, loading/merging OSM tiles,
parsing OSM geometry and relations, streaming LODES gzip/CSV files, deriving baseline
signals and raster masks, building the spatial index, simulation planning/serial
commits. These all run externally, followed by geometry preparation in
[omaha_scene.py](omaha_scene.py). Blender then imports one mesh chunk per timer tick,
with at most 12,000 vertices per chunk, including splitting unusually large polygons
without changing their faces. Completed replay also loads saved data externally.

The **Omaha** panel reports these phases and yearly/mesh progress. Individual Blender
mesh updates, device initialization, and final scene activation are still native,
indivisible operations and can briefly pause the UI; timers cannot preempt them.
Optional automatic `.blend` saving and rendering remain synchronous and may block;
they are disabled by default. Cancellation is available between scene-import ticks,
not during a synchronous native export.

Run **`live_logger.py` first**, then open and run the saved **`blender.py`**. The logger
mirrors Python stdout/stderr to Blender's `LIVE_CONSOLE_LOG` text block and a disk log.
The timer forwards the child's disk log into the existing logger in bounded reads.
You can also open a separate **PowerShell** window and watch the file:

```powershell
Get-Content "$env:USERPROFILE\Documents\Blender_Logs\blender_live_console.log" -Wait -Tail 30
```

Starting the logger again resets its disk log. It records printed messages; it is not
an independent heartbeat. The child also writes directly to the job's `calculation.log`,
whose directory is printed at launch, even if Blender's display pauses.

| Message | What it means in the current code |
| --- | --- |
| `Local input cache validated.` | Required source files passed local validation. Geography parsing and baseline preparation still follow. |
| `OSM: loading ...` | The child reads and merges ordered local tiles. The panel separately reports geometry/relation parsing. |
| `LODES: streaming ...` | Work is starting on that state's crosswalk and WAC/RAC files. Each compressed CSV may take time to read and aggregate. |
| `Baseline: ...` | Parsing, derived signals/raster masks, and the baseline spatial index have completed. |
| `Data preparation + baseline save: ...` | Baseline preparation and saving have finished. This elapsed-time report appears after the work, not as a live counter. |
| `Worker pool ready: ...` | The external broker and spawned planning workers have started. The pool starts lazily when a qualifying batch is available. |
| `Year ... timings:` / `Year ...:` | Annual simulation progress and development/demand results. Planning, ranking, checkpoint writing, and serial commits contribute to each year. |
| `Blender scene construction: importing ...` | External geometry preparation finished; bounded main-thread mesh imports are starting. |
| `Blender scene construction: <seconds> s` | Scene import finished. Optional saving/rendering may still follow. |
| `Ready: ...` | The scene is complete and the log reports the timeline's year/frame mapping. |

Timing reports depend on `PROFILE_PERFORMANCE`, enabled by default. Small/cheap workloads
may report `Pool not started`; interpreter startup failures report `SERIAL FALLBACK`.
Neither message by itself means the simulation has stopped.

**An advancing phase, year, mesh count, or log indicates progress.** During long loops,
the child's `progress.json` heartbeat updates roughly every second; the panel warns
after ten seconds without a recent heartbeat. A heartbeat shows that the reporting
thread is alive, not that the computation itself is advancing. Inspect the last phase,
child Python CPU/disk activity, and elapsed time relative to comparable runs. A much
longer-than-usual pause with no advancing work and idle processes merits investigation;
errors appear in the panel and logs. There is no universal baseline timeout. The
prefetcher's HTTP deadline does not apply to these offline stages.

### Why doesn't Blender use 100% of my CPU, GPU, and RAM while the simulation is running?

Utilization depends on the current stage. The goal is to finish correct work efficiently,
not to maximize Task Manager percentages.

- **CPU:** the tested workstation has **20 logical CPU threads**, so approximately
  **5% total CPU can represent one logical thread fully occupied**. Scheduling and
  hybrid-core differences make this approximate. OSM parsing, LODES aggregation,
  baseline derivation, and geometry preparation are primarily serial work in the
  standalone calculation process. Mesh creation runs on Blender's main thread in
  bounded steps. Some NumPy/native operations can behave differently, but
  these stages are not distributed across the simulation worker pool.
- **Parallel planning:** `CPU_WORKERS = 16` is a ceiling for independent site-plan
  precomputation, capped by detected CPUs and the first qualifying batch. The pool
  is started lazily and may be skipped for cheap work. CPU activity should rise
  across the child Python processes while they are planning. Candidate decisions,
  demand updates, authoritative commits, and checkpoint writing remain serial;
  the parent also waits for batches. Sixteen workers do not imply 100% CPU throughout.
- **GPU:** OptiX is configured for **Cycles rendering**, with the existing preference
  for the RTX 3080 Ti and CPU fallback unless OptiX is required. It does not accelerate
  ordinary Python, CSV/OSM processing, baseline parsing, or most simulation logic.
  Automatic final rendering is disabled by default, so idle GPU compute is expected
  during data preparation and simulation. Viewport display is separate from simulation.
- **RAM:** unused memory is normal. Memory use follows the dataset, arrays, geometry,
  and scene size; planning workers share read-only memory-mapped inputs rather than
  deliberately filling RAM. Low memory use does not indicate a failure.
- **Network:** this project's network activity should be zero during Blender execution.
  `blender.py` forces offline data loading; input acquisition happens separately in
  `prefetch_omaha_data.py`. Other applications or Blender add-ons may have their own traffic.

The observed preview simulation benchmark on the tested workstation was:

| Simulation mode | Elapsed time |
| --- | ---: |
| Serial | 96.471 s |
| 16 workers | 41.162 s |
| Speedup | ~2.34× |

These are measured simulation times from that run, not a full-pipeline speed guarantee.
Full-metro testing has identified **baseline/data preparation as the next significant
performance bottleneck**: one observed run took **820.957 s** for baseline preparation
and saving with 259,230 OSM buildings, 101,761 roads, 8,831 land features, and 8,602
developed cells. Moving that work externally frees Blender's UI; it does not
parallelize preparation or promise a faster baseline.

## Code style and checks

Formatting and basic Pyright/Pylance type-check settings live in `pyproject.toml`.
To format, lint, and test with ordinary Python and NumPy installed:

```sh
python -m pip install ruff==0.16.10
ruff format .
ruff check .
python -m unittest discover -s tests -q
```
