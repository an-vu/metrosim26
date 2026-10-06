#!/usr/bin/env python3
"""Blender entry point. Open this saved file in the Text Editor and run it.

Edit omaha_config.py for scenario settings. Run prefetch_omaha_data.py outside
Blender first; this entry point always uses local data only.
"""

import sys
import time
from pathlib import Path


def project_directory() -> Path:
    """Locate saved sibling modules, including Blender Text Editor execution."""
    candidates = []
    bpy = sys.modules.get("bpy")
    if bpy is not None:
        text = getattr(getattr(bpy.context, "space_data", None), "text", None)
        if text is not None and text.filepath:
            candidates.append(Path(bpy.path.abspath(text.filepath)).parent)
    script = globals().get("__file__")
    if script:
        if bpy is not None and str(script).startswith("//"):
            script = bpy.path.abspath(script)
        candidates.append(Path(script).resolve().parent)
    required = (
        "omaha_config.py",
        "omaha_data.py",
        "omaha_simulation.py",
        "omaha_workers.py",
        "omaha_blender.py",
    )
    for directory in candidates:
        if all((directory / name).is_file() for name in required):
            return directory.resolve()
    raise RuntimeError(
        "Cannot locate Omaha modules. Keep all project files together "
        "and open the saved blender.py in Blender’s Text Editor; "
        "an unsaved pasted text block has no reliable project directory."
    )


def main() -> None:
    import bpy  # pyright: ignore[reportMissingImports] -- provided by Blender

    if bpy.app.version[:3] != (5, 2, 2):
        raise RuntimeError(f"This script targets Blender 5.2.2; detected {bpy.app.version_string}.")
    directory = str(project_directory())
    if directory in sys.path:
        sys.path.remove(directory)
    sys.path.insert(0, directory)
    # Reload settings so edits on disk take effect when rerunning in the Text Editor.
    import importlib

    import omaha_config

    importlib.reload(omaha_config)
    from omaha_blender import build_blender_scene, finalize_blender_output
    from omaha_simulation import prepare_simulation

    cfg = omaha_config.make_config()
    cfg["data_mode"] = "OFFLINE"
    grid, baseline, result = prepare_simulation(cfg)
    cfg["run_directory"] = result["run_directory"]
    started = time.perf_counter()
    scene = build_blender_scene(cfg, grid, baseline, result)
    if cfg.get("profile_performance", True):
        print(f"Blender scene construction: {time.perf_counter() - started:.3f} s")
    finalize_blender_output(cfg, scene)
    print(f"Ready: frame 1 = {cfg['base_year']}; frame {scene.frame_end} = {cfg['end_year']}.")
    print(f"For 2050 use frame {2050 - cfg['base_year'] + 1} in a full run.")
    print(f"Yearly states and summary: {Path(result['run_directory']) / 'simulation'}")


if __name__ == "__main__":
    main()
