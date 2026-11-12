# -*- coding: utf-8 -*-
"""导出真实 main 的离线契约回归；全部产物仅写入 review 临时目录。"""
import contextlib
import copy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
E = importlib.import_module('bench-export')
REVIEW_TEMP = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'Temp' / 'raceway-review' / 'export-identity'
if os.name != 'nt':
    REVIEW_TEMP = Path(tempfile.gettempdir()) / 'raceway-review' / 'export-identity'


def operation(task, error=0):
    return {'task': task, 'operation': task, 'error_rate': error,
            'throughput': {'median': 100, 'unit': 'docs/s' if E.is_write_task(task) else 'ops/s'},
            'latency': {'50_0': 10, '90_0': 20, '99_0': 25, 'unit': 'ms'},
            'service_time': {'50_0': 10, '90_0': 20, '99_0': 25, 'unit': 'ms'}}


def race(rid, phase='wq', variant='hv1024x', car='jdk25', build='3015'):
    tasks = ([operation('index-append')] if phase in ('w', 'wq') else [])
    if phase in ('q', 'wq'):
        tasks.append(operation('knn-search-10-100'))
    return {'race-id': rid, 'race-timestamp': '20261108T010000Z',
            'track': variant, 'challenge': {'w': 'write-only', 'q': 'query-only', 'wq': 'full'}[phase],
            'user-tags': {'campaign': 'review', 'car': car, 'round': '1', 'engine': 'easysearch',
                          'engine_ver': '2.5.0-' + build, 'nnodes': '2', 'heap': '16g'},
            'cluster': {'distribution-version': '2.5.0', 'distribution-flavor': 'oss'},
            'results': {'op_metrics': tasks, 'store_size': 1000000000}}


def sidecar(r, fid, phase='wq', build='3015', index_type='hnsw'):
    return {'run': {'fid': fid, 'phase': phase, 'race_id': r['race-id']},
            'engine': {'pkg': 'easysearch-2.5.0-%s-linux-x64.tar.gz' % build,
                       'version_measured_nodes': ['2.5.0']},
            'jdk': {'per_node': [{'vendor_detected': 'OpenJDK', 'major_measured': '25.0.4'}],
                    'measured_nodes': ['25.0.4']},
            'run_params': {'knn_params': {'index_type': index_type}},
            'env': {'env_id': 'review-env', 'vm_nodes': [{'hostname': 'node-alpha'}, {'hostname': 'node-beta'}]}}


class ExportFixture(unittest.TestCase):
    def setUp(self):
        REVIEW_TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='contract-', dir=REVIEW_TEMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = self.base / 'store'
        self.campaigns = self.base / 'campaigns'
        self.camp = self.campaigns / 'review'
        self.store.mkdir()
        self.camp.mkdir(parents=True)
        self.status = {}
        self.counter = 0

    def dump(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def add(self, r, fid, phase='wq', s=None, verdict='OK', samples=3, nodes=('node-alpha', 'node-beta'), status='VALID'):
        self.dump(self.store / r['race-id'] / 'race.json', r)
        self.dump(self.camp / 'results' / (fid + '-' + phase + '-race.json'), r)
        self.dump(self.camp / 'results' / (fid + '-' + phase + '-sidecar.json'), s or sidecar(r, fid, phase))
        if status is not None:
            self.status[fid] = status
        (self.camp / 'RACES.md').write_text('\n'.join('| %s | %s | - | - |' % pair for pair in self.status.items()), encoding='utf-8')
        for node in nodes:
            p = self.camp / 'monitors' / (fid + '-' + phase + '-' + node + '-summary.txt')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('samples=%s steal_avg=0 steal_max=0 foreign_max=0 swapin=0\nENV-VERDICT: %s\n' % (samples, verdict), encoding='utf-8')

    def export(self):
        self.counter += 1
        out = self.base / ('out-%d' % self.counter)
        args = ['bench-export.py', '--races', str(self.store), '--campaigns', str(self.campaigns), '--out', str(out)]
        with mock.patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = E.main()
        def docs(name):
            p = out / ('benchmark-result-' + name + '.jsonl')
            return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []
        return rc, docs('race'), docs('metric'), out


class TestExportContract(ExportFixture):
    def test_full_payload_units_variants_system_and_preflight(self):
        r = race('full', variant='hv1024x-int8')
        fid = 'jdk25-hv1024x-int8-r1'
        self.add(r, fid, s=sidecar(r, fid, index_type='int8_hnsw'))
        rc, runs, metrics, out = self.export()
        self.assertEqual(rc, 0)
        row = runs[0]
        self.assertEqual(row['race_id'], 'review|jdk25-hv1024x-int8-r1|a1')
        self.assertEqual(row['round'], 1)
        self.assertEqual(row['track'], 'hv1024x')
        self.assertEqual(row['track_variant'], 'hv1024x-int8')
        self.assertEqual(row['attempt'], 1)
        self.assertEqual(row['comparison_key'], row['series_key'] + '|hv1024x-int8')
        self.assertTrue(metrics)
        self.assertTrue(all(m['track_variant'] == row['track_variant'] and m['comparison_key'] == row['comparison_key'] and m['knn_index_type'] == 'int8_hnsw' for m in metrics))
        self.assertEqual(next(m['value'] for m in metrics if m['family'] == 'latency' and m['stat'] == 'p99'), .025)
        self.assertEqual(next(m['value'] for m in metrics if m['family'] == 'disk'), 1)
        self.assertEqual(row['@timestamp'], '2026-11-08T01:00:00Z')
        self.assertEqual(row['run_start'], row['@timestamp'])
        A = importlib.import_module('bench-apply')
        templates, snapshots = A.preflight(str(out), list(A.IDX))
        self.assertEqual(len(snapshots['benchmark-result-race']), 1)
        self.assertEqual(templates['benchmark-result-metric']['template']['mappings']['dynamic'], 'strict')
        (out / 'benchmark-result-metric.jsonl').write_text('', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'manifest.bench_metric'):
            A.preflight(str(out), list(A.IDX))

    def test_geo_runner_id_not_display_name(self):
        r = race('geo', variant='track')
        fid = 'jdk25-geo-r1'
        self.add(r, fid)
        rc, runs, _, _ = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual((runs[0]['track'], runs[0]['track_variant']), ('geonames', 'geo'))

    def test_opensearch_package_overrides_oss(self):
        r = race('os', variant='hv1024x-os', car='os332')
        fid = 'os332-hv1024x-os-r1'
        s = sidecar(r, fid)
        s['engine'] = {'pkg': 'opensearch-3.3.2-linux-x64.tar.gz', 'version_measured_nodes': ['3.3.2']}
        self.add(r, fid, s=s)
        rc, runs, _, _ = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual(runs[0]['engine'], 'opensearch')
        self.assertIsNone(runs[0]['engine_build'])
        self.assertTrue(runs[0]['series_key'].startswith('os@3.3.2@—@'))

    def test_unjudged_retry_does_not_borrow_valid(self):
        self.status['jdk25-hv1024x-r1'] = 'VALID'
        self.add(race('retry'), 'jdk25-hv1024x-r1-try2', status=None)
        rc, runs, metrics, out = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual((runs, metrics), ([], []))
        self.assertEqual(json.loads((out / 'manifest.json').read_text(encoding='utf-8'))['dropped']['by_reason'], {'not_valid': 1})

    def test_partial_then_complete_uses_stable_key(self):
        fid = 'jdk25-hv1024x-r1'
        self.add(race('query', 'q'), fid, phase='q')
        self.assertEqual(self.export()[1], [])
        self.add(race('write', 'w'), fid, phase='w')
        rc, runs, metrics, out = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual(runs[0]['race_id'], E.measurement_id('review', fid, 1))
        self.assertEqual(runs[0]['challenge'], ['query-only', 'write-only'])
        rc2, _, _, out2 = self.export()
        self.assertEqual(rc2, 0)
        for name in ('race', 'metric', 'env'):
            self.assertEqual((out / ('benchmark-result-' + name + '.jsonl')).read_bytes(), (out2 / ('benchmark-result-' + name + '.jsonl')).read_bytes())

    def test_different_builds_cannot_merge(self):
        fid = 'jdk25-hv1024x-r1'
        w, q = race('w', 'w'), race('q', 'q')
        self.add(w, fid, 'w', sidecar(w, fid, 'w', build='3009'))
        self.add(q, fid, 'q', sidecar(q, fid, 'q', build='3015'))
        rc, runs, metrics, _ = self.export()
        self.assertNotEqual(rc, 0)
        self.assertEqual((runs, metrics), ([], []))

    def test_index_type_fid_inference_and_jinja_rejection(self):
        self.assertEqual(E.index_type_of({}, 'hv1024x-int8-codec-zstd-sr'), 'int8_hnsw')
        with self.assertRaises(ValueError):
            E.index_type_of({'run_params': {'knn_params': {'index_type': '{{ vector_index_type }}'}}}, 'hv1024x-int8')

    def test_missing_monitor_nodes_or_bad_verdict_not_exported(self):
        for case, kwargs in [('missing', {'nodes': ('node-alpha',)}), ('warn', {'verdict': 'WARN'}),
                             ('contaminated', {'verdict': 'CONTAMINATED'}), ('no-data', {'samples': 0}), ('absent', {'nodes': ()})]:
            with self.subTest(case=case):
                r = race('monitor-' + case)
                fid = 'jdk25-hv1024x-r1'
                # 清除上个子样例的状态与摘要，保留各自隔离的 race。
                import shutil
                shutil.rmtree(self.store); self.store.mkdir()
                shutil.rmtree(self.camp); self.camp.mkdir()
                self.add(r, fid, **kwargs)
                rc, runs, metrics, _ = self.export()
                self.assertEqual(rc, 0)
                self.assertEqual((runs, metrics), ([], []))

    def test_monitor_missing_or_invalid_noise_rejected(self):
        fid = 'jdk25-hv1024x-r1'
        self.add(race('monitor-noise'), fid)
        path = self.camp / 'monitors' / (fid + '-wq-node-alpha-summary.txt')
        for omitted in ('steal_avg', 'steal_max', 'foreign_max', 'swapin'):
            with self.subTest(omitted=omitted):
                values = dict(samples=3, steal_avg=0, steal_max=0, foreign_max=0, swapin=0)
                del values[omitted]
                path.write_text(' '.join('%s=%s' % pair for pair in values.items()) + '\nENV-VERDICT: OK\n', encoding='utf-8')
                self.assertEqual(self.export()[1], [])
        for value in (-1, float('nan'), float('inf')):
            path.write_text('samples=3 steal_avg=0 steal_max=%s foreign_max=0 swapin=0\nENV-VERDICT: OK\n' % value, encoding='utf-8')
            self.assertEqual(self.export()[1], [])
        for tail in ('ENV-VERDICT: OK\nENV-VERDICT: FAIL\n', 'ENV-VERDICT: OK\nENV-VERDICT: OK\n'):
            path.write_text('samples=3 steal_avg=0 steal_max=0 foreign_max=0 swapin=0\n' + tail, encoding='utf-8')
            self.assertEqual(self.export()[1], [])
        path.write_text('samples=3 steal_avg=0 steal_max=0 foreign_max=0 swapin=1\nENV-VERDICT: OK\n', encoding='utf-8')
        self.assertEqual(self.export()[1], [])

    def test_write_only_requires_explicit_protocol(self):
        fid = 'jdk25-hv1024x-r1'
        r = race('write', 'w')
        self.add(r, fid, 'w')
        self.assertEqual(self.export()[1], [])
        r['user-tags']['measurement_protocol'] = 'write-only'
        self.add(r, fid, 'w')
        self.assertEqual(len(self.export()[1]), 1)

    def test_conflicting_statuses_cannot_override_failure_with_valid(self):
        fid = 'jdk25-hv1024x-r1'
        self.add(race('status-conflict'), fid)
        p = self.camp / 'RACES.md'
        for states in [('RACE-FAIL', 'VALID'), ('VALID', 'RACE-FAIL')]:
            with self.subTest(states=states):
                p.write_text('\n'.join('| %s | %s | - | - |' % (fid, status) for status in states), encoding='utf-8')
                rc, runs, metrics, _ = self.export()
                self.assertNotEqual(rc, 0)
                self.assertEqual((runs, metrics), ([], []))
        p.write_text(('| %s | VALID | - | - |\n' % fid) * 2, encoding='utf-8')
        self.assertEqual(len(self.export()[1]), 1)

    def test_required_task_throughput_median_must_be_finite_positive(self):
        fid = 'jdk25-geo-r1'
        for task in ('index-append', 'knn-search-10-100', 'final-stats'):
            for median in (None, 0, -1, True, '100'):
                with self.subTest(task=task, median=median):
                    r = race('null-tp', variant='track')
                    if task == 'final-stats':
                        r['results']['op_metrics'][1] = operation(task)
                    target = next(o for o in r['results']['op_metrics'] if o['task'] == task)
                    target['throughput'] = {'min': None, 'mean': None, 'median': median, 'max': None, 'unit': 'docs/s'}
                    self.add(r, fid)
                    rc, runs, metrics, out = self.export()
                    self.assertEqual(rc, 0)
                    self.assertEqual((runs, metrics), ([], []))
                    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['dropped']['by_reason'], {'invalid_required_throughput': 1})

    def test_nan_inf_invalid_error_rates_fail_closed(self):
        for value in [None, -1, 1.1, '0', True, float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                E.task_quality({'error_rate': value}, 'upsert-hot')
        fid = 'jdk25-hv1024x-r1'
        r = race('nan')
        r['results']['op_metrics'][0]['error_rate'] = float('nan')
        self.add(r, fid)
        self.assertNotEqual(self.export()[0], 0)

    def test_all_tasks_require_zero_error_and_keep_evidence(self):
        self.assertEqual(E.task_quality({'error_rate': 0}, 'index-append'), 'ok')
        self.assertEqual(E.task_quality({'error_rate': .001}, 'index-append'), 'degraded')
        self.assertEqual(E.task_quality({'error_rate': .001}, 'upsert-hot'), 'degraded')
        r = race('errors'); fid = 'jdk25-hv1024x-r1'
        r['results']['op_metrics'][0]['error_rate'] = .001
        self.add(r, fid)
        rc, runs, metrics, _ = self.export()
        self.assertEqual(rc, 0)
        self.assertFalse(runs[0]['trusted'])
        error = next(m for m in metrics if m['family'] == 'error_rate' and m['task'] == 'index-append')
        self.assertEqual(error['value'], .001)
        self.assertFalse(error['trusted'])

    def test_upsert_full_lifecycle_final_stats(self):
        r = race('upsert', variant='geonames-upsert')
        r['results']['op_metrics'] = [operation('upsert-update-hot'), operation('index-update-native'),
                                      dict(operation('final-stats'), **{'operation-type': 'index-stats'})]
        fid = 'jdk25-geonames-upsert-r1'
        self.add(r, fid)
        rc, runs, _, _ = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual(runs[0]['track_variant'], 'geonames-upsert')

    def test_upsert_write_classification(self):
        self.assertTrue(E.is_write_task('upsert-hot'))
        self.assertTrue(E.is_write_task('index-update-native'))
        self.assertFalse(E.is_write_task('index-stats'))
        self.assertTrue(E.is_query_task({'task': 'final-stats', 'operation-type': 'index-stats'}))

    def test_output_directory_is_not_overwritten(self):
        r = race('ok'); fid = 'jdk25-hv1024x-r1'
        self.add(r, fid)
        rc, _, _, out = self.export()
        self.assertEqual(rc, 0)
        before = (out / 'benchmark-result-race.jsonl').read_bytes()
        args = ['bench-export.py', '--races', str(self.store), '--campaigns', str(self.campaigns), '--out', str(out)]
        with mock.patch.object(sys, 'argv', args), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(E.main(), 0)
        self.assertEqual(before, (out / 'benchmark-result-race.jsonl').read_bytes())


if __name__ == '__main__':
    unittest.main()
