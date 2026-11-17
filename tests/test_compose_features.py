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

    def test_phase_qualified_files_concat_per_node(self):
        """真实布局：<fid>-<相位>-<node>.csv（load-monitor 按相位分 tag）。

        同节点跨相位的多份 CSV 拼成一条曲线；旧行为按 <fid>-<node>.csv 找不到文件，
        真机上整个监控卡永远"该场次没有监控 CSV"——这里钉住文件名解析。
        """
        write_campaign(self.root, 'mon-camp2',
                       env=(ENV_HEADER
                            + 'r-x-r1,wq,n1,1.1.1.1,2,0,0,0,0,0,OK\n'
                            + 'r-x-r1,q,n1,1.1.1.1,2,0,0,0,0,0,OK\n'))
        mon = os.path.join(self.root, 'campaigns', 'mon-camp2', 'monitors')
        with open(os.path.join(mon, 'r-x-r1-wq-n1.csv'), 'w', encoding='utf-8') as f:
            f.write('ts,cpu_usr,cpu_steal,bench_cpu,load1\n100,10,0,30,2\n105,20,0,31,2\n')
        with open(os.path.join(mon, 'r-x-r1-q-n1.csv'), 'w', encoding='utf-8') as f:
            f.write('ts,cpu_usr,cpu_steal,bench_cpu,load1\n110,30,0,32,2\n')
        code, p = C.api_monitor(self.st, 'mon-camp2', 'r-x-r1')
        self.assertEqual(code, 200)
        self.assertEqual(len(p['series']), 1)
        self.assertEqual([r['cpu_usr'] for r in p['series'][0]['rows']], [10.0, 20.0, 30.0])

    def _race_json(self, root, cid, name):
        with open(os.path.join(root, 'campaigns', cid, 'results', name), 'w', encoding='utf-8') as f:
            json.dump({'results': {'op_metrics': [
                {'operation': 'bulk', 'task': 'index-append',
                 'throughput': {'unit': 'docs/s', 'median': 8067.8},
                 'latency': {'unit': 'ms', 'mean': 1.0}},
                {'operation': 'knn', 'task': 'knn-search-10-100',
                 'throughput': {'unit': 'ops/s', 'median': 32.28},
                 'latency': {'unit': 'ms', 'mean': 24.3, '50_0': 23.86, '99_0': 33.03}},
            ]}}, f)


class RaceMetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_race_detail_reads_results_op_metrics(self):
        """新版 rally 指标在 results.op_metrics（顶层为空）——钉住取数路径。"""
        write_campaign(self.root, 'rm-camp', plan='a-geo-r1\ta-conf\tgeo\t1\trecall\t30\n')
        d = os.path.join(self.root, 'campaigns', 'rm-camp', 'results')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'a-geo-r1-wq-race.json'), 'w', encoding='utf-8') as f:
            json.dump({'results': {'op_metrics': [
                {'operation': 'bulk', 'task': 'index-append',
                 'throughput': {'unit': 'docs/s', 'median': 100.0},
                 'latency': {'unit': 'ms', '50_0': 9.0, '99_0': 19.0}},
            ]}}, f)
        code, p = C.api_race_detail(self.st, 'rm-camp', 'a-geo-r1')
        self.assertEqual(code, 200)
        self.assertEqual(len(p['metrics']), 1)
        m = p['metrics'][0]
        self.assertEqual(m['task'], 'index-append')
        self.assertEqual(m['throughput'], 100.0)
        self.assertEqual(m['lat_p50'], 9.0)
        self.assertEqual(m['lat_p99'], 19.0)


class CampaignResultTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)

    def test_speedview_rows_verdicts_and_metrics(self):
        write_campaign(self.root, 'sv-camp',
                       plan='a-geo-r1\ta-conf\tgeo\t1\trecall\t30\n'
                            'b-geo-r1\tb-conf\tgeo\t1\trecall\t30\n',
                       races='| a-geo-r1 | VALID | OK | 60s |\n'
                             '| a-geo-r1 | VALID | OK | 58s |\n'
                             '| b-geo-r1 | FAIL | OK | 60s |\n')
        d = os.path.join(self.root, 'campaigns', 'sv-camp', 'results')
        os.makedirs(d, exist_ok=True)
        for fid, tput in (('a-geo-r1', 100.0), ('b-geo-r1', 200.0)):
            with open(os.path.join(d, '%s-wq-race.json' % fid), 'w', encoding='utf-8') as f:
                json.dump({'results': {'op_metrics': [
                    {'operation': 'bulk', 'task': 'index-append',
                     'throughput': {'unit': 'docs/s', 'median': tput}},
                    {'operation': 'knn', 'task': 'knn-10',
                     'throughput': {'unit': 'ops/s', 'median': tput / 10},
                     'latency': {'unit': 'ms', '50_0': 5.0, '99_0': 9.0}},
                ]}}, f)
        code, p = C.api_campaign_result(self.st, 'sv-camp')
        self.assertEqual(code, 200)
        by = {r['fid']: r for r in p['rows']}
        # 判定取最后一次出现（重试覆盖旧判定）
        self.assertEqual(by['a-geo-r1']['verdict'], 'VALID')
        self.assertEqual(by['b-geo-r1']['verdict'], 'FAIL')
        self.assertEqual(by['a-geo-r1']['metrics'][0]['throughput'], 100.0)
        # 最弱查询任务口径在 mock 前端，后端只供全量指标
        self.assertEqual(len(by['a-geo-r1']['metrics']), 2)

    def test_verdict_defaults_pending_without_races(self):
        write_campaign(self.root, 'sv-pend', plan='a-geo-r1\tc\tgeo\t1\trecall\t30\n')
        code, p = C.api_campaign_result(self.st, 'sv-pend')
        self.assertEqual(code, 200)
        self.assertEqual(p['rows'][0]['verdict'], 'PENDING')
        self.assertEqual(p['rows'][0]['metrics'], [])


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


class RouteContractTests(unittest.TestCase):
    """GET/POST 路由必须返回 (code, payload) 二元组（回归：写类 api_* 曾返回单个 dict，
    导致赛道文件页报 'not enough values to unpack'）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)
        # remote_operation 的子 shell 会 source vm-bench.env（真实部署必有），测试里造一个
        with open(os.path.join(self.root, 'vm-bench.env'), 'w', encoding='utf-8') as f:
            f.write('RESULTS_ROOT=%s/campaigns\n' % self.root)
        with open(os.path.join(self.root, 'tracks', 'a.json'), 'w', encoding='utf-8') as f:
            f.write('{"x":1}')

    def test_track_files_get_returns_tuple(self):
        code, ctype, body, cache = C._route_get_core(self.st, '/api/track-files', {})
        self.assertEqual(code, 200)
        self.assertEqual(ctype, 'application/json')
        self.assertIn('files', json.loads(body))

    def test_track_file_get_returns_tuple(self):
        code, ctype, body, cache = C._route_get_core(
            self.st, '/api/track-file', {'path': ['a.json']})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['path'], 'a.json')

    def test_post_write_returns_tuple(self):
        code, ctype, body, cache = C._post_core(
            self.st, '/api/track-write', {'path': 'b.json', 'content': '{"y":2}'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['path'], 'b.json')


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


class ComposeRefreshTest(unittest.TestCase):
    """bootstrap refresh=1 的车表重读：车定义文件变化在控制台无需重启即可见。

    语义契约：读不到/空文件 → **保留上一次车表** + 报 problem（刷新是改善操作，
    不该比不刷更糟）；解析出车 → 直接替换（校验问题照常上屏）。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='raceway-refresh-')
        self.cars_file = os.path.join(self.root, 'cars.conf')
        self.st = make_state(self.root)
        self.st.args.cars_file = self.cars_file
        with open(self.cars_file, 'w', encoding='utf-8') as f:
            f.write('carA ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0\n')
        self.st.reload_cars()

    def test_reload_picks_up_new_car(self):
        with open(self.cars_file, 'w', encoding='utf-8') as f:
            f.write('carA ENGINE_NAME=easysearch\n\ncarB ENGINE_NAME=elasticsearch\n')
        self.st.reload_cars()
        self.assertEqual([a.name for a in self.st.cars], ['carA', 'carB'])
        self.assertEqual(self.st.problems, [])

    def test_reload_keeps_old_table_on_empty_file(self):
        """半写状态（空文件）→ 旧车表继续可用，不被打瘫。"""
        with open(self.cars_file, 'w', encoding='utf-8') as f:
            f.write('')
        self.st.reload_cars()
        self.assertEqual([a.name for a in self.st.cars], ['carA'])
        self.assertTrue(any('保持上一次' in m for _, m in self.st.problems))

    def test_reload_keeps_old_table_on_missing_file(self):
        os.remove(self.cars_file)
        self.st.reload_cars()
        self.assertEqual([a.name for a in self.st.cars], ['carA'])
        self.assertTrue(any('重读失败' in m for _, m in self.st.problems))

    def test_refresh_catalog_stays_usable(self):
        C.refresh_catalog()
        self.assertIn('geo', C.TRACK_CATALOG)
        self.assertEqual(C.TRACK_CATALOG['geo']['name'], 'geo')
        # 变体随基座一起重建
        self.assertIn('geo-codec-zstd-sr', C.TRACK_CATALOG)


class AssetsTests(unittest.TestCase):
    """引擎与JDK 盘点（api_assets）：包路径判定与 runner resolve_engine 的 die 点一致，
    缺包标红；ssh 模式整页不可用。"""

    CARS = """# 注释行
ez3015 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
es819 ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
os24 ENGINE_NAME=opensearch ENGINE_VERSION=2.4.0 CAR_LABEL=os24
kona25 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/tencentkona-25.0.4 CAR_LABEL=kona25
jdk26 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/jdk-26 CAR_LABEL=jdk26
"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = make_state(self.root)
        # vm-bench.env：PKG_DIR 走 $ROOT 展开（真实部署的写法），附带一个 BUNDLE_* 引用
        with open(os.path.join(self.root, 'vm-bench.env'), 'w', encoding='utf-8') as f:
            f.write('PKG_DIR=$ROOT/pkg\n'
                    'BUNDLE_EZ3015=$ROOT/pkg/easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz\n')
        self.pkg = os.path.join(self.root, 'pkg')
        os.makedirs(self.pkg)
        self.st.cars = tuple(C.parse_cars(self.CARS))

    def by_car(self, payload, name):
        return next(d for d in payload['deps'] if d['car'] == name)

    def test_ssh_mode_unavailable(self):
        self.st.args.ssh = 'esrally'
        code, p = C.api_assets(self.st)
        self.assertEqual(code, 200)
        self.assertFalse(p['available'])
        self.assertTrue(p['reason'])

    def test_easysearch_pkg_path_and_stat(self):
        # 包已就位：路径与 resolve_engine 的 die 点一致，size/exists 来自 stat
        pkg = os.path.join(self.pkg, 'easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz')
        with open(pkg, 'wb') as f:
            f.write(b'\0' * 1024)
        code, p = C.api_assets(self.st)
        self.assertEqual(code, 200)
        d = self.by_car(p, 'ez3015')
        self.assertEqual(d['pkg'], pkg)
        self.assertTrue(d['pkg_stat']['exists'])
        self.assertEqual(d['pkg_stat']['size'], 1024)
        # BUNDLE_EZ3015 指向同一文件：引用 stat 同步就位
        self.assertTrue(p['bundles'][0]['exists'])
        # PKG_DIR 全清单收录该文件（目录不算）
        names = [x['name'] for x in p['pkg_files']]
        self.assertIn('easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz', names)

    def test_missing_pkg_flagged_not_present(self):
        # 包缺失 = 起跑必 die：这里 exists=False 让前端标红
        code, p = C.api_assets(self.st)
        d = self.by_car(p, 'ez3015')
        self.assertFalse(d['pkg_stat']['exists'])
        self.assertFalse(p['bundles'][0]['exists'])

    def test_engine_pkg_naming_per_engine(self):
        code, p = C.api_assets(self.st)
        root = self.pkg
        self.assertEqual(self.by_car(p, 'es819')['pkg'],
                         os.path.join(root, 'elasticsearch-8.19.23-linux-x86_64.tar.gz'))
        self.assertEqual(self.by_car(p, 'os24')['pkg'],
                         os.path.join(root, 'opensearch-2.4.0-linux-x64.tar.gz'))

    def test_jdk_hardcoded_mapping(self):
        # /jdk-26 结尾 → PKG_DIR/openjdk-26_linux-x64_bin.tar.gz（硬编码映射）
        jdkpkg = os.path.join(self.pkg, 'openjdk-26_linux-x64_bin.tar.gz')
        with open(jdkpkg, 'wb') as f:
            f.write(b'\0' * 512)
        code, p = C.api_assets(self.st)
        d = self.by_car(p, 'jdk26')
        self.assertTrue(d['jdk_mapped'])
        self.assertEqual(d['jdk_pkg'], jdkpkg)
        self.assertTrue(d['jdk_stat']['exists'])
        # 非 jdk-* 目录（如 TencentKona）：jdk_pkg 就是目录本身，只报告不映射
        d = self.by_car(p, 'kona25')
        self.assertEqual(d['jdk_pkg'], '/opt/jdks/tencentkona-25.0.4')
        self.assertFalse(d['jdk_stat']['exists'])

    def test_bundle_without_jdk_has_no_jdk_stat(self):
        # 自带 JDK 的车：jdk_pkg=None / jdk_mapped=False，且不产出 jdk_stat（前端据此省列）
        code, p = C.api_assets(self.st)
        d = self.by_car(p, 'ez3015')
        self.assertIsNone(d['jdk_pkg'])
        self.assertFalse(d['jdk_mapped'])
        self.assertNotIn('jdk_stat', d)

    def test_route_returns_json_tuple(self):
        code, ctype, body, cache = C._route_get_core(self.st, '/api/assets', {})
        self.assertEqual(code, 200)
        self.assertEqual(ctype, 'application/json')
        self.assertTrue(json.loads(body)['available'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
