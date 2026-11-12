"""Strict verifier tests. No ES/Kibana or listening sockets."""
import copy
import importlib.util
import io
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'kibana' / 'verify-dashboard.py'
SPEC = importlib.util.spec_from_file_location('dashboard_verifier_module', PATH)
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)


def fixtures():
    vis = {'id': 'vis', 'type': 'visualization', 'attributes': {'title': 'latency (s)', 'visState': json.dumps({
        'type': 'metrics', 'params': {'id': 'panel', 'series': [{'id': 's'}],
                                    'filter': {'language': 'lucene', 'query': 'trusted:true'}}})}}
    dashboard = {'id': 'bench-test', 'attributes': {'title': 'test',
        'panelsJSON': json.dumps([{'type': 'visualization', 'panelRefName': 'panel_1'}]),
        'timeFrom': 'now-30d', 'timeTo': 'now', 'kibanaSavedObjectMeta': {'searchSourceJSON': json.dumps({
            'query': {'language': 'kuery', 'query': 'NOT codec:(zstd or best_compression)'}, 'filter': []})}},
        'references': [{'name': 'panel_1', 'id': 'vis', 'type': 'visualization'}]}
    return dashboard, vis


class VerifierTests(unittest.TestCase):
    def run_verify(self, point=0, response=None, dashboard=None, vis=None, prefix=None):
        base, original = fixtures()
        dashboard = base if dashboard is None else dashboard
        vis = original if vis is None else vis
        if response is None:
            response = {'panel': {'series': [{'data': [[1234, point]]}]}}
        return V.verify_dashboards([dashboard], [vis], lambda *_: response, prefix, now_ms=1800000000000)

    def assertFails(self, **kwargs):
        result = self.run_verify(**kwargs)
        self.assertFalse(result['passed'], result)
        return result

    def test_zero_is_valid_and_timestamp_is_not_evidence(self):
        self.assertTrue(self.run_verify(point=0)['passed'])
        self.assertTrue(self.run_verify(point=-0.25)['passed'])
        for value in (None, math.nan, math.inf, -math.inf, '0', True):
            with self.subTest(value=value):
                self.assertFails(point=value)
        self.assertFails(response={'panel': {'series': [{'data': [[1234]]}]}})
        self.assertFails(response={'panel': {'series': [{'buckets': [{'key': 1234, 'doc_count': 4}]}]}})
        self.assertTrue(self.run_verify(response={'panel': {'series': [{'buckets': [{'key': 1234, 'value': 0}]}]}})['passed'])

    def test_exact_response_id_required(self):
        result = self.assertFails(response={'wrong': {'series': [{'data': [[1234, 10]]}]}})
        self.assertEqual(result['results'][0]['failed'], 1)
        self.assertFails(response={'panel': {'error': 'upstream', 'series': [{'data': [[1234, 1]]}]}})
        self.assertFails(response={'panel': {'errorMessage': 'bad'}})
        self.assertFails(response=[])

    def test_empty_and_corrupted_series_fail(self):
        for resp in ({'panel': {}}, {'panel': {'series': []}},
                     {'panel': {'series': [{'data': []}]}},
                     {'panel': {'series': [{'data': 'bad'}]}},
                     {'panel': {'series': [None]}}):
            with self.subTest(resp=resp):
                self.assertFails(response=resp)

    def test_no_dashboard_match_fails_without_requests(self):
        result = self.assertFails(prefix='not-found')
        self.assertEqual(result['results'], [])
        self.assertFalse(V.verify_dashboards([], [], lambda *_: self.fail('must not request'))['passed'])

    def test_missing_reference_or_visualization_fails(self):
        dashboard, vis = fixtures()
        dashboard['references'] = []
        self.assertFails(dashboard=dashboard)
        dashboard, vis = fixtures()
        dashboard['references'][0]['id'] = 'missing'
        self.assertFails(dashboard=dashboard)
        dashboard, vis = fixtures()
        dashboard['attributes']['panelsJSON'] = json.dumps([{'type': 'visualization', 'id': 'missing'}])
        self.assertFails(dashboard=dashboard)

    def test_direct_and_named_references_resolve(self):
        dashboard, vis = fixtures()
        dashboard['attributes']['panelsJSON'] = json.dumps([{'type': 'visualization', 'id': 'vis'}])
        dashboard['references'] = []
        self.assertTrue(self.run_verify(dashboard=dashboard)['passed'])
        self.assertTrue(self.run_verify()['passed'])
        dashboard['attributes']['panelsJSON'] = json.dumps([{'type': 'visualization', 'panelIndex': '1'}])
        dashboard['references'] = [{'name': '1', 'id': 'vis', 'type': 'visualization'}]
        self.assertTrue(self.run_verify(dashboard=dashboard)['passed'])

    def test_damaged_visstate_and_params_fail(self):
        for state in ('not JSON', '{}', '[]', '{"type":"metrics","params":{}}',
                      '{"type":"unknown","params":{}}',
                      '{"type":"markdown","params":{}}'):
            dashboard, vis = fixtures()
            vis['attributes']['visState'] = state
            with self.subTest(state=state):
                self.assertFails(vis=vis)
        dashboard, vis = fixtures()
        state = json.loads(vis['attributes']['visState'])
        state['params']['filter'] = 'trusted:true'
        vis['attributes']['visState'] = json.dumps(state)
        self.assertFails(vis=vis)

    def test_damaged_dashboard_fails(self):
        for panels in ('not JSON', '{}', '[]'):
            dashboard, vis = fixtures()
            dashboard['attributes']['panelsJSON'] = panels
            self.assertFails(dashboard=dashboard)
        dashboard, vis = fixtures()
        dashboard['attributes']['kibanaSavedObjectMeta']['searchSourceJSON'] = 'bad'
        self.assertFails(dashboard=dashboard)

    def test_markdown_only_not_successful_tsvb_verification(self):
        dashboard, vis = fixtures()
        vis['attributes']['visState'] = json.dumps({'type': 'markdown', 'params': {'markdown': 'hello'}})
        result = self.assertFails(vis=vis)
        self.assertEqual(result['results'][0]['skipped'], 1)
        self.assertIn('未验收任何 TSVB 面板', result['errors'])

    def test_kql_and_lucene_keep_their_languages(self):
        dashboard, vis = fixtures()
        requests = []
        pinned = {'meta': {'disabled': False}, 'query': {'term': {'campaign': 'x'}}}
        src = json.loads(dashboard['attributes']['kibanaSavedObjectMeta']['searchSourceJSON'])
        src['filter'] = [pinned]
        dashboard['attributes']['kibanaSavedObjectMeta']['searchSourceJSON'] = json.dumps(src)
        before = copy.deepcopy(vis)
        def post(path, body):
            requests.append((path, body))
            return {'panel': {'series': [{'data': [[0, 0]]}]}}
        result = V.verify_dashboards([dashboard], [vis], post, now_ms=1800000000000)
        self.assertTrue(result['passed'])
        body = requests[0][1]
        self.assertEqual(body['query'], [src['query']])
        self.assertEqual(body['filters'], [pinned])
        self.assertEqual(body['panels'][0]['filter'], {'language': 'lucene', 'query': 'trusted:true'})
        self.assertEqual(before, vis)

    def test_relative_time_uses_now_once(self):
        dashboard, vis = fixtures()
        requests = []
        now = 1800000000000
        def post(path, body):
            requests.append(body)
            return {'panel': {'series': [{'data': [[now, 0]]}]}}
        result = V.verify_dashboards([dashboard], [vis], post, now_ms=now)
        self.assertTrue(result['passed'])
        self.assertEqual(requests[0]['timerange'], {'min': now - 30 * 86400000, 'max': now})
        self.assertEqual(V.to_epoch_ms(None, now, now - 1000), now - 1000)

    def test_migrated_reference_name_and_index(self):
        dashboard, vis = fixtures()
        dashboard['attributes']['panelsJSON'] = json.dumps([
            {'type': 'visualization', 'panelRefName': 'panel_1', 'panelIndex': '1'}])
        dashboard['references'] = [{'name': '1', 'id': 'vis', 'type': 'visualization'}]
        self.assertTrue(self.run_verify(dashboard=dashboard)['passed'])
        dashboard['references'][0]['name'] = '2'
        self.assertFails(dashboard=dashboard)

    def test_request_errors_are_failures(self):
        dashboard, vis = fixtures()
        def post(*_):
            raise OSError('HTTP 503')
        result = V.verify_dashboards([dashboard], [vis], post, now_ms=1800000000000)
        self.assertFalse(result['passed'])
        self.assertEqual(result['results'][0]['failed'], 1)

    def test_cli_zero_matches_returns_nonzero(self):
        with patch.object(V, 'kget', return_value={'version': {'number': '7.17.29'}}), \
             patch.object(V, 'find_all', return_value=[]), patch('sys.argv', ['verify-dashboard.py', '--json']), \
             patch('sys.stdout', io.StringIO()) as output:
            self.assertEqual(V.main(), 1)
            self.assertFalse(json.loads(output.getvalue())['passed'])


if __name__ == '__main__':
    unittest.main()
