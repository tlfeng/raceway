#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-tools.py 命令行入口的契约测试。

验证新能力确实能从 shell 使用（CLI 与 Web/Go 复用同一套 api_* 逻辑）：
只读动作输出 JSON/文本、写动作落盘、参数守卫合理。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, 'bench-tools.py')


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'campaigns', 'camp1', 'monitors'), exist_ok=True)
        os.makedirs(os.path.join(self.root, 'campaigns', 'camp1', 'results'), exist_ok=True)
        os.makedirs(os.path.join(self.root, 'tracks', 'geo'), exist_ok=True)
        with open(os.path.join(self.root, 'run-bench.sh'), 'w') as f:
            f.write('#!/bin/bash\n')
        with open(os.path.join(self.root, 'vm-bench.env'), 'w') as f:
            f.write('RESULTS_ROOT=%s/campaigns\n' % self.root)
        # 最小 campaign
        d = os.path.join(self.root, 'campaigns', 'camp1')
        with open(os.path.join(d, 'PLAN.tsv'), 'w', encoding='utf-8') as f:
            f.write('fid\tconfig\ttrack\tround\trecall\test\na-geo-r1\tk1\tgeo\t1\trecall\t45\n')
        with open(os.path.join(d, 'RACES.md'), 'w', encoding='utf-8') as f:
            f.write('| 场次 | 状态 | 赛前卡点 | 用时 |\n|---|---|---|---|\n| a-geo-r1 | VALID | OK | 29min |\n')
        with open(os.path.join(d, 'PROGRESS.txt'), 'w', encoding='utf-8') as f:
            f.write('[t] a-geo-r1 完成: VALID\n')
        with open(os.path.join(d, 'results', 'a-geo-r1-wq-sidecar.json'), 'w', encoding='utf-8') as f:
            json.dump({'heap': '16g'}, f)
        with open(os.path.join(d, 'monitors', 'ENV-SUMMARY.csv'), 'w', encoding='utf-8') as f:
            f.write('fid,phase,node,ip,samples,steal_avg,steal_max,foreign_avg,foreign_max,swapin,env_verdict\n'
                    'a-geo-r1,w,n1,1.1.1.1,2,0.1,0.2,0,0,0,OK\n')
        with open(os.path.join(d, 'monitors', 'a-geo-r1-n1.csv'), 'w', encoding='utf-8') as f:
            f.write('ts,bench_cpu\n100,30.0\n105,31.5\n')
        with open(os.path.join(self.root, 'tracks', 'geo', 'index.json'), 'w', encoding='utf-8') as f:
            f.write('{"shards":5}')

    def run_tool(self, *args):
        env = dict(os.environ, PYTHONPATH=ROOT)
        return subprocess.run([sys.executable, TOOL, '--root', self.root] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_sidecar_json(self):
        p = self.run_tool('sidecar', 'camp1', 'a-geo-r1')
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data['sidecars'][0]['data']['heap'], '16g')

    def test_detail_json(self):
        p = self.run_tool('detail', 'camp1', 'a-geo-r1')
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data['plan'][2], 'geo')

    def test_monitor_json(self):
        p = self.run_tool('monitor', 'camp1', 'a-geo-r1')
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data['samples'], 2)
        self.assertIn('n1', data['nodes'])

    def test_log_text(self):
        p = self.run_tool('log', 'camp1', 'PROGRESS.txt')
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertIn('VALID', data['lines'][0])

    def test_report_text(self):
        p = self.run_tool('report', 'camp1')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('raceway 跑分报告', p.stdout)

    def test_est_json(self):
        p = self.run_tool('est', 'geo')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['suggest'], 29)

    def test_track_write_read_delete(self):
        p = self.run_tool('track-write', 'geo/cli.json', '--content', '{"cli":1}')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['sha256'])
        p = self.run_tool('--raw', 'track-read', 'geo/cli.json')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"cli":1', p.stdout)
        p = self.run_tool('track-del', 'geo/cli.json')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['deleted'])

    def test_track_ls(self):
        p = self.run_tool('track-ls')
        self.assertEqual(p.returncode, 0, p.stderr)
        paths = [f['path'] for f in json.loads(p.stdout)['files']]
        self.assertIn('geo/index.json', paths)

    def test_guard_missing_args(self):
        p = self.run_tool('sidecar')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('参数不足', p.stderr)
        self.assertEqual(p.stderr.count('参数不足'), 1)  # 不重复打印

    def test_close_defaults_dry_run(self):
        # 没有 campaign 结束行 → close 会给出 dry-run 结果；不传 --run 不改动 PROGRESS.txt
        prog = os.path.join(self.root, 'campaigns', 'camp1', 'PROGRESS.txt')
        with open(prog, encoding='utf-8') as f:
            before = f.read()
        p = self.run_tool('close', 'camp1')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['dry_run'])
        with open(prog, encoding='utf-8') as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_result_json(self):
        # 场内速览：逐场次判定行（PLAN × RACES 聚合，纯本地文件）
        p = self.run_tool('result', 'camp1')
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data['cid'], 'camp1')
        self.assertEqual(data['rows'][0]['fid'], 'a-geo-r1')
        self.assertEqual(data['rows'][0]['verdict'], 'VALID')

    def test_mutex_json(self):
        # 本地互斥预检：fixture 没有 runner 持锁 → 空闲
        p = self.run_tool('mutex')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['free'])

    def test_stop_requires_yes(self):
        # 杀进程操作必须显式 --yes（与页面二次确认同口径）
        p = self.run_tool('stop', 'camp1')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('--yes', p.stderr)
        self.assertEqual(p.stderr.count('--yes'), 1)  # 不重复打印

    def test_cars_validate(self):
        # runner 同口径校验：好文件 ok=true 退出 0；坏文件 ok=false 退出 1 且有行号
        good = os.path.join(self.root, 'cars-good.conf')
        with open(good, 'w', encoding='utf-8', newline='\n') as f:
            f.write('ez1 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 '
                    'ENGINE_BUILD=3010 JDK_DIR=/opt/jdks/jdk21 CAR_LABEL=ez1\n'
                    'es1 ENGINE_NAME=elasticsearch\n')
        p = self.run_tool('cars-validate', '--cars-file', good)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['ok'])

        bad = os.path.join(self.root, 'cars-bad.conf')
        with open(bad, 'w', encoding='utf-8', newline='\n') as f:
            f.write('os1 ENGINE_NAME=opensearch ENGINE_BUILD=99\n')
        p = self.run_tool('cars-validate', '--cars-file', bad)
        self.assertEqual(p.returncode, 1)
        data = json.loads(p.stdout)
        self.assertFalse(data['ok'])
        self.assertTrue(any(d['severity'] == 'error' for d in data['diagnostics']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
