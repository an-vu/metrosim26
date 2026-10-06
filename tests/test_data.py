"""Offline acquisition contracts; network responses are synthetic and mocked."""
import contextlib
import gzip
import http.client
import importlib.util
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from test_simulation import config, data, omaha as simulation, synthetic_baseline
import prefetch_omaha_data as prefetch


class Response(io.BytesIO):
    def geturl(self):
        return 'https://actual.example/dataset'


def payload(spec):
    if spec.kind == 'osm':
        return b'{"elements": [], "osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z"}}'
    block = '310010001001001' if spec.state == 'ne' else '190010001001001'
    if spec.kind == 'xwalk':
        content = f'tabblk2020,blklatdd,blklondd\n{block},41.25,-95.95\n'
    else:
        geocode = 'w_geocode' if spec.kind == 'wac' else 'h_geocode'
        content = f'{geocode},C000,CNS04,createdate\n{block},10,3,20260101\n'
    return gzip.compress(content.encode())


def populate(cfg):
    for spec in data.expected_inputs(cfg):
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        spec.path.write_bytes(payload(spec))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config.make_config(preview=True)
        self.cfg.update(cache_dir=self.temp.name, download_attempts=1, data_mode='OFFLINE')
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        data._VALIDATED_INPUTS.clear()

    def test_full_and_preview_inventory_and_transport_independence(self):
        full = config.make_config()
        self.assertFalse(full['preview_mode'])
        self.assertEqual(full['end_year'], 2076)
        self.assertEqual(full['cpu_workers'], 16)
        self.assertEqual(full['download_workers'], 1)
        self.assertEqual(len(data.osm_inputs(full)), 64)
        self.assertEqual(len(data.expected_inputs(full)), 70)
        original = [s.path for s in data.expected_inputs(self.cfg)]
        self.assertEqual(len(original), 10)
        identity = config.run_directory(self.cfg)
        self.cfg.update(overpass_url='https://other.example/', data_mode='ONLINE',
                        download_attempts=7, download_backoff_initial=90,
                        overpass_fallback_urls=['https://fallback.example/'])
        self.assertEqual([s.path for s in data.expected_inputs(self.cfg)], original)
        self.assertEqual(config.run_directory(self.cfg), identity)

    def test_valid_legacy_cache_skips_network_and_records_unknown_source(self):
        populate(self.cfg)
        with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Unexpected HTTP')):
            self.assertTrue(data.prefetch_data(self.cfg))
            self.assertTrue(data.prefetch_data(self.cfg))
        manifest = json.loads(data.cache_manifest_path(self.cfg).read_text())
        self.assertTrue(manifest['complete'])
        self.assertTrue(all(item['validation']['source_url'] is None for item in manifest['files']))

    def test_missing_cache_fails_before_any_large_file_validation_or_http(self):
        specs = data.expected_inputs(self.cfg)
        with patch.object(data, 'validate_input', side_effect=AssertionError('Not fail-fast')), \
             patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Offline HTTP')), \
             patch.object(data.time, 'sleep', side_effect=AssertionError('Offline retry')):
            with self.assertRaises(FileNotFoundError) as error:
                data.require_input_cache(self.cfg)
        with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Offline HTTP')):
            with self.assertRaises(FileNotFoundError):
                data._download_cached(specs[0], self.cfg)
        # Direct loader also checks for missing files without opening a socket.
        message = str(error.exception)
        for spec in specs:
            self.assertIn(spec.label, message)
        self.assertIn('prefetch_omaha_data.py --preview', message)

    def test_full_cache_succeeds_offline_and_loaders_share_inventory(self):
        self.cfg = config.make_config(preview=False)
        self.cfg['cache_dir'] = self.temp.name
        populate(self.cfg)
        with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Offline HTTP')):
            data.require_input_cache(self.cfg)
            grid = simulation.Grid(tuple(self.cfg['bounds']), self.cfg['cell_km'])
            elements, osm = data.load_osm(self.cfg, grid)
            arrays, lodes = data.load_lodes(self.cfg, grid)
        self.assertEqual(elements, [])
        self.assertEqual(len(osm['tiles']), 64)
        self.assertEqual(len(lodes['files']), 6)
        self.assertEqual(float(arrays['jobs'].sum()), 20.)
        self.assertEqual(float(arrays['industrial_jobs'].sum()), 6.)

    def test_incomplete_osm_and_bad_gzip_are_rejected(self):
        spec = data.osm_inputs(self.cfg)[0]
        spec.path.parent.mkdir(parents=True)
        for body in (b'', b'{', b'{}', b'[]', b'{"elements":{}}',
                     b'{"elements":[],"remark":"runtime error: timeout"}',
                     b'{"elements":[{"type":"way"}]}'):
            spec.path.write_bytes(body)
            with self.assertRaises((ValueError, EOFError)):
                data.validate_input(spec, force=True)
        lodes = data.lodes_input(self.cfg, 'ne', 'wac')
        lodes.path.parent.mkdir(parents=True)
        good = payload(lodes)
        for body in (good[:-5], gzip.compress(b'wrong,headers\n1,2\n'),
                     gzip.compress(b'w_geocode,C000\n'),
                     gzip.compress(b'w_geocode,C000\n1,not-a-number\n'),
                     good[:-8] + b'\0\0\0\0' + good[-4:]):
            lodes.path.write_bytes(body)
            with self.assertRaises((ValueError, OSError, EOFError)):
                data.validate_input(lodes, force=True)

    def test_interrupted_stream_is_atomic_and_rerun_fetches_only_failed_file(self):
        specs = data.expected_inputs(self.cfg)
        by_post = {s.query: s for s in specs if s.query}
        by_url = {s.url: s for s in specs if not s.query}
        failed = specs[2]
        calls = []
        class BrokenResponse(Response):
            def read(self, size=-1):
                if self.tell():
                    raise ConnectionResetError('interrupted stream')
                return super().read(12)
        def network(request, **kwargs):
            from urllib.parse import parse_qs
            spec = by_post[parse_qs(request.data.decode())['data'][0]] if request.data else by_url[request.full_url]
            calls.append(spec.path)
            return BrokenResponse(payload(spec)) if spec == failed else Response(payload(spec))
        with patch.object(data.urllib.request, 'urlopen', side_effect=network):
            self.assertFalse(data.prefetch_data(self.cfg))
        self.assertFalse(failed.path.exists())
        self.assertEqual(len(calls), 10)
        self.assertEqual(list(Path(self.temp.name).rglob('*.part')), [])
        calls.clear()
        failed_body = payload(failed)
        failed = None
        with patch.object(data.urllib.request, 'urlopen', return_value=Response(failed_body)) as request:
            self.assertTrue(data.prefetch_data(self.cfg))
            self.assertEqual(request.call_count, 1)
        manifest = json.loads(data.cache_manifest_path(self.cfg).read_text())
        self.assertTrue(manifest['complete'])
        self.assertTrue(all(item['validation']['source_url'] == 'https://actual.example/dataset'
                            for item in manifest['files']))

    def test_retry_after_and_transient_failures_and_fallback_provenance(self):
        spec = data.osm_inputs(self.cfg)[0]
        cfg = dict(self.cfg, data_mode='ONLINE', download_attempts=2,
                   overpass_fallback_urls=['https://fallback.example/'])
        failures = [urllib.error.HTTPError(spec.url, code, 'busy', {}, None)
                    for code in (502, 503, 504)]
        failures += [TimeoutError('timeout'), ConnectionResetError('reset'),
                     urllib.error.URLError(socket.gaierror('DNS failure')),
                     http.client.IncompleteRead(b'partial', 100)]
        failures += [urllib.error.HTTPError(spec.url, 429, 'busy', {'Retry-After': '123'}, None)]
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                spec.path.unlink(missing_ok=True)
                with patch.object(data.urllib.request, 'urlopen', side_effect=[failure, Response(payload(spec))]) as request, \
                     patch.object(data.time, 'sleep') as sleep:
                    record = data._download_cached(spec, cfg)
                delay = 123 if getattr(failure, 'code', None) == 429 else 30
                sleep.assert_called_once_with(delay)
                self.assertEqual(request.call_args.args[0].full_url, 'https://fallback.example/')
                self.assertEqual(record['requested_url'], 'https://fallback.example/')
                self.assertEqual(record['source_url'], 'https://actual.example/dataset')
        error = urllib.error.HTTPError(spec.url, 429, 'busy',
                                       {'Retry-After': 'Thu, 01 Jan 1970 00:02:00 GMT'}, None)
        self.addCleanup(error.close)
        with patch.object(data.time, 'time', return_value=60):
            self.assertEqual(data._retry_delay(error, 0, cfg), 60)

    def test_bad_response_never_replaces_existing_file(self):
        spec = data.osm_inputs(self.cfg)[0]
        spec.path.parent.mkdir(parents=True)
        spec.path.write_bytes(b'old invalid cache')
        cfg = dict(self.cfg, data_mode='ONLINE')
        with patch.object(data.urllib.request, 'urlopen', return_value=Response(b'{"remark":"timeout"}')):
            with self.assertRaises(RuntimeError):
                data._download_cached(spec, cfg)
        self.assertEqual(spec.path.read_bytes(), b'old invalid cache')
        self.assertEqual(list(spec.path.parent.glob('*.part')), [])

    def test_receipt_does_not_hide_changed_bytes_or_corruption(self):
        populate(self.cfg)
        self.assertTrue(data.prefetch_data(self.cfg))
        spec = data.osm_inputs(self.cfg)[0]
        spec.path.write_bytes(b'{"elements":[],"remark":"incomplete"}')
        data._VALIDATED_INPUTS.clear()  # Simulate a separate Blender process.
        with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Offline HTTP')):
            with self.assertRaisesRegex(FileNotFoundError, 'OSM tile 1'):
                data.require_input_cache(self.cfg)

    def test_check_only_writes_nothing_and_uses_no_network(self):
        populate(self.cfg)
        before = {p.relative_to(self.temp.name): p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Check HTTP')):
            self.assertEqual(prefetch.main(['--preview', '--check', '--cache-dir', self.temp.name]), 0)
        after = {p.relative_to(self.temp.name): p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_keyboard_interrupt_keeps_completed_inputs_and_removes_partial(self):
        spec = data.osm_inputs(self.cfg)[0]
        class Interrupted(Response):
            def read(self, size=-1):
                raise KeyboardInterrupt()
        with patch.object(data.urllib.request, 'urlopen', return_value=Interrupted(b'')):
            self.assertEqual(prefetch.main(['--preview', '--cache-dir', self.temp.name]), 130)
        self.assertFalse(spec.path.exists())
        self.assertEqual(list(Path(self.temp.name).rglob('*.part')), [])
        self.assertFalse(json.loads(data.cache_manifest_path(self.cfg).read_text())['complete'])


class EntryAndReplayTests(unittest.TestCase):
    def test_prefetch_imports_without_blender_from_another_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        script = '''import importlib.abc, runpy, sys
class NoBlender(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('bpy', 'mathutils', 'omaha_blender'):
            raise AssertionError('Blender dependency: ' + fullname)
sys.meta_path.insert(0, NoBlender())
sys.argv = [sys.argv[1], '--help']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, '-c', script, str(root/'prefetch_omaha_data.py')],
                                    cwd=tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--preview', result.stdout)

    def test_saved_text_path_beats_unusable_file_path(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('entry', root/'blender.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ('omaha_config.py', 'omaha_data.py', 'omaha_simulation.py',
                         'omaha_workers.py', 'omaha_blender.py'):
                (folder/name).touch()
            text = SimpleNamespace(filepath='//blender.py')
            bpy = SimpleNamespace(context=SimpleNamespace(space_data=SimpleNamespace(text=text)),
                                  path=SimpleNamespace(abspath=lambda path: str(folder/'blender.py')))
            with patch.dict(sys.modules, bpy=bpy), patch.object(entry, '__file__', '/missing/blender.py'):
                self.assertEqual(entry.project_directory(), folder.resolve())
                text.filepath = ''
                with self.assertRaisesRegex(RuntimeError, 'saved blender.py'):
                    entry.project_directory()

    def test_missing_inputs_do_not_invalidate_previous_completed_run(self):
        cfg = config.make_config(preview=True)
        cfg.update(bounds=[41.20, -96.05, 41.225, -96.015], end_year=2026,
                   profile_performance=False, enable_parallel_compute=False)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg.update(output_dir=tmp, cache_dir=str(Path(tmp)/'absent-cache'))
            with patch.object(simulation, 'build_baseline', side_effect=lambda c, g: synthetic_baseline(g)):
                _, _, result = simulation.prepare_simulation(cfg)
            metadata = Path(result['run_directory'])/'simulation'/'metadata.json'
            saved = metadata.read_bytes()
            with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Offline HTTP')):
                with self.assertRaisesRegex(FileNotFoundError, 'input cache is incomplete'):
                    simulation.prepare_simulation(cfg)
            self.assertEqual(metadata.read_bytes(), saved)
            cfg['run_mode'] = 'REPLAY'
            _, _, loaded = simulation.prepare_simulation(cfg)
            self.assertEqual(loaded['summary'], result['summary'])

    def test_legacy_run_replays_without_raw_cache_after_transport_identity_change(self):
        cfg = config.make_config(preview=True)
        cfg.update(bounds=[41.20, -96.05, 41.225, -96.015], end_year=2026,
                   profile_performance=False, enable_parallel_compute=False)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg.update(output_dir=tmp, cache_dir=str(Path(tmp)/'absent-cache'))
            with patch.object(simulation, 'build_baseline', side_effect=lambda c, g: synthetic_baseline(g)):
                _, _, result = simulation.prepare_simulation(cfg)
            directory = Path(result['run_directory'])
            historical = directory.with_name('preview_872197_legacyhash')
            directory.rename(historical)
            metadata_path = historical/'simulation'/'metadata.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['simulation_config']['overpass_url'] = 'https://old-endpoint.example/'
            metadata_path.write_text(json.dumps(metadata))
            cfg.update(run_mode='REPLAY', overpass_url='https://new-endpoint.example/')
            with patch.object(data.urllib.request, 'urlopen', side_effect=AssertionError('Replay HTTP')):
                _, _, loaded = simulation.prepare_simulation(cfg)
            self.assertEqual(Path(loaded['run_directory']), historical)
            self.assertEqual(result['summary'], loaded['summary'])
            shutil.copytree(historical, historical.with_name('preview_872197_secondlegacy'))
            with self.assertRaisesRegex(ValueError, 'Multiple matching legacy'):
                simulation.prepare_simulation(cfg)


if __name__ == '__main__':
    unittest.main()
