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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
S = importlib.import_module('result_selection')
C = importlib.import_module('car-compare')
K = importlib.import_module('codec-compare')
A = importlib.import_module('audit_track_settings')


class ComparisonSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.camp = Path(self.tmp.name) / 'demo'
        (self.camp / 'results').mkdir(parents=True)
        (self.camp / 'monitors').mkdir()
        self.statuses = []

    def dump(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def measurement(self, car='a', track='hv1024x-int8', rnd=1, attempt=1, status='VALID', build='3015'):
        fid = '%s-%s-r%d%s' % (car, track, rnd, '-try%d' % attempt if attempt > 1 else '')
        rid = fid + '-uuid'
        race = {'race-id': rid, 'challenge': 'full',
                'user-tags': {'campaign': 'demo', 'car': car, 'track': track, 'round': rnd, 'nnodes': 2},
                'results': {'op_metrics': [
                    {'task': 'index-append', 'duration': 1000, 'error_rate': 0,
                     'throughput': {'unit': 'docs/s', 'median': 100}},
                    {'task': 'knn-search-10-100', 'error_rate': 0,
                     'throughput': {'unit': 'ops/s', 'median': 10},
                     'service_time': {'unit': 'ms', '50_0': 2, '99_0': 4}},
                ]}}
        side = {'run': {'fid': fid, 'phase': 'wq', 'race_id': rid},
                'engine': {'pkg': 'easysearch-2.5.0-%s-linux-x64.tar.gz' % build,
                           'version_measured_nodes': ['2.5.0']},
                'jdk': {'per_node': [{'vendor_detected': 'OpenJDK', 'major_measured': '25.0.4'}]},
                'run_params': {'knn_params': {'index_type': 'int8_hnsw'}, 'shards': 3, 'replicas': 0},
                'corpus': {'docs': 100}}
        prefix = self.camp / 'results' / (fid + '-wq')
        self.dump(Path(str(prefix) + '-race.json'), race)
        self.dump(Path(str(prefix) + '-sidecar.json'), side)
        for node in ('worker-alpha', 'worker-beta'):
            (self.camp / 'monitors' / (fid + '-wq-' + node + '-summary.txt')).write_text(
                'node=%s samples=2 steal_avg=0 steal_max=0 foreign_max=0 swapin=0\nENV-VERDICT: OK\n' % node,
                encoding='utf-8')
        self.statuses.append('| %s | %s | PASS | 1min |\n' % (fid, status))
        (self.camp / 'RACES.md').write_text(''.join(self.statuses), encoding='utf-8')
        return fid, race, side

    def test_exact_valid_and_latest_attempt(self):
        self.measurement(attempt=1)
        latest, _, _ = self.measurement(attempt=2)
        self.measurement(rnd=2, status='RACE-FAIL')
        rows = S.select_measurements(str(self.camp), warn=lambda _: None)
        self.assertEqual([latest], [r['fid'] for r in rows])

    def test_missing_status_does_not_disable_filter(self):
        self.measurement(status='RACE-FAIL')
        self.assertEqual(set(), K.valid_fids(str(self.camp)))
        self.assertEqual({}, K.collect(str(self.camp)))
        (self.camp / 'RACES.md').unlink()
        self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))

    def test_malformed_or_missing_monitor_rejected(self):
        fid, _, _ = self.measurement()
        path = self.camp / 'monitors' / (fid + '-wq-worker-beta-summary.txt')
        path.unlink()
        self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))
        path.write_text('samples=2\nENV-VERDICT: OK\n', encoding='utf-8')
        self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))

    def test_half_race_not_selected(self):
        fid, race, side = self.measurement()
        prefix = self.camp / 'results' / (fid + '-wq')
        Path(str(prefix) + '-race.json').unlink()
        Path(str(prefix) + '-sidecar.json').unlink()
        race['challenge'] = 'write-only'
        side['run']['phase'] = 'w'
        self.dump(self.camp / 'results' / (fid + '-w-race.json'), race)
        self.dump(self.camp / 'results' / (fid + '-w-sidecar.json'), side)
        self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))

    def test_multi_segment_track_name_and_build_isolation(self):
        self.measurement(rnd=1)
        data = C.load([str(self.camp)])
        self.assertIn(('a', 'hv1024x-int8'), data)
        self.measurement(rnd=2, build='3009')
        with self.assertRaisesRegex(ValueError, '不能合池'):
            C.load([str(self.camp)])

    def test_conflicting_status_does_not_reuse_success(self):
        fid, _, _ = self.measurement()
        with (self.camp / 'RACES.md').open('a', encoding='utf-8') as f:
            f.write('| %s | RACE-FAIL | - | - |\n' % fid)
        self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))

    def test_error_rate_and_nonfinite_rejected(self):
        fid, race, _ = self.measurement()
        path = self.camp / 'results' / (fid + '-wq-race.json')
        for error in (0.1, None, float('nan'), float('inf')):
            race['results']['op_metrics'][0]['error_rate'] = error
            self.dump(path, race)
            self.assertEqual([], S.select_measurements(str(self.camp), warn=lambda _: None))

    def test_thresholds_fail_closed(self):
        path = Path(self.tmp.name) / 'thresholds.conf'
        for text in ('# empty\n', 'geo,index-append,Median Throughput,=>,3\n',
                     'geo,index-append,Median Throughput,>=,nan\n'):
            path.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError):
                C.load_thresholds(path)
        path.write_text('geo,index-append,Median Throughput,>=,3\n', encoding='utf-8')
        self.assertEqual(1, len(C.load_thresholds(path)))

    def test_assert_requires_three_samples(self):
        metric = {('Median Throughput', 'index-append'): 100}
        data = {('b', 'geo'): {('demo', 1): {'w': metric}}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, C.run_asserts(data, 'b', [('geo', 'index-append', 'Median Throughput', '>=', 1)]))

    def test_service_time_units_and_zero(self):
        race = {'results': {'op_metrics': [{'task': 'term', 'service_time': {'unit': 's', '50_0': 0, '99_0': 0.025}}]}}
        values = S.metrics_of(race)
        self.assertEqual(0, values[('50th percentile service time', 'term')])
        self.assertEqual(25, values[('99th percentile service time', 'term')])

    def test_explicit_pools_allow_same_car_across_campaigns(self):
        self.measurement(car='a')
        other = Path(self.tmp.name) / 'candidate'
        import shutil
        shutil.copytree(self.camp, other)
        argv = ['car-compare.py', '--a', 'a', '--b', 'a', '--a-campaign', str(self.camp), '--b-campaign', str(other)]
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, C.main())

    def test_vector_signature_keeps_algorithm_parameters(self):
        _fid, race, side = self.measurement()
        before = S._identity(side, race)
        side['run_params']['knn_params'].update(dims=256, m=128, ef_construction=10000)
        self.assertNotEqual(before, S._identity(side, race))

    def test_partial_node_store_is_not_cluster_total(self):
        path = self.camp / 'logs/fp/f-wq/nodes-stats.txt'
        path.parent.mkdir(parents=True)
        payload = {'_nodes': {'total': 3, 'successful': 1, 'failed': 2},
                   'nodes': {'one': {'indices': {'store': {'size_in_bytes': 2**30},
                                                 'segments': {'count': 1}, 'docs': {'count': 10}}}}}
        self.dump(path, payload)
        self.assertIsNone(K.post_merge_store(str(self.camp), 'f', 3)['store_gb'])
        payload['_nodes'] = {'total': 1, 'successful': 1, 'failed': 0}
        self.dump(path, payload)
        self.assertEqual(K.post_merge_store(str(self.camp), 'f', 1)['store_gb'], 1)

    def test_audit_empty_directory_fails(self):
        with patch.object(sys, 'argv', ['audit_track_settings.py', self.tmp.name]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, A.main())


    def test_json_payload_matches_text_table_and_keeps_text_mode(self):
        """--json 与文本表共用 write_rows/query_rows：数字必须同源；文本模式不受影响。"""
        self.measurement(car='a', rnd=1)
        self.measurement(car='b', rnd=1)
        buf = io.StringIO()
        with patch.object(sys, 'argv', ['car-compare', str(self.camp),
                                        '--a', 'a', '--b', 'b', '--json']), \
                contextlib.redirect_stdout(buf):
            rc = C.main()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual((payload['a'], payload['b']), ('a', 'b'))
        self.assertEqual([t['track'] for t in payload['tracks']], ['hv1024x-int8'])
        track = payload['tracks'][0]
        self.assertTrue(track['series_a'] and track['series_b'])
        wrow = next(r for r in track['write']
                    if r['task'] == 'index-append' and r['metric'] == 'Median Throughput')
        self.assertEqual(wrow['a_med'], 100)
        self.assertEqual(wrow['b_med'], 100)
        self.assertEqual(wrow['delta_pct'], 0.0)
        self.assertEqual(wrow['direction'], 'higher-better')
        self.assertEqual(wrow['a_n'], 1)
        qrow = next(r for r in track['query'] if r['task'] == 'knn-search-10-100')
        self.assertEqual((qrow['a_tput'], qrow['b_tput']), (10, 10))
        self.assertEqual((qrow['a_p50'], qrow['b_p50']), (2, 2))
        self.assertEqual((qrow['a_p99'], qrow['b_p99']), (4, 4))
        self.assertEqual(track['pool']['A']['rounds'], 1)

        buf2 = io.StringIO()
        with patch.object(sys, 'argv', ['car-compare', str(self.camp), '--a', 'a', '--b', 'b']), \
                contextlib.redirect_stdout(buf2):
            self.assertEqual(C.main(), 0)
        text = buf2.getvalue()
        self.assertIn('| index-append / Median Throughput |', text)
        self.assertIn('## hv1024x-int8 查询', text)


if __name__ == '__main__':
    unittest.main()
