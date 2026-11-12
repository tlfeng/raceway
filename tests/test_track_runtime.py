#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参数源运行时协议：只用合成语料、可审计桩，不依赖 esrally/集群。

运行：python -B -m unittest tests.test_track_runtime -v
搜索桩只发送 params['body']，不替参数源补 size；bulk 桩显式区分
created/updated/noop/document_missing，避免“请求成功”冒充真实更新。
"""
import ast
import copy
import io
import json
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TRACKS = ROOT / 'tracks'
ES_TRACKS = ('hv1024', 'hv1024x', 'hv1024x-bbqdisk')
OS_TRACKS = ('hv1024x-os', 'hv1024x-os-bbq', 'hv1024x-os-disk')


def render(text, params=None):
    """仅支持本测试涉及的标量/default/tojson/set/if，不执行任意 Jinja 代码。"""
    values = dict(params or {})
    missing = object()

    def expr(source):
        parts = [s.strip() for s in source.split('|')]
        token = parts[0]
        if token in ('true', 'false'):
            value = token == 'true'
        elif token in values:
            value = values[token]
        else:
            try:
                value = ast.literal_eval(token)
            except (ValueError, SyntaxError):
                value = missing
        for part in parts[1:]:
            if part.startswith('default(') and part.endswith(')'):
                if value is missing:
                    value = expr(part[8:-1])
            elif part == 'tojson':
                value = json.dumps(value)
            else:
                raise AssertionError('未支持的模板表达式: %s' % source)
        if value is missing:
            raise AssertionError('模板参数未定义: %s' % source)
        return value

    def assign(match):
        values[match[1]] = expr(match[2])
        return ''

    def condition(match):
        test = match[1].strip()
        enabled = test[:-11].strip() in values if test.endswith(' is defined') else bool(expr(test))
        return match[2] if enabled else ''

    text = re.sub(r'\{#.*?#\}', '', text, flags=re.S)
    text = re.sub(r'\{%\s*set\s+(\w+)\s*=\s*(.*?)\s*%\}', assign, text)
    text = re.sub(r'\{%\s*if\s+(.*?)\s*%\}(.*?)\{%\s*endif\s*%\}', condition, text, flags=re.S)
    text = re.sub(r'\{\{\s*(.*?)\s*\}\}', lambda m: str(expr(m[1])), text)
    if '{{' in text or '{%' in text:
        raise AssertionError('未处理的模板控制块')
    return text


def definitions(track, folder, params=None):
    result = []
    for path in sorted((TRACKS / track / folder).glob('*.json')):
        result.extend(json.loads('[' + render(path.read_text(encoding='utf-8'), params) + ']'))
    return {item['name']: item for item in result}


def load_track(name):
    path = TRACKS / name / 'track.py'
    namespace = {'__file__': str(path), '__name__': 'test_track_plugin'}
    stub = types.ModuleType('esrally')
    stub.exceptions = types.SimpleNamespace(SystemError=RuntimeError)
    with mock.patch.dict('sys.modules', {'esrally': stub}):
        exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
    return namespace


class SearchRunnerStub:
    """Rally search 的传输边界：只有 body 进入请求，外层 size 无效。"""
    @staticmethod
    def request(params):
        return copy.deepcopy(params['body'])


class BulkStoreStub:
    """最小 update/index 语义桩；真实请求与断言均由测试传入。"""
    def __init__(self):
        self.docs = {}

    def apply(self, requests):
        results = []
        for request in requests:
            lines = request['body']
            for i in range(0, len(lines), 2):
                action, meta = next(iter(json.loads(lines[i]).items()))
                body = json.loads(lines[i + 1])
                key = meta['_id']
                if action == 'index':
                    result = 'updated' if key in self.docs else 'created'
                    self.docs[key] = body
                elif key not in self.docs:
                    if not body.get('doc_as_upsert'):
                        raise KeyError('document_missing: %s' % key)
                    self.docs[key] = body['doc']
                    result = 'created'
                else:
                    merged = dict(self.docs[key], **body['doc'])
                    result = 'noop' if body.get('detect_noop', True) and merged == self.docs[key] else 'updated'
                    self.docs[key] = merged
                results.append((key, result))
        return results


class KnnRuntimeTest(unittest.TestCase):
    def source(self, name, params):
        cls = load_track(name)['KnnParamSource']
        self.vectors = [[1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022]
        queries = '\n'.join(json.dumps(v) for v in self.vectors)
        track = types.SimpleNamespace(indices=[types.SimpleNamespace(name='vectors')])
        with mock.patch('builtins.open', return_value=io.StringIO(queries)):
            return cls(track, params)

    def test_final_ann_body_uses_size_and_engine_budget(self):
        for name in ES_TRACKS + OS_TRACKS:
            for operation in definitions(name, 'operations').values():
                with self.subTest(track=name, operation=operation['name']):
                    params = self.source(name, operation).params()
                    body = SearchRunnerStub.request(params)
                    self.assertNotIn('size', params)
                    self.assertEqual(body['size'], operation['k'])
                    self.assertIs(body['_source'], False)
                    if name in ES_TRACKS:
                        knn = body['knn']
                        self.assertEqual(knn['num_candidates'], operation['num-candidates'])
                        self.assertEqual(knn['query_vector'], self.vectors[0])
                        self.assertNotIn('method_parameters', knn)
                    else:
                        knn = body['query']['knn']['emb']
                        self.assertNotIn('num-candidates', operation)
                        self.assertEqual(knn['vector'], self.vectors[0])
                        self.assertEqual(knn['method_parameters'], {'ef_search': operation['ef-search']})
                        if name.endswith('-disk'):
                            self.assertEqual(knn['rescore'], {'oversample_factor': 2.0})
                        else:
                            self.assertNotIn('rescore', knn)
                    self.assertEqual(knn['k'], body['size'])

    def test_exact_body_size_and_query_rotation(self):
        for name in ES_TRACKS + OS_TRACKS:
            with self.subTest(track=name):
                source = self.source(name, {'k': 100, 'exact': True})
                bodies = [SearchRunnerStub.request(source.params()) for _ in range(3)]
                self.assertEqual([b['size'] for b in bodies], [100, 100, 100])
                script = bodies[0]['query']['script_score']['script']
                field = "doc['emb']" if name in OS_TRACKS else "'emb'"
                self.assertIn(field, script['source'])
                self.assertEqual(bodies[0], bodies[2])
                self.assertNotEqual(bodies[0], bodies[1])
                self.assertNotIn('knn', bodies[0])

    def test_os_custom_budget_is_not_discarded(self):
        for name in OS_TRACKS:
            params = {'k': 17, 'ef-search': 321, 'oversample-factor': 3.0}
            body = SearchRunnerStub.request(self.source(name, params).params())
            knn = body['query']['knn']['emb']
            self.assertEqual(body['size'], 17)
            self.assertEqual(knn['method_parameters']['ef_search'], 321)
            self.assertEqual(knn['rescore']['oversample_factor'], 3.0)

    def test_os_disk_mapping_is_explicit_faiss_hnsw(self):
        mapping = json.loads((TRACKS / 'hv1024x-os-disk/index.json').read_text(encoding='utf-8'))
        emb = mapping['mappings']['properties']['emb']
        self.assertEqual(emb['mode'], 'on_disk')
        self.assertEqual(emb['compression_level'], '32x')
        self.assertEqual(emb['method'], {'name': 'hnsw', 'engine': 'faiss',
                                        'parameters': {'m': 32, 'ef_construction': 100}})


class UpsertRuntimeTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.gettempdir()) / 'raceway-review' / 'tracks-static'
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='runtime-', dir=root)
        self.addCleanup(temporary.cleanup)
        self.corpus = Path(temporary.name) / 'synthetic-documents.json'
        self.documents = [{'geonameid': i * 101, 'name': '地名%d' % i, 'population': i * 10}
                          for i in range(1, 12)]
        self.corpus.write_text(''.join(json.dumps(d, ensure_ascii=False) + '\n' for d in self.documents), encoding='utf-8')
        self.track = types.SimpleNamespace(indices=[types.SimpleNamespace(name='geonames-upsert')], corpora=[])
        self.cls = load_track('geonames-upsert')['UpsertParamSource']
        self.ops = definitions('geonames-upsert', 'operations',
                               {'bulk_size': 3, 'upsert_seed': 23, 'corpus_file': str(self.corpus)})

    def drain(self, operation, clients=3):
        source = self.cls(self.track, operation)
        sources, requests = [], []
        for index in range(clients):
            part = source.partition(index, clients)
            sources.append(part)
            for _ in range(100):
                try:
                    requests.append(part.params())
                except StopIteration:
                    break
            else:
                self.fail('参数源没有正常耗尽')
            with self.assertRaises(StopIteration):
                part.params()  # 耗尽后不能重新开始，再次覆盖。
            self.assertEqual(part.percent_completed, 1.0)
        return requests

    @staticmethod
    def pairs(requests):
        return [(json.loads(req['body'][i]), json.loads(req['body'][i + 1]))
                for req in requests for i in range(0, len(req['body']), 2)]

    def test_all_writes_use_same_bulk_size_and_custom_source(self):
        for operation in list(self.ops.values())[:5]:
            self.assertEqual(operation['bulk-size'], 3)
            self.assertEqual(operation['param-source'], 'upsert-source')
            self.assertEqual(operation['corpus-file'], str(self.corpus))
            self.assertEqual(operation['upsert-seed'], 23)
            self.assertNotIn('conflicts', operation)
        defaults = definitions('geonames-upsert', 'operations')
        self.assertEqual({op['bulk-size'] for op in list(defaults.values())[:5]}, {500})

    def test_cold_hot_and_native_use_identical_full_natural_id_distribution(self):
        upsert, native = BulkStoreStub(), BulkStoreStub()
        cold_requests = self.drain(self.ops['upsert-insert'])
        index_requests = self.drain(self.ops['index-append-ids'])
        cold = upsert.apply(cold_requests)
        index = native.apply(index_requests)
        self.assertEqual(cold, index)
        ids = [str(doc['geonameid']) for doc in self.documents]
        self.assertEqual([key for key, _ in cold], ids)
        self.assertEqual([result for _, result in cold], ['created'] * len(ids))
        self.assertEqual(upsert.docs, native.docs)
        self.assertEqual({d['raceway_revision'] for d in upsert.docs.values()}, {0})
        hot_requests = self.drain(self.ops['upsert-update'])
        update_requests = self.drain(self.ops['index-update-native'])
        hot = upsert.apply(hot_requests)
        update = native.apply(update_requests)
        self.assertEqual(hot, update)
        self.assertEqual(hot, [(key, 'updated') for key in ids])
        self.assertEqual(upsert.docs, native.docs)
        self.assertEqual({d['raceway_revision'] for d in upsert.docs.values()}, {1})
        for (_, hot_body), (_, native_body) in zip(self.pairs(hot_requests), self.pairs(update_requests)):
            self.assertIs(hot_body.pop('doc_as_upsert'), True)
            self.assertNotIn('doc_as_upsert', native_body)
            self.assertEqual(hot_body, native_body)
            self.assertIs(native_body['detect_noop'], False)
        # 同一 hot 任务再跑也必须强制更新，不依赖 revision 再次变化。
        self.assertTrue(all(result == 'updated' for _, result in upsert.apply(hot_requests)))
        with self.assertRaises(KeyError):
            BulkStoreStub().apply(update_requests)

    def test_mixed_is_reproducible_and_writes_revision_two(self):
        store = BulkStoreStub()
        store.apply(self.drain(self.ops['upsert-insert']))
        store.apply(self.drain(self.ops['upsert-update']))
        mixed = dict(self.ops['upsert-mixed'], **{'new-doc-ratio': 50})
        requests = self.drain(mixed)
        self.assertEqual(requests, self.drain(mixed))
        results = store.apply(requests)
        self.assertEqual(len(results), len(self.documents))
        self.assertEqual({result for _, result in results}, {'created', 'updated'})
        for (meta, body), (key, result) in zip(self.pairs(requests), results):
            base = body['doc']['geonameid']
            self.assertIn(int(key), (base, base + mixed['new-id-offset']))
            self.assertEqual(result, 'created' if int(key) != base else 'updated')
            self.assertEqual(store.docs[key]['raceway_revision'], 2)
            self.assertIs(body['detect_noop'], False)
        all_new = dict(mixed, **{'new-doc-ratio': 100})
        self.assertTrue(all(int(next(iter(meta.values()))['_id']) == body['doc']['geonameid'] + mixed['new-id-offset']
                            for meta, body in self.pairs(self.drain(all_new))))

    def test_partitions_and_percentage_cover_prefix_once(self):
        for clients in (1, 3, 5):
            for operation in ('upsert-insert', 'index-append-ids', 'index-update-native'):
                params = dict(self.ops[operation], **{'ingest-percentage': 70})
                requests = self.drain(params, clients)
                pairs = self.pairs(requests)
                ids = [next(iter(meta.values()))['_id'] for meta, _ in pairs]
                count = round(len(self.documents) * .7)
                self.assertEqual(ids, [str(d['geonameid']) for d in self.documents[:count]])
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(sum(req['bulk-size'] for req in requests), count)
                self.assertTrue(all(req['bulk-size'] <= 3 for req in requests))

    def test_challenges_preserve_rebuild_order_and_equal_clients(self):
        challenges = definitions('geonames-upsert', 'challenges', {'bulk_indexing_clients': 4})
        for name in ('full', 'upsert-flow', 'native-update'):
            tasks = challenges[name]['schedule']
            for task in tasks:
                if task.get('operation') in ('upsert-insert', 'upsert-update', 'upsert-mixed',
                                             'index-append-ids', 'index-update-native'):
                    self.assertEqual(task['clients'], 4)
            operations = [t['operation'] if isinstance(t['operation'], str)
                          else t['operation']['operation-type'] for t in tasks]
            if name == 'full':
                self.assertLess(operations.index('upsert-mixed'), operations.index('index-append-ids'))
                self.assertEqual(operations.count('delete-index'), 2)
                self.assertEqual(operations.count('create-index'), 2)
            if name in ('full', 'native-update'):
                self.assertLess(operations.index('index-append-ids'), operations.index('index-update-native'))

    def test_validation_and_runner_parameters(self):
        for invalid in ({'bulk-size': 0}, {'ingest-percentage': 101}, {'new-doc-ratio': -1},
                        {'action': 'update', 'new-doc-ratio': 25}):
            with self.assertRaises(RuntimeError):
                self.cls(self.track, dict(self.ops['upsert-insert'], **invalid))
        params = dict(self.ops['upsert-insert'], **{'detailed-results': True, 'request-timeout': 90})
        requests = self.drain(params)
        self.assertTrue(all(req['detailed-results'] and req['request-timeout'] == 90 for req in requests))
        self.corpus.write_text('{"name":"缺少ID"}\n', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.drain(self.ops['upsert-insert'], clients=1)


if __name__ == '__main__':
    unittest.main()
