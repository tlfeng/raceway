#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扩展功能 API 的契约测试：路径校验、报告、est 校准、监控时序、日志过滤。

只测纯函数与本地文件读取路径（不依赖 ssh / 远端），Go 后端 handler 与这些
Python 函数共用同一语义（Go 侧另有 features_test.go）。
"""
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import importlib
C = importlib.import_module('bench-compose')


def make_state(root, campaigns=None):
    args = types.SimpleNamespace(root=root, ssh=None, results_root=os.path.join(root, 'campaigns'),
                                 cars_file=os.path.join(root, 'nofile.conf'),
                                 es_auth=None, es_url=None, no_history=True, port=8899,
                                 kibana_url='', cars=None, no_open=True)
    for name in ('campaigns', 'tracks'):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    st = C.ComposeState(args)
    st.results_root = args.results_root
    st.src.root = args.results_root
    return st


def write_campaign(root, cid, *, plan=None, races=None, env=None, sidecar=None, monitor=None):
    d = os.path.join(root, 'campaigns', cid)
    os.makedirs(os.path.join(d, 'results'), exist_ok=True)
    os.makedirs(os.path.join(d, 'monitors'), exist_ok=True)
    if plan is not None:
        with open(os.path.join(d, 'PLAN.tsv'), 'w', encoding='utf-8') as f:
            f.write('fid\tconfig\ttrack\tround\trecall\test\n' + plan)
    if races is not None:
        with open(os.path.join(d, 'RACES.md'), 'w', encoding='utf-8') as f:
            f.write('| 场次 | 状态 | 赛前卡点 | 用时 |\n|---|---|---|---|\n' + races)
    if env is not None:
        with open(os.path.join(d, 'monitors', 'ENV-SUMMARY.csv'), 'w', encoding='utf-8') as f:
            f.write(env)
    if sidecar is not None:
        with open(os.path.join(d, 'results', 'a-geo-r1-wq-sidecar.json'), 'w', encoding='utf-8') as f:
            json.dump(sidecar, f)
    if monitor is not None:
        with open(os.path.join(d, 'monitors', 'a-geo-r1-n1.csv'), 'w', encoding='utf-8') as f:
            f.write(monitor)
    with open(os.path.join(d, 'PROGRESS.txt'), 'w', encoding='utf-8') as f:
        f.write('[t] campaign %s 启动: cars=[a] tracks=geo rounds=1\n' % cid)
        f.write('[t] a-geo-r1 完成: VALID\n')
    return d


ENV_HEADER = ('fid,phase,node,ip,samples,steal_avg,steal_max,foreign_avg,foreign_max,swapin,env_verdict\n')


class SafePathTests(unittest.TestCase):
    def test_track_rel_blocks_traversal(self):
        for bad in ('', '../x', 'a/../b', '/etc/passwd', 'a\\b', 'a/./b', 'a b', 'a\0b'):
            with self.assertRaises(ValueError, msg=bad):
                C.safe_track_rel(bad)
        self.assertEqual(C.safe_track_rel('geonames/index.json'), 'geonames/index.json')

    def test_camp_rel_blocks_non_text(self):
        for bad in ('../x', '/etc/passwd', 'a/../../b', 'x.exe'):
            with self.assertRaises(ValueError, msg=bad):
                C.safe_camp_rel(bad)
        self.assertEqual(C.safe_camp_rel('monitors/ENV-SUMMARY.csv'), 'monitors/ENV-SUMMARY.csv')


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_reads_wq_sidecar(self):
        write_campaign(self.root, 'sidecar-camp', sidecar={
            'engine': {'version': '2.5.0'}, 'jdk': {'version': '25'}, 'heap': '16g', 'cores': 32,
        })
        code, payload = C.api_sidecar(self.st, 'sidecar-camp', 'a-geo-r1')
        self.assertEqual(code, 200)
        self.assertEqual(len(payload['sidecars']), 1)
        self.assertEqual(payload['sidecars'][0]['phase'], 'wq')
        self.assertEqual(payload['sidecars'][0]['data']['jdk']['version'], '25')

    def test_bad_cid(self):
        code, payload = C.api_sidecar(self.st, '../evil', 'a-geo-r1')
        self.assertEqual(code, 400)


class RaceDetailTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_plan_races_progress(self):
        write_campaign(self.root, 'detail-camp',
                       plan='a-geo-r1\tk1\tgeo\t1\trecall\t45\n',
                       races='| a-geo-r1 | VALID | OK | 29min |\n')
        code, p = C.api_race_detail(self.st, 'detail-camp', 'a-geo-r1')
        self.assertEqual(code, 200)
        self.assertEqual(p['plan'][2], 'geo')
        self.assertTrue(any('VALID' in r for r in p['races']))
        self.assertTrue(any('a-geo-r1' in ln for ln in p['progress']))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_parses_5s_series(self):
        write_campaign(self.root, 'mon-camp',
                       env=ENV_HEADER + 'a-geo-r1,w,n1,1.1.1.1,2,0.1,0.2,0,0,0,OK\n',
                       monitor=('ts,cpu_usr,cpu_steal,bench_cpu,load1\n'
                                '100,12.5,0.1,30.0,2.1\n105,13.0,0.2,31.5,2.3\n'))
        code, p = C.api_monitor(self.st, 'mon-camp', 'a-geo-r1')
        self.assertEqual(code, 200)
        self.assertEqual(p['interval'], 5)
        self.assertEqual(len(p['series']), 1)
        self.assertEqual(p['series'][0]['node'], 'n1')
        self.assertEqual(p['series'][0]['rows'][1]['bench_cpu'], 31.5)


class LogfileTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_filter_and_tail(self):
        write_campaign(self.root, 'log-camp')
        with open(os.path.join(self.root, 'campaigns', 'log-camp', 'debug.log'), 'w', encoding='utf-8') as f:
            f.write('alpha OK\nbeta FAIL\ngamma OK\ndelta FAIL\n')
        code, p = C.api_logfile(self.st, 'log-camp', 'debug.log', 2, 'FAIL')
        self.assertEqual(code, 200)
        self.assertEqual(p['total'], 2)
        self.assertEqual(p['lines'], ['beta FAIL', 'delta FAIL'])

    def test_missing_file(self):
        write_campaign(self.root, 'log-camp')
        code, p = C.api_logfile(self.st, 'log-camp', 'nope.log', 10, '')
        self.assertEqual(code, 404)


class EstSuggestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_median_from_races(self):
        # 三轮不同 campaign 的实测时长 30/40/50 分钟 → 建议 40
        for i, mins in enumerate((30, 40, 50)):
            write_campaign(self.root, 'est-c%d' % i,
                           plan='a-geo-r1\tk1\tgeo\t1\trecall\t45\n',
                           races='| a-geo-r1 | VALID | OK | %dmin |\n' % mins)
        code, p = C.api_est_suggest(self.st, 'geo')
        self.assertEqual(code, 200)
        self.assertTrue(p['available'])
        self.assertEqual(p['suggest'], 40)
        self.assertEqual(p['n'], 3)

    def test_no_history(self):
        code, p = C.api_est_suggest(self.st, 'geo')
        self.assertEqual(code, 200)
        self.assertFalse(p['available'])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_markdown_report(self):
        write_campaign(self.root, 'rep-camp',
                       plan='a-geo-r1\tk1\tgeo\t1\trecall\t45\n',
                       races='| a-geo-r1 | VALID | OK | 29min |\n',
                       env=ENV_HEADER + 'a-geo-r1,q,n1,1.1.1.1,80,0.4,3.2,0,0,0,CONTAMINATED\n')
        code, p = C.api_report(self.st, 'rep-camp', 'md')
        self.assertEqual(code, 200)
        self.assertIn('raceway 跑分报告', p['text'])
        self.assertIn('CONTAMINATED', p['text'])
        code, p = C.api_report(self.st, 'rep-camp', 'html')
        self.assertEqual(code, 200)
        self.assertIn('<!doctype html>', p['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
