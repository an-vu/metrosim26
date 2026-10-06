"""Performance invariants, including real subprocesses using Windows-style spawn."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from test_simulation import omaha as m, config, workers as geometry, synthetic_baseline, rectangle


def performance_case():
    cfg = config.make_config(preview=True)
    cfg.update(initial_households=10000., initial_jobs=4000., initial_housing_vacancy=0.,
               initial_job_vacancy=0., annual_household_growth_rate=.08,
               annual_job_growth_rate=.06, end_year=2035, profile_performance=False)
    grid = m.Grid((41.20, -96.05, 41.225, -96.015), .35)
    cfg['bounds'] = list(grid.bounds)
    baseline = synthetic_baseline(grid)
    for gy in range(grid.ny):
        for gx in range(grid.nx):
            x, y = grid.center(gx, gy)
            for i in range(16):
                px, py = x - .15 + (i % 4) * .08, y - .15 + (i // 4) * .08
                baseline['buildings'].append({
                    'id': f'{gx}/{gy}/{i}', 'polygons': [{'outer': rectangle(px, py, px+.015, py+.02), 'holes': []}],
                    'height_m': 6., 'arch': m.SUBURBAN})
    baseline['spatial_index'] = geometry.prepare_spatial_index(baseline, grid)
    return cfg, grid, baseline


def checkpoint_hashes(directory):
    hashes = {}
    for path in sorted(Path(directory).iterdir()):
        data = path.read_bytes()
        if path.suffix == '.npz':
            with np.load(path, allow_pickle=False) as archive:
                data = b''.join(key.encode() + archive[key].dtype.str.encode()
                                + str(archive[key].shape).encode() + archive[key].tobytes()
                                for key in sorted(archive.files))
        if path.suffix == '.json':
            data = json.dumps(json.loads(data), sort_keys=True, separators=(',', ':')).encode()
        hashes[path.name] = hashlib.sha256(data).hexdigest()
    return hashes


class PerformanceTests(unittest.TestCase):
    def test_preoptimization_golden_checkpoints(self):
        cfg, grid, baseline = performance_case()
        cfg.update(enable_parallel_compute=False, cpu_workers=1)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            m.run_simulation(cfg, grid, baseline, tmp)
            actual = checkpoint_hashes(tmp)
        expected = json.loads((Path(__file__).parent / 'fixtures' / 'preoptimization_hashes.json').read_text())
        self.assertEqual(actual, expected)

    def test_spawn_matches_serial_and_ignores_completion_order(self):
        cfg, grid, baseline = performance_case()
        cfg.update(parallel_min_tasks=1, parallel_batch_size=16, parallel_min_batch_seconds=0)
        baseline['urban'][:] = True
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            cfg.update(cpu_workers=1)
            m.run_simulation(cfg, grid, baseline, root/'serial')
            reference = checkpoint_hashes(root/'serial')
            for workers in (2, 16):
                cfg.update(cpu_workers=workers)
                # Exercise a 16-process request even on the smaller development host.
                # Reverse submission order: authoritative commits must remain unchanged.
                prefetch = m._PlanningCache.prefetch
                def reverse_tasks(cache, tasks):
                    return prefetch(cache, list(reversed(tasks)))
                with patch.object(m.os, 'cpu_count', return_value=20), \
                     patch.object(m._PlanningCache, 'prefetch', reverse_tasks):
                    with m._PlanningCache(cfg, grid, baseline) as cache:
                        m._run_simulation(cfg, grid, baseline, root/str(workers), cache)
                        self.assertGreater(cache.counts['parallel_plans'], 0, 'Test silently fell back to serial')
                self.assertEqual(checkpoint_hashes(root/str(workers)), reference)
                for path in (root/'serial').iterdir():
                    self.assertEqual(path.read_bytes(), (root/str(workers)/path.name).read_bytes())

    def test_mapped_geometry_matches_local_for_all_archetypes(self):
        cfg, grid, baseline = performance_case()
        x, y = grid.center(4, 4)
        baseline['land'] = [{'id': 'water', 'kind': 'water', 'polygons': [
            {'outer': rectangle(x-.16, y-.16, x+.16, y+.16),
             'holes': [rectangle(x-.12, y-.12, x+.12, y+.12)]}]}]
        baseline['roads'] = [{'id': 'road', 'points': [(x-.1, y), (x+.1, y)],
                              'width_km': .012, 'class': 'primary'}]
        geometry.prepare_spatial_index(baseline, grid)
        with tempfile.TemporaryDirectory() as tmp:
            m.omaha_workers.write_worker_state(tmp, cfg, grid, baseline)
            wc, wg, wb = m.omaha_workers.load_worker_state(tmp)
            for arch in range(1, 9):
                expected = m.make_site_plan(cfg, grid, baseline, 4, 4, arch, 0, 2027)
                actual = m.omaha_workers.make_site_plan(wc, wg, wb, 4, 4, arch, 0, 2027)
                self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(actual, sort_keys=True))
            # Release all maps before cleanup on Windows.
            del wb

    def test_performance_settings_do_not_change_scenario_identity(self):
        cfg = config.make_config(preview=True)
        original = m.simulation_config(cfg)
        path = m.run_directory(cfg)
        cfg.update(cpu_workers=1, enable_parallel_compute=False, parallel_batch_size=3,
                   profile_performance=False, worker_python='different-python', validate_parallel_results=True)
        self.assertEqual(m.simulation_config(cfg), original)
        self.assertEqual(m.run_directory(cfg), path)
        for key in config.PERFORMANCE_KEYS:
            cfg.pop(key, None)
        self.assertEqual(m.simulation_config(cfg), original)

    def test_unavailable_interpreter_falls_back_but_calculation_errors_raise(self):
        cfg, grid, baseline = performance_case()
        cfg.update(parallel_min_tasks=1, parallel_min_batch_seconds=0, cpu_workers=2, worker_python='/missing/python')
        with contextlib.redirect_stdout(io.StringIO()) as log:
            with m._PlanningCache(cfg, grid, baseline) as cache:
                cache.prefetch([(4, 4, m.SUBURBAN, 0, 2027)])
                self.assertFalse(cache.enabled)
                self.assertGreater(len(cache.template(4, 4, m.SUBURBAN, 0, 2027)['buildings']), 0)
        self.assertIn('SERIAL FALLBACK', log.getvalue())
        cfg['worker_python'] = sys.executable
        with m._PlanningCache(cfg, grid, baseline) as cache:
            with self.assertRaisesRegex(RuntimeError, 'Worker calculation failed'):
                cache.prefetch([(4, 4, 999, 0, 2027)])

    def test_worker_executable_preserves_venv_path(self):
        cfg = config.make_config(preview=True)
        cfg['worker_python'] = sys.executable
        self.assertEqual(m._worker_python(cfg), str(Path(sys.executable).absolute()))


if __name__ == '__main__':
    unittest.main()
