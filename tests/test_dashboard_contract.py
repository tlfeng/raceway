"""Dashboard contract regressions. Pure functions and injected ES fixtures only."""
import ast
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
KIBANA = ROOT / 'kibana'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = load('dashboard_contract_module', KIBANA / 'make_dashboard.py')
M = load('manifest_contract_module', KIBANA / 'panel-manifest.py')


class DashboardContractTests(unittest.TestCase):
    def setUp(self):
        self.groups = D.group_tasks(['term', 'phrase', 'knn-search-10-100', 'new-business-v2',
                                    'index-stats', 'refresh-after-index', 'wait-for-single-segment'])
        self.specs = D.track_panel_specs('hv1024x', self.groups, '1d')

    def test_unknown_tasks_and_workloads_never_merge(self):
        self.assertEqual({tasks[0] for _gid, _label, tasks in self.groups},
                         {'term', 'phrase', 'knn-search-10-100', 'new-business-v2',
                          'index-stats', 'refresh-after-index', 'wait-for-single-segment'})
        self.assertTrue(all(len(tasks) == 1 for _g, _l, tasks in self.groups))
        self.assertEqual(len({gid for gid, _l, _t in self.groups}), len(self.groups))

    def test_measurements_gate_but_error_evidence_survives(self):
        self.assertNotIn('trusted', D.DASH_SCOPE)
        errors = [s for s in self.specs if s[0].startswith('err-')]
        self.assertEqual(len(errors), len(self.groups))
        for key, _title, filt, _fmt, _terms, agg, _iv in self.specs:
            self.assertEqual('trusted:true' in filt, not key.startswith('err-'))
            self.assertEqual(agg, 'max' if key.startswith(('err-', 'system-')) else 'percentile')
        queries = []
        def count(q):
            queries.append(q)
            return 1 if 'family:error_rate' in q and 'trusted' not in q else 0
        with patch.object(D, '_count', side_effect=count):
            self.assertTrue(D.panel_has_data(errors[0][2], D.variant_scope('hv1024x')))
        self.assertNotIn('trusted', queries[0])
        self.assertIn('track_variant:"hv1024x"', queries[0])

    def test_every_stat_and_resource_keeps_experiment_identity(self):
        self.assertTrue(self.specs)
        for _key, _title, filt, _fmt, terms, agg, _iv in self.specs:
            self.assertEqual(terms, 'comparison_key')
            self.assertNotIn('stat:(', filt)
            self.assertNotIn('metric:(', filt)
            self.assertNotEqual(agg, 'avg')
        latency = [s for s in self.specs if s[0].startswith('service_time-')]
        self.assertEqual(len(latency), 3 * sum(D.is_measurement_task(tasks[0]) for _g, _l, tasks in self.groups))
        for key, _title, filt, *_ in self.specs:
            if 'task:"index-stats"' in filt:
                self.assertTrue(key.startswith('err-'))
        gc = [s for s in self.specs if '-gc-' in s[0]]
        self.assertEqual(len(gc), 4)

    def test_tsvb_median_uses_supported_percentile_shape_and_order(self):
        state = D.tsvb_timeseries('median', 'trusted:true', '0.00')
        series = state['params']['series'][0]
        metric = series['metrics'][0]
        self.assertEqual(metric['type'], 'percentile')
        self.assertEqual(metric['percentiles'], [{'value': '50', 'mode': 'line'}])
        self.assertEqual(metric['field'], 'value')
        self.assertEqual(series['terms_order_by'], '_key')
        self.assertEqual(series['terms_field'], 'comparison_key')
        self.assertEqual(state['params']['time_field'], 'run_start')
        with self.assertRaises(ValueError):
            D.tsvb_timeseries('bad', '*', '0', metric_type='avg')

    def test_runner_bases_and_variants_are_exact(self):
        self.assertEqual(D.runner_base('geonames'), 'geo')
        for base in ('hv1024x-os-bbq', 'hv1024x-os-disk', 'hv1024x-bbqdisk'):
            self.assertEqual(D.runner_base(base), base)
            self.assertIn('track_variant:"%s"' % base, D.variant_scope(base))
        baseline = D.variant_scope('hv1024x')
        self.assertIn('NOT codec:', baseline)
        self.assertIn('NOT source_reuse:true', baseline)
        for variant in ('hv1024x-int8', 'hv1024x-bbq', 'hv1024x-codec-def-sr', 'hv1024x-codec-zstd'):
            scope = D.variant_scope('hv1024x', variant)
            self.assertEqual(scope, 'track_variant:"%s"' % variant)
            self.assertNotIn('NOT', scope)

    def test_preflight_uses_same_scope_as_dashboard_and_query_errors_fail(self):
        queries = []
        with patch.object(D, '_count', side_effect=lambda q: queries.append(q) or 1):
            objects, dropped = D.build_track_dashboard('hv1024x', self.groups[:1], '1d', 'now-30d', 'now',
                                                       'hv1024x-bbq')
        self.assertFalse(dropped)
        dashboard = objects[-1]
        scope = json.loads(dashboard['attributes']['kibanaSavedObjectMeta']['searchSourceJSON'])['query']['query']
        self.assertEqual(scope, 'track_variant:"hv1024x-bbq"')
        states = [json.loads(o['attributes']['visState']) for o in objects[:-1]]
        self.assertEqual(queries, [D.scoped_query(s['params']['filter']['query'], scope) for s in states])
        self.assertTrue(all(s['params']['series'][0]['terms_field'] == 'comparison_key' for s in states))
        with patch.object(D, '_count', side_effect=ValueError('invalid query')):
            with self.assertRaisesRegex(ValueError, 'invalid query'):
                D.panel_has_data('*')

    def test_composite_discovers_later_variants_and_tasks(self):
        calls = []
        pages = [
            {'buckets': [{'key': {'track': 'hv1024x', 'variant': 'hv1024x', 'task': 'term',
                                  'car': 'ez@2.5.0@3009@jdk25|hv1024x'}, 'doc_count': 3}],
             'after_key': {'track': 'hv1024x'}},
            {'buckets': [{'key': {'track': 'hv1024x', 'variant': 'hv1024x-bbq', 'task': 'new-query',
                                  'car': 'ez@2.5.0@3015@jdk25|hv1024x-bbq'}, 'doc_count': 4}]},
        ]
        def post(path, body):
            calls.append(body)
            return {'aggregations': {'items': pages.pop(0)}}
        with patch.object(D, 'es_post', side_effect=post):
            facts = D.es_track_facts()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['aggs']['items']['composite']['after'], {'track': 'hv1024x'})
        self.assertIn('hv1024x-bbq', facts['hv1024x']['variants'])
        self.assertIn('new-query', facts['hv1024x']['tasks'])
        missing = {'key': {'track': 'hv1024x', 'variant': None, 'task': 'term', 'car': 'x'}, 'doc_count': 1}
        with patch.object(D, 'composite_buckets', return_value=iter([missing])):
            with self.assertRaises(ValueError):
                D.es_track_facts()

    def test_index_patterns_require_and_expose_new_fields(self):
        defs = [('@timestamp', 'date'), ('run_start', 'date')] + [
            (name, 'keyword') for name in ('series_key', 'track_variant', 'comparison_key', 'knn_index_type')]
        with patch.object(D, 'flat_fields', return_value=defs):
            for index in ('benchmark-result-metric', 'benchmark-result-race'):
                fields = json.loads(D.index_pattern_obj(index)['attributes']['fields'])
                self.assertIn('comparison_key', {f['name'] for f in fields})
        with patch.object(D, 'flat_fields', return_value=defs[:-1]):
            with self.assertRaisesRegex(ValueError, 'knn_index_type'):
                D.index_pattern_obj('benchmark-result-metric')

    def test_dimension_tables_aggregate_percentile50_and_do_not_mix_variants(self):
        bodies = []
        def bucket(variant, value):
            return {'key': {'track': 'ds-j1024', 'variant': variant,
                            'car': 'ez@2.5.0@3015@jdk25|' + variant}, 'n': {'value': 3},
                    **{key: {'v': {'values': {'50.0': value}}} for key, *_ in D.DS_TABLE_SPECS}}
        def post(path, body):
            bodies.append(body)
            return {'aggregations': {'items': {'buckets': [bucket('ds-j1024', 0.025),
                                                            bucket('ds-j1024-int8', 999)]}}}
        with patch.object(D, 'es_post', side_effect=post):
            rows, cars = D.ds_collect()
        self.assertEqual(rows[('ds-j1024', 'ez@2.5.0@3015@jdk25')]['p99'], 0.025)
        self.assertEqual(len(rows), 1)
        for key, *_ in D.DS_TABLE_SPECS:
            agg = bodies[0]['aggs']['items']['aggs'][key]['aggs']['v']
            self.assertEqual(agg, {'percentiles': {'field': 'value', 'percents': [50]}})
        self.assertEqual(cars[0]['tracks'], 1)
        table = D.ds_table_markdown('j', [(1024, 'ds-j1024')], rows, cars[0]['series_key'])
        self.assertIn('p99 s', table)
        self.assertIn('0.0250', table)
        self.assertIn('当量', table)
        self.assertIn('Recall@10', table)

    def test_dimension_types_keep_seconds_and_comparison_key(self):
        facts = {'ds-j1024': {'variants': {'ds-j1024': {'cars': ['a', 'b'], 'tasks': ['knn-search-10-100']}}}}
        with patch.object(D, 'panel_has_data', return_value=True):
            objects = D.build_dimsweep_dashboard({'j': [(1024, 'ds-j1024')]}, {}, [], 'a', '1d', 'now-30d', 'now')
            objects += D.build_dimsweep_car_dashboard([('ds-j1024', 1024, 'j')], facts, '1d', 'now-30d', 'now')
        for obj in objects:
            if obj['type'] != 'visualization':
                continue
            state = json.loads(obj['attributes']['visState'])
            if state['type'] != 'metrics':
                continue
            self.assertEqual(state['params']['series'][0]['terms_field'], 'comparison_key')
            self.assertNotIn('(ms)', state['title'])
            self.assertIn('track_variant:', state['params']['filter']['query'])
        self.assertIn('ds-j1024', {t for ts in D.ds_lineage_tracks(facts).values() for _d, t in ts})

    def test_environment_table_escapes_comparison_key_separator(self):
        check = {'hits': {'total': {'value': 3}}, 'aggregations': {
            'trusted': {'buckets': [{'key': True, 'doc_count': 3}]},
            'variants': {'doc_count': 0}, 'missing_key': {'doc_count': 0}}}
        configs = [{'key': {'car': 'jdk25', 'series': 'ez@2.5.0@3015@jdk25',
                            'experiment': 'ez@2.5.0@3015@jdk25|hv1024x-int8'}, 'doc_count': 3}]
        with patch.object(D, 'es_get', return_value={'hits': {'hits': []}}), \
                patch.object(D, 'es_post', return_value=check), \
                patch.object(D, 'composite_buckets', return_value=iter(configs)):
            markdown = D.env_markdown()
        row = next(line for line in markdown.splitlines() if line.startswith('| jdk25'))
        self.assertEqual(row.count('|'), 5)
        self.assertIn('&#124;hv1024x-int8', row)
        self.assertTrue(row.endswith('| 3 |'))

    def test_manifest_reports_percentile_instead_of_avg(self):
        vis = D.visualization_obj('test', 'time (s)', 'trusted:true', '0.000')
        self.assertIn('percentile[50](value)', M.panel_row(1, vis))

    def test_deploy_readiness_predicate_without_shell_or_http(self):
        text = (KIBANA / 'deploy-kibana.sh').read_text(encoding='utf-8')
        code = text.split("status_ready() {", 1)[1].split("python3 -c '\n", 1)[1].split("\n'", 1)[0]
        cases = [({'status': {'overall': {'state': 'green'}}}, 0),
                 ({'status': {'overall': {'level': 'available'}}}, 0),
                 ({'status': {'overall': {'state': 'green', 'level': 'degraded'}}}, 1),
                 ({'status': {'overall': {'state': 'red'}}}, 1),
                 ({'status': {'overall': {'state': 'yellow'}}}, 1),
                 ({'status': {'overall': {'level': 'unavailable'}}}, 1), ({}, 1)]
        for payload, expected in cases:
            with self.subTest(payload=payload), patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                    patch('sys.stdout', io.StringIO()), self.assertRaises(SystemExit) as exit_:
                exec(compile(code, 'status_ready', 'exec'), {})
            self.assertEqual(exit_.exception.code, expected)
        with patch('sys.stdin', io.StringIO('not json')), patch('sys.stdout', io.StringIO()), \
                self.assertRaises(SystemExit) as exit_:
            exec(compile(code, 'status_ready', 'exec'), {})
        self.assertEqual(exit_.exception.code, 1)
        # curl 非零状态必须与 JSON ready 同时成功才算通过。
        self.assertIn('curl -fsS --max-time 5', text)
        self.assertIn('set -uo pipefail', text)
        self.assertNotIn("grep -q", text)

    def test_main_builds_all_track_variant_and_dimension_types_with_mocks(self):
        facts = {
            'geonames': {'variants': {'geo': {'tasks': ['term'], 'cars': ['a|geo']}}},
            'hv1024x': {'variants': {
                'hv1024x': {'tasks': ['knn-search-10-100'], 'cars': ['a|hv1024x']},
                'hv1024x-int8': {'tasks': ['new-query'], 'cars': ['a|hv1024x-int8']}}},
            'ds-j1024': {'variants': {'ds-j1024': {'tasks': ['knn-search-10-100'],
                                                 'cars': ['a|ds-j1024', 'b|ds-j1024']}}},
        }
        written = io.StringIO()
        class Output(io.StringIO):
            def close(self):
                written.write(self.getvalue())
                super().close()
        def index_obj(name):
            return {'id': name, 'type': 'index-pattern', 'attributes': {}}
        with patch.object(D, 'index_pattern_obj', side_effect=index_obj), \
             patch.object(D, 'es_track_facts', return_value=facts), \
             patch.object(D, 'panel_has_data', return_value=True), \
             patch.object(D, 'es_track_runs', return_value={'runs': 3}), \
             patch.object(D, 'ds_collect', return_value=({}, [{'series_key': 'a', 'tracks': 1, 'runs': 3}])), \
             patch.object(D, 'env_markdown', return_value='hardware fixture'), \
             patch('builtins.open', side_effect=lambda *_a, **_k: Output()), \
             patch('sys.stdout', io.StringIO()), \
             patch('sys.argv', ['make_dashboard.py', '--kibana-version', '7.17.29', '--out', 'mock.ndjson']):
            self.assertEqual(D.main(), 0)
        objects = [json.loads(line) for line in written.getvalue().splitlines()]
        dashboards = {o['id']: o for o in objects if o['type'] == 'dashboard'}
        self.assertEqual(set(dashboards), {'bench-geonames', 'bench-hv1024x',
            'bench-hv1024x-variant-hv1024x-int8', 'bench-ds-j1024', 'bench-dimsweep',
            'bench-dimsweep-car', 'bench-overview'})
        all_ids = {o['id'] for o in objects}
        for dashboard in dashboards.values():
            for ref in dashboard['references']:
                self.assertIn(ref['id'], all_ids)
        nav = next(o for o in objects if o['id'] == 'bench-vis-nav')
        self.assertIn('bench-dimsweep-car', nav['attributes']['visState'])

    def test_python_sources_compile_without_main(self):
        for path in KIBANA.glob('*.py'):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))

    @unittest.skipUnless(shutil.which('node'), 'Node runtime not installed; no proxy is started')
    def test_pit_translation_pure_node(self):
        code = r'''
const assert = require('assert');
const shim = require(process.argv[1]);
const t = shim.translatePitResponse;
const ok = JSON.stringify({pits:[{pit_id:'p',successful:true}]});
const closed = JSON.parse(t(201, ok, true));
assert.equal(closed.succeeded, true); assert.equal(closed.num_freed, 1);
for (const status of [401,403,404,500]) { assert.equal(t(status, ok, true), null); }
for (const body of ['bad','{}','[]','{"error":"bad"}',
  '{"pits":[]}', '{"pits":[{"pit_id":"p","successful":false}]}',
  '{"pits":[{"successful":true}]}', '{"succeeded":false,"num_freed":0}']) {
  assert.equal(t(200, body, true), null);
}
assert.equal(JSON.parse(t(200, '{"pit_id":"p"}', false)).id, 'p');
assert.equal(t(500, '{"pit_id":"p"}', false), null);
assert.equal(t(200, '{"error":"bad","pit_id":"p"}', false), null);
'''
        result = subprocess.run([shutil.which('node'), '-e', code, str(KIBANA / 'shim-7.17-es250.js')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
