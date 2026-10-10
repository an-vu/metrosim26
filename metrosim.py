#!/usr/bin/env python3
"""Blender entry point. Open this saved file in the Text Editor and run it.

Run this file to open Sidebar (N) > MetroSim26. Choose a circular area, prepare
its data, then generate. Downloads and calculations run outside Blender's UI.
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
        "Cannot locate MetroSim26 modules. Keep metrosim.py beside the metrosim26 package folder "
        "and open the saved metrosim.py in Blender’s Text Editor; "
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
    # Blender caches imported Python modules and registered panel classes.
    # Refresh both when rerunning the saved launcher after source changes.
    import importlib

    previous = sys.modules.get("metrosim26.blender_ui")
    saved = {}
    if previous is not None:
        run = getattr(previous, "_ACTIVE_RUN", None)
        if run is not None and run.active:
            raise RuntimeError(
                "A MetroSim26 job is running. Finish or cancel it before reloading the panel."
            )
        watcher = getattr(previous, "location_watch", None)
        if watcher is not None and bpy.app.timers.is_registered(watcher):
            bpy.app.timers.unregister(watcher)
        previous._cancel_on_exit()
        if getattr(previous, "_REGISTERED", False):
            import atexit

            settings = getattr(bpy.context.scene, "metrosim26_setup", None)
            setup_type = getattr(previous, "METROSIM26_PG_setup", None)
            # Registered properties survive New File, but that scene has not
            # received storage defaults yet. Preserve only initialized settings.
            if settings is not None and settings.include_defaults_version >= 1:
                for name in getattr(setup_type, "__annotations__", {}):
                    saved[name] = getattr(settings, name)
            callback = getattr(run, "timer_callback", None)
            if callback is not None and bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
            exit_callback = getattr(previous, "_cancel_on_exit", None)
            if exit_callback is not None:
                atexit.unregister(exit_callback)
            load_callback = getattr(previous, "_cancel_on_load", None)
            if load_callback in bpy.app.handlers.load_pre:
                bpy.app.handlers.load_pre.remove(load_callback)
            if hasattr(bpy.types.Scene, "metrosim26_setup"):
                del bpy.types.Scene.metrosim26_setup
            classes = getattr(previous, "REGISTERED_CLASSES", None)
            if classes is None:
                # Support a panel already loaded before the module split.
                classes = tuple(
                    value
                    for name, value in vars(previous).items()
                    if name.startswith("METROSIM26_")
                    and isinstance(value, type)
                    and getattr(value, "is_registered", False)
                )
            for cls in reversed(classes):
                bpy.utils.unregister_class(cls)
    for name in (
        "regions.omaha_council_bluffs",
        "geometry.terrain",
        "geometry.coastline",
        "geography",
        "config",
        "workers",
        "runtime",
        "datasets.files",
        "datasets.inputs",
        "datasets.validation",
        "datasets.cache",
        "datasets.download",
        "datasets.osm",
        "datasets.lodes",
        "datasets.baseline",
        "datasets.location",
        "datasets.preparation",
        "data",
        "history",
        "projects",
        "infrastructure",
        "growth.capacity",
        "growth.planning",
        "growth.checkpoints",
        "growth.engine",
        "simulation",
        "geometry.polygons",
        "geometry.buildings",
        "geometry.roads",
        "geometry.land",
        "geometry.water",
        "geometry.lifetimes",
        "geometry.packets",
        "scene",
        "storage",
        "blender.cameras",
        "blender.lighting",
        "blender.presentation",
        "blender.coordinates",
        "blender.panel",
        "blender.properties",
        "blender.operators",
        "blender.reveal",
        "blender.scene_import",
        "blender.job",
        "blender_ui",
    ):
        module = sys.modules.get("metrosim26." + name)
        if module is not None:
            importlib.reload(module)
    from metrosim26 import config
    from metrosim26.blender_ui import open_setup

    cfg = config.make_config()
    cfg["data_mode"] = "OFFLINE"
    open_setup(cfg)
    settings = bpy.context.scene.metrosim26_setup
    for name, value in saved.items():
        setattr(settings, name, value)


if __name__ == "__main__":
    main()
