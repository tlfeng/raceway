#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compose 安全契约回归：所有远端/进程调用均 mock，不运行 runner 或接 ES。"""
import contextlib
import dataclasses
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
C = importlib.import_module('bench-compose')
H = importlib.import_module('bench-history')
CARS = 'a ENGINE_NAME=easysearch ENGINE_BUILD=3015 CAR_LABEL=a\nb ENGINE_NAME=easysearch ENGINE_BUILD=3015 CAR_LABEL=b\n'
REVIEW_TEMP = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'Temp' / 'raceway-review' / 'compose'


def state():
    return types.SimpleNamespace(cars=tuple(C.parse_cars(CARS)),
                                 args=types.SimpleNamespace(root='/opt/raceway', ssh='review@never-connect',
                                                            ds_hours=0, camp_prefix=None),
                                 lock=threading.Lock(), history=lambda: H.Coverage(available=False))


def body(**changes):
    result = dict(cars=['a'], tracks=['geo'], rounds=1, recall='recall', ds_hours=2)
    result.update(changes)
    return result


def snapshot(**changes):
    return C.api_snapshot(state(), body(**changes))[0]


class NoExternalCalls(unittest.TestCase):
    def setUp(self):
        self.guards = contextlib.ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch.object(C, 'ssh_run', side_effect=AssertionError('禁止真实 SSH')))
        self.guards.enter_context(patch.object(subprocess, 'Popen', side_effect=AssertionError('禁止启动进程')))
        self.guards.enter_context(patch.object(H, 'fetch_coverage', side_effect=AssertionError('禁止 ES')))


class TestSnapshot(NoExternalCalls):
    def test_deterministic_frozen_snapshot(self):
        a, b = snapshot(), snapshot()
        self.assertEqual(a, b)
        self.assertEqual(a.digest, b.digest)
        self.assertIsInstance(a.cars, tuple)
        self.assertIsInstance(a.rows, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.tag = 'changed'
        self.assertNotEqual(a.digest, snapshot(ds_hours=3).digest)
        self.assertNotEqual(a.digest, snapshot(recall='norecall').digest)
        self.assertNotEqual(a.digest, snapshot(camp_prefix='different').digest)

    def test_empty_unknown_duplicate_cars_and_bad_inputs_are_400(self):
        for values in (dict(cars=[]), dict(cars=['missing']), dict(cars=['a', 'missing']),
                       dict(cars=['a', 'a']), dict(cars='a'), dict(tracks=['missing']),
                       dict(tracks=['geo', 'geo']), dict(rounds=0), dict(rounds=1.5),
                       dict(ds_hours=-1), dict(ds_hours=2.5), dict(est='geo=0'),
                       dict(recall='wrong'), dict(include_covered='false'), dict(tag='../outside'), dict(tag='a\\b'),
                       dict(tag='a\n'), dict(tag='/absolute'), dict(tag='x;id')):
            with self.subTest(values=values):
                self.assertEqual(C.api_render(state(), body(**values))[0], 400)

    def test_digest_mismatch_rejected_before_upload(self):
        st = state()
        with patch.object(C, 'upload') as upload:
            code, _ = C.api_start(st, body(digest='wrong'))
        self.assertEqual(code, 400)
        upload.assert_not_called()

    def test_same_snapshot_for_upload_dryrun_and_start_despite_concurrent_render(self):
        st = state()
        seen = []
        started = threading.Event()
        finished = threading.Event()
        def other_request():
            self.assertTrue(started.wait(2))
            self.assertEqual(C.api_render(st, body(cars=['b'], tracks=['nested']))[0], 200)
            finished.set()
        worker = threading.Thread(target=other_request)
        worker.start()
        def dry(args, snap):
            seen.append(snap)
            started.set()
            self.assertTrue(finished.wait(2))
            return 0, 'ok'
        with patch.object(C, 'remote_mutex_check', return_value=None), \
                patch.object(C, 'upload', side_effect=lambda a, s: seen.append(s)), \
                patch.object(C, 'remote_dry_run', side_effect=dry), \
                patch.object(C, 'remote_start', side_effect=lambda a, s: (seen.append(s) or ('camp', 'log'))):
            code, data = C.api_start(st, body())
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(code, 200)
        self.assertTrue(all(s is seen[0] for s in seen))
        self.assertEqual(seen[0].cars[0].name, 'a')
        self.assertEqual(data['digest'], seen[0].digest)

    def test_preview_start_reuse_digest_and_payload(self):
        st = state()
        seen = []
        with patch.object(C, 'remote_mutex_check', return_value=None), \
                patch.object(C, 'upload', side_effect=lambda a, s: seen.append(s)), \
                patch.object(C, 'remote_dry_run', return_value=(0, 'ok')), \
                patch.object(C, 'remote_start', return_value=('camp', 'log')):
            code, preview = C.api_preview(st, body())
            self.assertEqual(code, 200)
            code, started = C.api_start(st, body(digest=preview['digest']))
        self.assertEqual(code, 200)
        self.assertEqual(started['digest'], preview['digest'])
        self.assertEqual(seen[0].payload(), seen[1].payload())

    def test_dryrun_failure_blocks_start(self):
        with patch.object(C, 'remote_mutex_check', return_value=None), \
                patch.object(C, 'upload'), patch.object(C, 'remote_dry_run', return_value=(7, 'bad')), \
                patch.object(C, 'remote_start') as start:
            self.assertEqual(C.api_start(state(), body())[0], 400)
        start.assert_not_called()

    def test_cli_yes_is_checked_before_work(self):
        with patch.object(C, 'compose_all') as compose:
            with self.assertRaises(SystemExit):
                C.cmd_start(types.SimpleNamespace(yes=False))
        compose.assert_not_called()


class TestRemoteContract(NoExternalCalls):
    def test_transport_failure_is_closed_even_with_success_marker(self):
        with patch.object(C, 'ssh_run', return_value=(255, 'STARTED\n')):
            self.assertIn('拒绝起跑', C.remote_mutex_check(state().args))
            with self.assertRaises(SystemExit):
                C.remote_operation(state().args, 'launch', snapshot().payload())
        for error in (OSError('offline'), subprocess.TimeoutExpired('ssh', 1)):
            with patch.object(C, 'ssh_run', side_effect=error):
                self.assertIn('拒绝起跑', C.remote_mutex_check(state().args))

    def test_windows_remote_root_rejected_and_payload_is_stdin(self):
        args = state().args
        args.root = 'C:\\wrong'
        with self.assertRaises(SystemExit):
            C.remote_operation(args, 'inspect')
        args.root = "/opt/race way's"
        captured = []
        def call(target, cmd, **kw):
            captured.append((cmd, kw))
            return 0, json.dumps({'digest': snapshot().digest})
        with patch.object(C, 'ssh_run', side_effect=call):
            C.upload(args, snapshot())
        cmd, kw = captured[0]
        self.assertEqual(json.loads(kw['input_text']), snapshot().payload())
        self.assertNotIn(CARS, cmd)
        self.assertIn('source ./vm-bench.env', cmd)

    def test_start_checks_exact_campaign_pid_and_digest(self):
        snap = snapshot()
        def reply(args, mode, payload=None, **kw):
            self.assertEqual(mode, 'launch')
            self.assertEqual(payload['digest'], snap.digest)
            return dict(campaign=payload['cid'], digest=snap.digest, pid=4321,
                        results_root='/data/results', log='actual.log')
        with patch.object(C, 'remote_mutex_check', return_value=None), \
                patch.object(C, 'remote_operation', side_effect=reply):
            cid, log = C.remote_start(state().args, snap)
        self.assertTrue(cid.startswith('jdk3-'))
        self.assertEqual(log, 'actual.log')
        with patch.object(C, 'remote_mutex_check', return_value=None), \
                patch.object(C, 'remote_operation', return_value={'campaign':'wrong', 'pid':4321}):
            with self.assertRaises(SystemExit):
                C.remote_start(state().args, snap)

    def test_dryrun_uses_exact_snapshot_parameters(self):
        snap = snapshot(ds_hours=9, recall='norecall')
        with patch.object(C, 'remote_operation', return_value={'digest':snap.digest}), \
                patch.object(C, 'ssh_run', return_value=(0, 'ok')) as run:
            self.assertEqual(C.remote_dry_run(state().args, snap), (0, 'ok'))
        cmd = run.call_args.args[1]
        self.assertIn('DS_HOURS=9', cmd)
        self.assertIn(snap.digest, cmd)
        self.assertIn('schedules/' + snap.tag + '.conf', cmd)
        self.assertIn('--dry-run geo', cmd)


class TestRemoteHelper(NoExternalCalls):
    """在受控临时目录直接执行辅助程序；fcntl、进程和/proc以mock替代。"""
    def setUp(self):
        super().setUp()
        REVIEW_TEMP.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='safety-', dir=REVIEW_TEMP)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.results = self.root / 'custom-results'

    def helper(self, mode, payload, process=None, fail_lock=False, proc=None):
        fake_lock = types.ModuleType('fcntl')
        fake_lock.LOCK_EX, fake_lock.LOCK_NB = 2, 4
        def flock(*args):
            if fail_lock:
                raise BlockingIOError('busy')
        fake_lock.flock = flock
        original_open = os.open
        original_text, original_bytes = Path.read_text, Path.read_bytes
        def portable_open(path, flags, *args, **kwargs):
            if os.name == 'nt' and isinstance(path, (str, bytes, os.PathLike)) and Path(path).is_dir():
                return original_open(os.devnull, os.O_RDONLY)
            return original_open(path, flags, *args, **kwargs)
        def read_text(path, *args, **kwargs):
            proc_path = str(path).replace('\\', '/')
            if proc and proc_path.startswith('/proc/'):
                return proc[proc_path]
            return original_text(path, *args, **kwargs)
        def read_bytes(path, *args, **kwargs):
            proc_path = str(path).replace('\\', '/')
            if proc and proc_path.startswith('/proc/'):
                return proc[proc_path]
            return original_bytes(path, *args, **kwargs)
        out = io.StringIO()
        with patch.dict(sys.modules, {'fcntl':fake_lock}), \
                patch.dict(os.environ, {'RESULTS_ROOT':str(self.results)}), \
                patch.object(sys, 'argv', ['helper', mode]), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(Path, 'cwd', return_value=self.root), \
                patch.object(os, 'O_NOFOLLOW', getattr(os, 'O_NOFOLLOW', 0), create=True), \
                patch.object(os, 'open', side_effect=portable_open), \
                patch.object(os, 'chmod'), patch.object(os, 'fsync'), \
                patch.object(Path, 'read_text', read_text), patch.object(Path, 'read_bytes', read_bytes), \
                contextlib.redirect_stdout(out):
            with contextlib.ExitStack() as extra:
                if process is not None:
                    extra.enter_context(patch.object(subprocess, 'Popen', return_value=process))
                try:
                    exec(compile(C._REMOTE, '<compose-helper>', 'exec'), {'__name__':'__main__'})
                    code = 0
                except SystemExit as e:
                    code = e.code
        return code, json.loads(out.getvalue())

    def test_upload_same_content_is_idempotent_conflict_never_overwrites(self):
        snap = snapshot(tag='safe')
        self.assertEqual(self.helper('upload', snap.payload())[0], 0)
        path = self.root / 'schedules' / 'safe.conf'
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.helper('upload', snap.payload())[0], 0)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertNotEqual(self.helper('upload', snapshot(tag='safe', rounds=2).payload())[0], 0)
        self.assertEqual(path.read_bytes(), before)
        path.write_text('tampered', encoding='utf-8')
        self.assertNotEqual(self.helper('verify', snap.payload())[0], 0)

    def test_upload_traversal_and_partial_files_rejected(self):
        p = snapshot().payload()
        p['tag'] = '../outside'
        self.assertNotEqual(self.helper('upload', p)[0], 0)
        self.assertFalse((self.root / 'outside.conf').exists())
        d = self.root / 'schedules'
        d.mkdir(exist_ok=True)
        (d / 'partial.conf').write_text('untouched')
        self.assertNotEqual(self.helper('upload', snapshot(tag='partial').payload())[0], 0)
        self.assertEqual((d / 'partial.conf').read_text(), 'untouched')
        self.assertFalse((d / 'partial-cars.conf').exists())

    def test_results_root_and_busy_lock(self):
        self.assertEqual(self.helper('inspect', {})[1]['results_root'], str(self.results.resolve()))
        self.assertNotEqual(self.helper('mutex', {}, fail_lock=True)[0], 0)
        self.assertEqual(self.helper('mutex', {})[0], 0)

    def test_launch_early_exit_is_failure(self):
        snap = snapshot()
        self.assertEqual(self.helper('upload', snap.payload())[0], 0)
        fake = types.SimpleNamespace(pid=4321, returncode=7, poll=lambda:7)
        code, data = self.helper('launch', dict(snap.payload(), cid='exact'), process=fake)
        self.assertNotEqual(code, 0)
        self.assertIn('提前退出', data['error'])

    def test_launch_meta_mismatch_is_failure(self):
        snap = snapshot()
        self.assertEqual(self.helper('upload', snap.payload())[0], 0)
        camp = self.results / 'wrong-meta'
        fake = types.SimpleNamespace(pid=4321, poll=lambda:None)
        def spawn(*a, **kw):
            camp.mkdir(parents=True)
            (camp / 'RUNNER.pid').write_text('9999\n')
            (camp / 'RUNNER.meta').write_text('9999\t99\tboot\t/script.sh\n')
            return fake
        proc={'/proc/4321/stat':'4321 (bash) ' + ' '.join(['S'] + ['0']*18 + ['99']),
              '/proc/sys/kernel/random/boot_id':'boot\n', '/proc/4321/cmdline':b'bash\0/script.sh\0'}
        with patch.object(subprocess, 'Popen', side_effect=spawn):
            code, data = self.helper('launch', dict(snap.payload(), cid='wrong-meta'), proc=proc)
        self.assertNotEqual(code, 0)
        self.assertIn('PID 不一致', data['error'])

    def test_launch_checks_meta_and_exact_campaign(self):
        snap = snapshot()
        self.assertEqual(self.helper('upload', snap.payload())[0], 0)
        camp = self.results / 'exact'
        fake = types.SimpleNamespace(pid=4321, poll=lambda:None)
        def spawn(*a, **kw):
            camp.mkdir(parents=True)
            (camp / 'RUNNER.pid').write_text('4321\n')
            (camp / 'RUNNER.meta').write_text('4321\t99\tboot\t' + os.path.abspath('snapshot-run-bench.sh') + '\n')
            return fake
        proc={'/proc/4321/stat':'4321 (bash) ' + ' '.join(['S'] + ['0']*18 + ['99']),
              '/proc/sys/kernel/random/boot_id':'boot\n',
              '/proc/4321/cmdline':b'bash\0' + os.fsencode(os.path.abspath('snapshot-run-bench.sh')) + b'\0geo\0'}
        with patch.object(subprocess, 'Popen', side_effect=spawn):
            code, data = self.helper('launch', dict(snap.payload(), cid='exact'), proc=proc)
        self.assertEqual(code, 0, data)
        self.assertEqual(data['campaign'], 'exact')
        self.assertEqual(data['pid'], 4321)


class TestServiceSafety(NoExternalCalls):
    def handler(self, headers):
        handler = object.__new__(C.Handler)
        handler.server = types.SimpleNamespace(server_address=('127.0.0.1', 8802),
                                              state=types.SimpleNamespace(csrf_token='secret'))
        handler.headers = headers
        return handler

    def test_host_origin_and_csrf(self):
        good={'Host':'127.0.0.1:8802', 'Origin':'http://127.0.0.1:8802',
              'X-Raceway-CSRF':'secret', 'Sec-Fetch-Site':'same-origin'}
        self.assertTrue(self.handler(good)._local_request(write=True))
        for changes in ({'Host':'evil.example:8802'}, {'Origin':'http://evil.example'},
                        {'Origin':'null'}, {'X-Raceway-CSRF':''}, {'X-Raceway-CSRF':'非ASCII'},
                        {'Sec-Fetch-Site':'cross-site'}):
            self.assertFalse(self.handler(dict(good, **changes))._local_request(write=True))
        self.assertFalse(self.handler({'Host':'127.0.0.1:8802'})._local_request(write=True))

    def test_bind_is_local_and_bootstrap_omits_secrets(self):
        import inspect
        self.assertIn("ThreadingHTTPServer(('127.0.0.1', args.port)", inspect.getsource(C.cmd_serve))
        st = state()
        st.problems = []
        st.args.cars_file = '/cars.conf'
        st.args.kibana_url = 'http://127.0.0.1:5601'
        st.args.es_auth = 'admin:do-not-leak'
        st.csrf_token = 'token'
        st.src = types.SimpleNamespace(root='/custom/results')
        captured = []
        h = self.handler({'Host':'127.0.0.1:8802'})
        h._send = lambda code, kind, data: captured.append(json.loads(data))
        h._route_get('/api/bootstrap', {}, st)
        self.assertEqual(captured[0]['csrf_token'], 'token')
        self.assertNotIn('config', captured[0]['cars'][0])
        self.assertNotIn('do-not-leak', json.dumps(captured))

    def test_post_rejects_cross_site_before_dispatch(self):
        h = self.handler({'Host':'127.0.0.1:8802', 'Origin':'https://evil.example'})
        h.path = '/api/start'
        h.rfile = io.BytesIO(b'{}')
        calls = []
        h._send = lambda *args: calls.append(args)
        with patch.object(C, 'api_start') as start:
            h.do_POST()
        start.assert_not_called()
        self.assertEqual(calls[0][0], 403)

    def test_bundle_embeds_all_local_dependencies(self):
        import builtins
        sources = {'identity.py':'A=1', 'result_selection.py':'B=2', 'car-compare.py':'C=3'}
        def source(path, **kwargs):
            return io.StringIO(sources[os.path.basename(path)])
        with patch.object(builtins, 'open', side_effect=source):
            bundle = C.comparison_bundle()
        compile(bundle, '<bundle>', 'exec')
        for name in ('identity', 'result_selection', 'car-compare'):
            self.assertIn(name, bundle)
        self.assertIn('sys.modules[name] = module', bundle)

    def test_cid_rejects_trailing_newline(self):
        self.assertIsNotNone(C.check_cid('good\n'))

    def test_comparison_bundle_is_self_contained_and_uses_exact_pools(self):
        st = state()
        st.src = types.SimpleNamespace(root='/data/custom-results')
        with patch.object(C, 'comparison_bundle', return_value='bundle'), \
                patch.object(C, 'ssh_run', return_value=(0, 'comparison')) as run:
            code, data = C.api_compare(st, dict(a='old', b='new', car_a='a', car_b='a'))
        self.assertEqual(code, 200)
        command = run.call_args.args[1]
        self.assertIn('--a-campaign /data/custom-results/old', command)
        self.assertIn('--b-campaign /data/custom-results/new', command)
        self.assertEqual(run.call_args.kwargs['input_text'], 'bundle')


class TestTrend(NoExternalCalls):
    def _st(self, es_auth=None):
        st = state()
        st.args.es_auth = es_auth
        return st

    def test_no_auth_returns_available_false_without_touching_es(self):
        with patch.dict(os.environ, {'RACEWAY_ES_AUTH': ''}):
            code, payload = C.api_trend(self._st(None), {'car': ['jdk25'], 'track': ['geo']})
        self.assertEqual(code, 200)
        self.assertFalse(payload['available'])
        self.assertIn('口令', payload['error'])

    def test_car_and_track_required(self):
        st = self._st('u:p')
        for q in ({}, {'car': ['jdk25']}, {'track': ['geo']}):
            with self.subTest(q=q):
                self.assertEqual(C.api_trend(st, q)[0], 400)

    def test_injection_looking_params_are_400(self):
        st = self._st('u:p')
        for q in ({'car': ['a"b'], 'track': ['geo']},
                  {'car': ['jdk25'], 'track': ['geo'], 'task': ['x;rm']},
                  {'car': ['jdk25'], 'track': ['../../etc']}):
            with self.subTest(q=q):
                self.assertEqual(C.api_trend(st, q)[0], 400)

    def test_rows_grouped_by_series_variant_task_stat(self):
        st = self._st('u:p')
        resp = {'hits': {'hits': [
            {'_source': {'series_key': 'ez@2.5.0@3015', 'track_variant': 'geo', 'task': 'index-append',
                         'stat': 'median', 'value': 100.0, 'unit': 'docs/s', 'campaign': 'c1',
                         'direction': 'higher-better', 'run_start': '2026-11-11T10:00:00+08:00'}},
            {'_source': {'series_key': 'es@8.19', 'track_variant': 'geo', 'task': 'index-append',
                         'stat': 'median', 'value': 90.0, 'unit': 'docs/s', 'campaign': 'c1',
                         'direction': 'higher-better', 'run_start': '2026-11-11T10:05:00+08:00'}},
            {'_source': {'value': 'not-a-number'}},          # 非数值行丢弃，不进趋势
        ]}}
        seen = {}
        def fake_query(ssh, body, **kw):
            seen.update(body=body, path=kw.get('path', ''))
            return resp
        with patch.dict(os.environ, {'RACEWAY_ES_AUTH': 'u:p'}), \
                patch.object(H, 'es_query', side_effect=fake_query):
            code, payload = C.api_trend(st, {'car': ['jdk25'], 'track': ['geo'],
                                             'family': ['throughput'], 'stat': ['median']})
        self.assertEqual(code, 200)
        self.assertTrue(payload['available'])
        self.assertEqual(payload['n'], 2)
        self.assertEqual(payload['unit'], 'docs/s')
        keys = [s['key'] for s in payload['series']]
        self.assertEqual(len(keys), 2)
        self.assertTrue(any('ez@2.5.0@3015' in k for k in keys))
        # 查询体口径：只读 + trusted 过滤 + 指标库索引
        self.assertEqual(seen['body']['query']['bool']['filter'][2], {'term': {'trusted': True}})
        self.assertIn('benchmark-result-metric', seen['path'])

    def test_unavailable_store_reason_passes_through(self):
        st = self._st('u:p')
        with patch.dict(os.environ, {'RACEWAY_ES_AUTH': 'u:p'}), \
                patch.object(H, 'es_query', side_effect=H.HistoryUnavailable('指标库没起')):
            code, payload = C.api_trend(st, {'car': ['jdk25'], 'track': ['geo']})
        self.assertEqual(code, 200)
        self.assertFalse(payload['available'])
        self.assertIn('指标库没起', payload['error'])


class TestTracksExportsApply(NoExternalCalls):
    def test_tracks_returns_remote_listing(self):
        st = state()
        with patch.object(C, 'ssh_run', return_value=(0, '可用赛道: geo ...')) as run:
            code, payload = C.api_tracks(st)
        self.assertEqual(code, 200)
        self.assertIn('geo', payload['result'])
        self.assertIn('run-bench.sh list', run.call_args.args[1])

    def test_tracks_missing_script_is_400(self):
        st = state()
        with patch.object(C, 'ssh_run', return_value=(0, 'MISSING')):
            code, payload = C.api_tracks(st)
        self.assertEqual(code, 400)
        self.assertIn('run-bench.sh', payload['error'])

    def test_exports_parses_remote_json_last_line(self):
        st = state()
        with patch.object(C, 'ssh_run',
                          return_value=(0, '[{"tag":"t1","assertions":"pass",'
                                           '"counts":{"bench_metric":3},"mtime":1}]')):
            code, payload = C.api_exports(st)
        self.assertEqual(code, 200)
        self.assertEqual(payload['exports'][0]['tag'], 't1')

    def test_exports_unparsable_is_500(self):
        st = state()
        with patch.object(C, 'ssh_run', return_value=(0, 'Traceback ... 崩了')):
            code, _ = C.api_exports(st)
        self.assertEqual(code, 500)

    def test_apply_validates_tag_and_url_before_remote(self):
        st = state()
        for b in ({'export': '../x', 'es': 'https://127.0.0.1:9237'},
                  {'export': 't1', 'es': 'ftp://127.0.0.1'},
                  {'export': 't1'},
                  {'export': '', 'es': ''}):
            with self.subTest(b=b):
                self.assertEqual(C.api_apply(st, b)[0], 400)

    def test_apply_builds_command_with_flags_and_dir_under_exports(self):
        st = state()
        with patch.object(C, 'ssh_run', return_value=(0, '完成')) as run:
            code, _ = C.api_apply(st, {'export': 't1', 'es': 'https://127.0.0.1:9237',
                                       'auth': 'admin:pw', 'insecure': True, 'drop': True})
        self.assertEqual(code, 200)
        cmd = run.call_args.args[1]
        self.assertIn('--drop', cmd)
        self.assertIn('--insecure', cmd)
        self.assertIn('--auth admin:pw', cmd)
        self.assertIn('/exports/t1', cmd)
        self.assertTrue(run.call_args.kwargs.get('input_text', '').startswith('#!'))

    def test_apply_without_auth_and_flags_omits_them(self):
        st = state()
        with patch.object(C, 'ssh_run', return_value=(0, '完成')) as run:
            code, _ = C.api_apply(st, {'export': 't1', 'es': 'http://localhost:9200'})
        self.assertEqual(code, 200)
        cmd = run.call_args.args[1]
        self.assertNotIn('--auth', cmd)
        self.assertNotIn('--drop', cmd)
        self.assertNotIn('--insecure', cmd)


if __name__ == '__main__':
    unittest.main()
