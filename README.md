# Urban Generator - Omaha

A Blender 5.2.2 city-growth scenario using OpenStreetMap geometry and Census LODES data. Downloads happen outside Blender; Blender loads the local cache, simulates growth, and builds the scene.

## Setup

Keep all Python files together. Edit **`omaha_config.py`** for settings.

Defaults: full Omaha/Council Bluffs area, 2026–2076, 16 simulation workers, and offline data loading in Blender.

## 1. Download the data outside Blender

Open **PowerShell** and run:

```powershell
Set-Location "C:\Users\anvu1\Desktop\urban-generator"
$blenderPython = Get-ChildItem "C:\Program Files (x86)\Steam\steamapps\common\Blender\*\python\bin\python.exe" -File |
    Sort-Object FullName -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $blenderPython) { throw "Bundled Python not found; check the Blender installation folder." }
& $blenderPython .\prefetch_omaha_data.py --full
```

Adjust the folder paths if needed. Alternatively, with ordinary Python and NumPy installed:

```powershell
python .\prefetch_omaha_data.py --full
```

Wait for **`CACHE READY FOR BLENDER`**. If interrupted or a download fails, rerun the same command; completed files are reused. The cache is stored in `Documents\Blender_Omaha_V3_Cache`.

## 2. Run inside Blender

1. Run your existing **`live_logger.py`**.
2. Open the saved **`blender.py`** in Blender’s Text Editor and run it. The other modules import automatically.
3. Scrub the timeline: frame **1 = 2026**, frame **25 = 2050**, frame **51 = 2076**.

Results are saved in `Documents\Blender_Omaha_V3_Output`. Blender reports missing data instead of downloading it.

## Useful settings

In `omaha_config.py`:

- **Preview:** set `PREVIEW_MODE = True`, then prefetch with `--preview` instead of `--full`. Preview ends in 2035.
- **Recalculate:** `RUN_MODE = "SIMULATE"`.
- **Replay saved results:** `RUN_MODE = "REPLAY"`.
- **Reuse results or calculate if absent:** `RUN_MODE = "AUTO"`.

Keep your logger unchanged. No hard-coded project path is needed in the scripts.

## Code style and checks

Formatting and basic Pyright/Pylance type-check settings live in `pyproject.toml`.
To format, lint, and test with ordinary Python and NumPy installed:

```sh
python -m pip install ruff==0.16.10
ruff format .
ruff check .
python -m unittest discover -s tests -q
```
