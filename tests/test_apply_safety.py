# -*- coding: utf-8 -*-
"""入库 main 的离线安全回归：post 全部为内存桩，不接 ES。"""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
A = importlib.import_module('bench-apply')
B = importlib.import_module('build-race-store')
REVIEW_TEMP = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'Temp' / 'raceway-review' / 'export-identity'
if os.name != 'nt':
    REVIEW_TEMP = Path(tempfile.gettempdir()) / 'raceway-review' / 'export-identity'


class TestApplySafety(unittest.TestCase):
    def setUp(self):
        REVIEW_TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='apply-', dir=REVIEW_TEMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.calls = []
        self.idx = 'benchmark-result-env'
        self.path = self.base / (self.idx + '.jsonl')
        self.templates = {self.idx: {'index_patterns': [self.idx], 'template': {'mappings': {
            'dynamic': 'strict', 'properties': {'env_id': {'type': 'keyword'}, 'notes': {'type': 'keyword'}, 'vcpu': {'type': 'integer'}}}}}}
        self.save_templates()
        self.save_docs([{'_index': self.idx, '_id': 'env-a', 'env_id': 'env-a', 'notes': '中文环境'}])

    def save_templates(self):
        (self.base / 'templates.json').write_text(json.dumps(self.templates, ensure_ascii=False), encoding='utf-8')

    def save_docs(self, docs):
        self.path.write_text(''.join(json.dumps(d, ensure_ascii=False) + '\n' for d in docs), encoding='utf-8')

    def post(self, url, body, auth, insecure, ctype='application/x-ndjson', method='POST'):
        self.calls.append((method, url, body))
        if '/_bulk' in url:
            lines = body.splitlines()
            return {'items': [{'index': {'status': 201}} for _ in range(len(lines)//2)]}
        return {'acknowledged': True}

    def run_main(self, *extra, post=None):
        argv = ['bench-apply.py', '--dir', str(self.base), '--es', 'http://offline.invalid', '--only', self.idx, '--drop'] + list(extra)
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(A, 'post', side_effect=post or self.post), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return A.main()

    def assert_rejected_without_requests(self, *extra):
        self.assertNotEqual(self.run_main(*extra), 0)
        self.assertEqual(self.calls, [])

    def test_valid_preflight_then_drop_put_bulk_refresh(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual([method for method, _, _ in self.calls], ['DELETE', 'PUT', 'POST', 'POST'])
        bulk = next(body for _, url, body in self.calls if '/_bulk' in url)
        self.assertIn('中文环境', bulk)

    def test_missing_dir_or_file_does_not_delete(self):
        self.assert_rejected_without_requests('--dir', str(self.base / 'missing'))
        self.path.unlink()
        self.assert_rejected_without_requests()

    def test_empty_selected_input_never_deletes(self):
        self.path.write_text('', encoding='utf-8')
        self.assert_rejected_without_requests()

    def test_unknown_only_duplicate_only_or_bad_batch_fails_closed(self):
        for args in [('--only', 'oops'), ('--only', ''), ('--only', self.idx + ',' + self.idx), ('--batch', '0'), ('--batch', '-1')]:
            with self.subTest(args=args):
                self.assert_rejected_without_requests(*args)

    def test_invalid_templates_do_not_delete(self):
        for change in ['missing', 'wildcard', 'not-strict', 'bad-field']:
            with self.subTest(change=change):
                original = json.loads(json.dumps(self.templates))
                if change == 'missing':
                    self.templates = {}
                elif change == 'wildcard':
                    self.templates[self.idx]['index_patterns'] = ['*']
                elif change == 'not-strict':
                    self.templates[self.idx]['template']['mappings']['dynamic'] = True
                else:
                    self.templates[self.idx]['template']['mappings']['properties']['vcpu']['type'] = 'bogus'
                self.save_templates()
                self.assert_rejected_without_requests()
                self.templates = original

    def test_jsonl_syntax_ids_index_mapping_nan_utf8_rejected_before_drop(self):
        valid = {'_index': self.idx, '_id': 'env-a', 'env_id': 'env-a'}
        variants = [
            [dict(valid, _id='')], [dict(valid, _id=12)], [dict(valid, _id='x'*513)],
            [valid, valid], [dict(valid, _index='benchmark-result-race')],
            [dict(valid, vcpu='2')], [dict(valid, vcpu=float('nan'))],
            [dict(valid, vcpu=float('inf'))], [dict(valid, unexpected=3)],
        ]
        for docs in variants:
            with self.subTest(docs=docs):
                self.save_docs(docs)
                self.assert_rejected_without_requests()
        for text in ['{"_id":', '{"_id":"a","_id":"b"}', '[]']:
            self.path.write_text(text, encoding='utf-8')
            self.assert_rejected_without_requests()
        self.path.write_bytes(b'\xff')
        self.assert_rejected_without_requests()

    def test_later_selected_file_invalid_prevents_all_deletes(self):
        self.templates['benchmark-result-race'] = {'index_patterns': ['benchmark-result-race'], 'template': {'mappings': {'dynamic': 'strict', 'properties': {'race_id': {'type': 'keyword'}}}}}
        self.save_templates()
        (self.base / 'benchmark-result-race.jsonl').write_text('{bad', encoding='utf-8')
        self.assert_rejected_without_requests('--only', self.idx + ',benchmark-result-race')

    def test_delete_http_failure_aborts_before_put_and_bulk(self):
        def fail(*args, **kwargs):
            self.calls.append(('DELETE', args[0], args[1]))
            raise urllib.error.HTTPError(args[0], 403, 'forbidden', {}, None)
        self.assertEqual(self.run_main(post=fail), 1)
        self.assertEqual(len(self.calls), 1)

    def test_delete_404_can_continue(self):
        def missing(url, body, auth, insecure, ctype='application/x-ndjson', method='POST'):
            if method == 'DELETE':
                self.calls.append((method, url, body))
                raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
            return self.post(url, body, auth, insecure, ctype, method)
        self.assertEqual(self.run_main(post=missing), 0)
        self.assertEqual(self.calls[1][0], 'PUT')

    def test_delete_uses_prevalidated_snapshot_not_changed_files(self):
        def mutate(url, body, auth, insecure, ctype='application/x-ndjson', method='POST'):
            if method == 'DELETE':
                self.path.write_text('{replaced invalid file', encoding='utf-8')
                (self.base / 'templates.json').write_text('{}', encoding='utf-8')
            return self.post(url, body, auth, insecure, ctype, method)
        self.assertEqual(self.run_main(post=mutate), 0)
        self.assertIn('中文环境', next(body for _, url, body in self.calls if '/_bulk' in url))

    def test_unacknowledged_delete_aborts(self):
        self.assertEqual(self.run_main(post=lambda *a, **k: {'acknowledged': False}), 1)

    def test_manifest_counts_must_match_selected_payload(self):
        for count in (0, 2, None, True, '1'):
            with self.subTest(count=count):
                (self.base / 'manifest.json').write_text(json.dumps({'assertions': {'status': 'pass'}, 'bench_env': count}), encoding='utf-8')
                self.assert_rejected_without_requests()
        (self.base / 'manifest.json').write_text(json.dumps({'assertions': {'status': 'pass'}, 'bench_env': 1}), encoding='utf-8')
        self.assertEqual(self.run_main(), 0)

    def test_selected_race_metric_must_both_be_nonempty(self):
        for idx in ('benchmark-result-race', 'benchmark-result-metric'):
            self.templates[idx] = {'index_patterns': [idx], 'template': {'mappings': {'dynamic': 'strict', 'properties': {'race_id': {'type': 'keyword'}}}}}
            (self.base / (idx + '.jsonl')).write_text('', encoding='utf-8')
        self.save_templates()
        self.assert_rejected_without_requests('--only', ','.join(A.IDX))

    def test_failed_manifest_blocks_drop(self):
        (self.base / 'manifest.json').write_text('{"assertions":{"status":"fail"}}', encoding='utf-8')
        self.assert_rejected_without_requests()


class TestRaceStoreSafety(unittest.TestCase):
    def setUp(self):
        REVIEW_TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='store-', dir=REVIEW_TEMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.campaigns = self.base / 'campaigns'
        self.results = self.campaigns / 'review' / 'results'
        self.results.mkdir(parents=True)
        self.out = self.base / 'store'

    def source(self, name, rid):
        (self.results / name).write_text(json.dumps({'race-id': rid, 'notes': '中文'}), encoding='utf-8')

    def test_unsafe_id_and_duplicate_id_rejected(self):
        self.source('a-race.json', '../escape')
        with self.assertRaises(ValueError):
            B.collect_sources(str(self.campaigns))
        self.source('a-race.json', 'same')
        self.source('b-race.json', 'same')
        with self.assertRaises(ValueError):
            B.collect_sources(str(self.campaigns))

    def test_broken_input_does_not_clear_existing_output(self):
        self.out.mkdir()
        sentinel = self.out / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            B.build_store(str(self.campaigns), str(self.out), True)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
        self.source('a-race.json', 'a')
        with self.assertRaises(ValueError):
            B.build_store(str(self.campaigns), str(self.out), True)
        self.assertTrue(sentinel.exists())

    def test_symlink_failure_leaves_original_output(self):
        self.source('a-race.json', 'a')
        self.out.mkdir()
        with mock.patch.object(B.os, 'symlink', side_effect=OSError('permission denied')):
            with self.assertRaises(OSError):
                B.build_store(str(self.campaigns), str(self.out), True)
        self.assertTrue(self.out.is_dir())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_rebuild_refuses_source_overlap(self):
        self.source('a-race.json', 'a')
        with self.assertRaises(ValueError):
            B.build_store(str(self.campaigns), str(self.campaigns), True)


if __name__ == '__main__':
    unittest.main()
