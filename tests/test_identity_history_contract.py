#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份、历史覆盖与采集契约回归；纯离线，不启动 shell/SSH/ES，仅临时目录夹具。"""
import copy
import importlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
I = importlib.import_module('identity')
H = importlib.import_module('bench-history')
Q = importlib.import_module('bench-queue')
S = importlib.import_module('bench-sidecar')


def row(**changes):
    result = {
        'campaign': 'c1', 'race_id': 'race-1', 'fid': 'jdk25-hv1024x-r1', 'attempt': 1,
        'car_code': 'jdk25', 'engine': 'easysearch', 'engine_build': '3015',
        'series_key': 'ez@2.5.0@3015@graalvm25', 'track': 'hv1024x',
        'track_variant': 'hv1024x', 'round': 1, 'challenge': ['full'], 'trusted': True,
    }
    result.update(changes)
    return result


def response(documents, total=None, scroll_id='scroll-1'):
    result = {'timed_out': False, '_shards': {'total': 1, 'successful': 1, 'failed': 0},
              'hits': {'total': {'value': len(documents) if total is None else total,
                                 'relation': 'eq'},
                       'hits': [{'_index': H.INDEX, '_id': str(n), '_source': source}
                                for n, source in documents]}}
    if scroll_id is not None:
        result['_scroll_id'] = scroll_id
    return result


class NoExternalCalls(unittest.TestCase):
    def setUp(self):
        for name in ('subprocess.run', 'urllib.request.urlopen'):
            guard = patch(name, side_effect=AssertionError('测试禁止调用外部服务'))
            guard.start()
            self.addCleanup(guard.stop)


class TestIdentityContract(NoExternalCalls):
    def test_car_code_is_authoritative_and_shared(self):
        document = row(user_tags={'car': 'wrong'}, car='wrong')
        self.assertEqual(I.car_of(document), 'jdk25')
        self.assertEqual(H.row_build_key(document), 'jdk25@3015')
        cov = H.build_coverage([document])
        self.assertTrue(cov.get('jdk25', 'hv1024x', 1).covered_for('jdk25@3015'))

    def test_plugin_array_is_not_car_identity(self):
        self.assertEqual(I.car_of({'car': ['external']}), '')

    def test_opensearch_without_build_has_real_engine_prefix(self):
        self.assertEqual(I.series_key_of('opensearch', '3.6.0', None, 'OpenJDK', '25', car='os36'),
                         'os@3.6.0@—@openjdk25')
        self.assertEqual(S.engine_name('opensearch-3.6.0-linux-x64.tar.gz'), 'opensearch')
        self.assertEqual(S.version_from_pkg('opensearch-3.6.0-linux-x64.tar.gz'), '3.6.0')

    def test_independent_geonames_upsert_track_is_not_renamed(self):
        self.assertEqual(I.runner_track_id('geonames-upsert'), 'geonames-upsert')

    def test_independent_geonames_upsert_codec_variant_is_not_renamed(self):
        self.assertEqual(I.runner_track_id('geonames-upsert-codec-def'), 'geonames-upsert-codec-def')

    def test_comparison_key_does_not_change_engine_series(self):
        series = 'ez@2.5.0@3015@graalvm25'
        self.assertEqual(I.comparison_key_of(series, 'geonames-codec-def'), series + '|geo-codec-def')
        self.assertNotEqual(I.comparison_key_of(series, 'hv1024x'),
                            I.comparison_key_of(series, 'hv1024x-int8'))
        with self.assertRaises(ValueError):
            I.comparison_key_of(series, '')


class TestCoverageContract(NoExternalCalls):
    def plan(self, rows, tracks=('hv1024x',), build='3015'):
        return H.plan_cells(H.build_coverage(rows), [('jdk25', build)], tracks, 1)

    def test_write_only_and_missing_challenge_are_half(self):
        for fields in ({'challenge': ['write-only']}, {'challenge': ['index-only']},
                       {'challenge': None}, {'challenge': ['unknown']}):
            with self.subTest(fields=fields):
                document = row(**fields)
                self.assertEqual(H._verdict_of([document]), H.VERDICT_HALF)
                self.assertEqual(len(self.plan([document])['skipped']), 0)

    def test_same_real_measurement_can_pair_split_phases(self):
        documents = [row(challenge=['write-only']),
                     row(challenge=['query-only'], race_id='query-uuid')]
        self.assertEqual(len(self.plan(documents)['skipped']), 1)

    def test_phase_pairs_never_cross_campaign_attempt_or_identity(self):
        for changes in ({'campaign': 'c2'}, {'attempt': 2},
                        {'fid': 'jdk25-hv1024x-r1-try2', 'attempt': 2},
                        {'fid': 'another-real-fid'}, {'engine_build': '3020'},
                        {'series_key': 'ez@2.5.0@3015@kona25'}):
            with self.subTest(changes=changes):
                documents = [row(challenge=['write-only']),
                             row(challenge=['query-only'], **changes)]
                self.assertEqual(len(self.plan(documents)['skipped']), 0)
                self.assertEqual(H._verdict_of(documents), H.VERDICT_HALF)

    def test_source_real_fid_can_identify_split_measurement(self):
        documents = [row(fid=None, source=['sidecar:actual-fid-try2-w'], attempt=2,
                         challenge=['write-only']),
                     row(fid=None, source=['sidecar:actual-fid-try2-q'], attempt=2,
                         challenge=['query-only'], race_id='q-id')]
        self.assertEqual(H._verdict_of(documents), H.VERDICT_VALID)

    def test_missing_real_fid_uses_single_race_id_not_cell_coordinates(self):
        documents = [row(fid=None, challenge=['write-only']),
                     row(fid=None, challenge=['query-only'], race_id='other-measurement')]
        self.assertEqual(H._verdict_of(documents), H.VERDICT_HALF)
        self.assertEqual(H._verdict_of([row(fid=None)]), H.VERDICT_VALID)

    def test_successful_measurement_survives_other_attempt_failure(self):
        documents = [row(status='QUERY-FAIL'),
                     row(fid='jdk25-hv1024x-r1-try2', attempt=2, race_id='race-2')]
        self.assertEqual(len(self.plan(documents)['skipped']), 1)

    def test_untrusted_complete_measurement_not_covered(self):
        document = row(trusted=False)
        self.assertEqual(H._verdict_of([document]), H.VERDICT_DEGRADED)
        self.assertEqual(len(self.plan([document])['skipped']), 0)

    def test_environment_failure_is_never_covered(self):
        document = row(status='ENV-FAIL')
        self.assertEqual(H._verdict_of([document]), H.VERDICT_FAIL)
        self.assertEqual(len(self.plan([document])['skipped']), 0)
        self.assertTrue(Q.is_fail('ENV-FAIL'))
        progress = Q.parse_progress('[2026-10-20 10:00:00] [1] a-geo-r1 ENV-FAIL')
        self.assertEqual(progress['events'][0][1][3], 'ENV-FAIL')

    def test_complete_build_does_not_cover_other_build(self):
        self.assertEqual(len(self.plan([row()], build='3020')['skipped']), 0)

    def test_actual_variant_and_geo_runner_alias_are_authoritative(self):
        documents = [row(track='geonames', track_variant='geonames', codec='default'),
                     row(track='geonames', track_variant='geonames-codec-def', codec='default'),
                     row(track_variant='hv1024x-int8', knn_index_type='int8_hnsw')]
        cov = H.build_coverage(documents)
        self.assertIsNotNone(cov.get('jdk25', 'geo', 1))
        self.assertIs(cov.get('jdk25', 'geo', 1), cov.get('jdk25', 'geonames', 1))
        self.assertEqual(cov.tracks, ['geo', 'geo-codec-def', 'hv1024x-int8'])
        self.assertIsNone(cov.get('jdk25', 'hv1024x', 1))


class TestHistoryQueryContract(NoExternalCalls):
    def test_query_fields_include_complete_measurement_and_vector_identity(self):
        self.assertTrue({'car_code', 'track_variant', 'knn_index_type', 'fid', 'attempt',
                         'race_id', 'source', 'challenge', 'series_key'} <= set(H._SOURCE_FIELDS))

    def test_ssh_auth_stays_on_stdin_and_http_failure_is_unavailable(self):
        process = SimpleNamespace(returncode=22, stdout='', stderr='HTTP 401')
        with patch('subprocess.run', return_value=process) as mocked:
            cov = H.fetch_coverage(ssh='not-used', auth='admin:p"ass\\word\nnext')
        self.assertFalse(cov.available)
        args, kwargs = mocked.call_args
        self.assertNotIn('p"ass', ' '.join(args[0]))
        self.assertIn('-fsSk', args[0][-1])
        self.assertIn('\\n', kwargs['input'])
        self.assertEqual(kwargs['input'].count('\n'), 1)

    def test_json_http_error_and_schema_failure_are_unavailable(self):
        invalid = [{'error': {'type': 'security_exception'}, 'status': 401}, [], {},
                   {'hits': {'hits': []}}]
        timed_out = response([])
        timed_out['timed_out'] = True
        invalid.append(timed_out)
        partial = response([])
        partial['_shards']['failed'] = 1
        invalid.append(partial)
        for payload in invalid:
            with self.subTest(payload=payload), patch('subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload), stderr='')):
                self.assertFalse(H.fetch_coverage(ssh='not-used').available)

    def test_scroll_reads_beyond_10000_with_exact_total_and_cleanup(self):
        total = 10001
        calls = []
        def query(ssh, body, **kwargs):
            calls.append((copy.deepcopy(body), kwargs))
            if kwargs.get('method') == 'DELETE':
                return {'succeeded': True}
            pages = len([x for x in calls if x[1].get('method') != 'DELETE']) - 1
            start = pages * H.PAGE_SIZE
            return response([(n, row(race_id='race-%d' % n))
                             for n in range(start, min(start + H.PAGE_SIZE, total))], total=total)
        with patch.object(H, 'es_query', side_effect=query):
            rows, count = H.fetch_rows()
        self.assertEqual((len(rows), count), (total, total))
        self.assertEqual(calls[0][1]['path'], H.INDEX + '/_search?scroll=2m')
        self.assertIs(calls[0][0]['track_total_hits'], True)
        self.assertEqual(calls[-1][1]['method'], 'DELETE')

    def test_inexact_total_duplicate_and_truncated_pages_fail_closed(self):
        inexact = response([(0, row())], total=10000, scroll_id=None)
        inexact['hits']['total']['relation'] = 'gte'
        truncated = response([(0, row())], total=2, scroll_id=None)
        missing_source = response([(0, row())], scroll_id=None)
        del missing_source['hits']['hits'][0]['_source']
        for page in (inexact, truncated, missing_source):
            with self.subTest(page=page), patch.object(H, 'es_query', return_value=page):
                self.assertFalse(H.fetch_coverage().available)
        first = response([(0, row())], total=2)
        with patch.object(H, 'es_query', side_effect=[first, first, {'succeeded': True}]):
            self.assertFalse(H.fetch_coverage().available)

    def test_document_schema_failure_is_unavailable_not_empty_history(self):
        for change in ({'car_code': None}, {'track_variant': None}, {'round': '1'},
                       {'challenge': 'full'}, {'trusted': None}):
            with self.subTest(change=change):
                page = response([(0, row(**change))], scroll_id=None)
                with patch.object(H, 'es_query', return_value=page):
                    self.assertFalse(H.fetch_coverage().available)

    def test_total_limit_does_not_return_partial_coverage(self):
        page = response([], total=20001)
        with patch.object(H, 'es_query', side_effect=[page, {'succeeded': True}]):
            self.assertFalse(H.fetch_coverage().available)

    def test_empty_success_is_available_but_outage_is_not(self):
        with patch.object(H, 'es_query', return_value=response([], scroll_id=None)):
            self.assertTrue(H.fetch_coverage().available)
        with patch.object(H, 'es_query', side_effect=H.HistoryUnavailable('offline')):
            cov = H.fetch_coverage()
        self.assertFalse(cov.available)
        self.assertEqual(H.plan_cells(cov, [('jdk25', '3015')], ['hv1024x'], 1)['skipped'], [])


class TestQueueSelfCheck(NoExternalCalls):
    def campaign(self, events, cars='a b c', track='geo', rounds=2):
        lines = ['[2026-10-20 10:00:00] campaign c 启动: configs=[%s] tracks=%s rounds=%d' %
                 (cars, track, rounds)]
        for seq, fid, kind in events:
            if kind == 'start':
                line = '===== [%d] %s 开始 =====' % (seq, fid)
            else:
                line = '[%d] %s 跳过（引擎不支持该变体）' % (seq, fid)
            lines.append('[2026-10-20 10:01:00] ' + line)
        class MemorySource:
            root = 'memory'
            def read(self, cid, rel):
                return '\n'.join(lines) if rel == 'PROGRESS.txt' else None
        return Q.build_campaign(MemorySource(), 'c')

    def test_legal_skip_remains_a_queue_position(self):
        campaign = self.campaign([(1, 'a-geo-r1', 'start'), (2, 'b-geo-r1', 'skip'),
                                  (3, 'c-geo-r1', 'start')])
        self.assertTrue(campaign.plan_known, campaign.warnings)
        self.assertEqual(campaign.pending, 3)
        self.assertEqual(campaign.fields[1].status, Q.STATUS_SKIP)

    def test_extra_tail_wrong_sequence_and_changed_identity_are_rejected(self):
        for events in ([(1, 'a-geo-r1', 'start'), (2, 'a-nested-r1', 'start')],
                       [(2, 'a-geo-r1', 'start')],
                       [(1, 'a-geo-r1', 'start'), (1, 'b-geo-r1', 'start')]):
            with self.subTest(events=events):
                self.assertFalse(self.campaign(events, cars='a', rounds=1).plan_known)

    def test_retry_same_sequence_does_not_shift_plan(self):
        campaign = self.campaign([(1, 'a-geo-r1', 'start'),
                                  (1, 'a-geo-r1-try2', 'start'), (2, 'b-geo-r1', 'start')])
        self.assertTrue(campaign.plan_known, campaign.warnings)


class TestFrozenRunnerPlan(NoExternalCalls):
    PLAN = ('fid\tconfig\ttrack\tround\trecall\test\n'
            'jdk25-geo-r1\tENGINE_NAME=easysearch CAR_LABEL=jdk25 JDK_DIR=\tgeo\t1\tnorecall\t0\n'
            'jdk25-hv1024x-r1\tENGINE_NAME=easysearch CAR_LABEL=jdk25 JDK_DIR=\thv1024x\t1\trecall\t30\n')

    def campaign(self, **files):
        contents = {'PLAN.tsv': self.PLAN, 'DEADLINE': '0', **files}
        class MemorySource:
            root = 'memory'
            def read(self, cid, rel):
                return contents.get(rel)
        return Q.build_campaign(MemorySource(), 'c')

    def test_plan_is_authoritative_and_config_spaces_preserved(self):
        campaign = self.campaign(**{'STATE.txt': 'OK jdk25-hv1024x-r1\n'})
        self.assertTrue(campaign.plan_known)
        self.assertEqual(campaign.mode, 'plan')
        self.assertEqual((campaign.total, campaign.valid, campaign.pending), (2, 1, 1))
        self.assertEqual(campaign.fields[0].car, 'jdk25')
        self.assertEqual(campaign.fields[1].status, Q.STATUS_VALID)

    def test_numeric_state_never_binds_new_plan(self):
        campaign = self.campaign(**{'STATE.txt': 'OK 1\n'})
        self.assertEqual(campaign.valid, 0)
        self.assertTrue(any('旧行号' in warning for warning in campaign.warnings))

    def test_invalid_plan_never_falls_back_to_log_guess(self):
        campaign = self.campaign(**{'PLAN.tsv': 'fid config track\n',
                                  'PROGRESS.txt': '[2026-10-20 10:00:00] campaign c 启动: configs=[a] tracks=geo rounds=1'})
        self.assertFalse(campaign.plan_known)
        self.assertEqual(campaign.pending, 0)

    def test_deadline_epoch_and_disabled_guard_override_log(self):
        log = '[2026-10-20 10:00:00] 窗底守卫: 12h（截止 2026-10-20 22:00:00 之前放不下）'
        disabled = self.campaign(**{'PROGRESS.txt': log})
        self.assertEqual(disabled.deadline, '')
        epoch = 1792454400
        active = self.campaign(**{'DEADLINE': str(epoch), 'PROGRESS.txt': log})
        import datetime
        expected = datetime.datetime.fromtimestamp(epoch, datetime.timezone(datetime.timedelta(hours=8)))
        self.assertEqual(active.deadline, expected.strftime('%Y-%m-%d %H:%M:%S'))


class TestSidecarVectorContract(NoExternalCalls):
    SPEC = '''{"mappings":{"properties":{"emb":{"type":"dense_vector","dims":1024,
              "index_options":{"type":"{{vector_index_type | default('hnsw')}}",
              "m":32,"ef_construction":100}}}}}'''

    def test_default_and_override_rendered_without_jinja(self):
        for params, expected in (({}, 'hnsw'), ({'vector_index_type': 'int8_hnsw'}, 'int8_hnsw')):
            with self.subTest(params=params):
                result = S.parse_knn_params(self.SPEC, {}, track_params=params)
                self.assertEqual(result['index_type'], expected)
                self.assertNotIn('{{', json.dumps(result))
        unknown = S.parse_knn_params(self.SPEC.replace(" | default('hnsw')", ''), {})
        self.assertNotIn('index_type', unknown)

    def test_mapping_wins_over_conflicting_declared_type_and_parameters(self):
        mapping = {'vectors': {'mappings': {'properties': {'emb': {'type': 'dense_vector',
                   'dims': 1024, 'element_type': 'float',
                   'index_options': {'type': 'bbq_hnsw', 'm': 16, 'ef_construction': 200}}}}}}
        result = S.parse_knn_params(self.SPEC, {}, track_params={'vector_index_type': 'int8_hnsw'},
                                    mapping_text=json.dumps(mapping))
        self.assertEqual((result['index_type'], result['m'], result['ef_construction']),
                         ('bbq_hnsw', 16, 200))
        self.assertEqual(result['index_params_source'], 'index mapping measured')

    def test_track_meta_reads_actual_template_and_passed_params(self):
        result = S.track_meta(str(ROOT / 'tracks' / 'hv1024x'), 'vector_index_type:int8_hnsw')
        self.assertEqual(result['knn_params']['index_type'], 'int8_hnsw')

    def test_shards_and_replicas_use_measured_settings_then_rendered_spec(self):
        track = str(ROOT / 'tracks' / 'hv1024x')
        declared = S.track_meta(track, '')
        self.assertEqual((declared['shards'], declared['replicas']), (3, 0))
        measured = S.track_meta(track, 'number_of_shards:5', settings_text=json.dumps({
            'vectors': {'settings': {'index.number_of_shards': '2', 'index.number_of_replicas': '1'}}}))
        self.assertEqual((measured['shards'], measured['replicas']), (2, 1))
        missing = S.track_meta(None, '')
        self.assertIsNone(missing['shards'])
        self.assertIsNone(missing['replicas'])

    def test_measured_opensearch_disk_mode_is_preserved(self):
        measured = {'vectors': {'mappings': {'properties': {'emb': {'type': 'knn_vector',
                    'dimension': 1024, 'mode': 'on_disk', 'compression_level': '32x',
                    'method': {'name': 'hnsw', 'engine': 'faiss'}}}}}}
        params = S.mapping_knn_params(json.dumps(measured))
        self.assertEqual((params['mode'], params['compression_level']), ('on_disk', '32x'))

    def test_actual_task_fragments_keep_each_budget_and_k(self):
        params = S.track_meta(str(ROOT / 'tracks' / 'hv1024x-os'), '')['knn_params']
        self.assertEqual(params['search_budget_by_task']['knn-search-10-100'],
                         {'type': 'ef_search', 'value': 100})
        self.assertEqual(params['search_budget_by_task']['knn-search-100-1000'],
                         {'type': 'ef_search', 'value': 1000})
        self.assertEqual(params['k_by_task'], {'knn-search-10-100': 10, 'knn-search-100-1000': 100})
        self.assertNotIn('ef_search', params)
        self.assertNotIn('num_candidates', params)

    def test_runtime_symlink_and_oversized_artifacts_are_not_read_as_specs(self):
        # prepare_tracks 会把语料软链进 track 目录（documents-*.json → GB 级语料）。
        # track_meta 只认 git 定版定义：软链与超限运行时产物不得整读，
        # 否则在压测机上会把 22.7GB 语料读进内存，sidecar 当场挂死。
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / 'index.json').write_text(
            '{"mappings":{"properties":{"emb":{"type":"dense_vector","dims":1024}}}}', encoding='utf-8')
        corpus = d / 'documents-1024.json'
        corpus.write_bytes(b'x' * 4096)
        link = d / 'documents-1024-link.json'
        os.symlink(str(corpus), str(link))
        real_read = S.read

        def reject_runtime_artifacts(raw, name):
            p = os.path.join(raw, name)
            if os.path.islink(p) or (os.path.isfile(p) and os.path.getsize(p) > S.SPEC_MAX_BYTES):
                raise AssertionError('运行时产物不得整读: ' + p)
            return real_read(raw, name)

        with patch.object(S, 'read', reject_runtime_artifacts), \
                patch.object(S, 'SPEC_MAX_BYTES', 1024):
            meta = S.track_meta(str(d), '')
        self.assertEqual(meta['knn_params']['dims'], 1024)

    def test_opensearch_ef_search_is_not_num_candidates(self):
        spec = '''{"mappings":{"properties":{"emb":{"type":"knn_vector","dimension":1024,
                   "method":{"name":"hnsw","engine":"lucene",
                   "parameters":{"m":32,"ef_construction":100}}}}}}'''
        operation = {'knn.json': '{"name":"knn", "ef-search":1000,"k":100}'}
        result = S.parse_knn_params(spec, operation)
        self.assertEqual((result['index_type'], result['engine']), ('hnsw', 'lucene'))
        self.assertEqual(result['ef_search'], 1000)
        self.assertEqual(result['search_budget_type'], 'ef_search')
        self.assertNotIn('num_candidates', result)


if __name__ == '__main__':
    unittest.main()
