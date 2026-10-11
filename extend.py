"""Open this saved launcher in Blender and run it to extend any compatible saved run."""

import sys
from pathlib import Path


def main():
    import bpy

    if bpy.app.version[:3] != (5, 2, 2):
        raise RuntimeError(f"MetroSim26 targets Blender 5.2.2; detected {bpy.app.version_string}.")
    text = getattr(bpy.context.space_data, "text", None)
    path = bpy.path.abspath(text.filepath) if text and text.filepath else __file__
    root = Path(path).resolve().parent
    if not (root / "metrosim26" / "continuation.py").is_file():
        raise RuntimeError("Open the saved extend.py beside the MetroSim26 package folder.")
    sys.path.insert(0, str(root))
    from metrosim26.blender_ui import show_extension_dialog

    show_extension_dialog()


if __name__ == "__main__":
    main()
