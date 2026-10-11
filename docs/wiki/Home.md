# MetroSim 26

- [Setup and controls](https://github.com/an-vu/metrosim26#readme)
- [FAQ](FAQ)
- [Code guide](Code-guide)
- [Growth, projects and history](https://github.com/an-vu/metrosim26/blob/main/docs/evolution.md)
- [Data credits](Data-Credits)

## Follow a run

1. `metrosim.py` reloads the package and opens the Blender sidebar.
2. `blender_ui.py` reads settings and launches an external job through `runtime.py`.
3. `data.py` exposes `datasets/`: cache, downloads, validation, parsing and baseline preparation.
4. Map mode skips future growth. Growth mode uses `simulation.py`, `growth/` and `workers.py`.
5. `scene.py` connects the `geometry/` feature modules and exports packets with source feature IDs.
6. `blender/scene_import.py` imports and joins geometry in Blender.
7. `blender/cameras.py` frames the map; `lighting.py` sets lights; `presentation.py` handles materials and output.
8. `blender/reveal.py` previews a temporary circular sweep, then restores the finished map.

## Other places to look

- `config.py`: settings, paths and result identity.
- `storage.py`: organize validated downloads and saved-area indexes.
- `regions/omaha_council_bluffs.py`: local growth coverage, employment states and anchor.
- `history.py`, `projects.py`, `infrastructure.py`: optional growth features.
- `tests/`: offline behavior and geometry checks.

The wiki's editable source lives in `docs/wiki/` in the main repository. GitHub serves the published wiki from its separate wiki Git repository; a local wiki checkout is only needed when publishing.

- [Roadmap](Roadmap.md)
