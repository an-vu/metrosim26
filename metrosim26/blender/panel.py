"""Sidebar layout only. Receives state and actions without owning job or geometry behavior."""

import math
import time

from .presentation import PALETTES


def draw_status(
    self, context, run, *, readable_labels, LAYER_DEFAULTS, setup_config, estimate_text, smaller_map
):
    layout = self.layout
    settings = context.scene.metrosim26_setup
    box = layout.box()
    width = max(
        18,
        int((getattr(context.region, "width", 300) / context.preferences.system.ui_scale - 50) / 8),
    )
    box.label(text="Location")
    controls = box.column()
    controls.enabled = run is None or not run.active
    controls.prop(settings, "location_name")
    if settings.location_label:
        readable_labels(box, settings.location_label, width)
    controls.prop(settings, "view_mode")
    coordinates = controls.column(align=True)
    coordinates.label(text="Latitude (N / S)")
    coordinates.prop(settings, "coordinate_latitude", text="")
    coordinates.label(text="Longitude (E / W)")
    coordinates.prop(settings, "coordinate_longitude", text="")
    if settings.coordinate_error:
        readable_labels(controls, settings.coordinate_error, width)
    controls.separator()
    controls.label(text="Area")
    row = controls.row()
    row.prop(settings, "radius")
    row.prop(settings, "units", text="")
    controls.prop(
        settings,
        "show_include",
        text="Include",
        icon="TRIA_DOWN" if settings.show_include else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.show_include:
        for key in LAYER_DEFAULTS:
            row = controls.row()
            if key in {"ocean", "terrain"}:
                row.enabled = settings.view_mode == "MAP" and (
                    key != "ocean" or settings.include_water
                )
            row.prop(settings, "include_" + key)
        readable_labels(
            controls,
            "Terrain and ocean need Download Area. Unchecked layers keep their cached data.",
            width,
        )
    if settings.view_mode == "GROWTH":
        controls.prop(settings, "end_year")
        controls.prop(settings, "run_mode")
    controls.prop(
        settings,
        "show_storage",
        text="Storage",
        icon="TRIA_DOWN" if settings.show_storage else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.show_storage:
        controls.label(text="Downloads · Cache")
        controls.prop(settings, "cache_dir", text="")
        controls.label(text="Reuse another Downloads folder")
        controls.prop(settings, "reuse_cache_dir", text="")
        controls.operator("metrosim26.find_cached_data", text="Find existing data")
        if settings.cache_summary:
            readable_labels(controls, settings.cache_summary, width)
        controls.label(text="Maps + logs · Output")
        controls.prop(settings, "output_dir", text="")
    radius = settings.radius * (1.609344 if settings.units == "MILES" else 1)
    area = math.pi * settings.radius**2
    box.label(text=f"Radius: {settings.radius:g} {settings.units.lower()}")
    box.label(text="Center to edge")
    box.label(text=f"Area: {area:,.0f} {'mi²' if settings.units == 'MILES' else 'km²'}")
    if radius >= 20:
        readable_labels(
            box,
            "Large area: may take hours and use substantial memory. Start smaller.",
            width,
        )
    try:
        cfg = setup_config(settings)
        controls.prop(
            settings,
            "show_details",
            text="Details",
            icon="TRIA_DOWN" if settings.show_details else "TRIA_RIGHT",
            emboss=False,
        )
        if settings.show_details:
            for line in estimate_text(cfg):
                readable_labels(box, line, width)
        controls.separator()
        controls.label(text="Build")
        controls.prop(settings, "progressive_build")
        if settings.view_mode == "MAP":
            controls.prop(settings, "auto_build")
            if smaller_map(cfg) is not None:
                controls.label(text="Smaller map found")
                controls.prop(settings, "build_choice", text="")
        controls.operator("metrosim26.preview_area")
        controls.operator("metrosim26.prepare_area")
        controls.operator(
            "metrosim26.generate_city",
            text="Build Map" if settings.view_mode == "MAP" else "Simulate",
        )
    except ValueError as error:
        readable_labels(box, str(error), width)
    if any(obj.get("metrosim26_camera_role") for obj in context.scene.objects):
        cameras = layout.box()
        cameras.label(text="Camera view")
        row = cameras.row(align=True)
        row.operator("metrosim26.camera_view", text="Overview").view = "overview"
        row.operator("metrosim26.camera_view", text="Map detail").view = "detail"
    layout.prop(
        settings,
        "show_style",
        text="Palette",
        icon="TRIA_DOWN" if settings.show_style else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.show_style:
        style = layout.box()
        style.prop(settings, "palette_preset", text="")
        for key in PALETTES["NATURAL"]:
            style.prop(settings, "color_" + key)
    layout.prop(
        settings,
        "show_cleanup",
        text="Cleanup",
        icon="TRIA_DOWN" if settings.show_cleanup else "TRIA_RIGHT",
        emboss=False,
    )
    if settings.show_cleanup:
        clean = layout.column()
        clean.enabled = run is None or not run.active
        clean.operator("metrosim26.consolidate_data")
        clean.operator("metrosim26.clean_scene")
        clean.operator("metrosim26.clean_jobs")
    if run is None or not run.active:
        layout.operator("metrosim26.extend_saved_run")
    if run is None:
        layout.label(text="Preview area, prepare data, then generate.")
        return
    status = run.status
    layout.label(text="Phase: " + status["phase"].replace("_", " ").title())
    readable_labels(layout, status.get("message", ""), width)
    if status.get("warning"):
        readable_labels(layout, status["warning"], width)
    year = status.get("year")
    if year is not None and settings.view_mode == "GROWTH":
        layout.label(text=f"Year: {year} / {status['end_year']}")
    if settings.show_details:
        layout.label(text=f"Workers: {status.get('worker_processes', 0)}")
    elapsed = status.get("elapsed_seconds", 0)
    layout.label(text=f"Elapsed: {int(elapsed // 60)}m {int(elapsed % 60)}s")
    if status.get("geometry_records") is not None:
        layout.label(text=f"Geometry: {status['geometry_records']:,} records")
    if run.importer is not None:
        layout.label(text=f"Meshes: {run.importer.done:,} / {run.importer.total:,}")
        if run.importer.scene is not None:
            layout.label(text=f"Reused objects: {run.importer.scene.get('reused_objects', 0):,}")
    if status["phase"] == "building_scene":
        layout.progress(factor=status.get("scene_progress", 0), text="Scene meshes")
    elif status["phase"] == "simulation" and year is not None:
        span = max(1, status["end_year"] - status["start_year"])
        layout.progress(
            factor=max(0, min(1, (year - status["start_year"]) / span)),
            text="Simulation year",
        )
    heartbeat = status.get("heartbeat_utc")
    if run.active and heartbeat is not None and time.time() - heartbeat > 10:
        layout.label(text="No recent heartbeat; check calculation.log", icon="ERROR")
    if status.get("error"):
        layout.label(text="See live log or job calculation.log", icon="ERROR")
    if status["phase"] == "downloading":
        layout.progress(factor=status.get("download_progress", 0), text="Ready files")
    if status["phase"] == "building_scene":
        layout.prop(settings, "pause_reveal", text="Pause visible build")
        if settings.view_mode != "MAP":
            layout.prop(settings, "reveal_delay")
    if run.active:
        row = layout.row()
        row.enabled = not run.cancel_requested
        row.operator("metrosim26.cancel_simulation")
