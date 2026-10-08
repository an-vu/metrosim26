# MetroSim26

A city-growth simulation for **Blender 5.2.2**.

- Starts with real map geometry and employment data for Omaha / Council Bluffs.
- Simulates growth from **2026 to 2076**.
- Builds a Blender scene so you can explore each year.
- Runs calculations in a separate Python process.
- Shows a possible future, not a prediction.

## Start here

- Use **Windows or macOS**.
- Install **Blender 5.2.2**. The launcher checks this exact version.
- For the data downloader, use **Python 3.11 or newer** with NumPy.
- Keep the whole repository together. Open the saved `blender.py`; do not paste it into an unnamed Blender text block.

### 1. Download the data

Run these commands from your repository folder. Change the first path if you saved it somewhere else.

**Windows — PowerShell**

```powershell
cd "$HOME\Desktop\metrosim26"
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy
.\.venv\Scripts\python.exe scripts\prefetch_data.py --full
```

**macOS — Terminal**

```sh
cd ~/Desktop/metrosim26
python3 -m venv .venv
.venv/bin/python -m pip install numpy
.venv/bin/python scripts/prefetch_data.py --full
```

- On Mac, check `python3 --version` first: it must be **3.11+**.
- Wait for **`CACHE READY FOR BLENDER`**.
- If downloading stops, run the last command again. Completed files are reused.
- Blender reads this local cache; it does not download data during a run.

### 2. Open Blender

1. Open Blender **5.2.2** → **Scripting** → **Text Editor**.
2. Optional: open and run `live_logger.py` to record the console.
3. Open the root `blender.py` from disk → **Run Script**.
4. In the 3D Viewport, press **N** → **MetroSim26** to watch progress or cancel.
5. Wait for **Ready**, then scrub the timeline.

- **Frame 1:** 2026.
- **Frame 25:** 2050.
- **Frame 51:** 2076.
- The launcher finds the other files and Blender's Python automatically.
- Mac rendering currently uses the **CPU fallback**. Metal is not configured by this project.
- If Python detection fails, set `WORKER_PYTHON` in `metrosim26/config.py`. Its NumPy version must match Blender's.

## Change the run

Edit **[metrosim26/config.py](metrosim26/config.py)**.

| Setting | What it does |
| --- | --- |
| `PREVIEW_MODE = True` | Smaller run, ending in 2035. Download with `--preview` instead of `--full`. |
| `RUN_MODE = "SIMULATE"` | Calculate a new run. |
| `RUN_MODE = "REPLAY"` | Open compatible saved results. |
| `RUN_MODE = "AUTO"` | Reuse compatible results, or calculate if none exist. |
| `RUN_MODE = "EXTEND"` | Continue a completed run identified by `EXTEND_FROM`. |
| `CPU_WORKERS = 16` | Maximum planning workers; the actual count may be lower. |
| `ENABLE_PROJECTS = True` | Add fictional developments built in phases. |
| `ENABLE_INFRASTRUCTURE = True` | Add demand-driven access roads. |

- Projects and infrastructure are off by default.
- Historical evidence and optional features: [feature guide](docs/evolution.md).
- To calculate only additional years, see the extension instructions below.

## Extend a completed run

Keep the original `.blend` and simulation output folder. In `metrosim26/config.py`, set:

```python
RUN_MODE = "EXTEND"
EXTEND_FROM = r"C:\path\to\completed_run"  # Or its simulation subfolder
END_YEAR = 2096
```

Keep the source run's other scenario settings, including optional feature flags.
Run the saved root `blender.py` as usual. For a source ending in 2076, calculation
starts in **2077**, reusing its saved baseline and state without downloading inputs.
The source is checksum-validated and retained; extended results go into a separate
run folder. Repeating EXTEND creates another result, never overwrites the source.

Blender imports the full extended timeline into a **new scene**. The original scene
is preserved. This first implementation does not append meshes to an existing scene
or transfer your manual scene edits, camera changes, or materials into the new one.
Scene preparation/import still takes time even though earlier years are not recalculated.

For subsequent playback, keep the original `EXTEND_FROM`, target `END_YEAR`, and
output directory unchanged and switch to `RUN_MODE = "REPLAY"`.
To extend again, use the newly completed result folder as `EXTEND_FROM` and increase
`END_YEAR`. Keep source folders available: source metadata contributes to run identity.

You can validate an extension outside Blender without writing any output:

```sh
python scripts/extend_run.py "path/to/completed_run" --end-year 2096 --check
```

Omit `--check` to calculate without Blender; `--output-dir` changes the output parent.
Scenario settings still come from `config.py`. Completed legacy
`omaha-demand-sites-3.0` runs without optional layers can be restored from their
annual states and version history, even without `continuation.json`.
Runs with projects/infrastructure require their continuation contract. Other model
versions, changed scenario settings, incomplete runs, and damaged files are rejected.
Use the same Python/NumPy environment for numerical reproducibility across extensions.

## Find your files

Paths are relative to your home folder on both systems:

- **Input cache:** `Documents/Blender_MetroSim26_V3_Cache`.
- **Results:** `Documents/Blender_MetroSim26_V3_Output`.
- **Optional console log:** `Documents/Blender_Logs/blender_live_console.log`.
- Each run prints its job folder. It contains `progress.json` and `calculation.log`.
- Canceling keeps completed runs and downloaded data. It does not resume an interrupted simulation.
- After upgrading from Urban Generator, download again and use `SIMULATE`. Old results use a different identity.

## Code map

```text
scripts/prefetch_data.py → local data cache
blender.py → blender_ui.py → runtime.py
runtime.py → data.py → simulation.py → workers.py
runtime.py → scene.py → Blender scene
```

- **`config.py`:** settings and paths.
- **`data.py`:** read maps and employment data; prepare the starting city.
- **`simulation.py`:** decide how the city grows each year.
- **`workers.py`:** calculate building layouts.
- **`runtime.py`:** run calculations, save results, and report progress.
- **`scene.py`:** prepare geometry for Blender.
- **`blender_ui.py`:** progress panel and scene import.
- **`history.py`, `projects.py`, `infrastructure.py`:** optional city-history features.
- **`tests/`:** offline checks. **`docs/`:** guides and wiki source pages.

## Help

- [FAQ — data, progress, and performance](https://github.com/an-vu/metrosim26/wiki/FAQ).
- [Wiki home](https://github.com/an-vu/metrosim26/wiki). Source copies are maintained in `docs/wiki/`.

## Developer checks

Use Python 3.11+ with NumPy installed:

```sh
python -m pip install ruff==0.16.10
python -m ruff check .
python -m unittest discover -s tests -q
```
# Extend an existing saved run

Open the repository's saved `extend.py` in Blender's Text Editor and run it.
The **Extend Saved Run** dialog reads the current scene's saved-run link when
available. Otherwise select the completed run folder (or its `simulation`
subfolder), then choose the new end year. Scenario settings are restored from
the saved metadata, independently of the current scenario defaults in config.py.
After a job completes, the MetroSim26 sidebar also offers **Extend Saved Run**.

Compatible inputs are completed schema-3 MetroSim26 runs and the explicitly
supported `omaha-demand-sites-3.0` legacy runs. Saved files and continuation
contracts are validated before calculation. Partial runs, unsupported versions,
and missing or modified checkpoints are rejected. A `.blend` alone cannot
restore the simulation. If a saved folder moved, select its new location.
Newly imported scenes record the saved-run location; older scenes can use their
`configuration_json` run location where present. Save the resulting Blender
file to retain that connection. This is a launcher, not an installed add-on.

Only additional years are calculated; the full timeline is imported into a new
scene. Original results are preserved, and manual scene edits are not copied.
The output location uses the current `OUTPUT_DIR` setting. Existing personalized
launchers remain usable, but `extend.py` replaces the need for city/year-specific
launchers. For a read-only compatibility check outside Blender:

```text
python scripts/extend_run.py PATH_TO_SAVED_RUN --end-year 2096 --check
```
