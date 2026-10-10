"""Blender button actions. Session state and job launch are explicit callbacks."""

import json
import math
import shutil
import time
from pathlib import Path

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


def make_operator_types(*, get_run, setup_config, start_blender_job, storage_path):
    class METROSIM26_OT_cancel(bpy.types.Operator):
        bl_idname = "metrosim26.cancel_simulation"
        bl_label = "Cancel"

        def execute(self, context):
            run = get_run()
            if run is not None:
                run.cancel()
            return {"FINISHED"}

    class METROSIM26_OT_clean_scene(bpy.types.Operator):
        bl_idname = "metrosim26.clean_scene"
        bl_label = "Clean current Blender project"
        bl_description = (
            "Remove unused MetroSim objects, meshes and materials from this Blender project"
        )
        bl_options = {"REGISTER", "UNDO"}
        __annotations__ = {
            "older_maps": bpy.props.BoolProperty(name="Remove older MetroSim scenes", default=False)
        }

        def invoke(self, context, event):
            return context.window_manager.invoke_props_dialog(self)

        def draw(self, context):
            self.layout.label(text="Remove unused MetroSim data only.")
            self.layout.prop(self, "older_maps")

        def execute(self, context):
            run = get_run()
            if run is not None and run.active:
                self.report({"ERROR"}, "Finish or cancel the current job first.")
                return {"CANCELLED"}
            protected = {window.scene.name for window in context.window_manager.windows}
            removed = 0
            if self.older_maps:
                for scene in list(bpy.data.scenes):
                    if scene.name not in protected and (
                        scene.get("metrosim26_map") or scene.get("metrosim26_simulation")
                    ):
                        bpy.data.scenes.remove(scene)
                        removed += 1
            for database in (
                bpy.data.collections,
                bpy.data.objects,
                bpy.data.meshes,
                bpy.data.materials,
                bpy.data.cameras,
                bpy.data.lights,
                bpy.data.worlds,
            ):
                for item in list(database):
                    if item.users == 0 and item.get("metrosim26_generated_visualization"):
                        database.remove(item)
                        removed += 1
            self.report({"INFO"}, f"Removed {removed} unused MetroSim items.")
            return {"FINISHED"}

    class METROSIM26_OT_camera_view(bpy.types.Operator):
        bl_idname = "metrosim26.camera_view"
        bl_label = "Switch map camera"
        bl_description = "Switch between the whole-area overview and a tilted neighborhood view"
        __annotations__ = {"view": bpy.props.StringProperty(default="overview")}

        def execute(self, context):
            camera = next(
                (
                    obj
                    for obj in context.scene.objects
                    if obj.type == "CAMERA" and obj.get("metrosim26_camera_role") == self.view
                ),
                None,
            )
            if camera is None:
                self.report({"ERROR"}, "Build a map first to create this camera")
                return {"CANCELLED"}
            context.scene.camera = camera
            for area in context.screen.areas if context.screen else []:
                if area.type == "VIEW_3D":
                    area.spaces.active.region_3d.view_perspective = "CAMERA"
                    area.tag_redraw()
            return {"FINISHED"}

    class METROSIM26_OT_consolidate(bpy.types.Operator):
        bl_idname = "metrosim26.consolidate_data"
        bl_label = "Organize downloaded files"
        bl_description = "Collect reusable downloads and create a readable location index"

        def execute(self, context):
            run = get_run()
            if run is not None and run.active:
                self.report({"ERROR"}, "Finish or cancel the current job first")
                return {"CANCELLED"}
            try:
                from metrosim26.storage import consolidate

                result = consolidate(setup_config(context.scene.metrosim26_setup))
                self.report(
                    {"INFO"},
                    f"Saved Areas: {result['areas']} locations; {result['copied']} downloads copied; {result['invalid']} invalid files skipped",
                )
            except (OSError, ValueError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            return {"FINISHED"}

    class METROSIM26_OT_clean_jobs(bpy.types.Operator):
        bl_idname = "metrosim26.clean_jobs"
        bl_label = "Archive failed jobs"
        __annotations__ = {"days": bpy.props.IntProperty(name="Older than days", default=7, min=0)}

        def invoke(self, context, event):
            return context.window_manager.invoke_props_dialog(self)

        def draw(self, context):
            self.layout.label(text="Move failed/cancelled jobs to _Archived_Jobs.")
            self.layout.label(text="Downloads and successful results stay saved.")
            self.layout.prop(self, "days")

        def execute(self, context):
            run = get_run()
            if run is not None and run.active:
                self.report({"ERROR"}, "Finish or cancel the current job first.")
                return {"CANCELLED"}
            root = Path(storage_path(context.scene.metrosim26_setup.output_dir))
            removed = 0
            for job in root.glob("*/jobs/*"):
                progress = job / "progress.json"
                if (
                    not job.is_dir()
                    or job.is_symlink()
                    or not progress.is_file()
                    or time.time() - progress.stat().st_mtime < self.days * 86400
                ):
                    continue
                try:
                    status = json.loads(progress.read_text())
                    job_cfg = json.loads((job / "config.json").read_text())
                    if not str(job_cfg.get("model_version", "")).startswith("metrosim26-"):
                        continue
                except (OSError, ValueError):
                    continue
                if status.get("phase") not in {"error", "cancelled"}:
                    continue
                target = root / "_Archived_Jobs" / job.name
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(job), str(target))
                removed += 1
            self.report({"INFO"}, f"Archived {removed} failed jobs. Downloads kept.")
            return {"FINISHED"}

    class METROSIM26_OT_find_cache(bpy.types.Operator):
        bl_idname = "metrosim26.find_cached_data"
        bl_label = "Find existing data"

        def execute(self, context):
            run = get_run()
            if run is not None and run.active:
                self.report({"ERROR"}, "Wait for the current job to finish")
                return {"CANCELLED"}
            try:
                from metrosim26.data import discover_cached_inputs

                settings = context.scene.metrosim26_setup
                result = discover_cached_inputs(setup_config(settings))
                settings.cache_summary = (
                    f"{result['ready']} files available · {result['missing']} need downloading"
                )
                self.report({"INFO"}, settings.cache_summary)
            except (ValueError, OSError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            return {"FINISHED"}

    class METROSIM26_OT_prepare(bpy.types.Operator):
        bl_idname = "metrosim26.prepare_area"
        bl_label = "Download Area"

        def execute(self, context):
            try:
                cfg = setup_config(context.scene.metrosim26_setup)
                cfg["job_action"] = "PREFETCH"
                start_blender_job(cfg)
            except (ValueError, RuntimeError, OSError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            return {"FINISHED"}

    class METROSIM26_OT_generate(bpy.types.Operator):
        bl_idname = "metrosim26.generate_city"
        bl_label = "Generate City"

        def execute(self, context):
            try:
                start_blender_job(setup_config(context.scene.metrosim26_setup))
            except (ValueError, RuntimeError, OSError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            return {"FINISHED"}

    class METROSIM26_OT_preview(bpy.types.Operator):
        bl_idname = "metrosim26.preview_area"
        bl_label = "Preview Circle"

        def execute(self, context):
            try:
                cfg = setup_config(context.scene.metrosim26_setup)
            except ValueError as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            for obj in list(context.scene.objects):
                if obj.get("metrosim26_area_preview"):
                    mesh = obj.data
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
            radius = cfg["study_radius_km"]
            points = [
                (radius * math.cos(i * math.tau / 96), radius * math.sin(i * math.tau / 96), 0)
                for i in range(96)
            ]
            mesh = bpy.data.meshes.new("MetroSim26 area boundary")
            mesh.from_pydata(points, [(i, (i + 1) % 96) for i in range(96)], [])
            obj = bpy.data.objects.new("MetroSim26 area boundary", mesh)
            obj["metrosim26_area_preview"] = True
            context.scene.collection.objects.link(obj)
            context.scene.unit_settings.system = "METRIC"
            context.scene.unit_settings.scale_length = 1000
            return {"FINISHED"}

    return (
        METROSIM26_OT_cancel,
        METROSIM26_OT_clean_scene,
        METROSIM26_OT_camera_view,
        METROSIM26_OT_consolidate,
        METROSIM26_OT_clean_jobs,
        METROSIM26_OT_find_cache,
        METROSIM26_OT_prepare,
        METROSIM26_OT_generate,
        METROSIM26_OT_preview,
    )
