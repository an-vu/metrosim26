# MetroSim26 wiki

- **New here?** Follow the [README setup guide](https://github.com/an-vu/metrosim26#readme).
- **Have a question?** Read the [FAQ](FAQ.md).
- **Want history, projects, or new roads?** Read the [feature guide](https://github.com/an-vu/metrosim26/blob/main/docs/evolution.md).

## How it works

1. **Download:** `scripts/prefetch_data.py` saves maps and employment data.
2. **Launch:** `blender.py` starts the job and opens the progress panel.
3. **Prepare:** `data.py` builds the starting city from the cache.
4. **Simulate:** `simulation.py` chooses development; `workers.py` plans layouts.
5. **Display:** `scene.py` prepares geometry; `blender_ui.py` imports it into Blender.

- `runtime.py` manages the calculation process and saved results.
- `config.py` controls the run.
- Keep the repository folder together so these files can find each other.
