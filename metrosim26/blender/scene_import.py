"""Bounded mesh import, expansion reuse, feature joining, and cancellation cleanup."""

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from metrosim26.config import timeline_start

from .presentation import (
    _configure_camera_and_lighting,
    _configure_cycles,
    _new_material,
    _set_object_lifetime,
    activate_scene_preserving_view,
    camera_was_adjusted,
    collapse_generated_outliner,
    fit_overview_to_geometry,
)
from .reveal import reveal_map

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


def face_key(vertices, face, collection, material):
    points = np.round(np.asarray([vertices[index] for index in face]), 5).astype("<f8")
    points[points == 0] = 0
    return (collection, material, hashlib.blake2b(points.tobytes(), digest_size=16).digest())


class SceneImport:
    """One bounded mesh per step; all methods must run on Blender's main thread."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.scene: Any = None
        self.owned = []
        self.finished = False
        self.revealing = False
        self.done = 0
        self.total = 0
        self.message = "Reading mesh manifest"
        self.steps = self._steps()
        self.cfg = {}
        self.warning = ""
        self.viewport_render = {}
        if bpy is not None:
            self.viewport_render = {
                key: getattr(bpy.context.scene.cycles, key)
                for key in ("preview_samples", "use_preview_denoising")
            }
        self.source_camera = None
        if bpy is not None and bpy.context.scene.camera is not None:
            if any(
                area.type == "VIEW_3D" and area.spaces.active.region_3d.view_perspective == "CAMERA"
                for area in (bpy.context.screen.areas if bpy.context.screen else [])
            ):
                candidate = bpy.context.scene.camera
                # The starter camera is unrelated to the requested map scale.
                starter = (
                    candidate.data.type == "PERSP"
                    and abs(candidate.data.lens - 50) < 1e-4
                    and np.allclose(candidate.location, (7.358891, -6.925791, 4.958309), atol=1e-3)
                )
                if not starter and camera_was_adjusted(candidate):
                    self.source_camera = candidate

    def own(self, database, item):
        self.owned.append((database, item))
        return item

    def join_objects(self, objects, label, collection, material=None):
        """Join copies in the new scene; keep objects shared by earlier maps intact."""
        import bmesh

        combined = bmesh.new()
        try:
            for index, old in enumerate(objects, 1):
                self.message = f"Joining {label}: {index}/{len(objects)}"
                combined.from_mesh(old.data)
                yield
            bmesh.ops.remove_doubles(combined, verts=list(combined.verts), dist=1e-7)
            combined.normal_update()
            mesh = self.own(bpy.data.meshes, bpy.data.meshes.new(label))
            combined.to_mesh(mesh)
        finally:
            combined.free()
        mesh.materials.append(material or objects[0].data.materials[0])
        mesh["metrosim26_generated_visualization"] = True
        obj = self.own(bpy.data.objects, bpy.data.objects.new(label, mesh))
        for key in objects[0].keys():
            obj[key] = objects[0][key]
        collection.objects.link(obj)
        for old in objects:
            if old.name in collection.objects:
                collection.objects.unlink(old)
        yield

    def stitch_surfaces(self, scene, collections, materials):
        """Finish land and ocean independently of import and expansion batches."""
        preview = getattr(self, "preview_ground", [])
        for old in preview:
            if old.name in collections["ground"].objects:
                collections["ground"].objects.unlink(old)
        for label, group, material in (
            ("Study_Ground", "ground", "MetroSim26_ground"),
            ("Ocean", "land", "MetroSim26_water"),
        ):
            objects = [
                obj
                for obj in scene.objects
                if obj.type == "MESH"
                and obj.get("metrosim26_generated_visualization")
                and obj.name.startswith(label)
            ]
            if len(objects) == 1:
                objects[0].hide_set(False, view_layer=scene.view_layers[0])
                if objects[0].users == 1:
                    objects[0].name = label
            elif objects:
                yield from self.join_objects(
                    objects, label, collections[group], materials[material]
                )

    def stitch_features(self, scene, collections):
        """Feature identity determines final objects, independent of reveal bands."""
        groups = {}
        for obj in scene.objects:
            feature = obj.get("feature_id")
            if obj.type == "MESH" and feature and obj.get("metrosim26_generated_visualization"):
                key = (
                    obj.get("metrosim26_collection"),
                    obj.get("metrosim26_material"),
                    obj.get("feature_kind"),
                    feature,
                )
                groups.setdefault(key, []).append(obj)
        for (group, _material, kind, feature), objects in groups.items():
            label = f"{kind.title()} {feature}"
            if len(objects) == 1:
                if objects[0].users == 1:
                    objects[0].name = label
            else:
                yield from self.join_objects(objects, label, collections[group])

    def _steps(self):
        from metrosim26.blender_ui import apply_setup

        assert bpy is not None
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf8"))
        if manifest["schema_version"] != 1:
            raise ValueError("Unsupported mesh manifest schema")
        cfg = dict(manifest["cfg"], run_directory=manifest["result_directory"])
        self.cfg = cfg
        smooth_reveal = bool(
            cfg.get("view_mode") == "MAP"
            and cfg.get("progressive_build")
            and not bpy.app.background
            and cfg.get("study_radius_km")
        )
        stream_import = cfg.get("progressive_build") and not smooth_reveal
        self.warning = "; ".join(manifest.get("warnings", []))
        grid = SimpleNamespace(
            **manifest["grid"],
            min_z=manifest.get("height_bounds", [0, 0.3])[0],
            max_z=manifest.get("height_bounds", [0, 0.3])[1],
        )
        self.total = len(manifest["packets"])
        scene = self.own(
            bpy.data.scenes,
            bpy.data.scenes.new(
                "METROSIM26_MAP" if cfg.get("view_mode") == "MAP" else "METROSIM26_SIMULATION"
            ),
        )
        self.scene = scene
        for key, value in self.viewport_render.items():
            setattr(scene.cycles, key, value)
        apply_setup(scene.metrosim26_setup, cfg)
        scene["metrosim26_map"] = cfg.get("view_mode") == "MAP"
        scene["metrosim26_simulation"] = cfg.get("view_mode") != "MAP"
        scene["model_note"] = (
            "OpenStreetMap 3D map. Missing heights are estimates; no growth simulated."
            if cfg.get("view_mode") == "MAP"
            else "Experimental demand scenario constrained by real OSM and LODES; not a forecast."
        )
        scene["year_at_frame_1"] = timeline_start(cfg)
        scene["historical_note"] = (
            "Before BASE_YEAR: partial evidence reconstruction; hidden unknown features do not imply absence."
        )
        scene["future_note"] = (
            "Named development and infrastructure projects are fictional scenario outputs."
        )
        scene["project_events_file"] = str(
            Path(cfg["run_directory"]) / "simulation" / "events.json"
        )
        scene["seed"] = int(cfg["seed"])
        scene["configuration_json"] = json.dumps(cfg, sort_keys=True)
        scene["metrosim26_saved_run"] = str(Path(cfg["run_directory"]) / "simulation")
        scene["metrosim26_link_version"] = 1
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1000.0
        scene.unit_settings.length_unit = "KILOMETERS"
        yield
        collections = {}
        for key, name in (
            ("baseline", "Mapped Buildings"),
            ("land", "Water and Parks"),
            ("future", "Scenario Development"),
            ("stage", "Presentation"),
            ("ground", "Ground / Terrain"),
            ("observed_roads", "Observed Roads"),
            ("local_roads", "Scenario Local Roads"),
            ("infrastructure", "Scenario Infrastructure (Fictional)"),
        ):
            collection = self.own(bpy.data.collections, bpy.data.collections.new(name))
            collection["metrosim26_generated_visualization"] = True
            scene.collection.children.link(collection)
            collections[key] = collection
            yield
        materials = {}
        for name, values in manifest["materials"].items():
            materials[name] = self.own(bpy.data.materials, _new_material(name, **values))
            yield
        if stream_import:
            scene.frame_start = 1
            scene.frame_end = int(cfg["end_year"]) - timeline_start(cfg) + 1
            scene.frame_set(1)
            activate_scene_preserving_view(scene)
            _configure_camera_and_lighting(
                cfg,
                grid,
                scene,
                collections["stage"],
                own=self.own,
                source_camera=self.source_camera,
            )
            for area in bpy.context.screen.areas if bpy.context.screen else []:
                if area.type == "VIEW_3D":
                    area.spaces.active.clip_end = max(grid.width, grid.height) * 20
                    if self.source_camera is None:
                        area.spaces.active.region_3d.view_perspective = "CAMERA"
                    area.spaces.active.shading.color_type = "MATERIAL"
                    area.spaces.active.overlay.show_extras = False
            yield
        reused_faces = set()
        source = bpy.data.scenes.get(cfg.get("expand_scene", ""))
        self.preview_ground = []
        if source is not None and cfg.get("expand_inner_radius_km"):
            for old in source.objects:
                if old.type == "MESH" and old.name.startswith("Study_Ground"):
                    collections["ground"].objects.link(old)
                    self.preview_ground.append(old)
        if source is not None and cfg.get("expand_inner_radius_km"):
            for obj in source.objects:
                group = obj.get("metrosim26_collection")
                if (
                    obj.type == "MESH"
                    and obj.get("metrosim26_generated_visualization")
                    and group in collections
                    and not obj.name.startswith("Study_Ground")
                ):
                    collections[group].objects.link(obj)
                    scene["reused_objects"] = scene.get("reused_objects", 0) + 1
                    self.message = (
                        f"Expansion: retaining {scene['reused_objects']} existing objects"
                    )
                    yield
            scene["expansion_mode"] = "outer_ring"
        elif source is not None and cfg.get("view_mode") == "MAP":
            # Compare exact geometry before linking any old object. Boundary
            # meshes and changed OSM features rebuild, with no stale overlap.
            available = set()
            for check_index, packet in enumerate(manifest["packets"], 1):
                self.message = (
                    f"Expansion: checking new mesh {check_index}/{len(manifest['packets'])}"
                )
                with np.load(self.directory / packet["file"], allow_pickle=False) as arrays:
                    for face_index, (first, last) in enumerate(
                        zip(arrays["offsets"][:-1], arrays["offsets"][1:]), 1
                    ):
                        available.add(
                            face_key(
                                arrays["vertices"],
                                arrays["indices"][first:last],
                                packet["collection"],
                                packet["material"],
                            )
                        )
                        if face_index % 256 == 0:
                            yield
                yield
            for object_index, obj in enumerate(source.objects, 1):
                self.message = (
                    f"Expansion: checking existing object {object_index}/{len(source.objects)}"
                )
                if (
                    obj.type != "MESH"
                    or not obj.get("metrosim26_generated_visualization")
                    or obj.name.startswith("Study_Ground")
                    or (not obj.get("feature_id") and not obj.name.startswith("Ocean"))
                ):
                    continue
                if not np.allclose(np.asarray(obj.matrix_world), np.eye(4), atol=1e-8):
                    continue
                group = obj.get("metrosim26_collection")
                if not group:
                    labels = {
                        "Mapped Buildings": "baseline",
                        "Water and Parks": "land",
                        "Observed Roads": "observed_roads",
                    }
                    group = next(
                        (
                            value
                            for key, value in labels.items()
                            if any(c.name.split(".")[0] == key for c in obj.users_collection)
                        ),
                        None,
                    )
                material = obj.get("metrosim26_material")
                if not material and obj.data.materials:
                    material = obj.data.materials[0].name.split(".")[0]
                if group not in collections or not material:
                    continue
                vertices = [tuple(v.co) for v in obj.data.vertices]
                keys = set()
                for face_index, face in enumerate(obj.data.polygons, 1):
                    keys.add(face_key(vertices, face.vertices, group, material))
                    if face_index % 256 == 0:
                        yield
                if keys and keys <= available:
                    collections[group].objects.link(obj)
                    reused_faces.update(keys)
                    scene["reused_objects"] = scene.get("reused_objects", 0) + 1
                yield
            del available
        packet_index = 0
        while True:
            if packet_index >= len(manifest["packets"]):
                if manifest.get("complete", True):
                    break
                self.message = "Starting city ready; simulation continues"
                yield
                manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf8"))
                self.total = len(manifest["packets"])
                for key, record in manifest["materials"].items():
                    if key not in materials:
                        materials[key] = self.own(bpy.data.materials, _new_material(key, **record))
                continue
            packet = dict(manifest["packets"][packet_index])
            if packet["name"].startswith("Study_Ground"):
                packet["collection"] = "ground"
            packet_index += 1
            self.message = f"Building {packet['collection']}: mesh {self.done + 1}/{self.total}"
            with np.load(self.directory / packet["file"], allow_pickle=False) as arrays:
                vertices = arrays["vertices"].tolist()
                indices, offsets = arrays["indices"], arrays["offsets"]
                faces = [indices[a:b].tolist() for a, b in zip(offsets[:-1], offsets[1:])]
            if reused_faces:
                faces = [
                    face
                    for face in faces
                    if face_key(vertices, face, packet["collection"], packet["material"])
                    not in reused_faces
                ]
            if not faces:
                self.done += 1
                yield
                continue
            mesh = self.own(bpy.data.meshes, bpy.data.meshes.new(packet["name"]))
            mesh["metrosim26_generated_visualization"] = True
            mesh.from_pydata(vertices, [], faces)
            mesh.update()
            mesh.materials.append(materials[packet["material"]])
            obj = self.own(bpy.data.objects, bpy.data.objects.new(packet["name"], mesh))
            obj["metrosim26_generated_visualization"] = True
            obj["metrosim26_collection"] = packet["collection"]
            obj["metrosim26_material"] = packet["material"]
            for key, value in packet.get("properties", {}).items():
                obj[key] = value
            collections[packet["collection"]].objects.link(obj)
            if (
                packet["name"].startswith("Study_Ground")
                and self.preview_ground
                and all(math.hypot(v[0], v[1]) < cfg["expand_inner_radius_km"] for v in vertices)
            ):
                obj.hide_set(
                    True, view_layer=scene.view_layers[0]
                )  # Preserve the old visible center until final joining.
            if packet["lifetime"] is not None:
                _set_object_lifetime(obj, *packet["lifetime"])
            if stream_import and packet["lifetime"]:
                scene.frame_set(
                    max(scene.frame_current, packet["lifetime"][0] - timeline_start(cfg) + 1)
                )
            self.done += 1
            yield
        if cfg.get("view_mode") == "MAP":
            yield from self.stitch_surfaces(scene, collections, materials)
            yield from self.stitch_features(scene, collections)
        self.message = "Configuring timeline and render device"
        base_year = timeline_start(cfg)
        scene.frame_start = 1
        scene.frame_end = int(cfg["end_year"]) - base_year + 1
        scene.render.fps = int(cfg.get("render_fps", 2))
        for year in range(base_year, int(cfg["end_year"]) + 1):
            scene.timeline_markers.new(str(year), frame=year - base_year + 1)
        resolution = cfg.get("render_resolution", [1600, 1000])
        scene.render.resolution_x, scene.render.resolution_y = map(int, resolution)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.film_transparent = False
        _configure_cycles(cfg, scene)
        yield
        self.message = "Configuring camera and lighting"
        # Register presentation IDs as they are created, including on failure.
        if not stream_import:
            _configure_camera_and_lighting(
                cfg,
                grid,
                scene,
                collections["stage"],
                own=self.own,
                source_camera=self.source_camera,
            )
        yield
        scene.frame_set(1)
        if not smooth_reveal:
            activate_scene_preserving_view(scene)
        if bpy.context.screen is not None:
            for area in bpy.context.screen.areas:
                if area.type == "VIEW_3D":
                    space = area.spaces.active
                    space.clip_start = 0.001
                    space.clip_end = max(grid.width, grid.height) * 20.0
                    space.shading.color_type = "MATERIAL"
                    space.overlay.show_extras = False
                    if self.source_camera is None and not stream_import:
                        space.region_3d.view_perspective = "CAMERA"
        fit_overview_to_geometry(scene)
        collapse_generated_outliner()
        if smooth_reveal:
            yield from reveal_map(self, scene)
        self.finished = True

    def cleanup_step(self):
        """Remove one partial-build ID per tick; never touch prior scenes."""
        self.steps.close()
        if self.finished or not self.owned:
            return True
        database, item = self.owned.pop()
        database.remove(item, do_unlink=True)
        return not self.owned
