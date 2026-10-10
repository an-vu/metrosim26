"""Blender property declarations and defaults, independent of panel drawing."""

from .coordinates import latitude_text_changed, longitude_text_changed
from .presentation import PALETTES

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


def make_setup_type(*, LAYER_DEFAULTS, palette_changed, preset_changed):
    class METROSIM26_PG_setup(bpy.types.PropertyGroup):
        # Explicit annotations because this module uses postponed type annotations.
        __annotations__ = {
            **{
                "include_" + key: bpy.props.BoolProperty(
                    name={
                        "parks": "Parks and land",
                        "ocean": "Ocean / coastline",
                        "terrain": "Terrain / hills / mountains",
                        "ground": "Ground surface",
                    }.get(key, key.title()),
                    default=value,
                )
                for key, value in LAYER_DEFAULTS.items()
            },
            **{
                "color_" + key: bpy.props.FloatVectorProperty(
                    name={
                        "existing": "Buildings",
                        "road": "Roads",
                        "park": "Parks",
                        "water": "Water",
                        "ground": "Ground",
                    }[key],
                    subtype="COLOR",
                    size=3,
                    min=0,
                    max=1,
                    default=value,
                    update=palette_changed,
                )
                for key, value in PALETTES["NATURAL"].items()
            },
            "palette_preset": bpy.props.EnumProperty(
                name="Palette",
                items=[
                    ("APPLE", "Apple Maps", "Light land, green parks and blue water"),
                    ("NATURAL", "Natural", "Muted colors"),
                    (
                        "SLATE_EARTH",
                        "Slate & Earth",
                        "Concrete gray, charcoal roads, olive parks and slate blue water",
                    ),
                    ("BRIGHT", "Bright", "Clear contrasting colors"),
                    ("MONO", "Monochrome", "Gray map"),
                    ("CUSTOM", "Custom", "Use your own colors"),
                ],
                default="NATURAL",
                update=preset_changed,
            ),
            **{
                key: bpy.props.BoolProperty(default=False)
                for key in ("show_include", "show_style", "show_cleanup")
            },
            "location_coordinates": bpy.props.StringProperty(),
            "location_style": bpy.props.StringProperty(),
            "auto_location_name": bpy.props.StringProperty(),
            "location_name": bpy.props.StringProperty(
                name="Name",
                description="Optional name for output folders; downloaded cells stay shared in Cache",
            ),
            "location_label": bpy.props.StringProperty(name="Detected place"),
            "build_choice": bpy.props.EnumProperty(
                name="Existing map",
                items=[
                    (
                        "EXPAND",
                        "Expand existing",
                        "Reuse unchanged inner meshes; rebuild boundary and changed geometry",
                    ),
                    ("REBUILD", "Rebuild", "Create a fresh map; keep previous scenes"),
                ],
                default="EXPAND",
            ),
            "auto_build": bpy.props.BoolProperty(name="Auto-build after download", default=True),
            "show_storage": bpy.props.BoolProperty(default=False),
            "show_details": bpy.props.BoolProperty(default=False),
            "cache_dir": bpy.props.StringProperty(name="Cache", subtype="DIR_PATH"),
            "reuse_cache_dir": bpy.props.StringProperty(
                name="Existing downloads", subtype="DIR_PATH"
            ),
            "cache_summary": bpy.props.StringProperty(default=""),
            "output_dir": bpy.props.StringProperty(name="Results", subtype="DIR_PATH"),
            "view_mode": bpy.props.EnumProperty(
                name="Mode",
                default="MAP",
                items=[
                    ("MAP", "3D map", "View mapped geometry worldwide without simulating growth"),
                    (
                        "GROWTH",
                        "Growth simulation",
                        "Requires the supported regional employment inputs",
                    ),
                ],
            ),
            "coordinate_latitude": bpy.props.StringProperty(
                name="Latitude (N / S)",
                default="41.25000",
                update=latitude_text_changed,
                description="Enter latitude or paste a full latitude, longitude pair",
            ),
            "coordinate_longitude": bpy.props.StringProperty(
                name="Longitude (E / W)",
                default="-95.99000",
                update=longitude_text_changed,
                description="Enter longitude or paste a full latitude, longitude pair",
            ),
            "coordinate_error": bpy.props.StringProperty(),
            "include_defaults_version": bpy.props.IntProperty(default=0),
            "latitude": bpy.props.FloatProperty(
                name="Latitude (N / S)",
                description="Positive = north; negative = south",
                default=41.25,
                precision=5,
                min=-85,
                max=85,
            ),
            "longitude": bpy.props.FloatProperty(
                name="Longitude (E / W)",
                description="Positive = east; negative = west",
                default=-95.99,
                precision=5,
                min=-180,
                max=180,
            ),
            "radius": bpy.props.FloatProperty(name="Radius", default=2, min=0.1, max=200),
            "units": bpy.props.EnumProperty(
                items=[("KM", "km", "Kilometers"), ("MILES", "miles", "Miles")], default="MILES"
            ),
            "end_year": bpy.props.IntProperty(name="End year", default=2076, min=2026, max=2200),
            "run_mode": bpy.props.EnumProperty(
                name="Results",
                items=[
                    ("SIMULATE", "New simulation", "Calculate"),
                    ("AUTO", "Reuse if available", "Reuse compatible results"),
                    ("REPLAY", "Replay only", "Require saved results"),
                ],
            ),
            "progressive_build": bpy.props.BoolProperty(
                name="Show build progress",
                description="Reveal map chunks from the center outward while building",
                default=True,
            ),
            "reveal_delay": bpy.props.FloatProperty(
                name="Seconds per chunk", default=0.05, min=0.01, max=1.0
            ),
            "pause_reveal": bpy.props.BoolProperty(name="Pause visible build", default=False),
        }

    return METROSIM26_PG_setup
