#!/usr/bin/env python3
"""Blender entry point. Open this saved file in the Text Editor and run it.

Edit metrosim26/config.py for scenario settings. Run scripts/prefetch_data.py outside
Blender first; this entry point always uses local data only.
"""

import sys
from pathlib import Path


def project_directory() -> Path:
    """Locate the repository beside the saved launcher, including Text Editor execution."""
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
        "__init__.py",
        "config.py",
        "data.py",
        "simulation.py",
        "workers.py",
        "blender_ui.py",
        "runtime.py",
        "scene.py",
        "history.py",
        "projects.py",
        "infrastructure.py",
    )
    for directory in candidates:
        if all((directory / "metrosim26" / name).is_file() for name in required):
            return directory.resolve()
    raise RuntimeError(
        "Cannot locate MetroSim26 modules. Keep blender.py beside the metrosim26 package folder "
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

    from metrosim26 import config

    importlib.reload(config)
    from metrosim26.blender_ui import start_blender_job

    cfg = config.make_config()
    cfg["data_mode"] = "OFFLINE"
    start_blender_job(cfg)


if __name__ == "__main__":
    main()
