#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-compose.py 的回归测试（纯 stdlib，不碰集群、不碰被测引擎）。

跑法：
    python3 -m unittest discover -s tests -t . -v

覆盖的最要紧的不变式：
    **compose 生成的排程行序 == runner 非排程主循环的实际执行序**
    （赛道外层 → 轮次 → 车右旋，交错直接复刻 bench-queue.order_for_round）
排程模式下 runner 不再自行交错，交错是排程文件的义务——这里钉住它。
"""
import importlib
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

C = importlib.import_module('bench-compose')  # noqa: E402
Q = importlib.import_module('bench-queue')  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CARS_TEXT = """
# 注释行与空行都跳过

jdk25 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=jdk25 JDK_WANT=25|GraalVM
jdk26 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=jdk26 JDK_DIR=/opt/jdks/jdk-26 JDK_WANT=26|OpenJDK
es819 ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
"""


def car_names(cars):
    return [a.name for a in cars]


def create_temp_cars_file():
    """ComposeState 需要一个真实存在的车定义文件（Windows 无 /dev/null 可用）。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.conf')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(CARS_TEXT)
    return path


class TestParseCars(unittest.TestCase):
    def test_skips_comments_and_blanks(self):
        cars = C.parse_cars(CARS_TEXT)
        self.assertEqual(car_names(cars), ['jdk25', 'jdk26', 'es819'])

    def test_label_defaults_to_name_and_fields_extracted(self):
        cars = C.parse_cars(CARS_TEXT)
        by = {a.name: a for a in cars}
        self.assertEqual(by['jdk25'].label, 'jdk25')
        self.assertEqual(by['jdk26'].jdk_dir, '/opt/jdks/jdk-26')
        self.assertEqual(by['es819'].engine, 'elasticsearch')
        # config 原文保留（渲染 <tag>-cars.conf 时要原样回写）
        self.assertIn('JDK_DIR=/opt/jdks/jdk-26', by['jdk26'].config)

    def test_real_dimsweep_cars_file(self):
        # 车定义属内部资料，不随公共库公开：按与 bench-compose.py 相同的顺序找，
        # 找不到就跳过（本地留存副本用 RACEWAY_CARS_FILE 指过来即可跑）。
        path = (os.environ.get('RACEWAY_CARS_FILE')
                or os.path.join(REPO, 'schedules', 'dimsweep-cars.conf'))
        if not os.path.isfile(path):
            self.skipTest('车定义不在公共库里（%s），跳过；'
                          '用 RACEWAY_CARS_FILE 指向本地留存副本可跑' % path)
        with open(path, encoding='utf-8') as f:
            cars = C.parse_cars(f.read())
        self.assertEqual(car_names(cars), ['jdk25', 'jdk26', 'jdk27', 'es819'])
        self.assertFalse([m for s, m in C.validate_cars(cars) if s == 'error'])


class TestValidateCars(unittest.TestCase):
    def test_clean_file_has_no_errors(self):
        self.assertEqual([m for s, m in C.validate_cars(C.parse_cars(CARS_TEXT)) if s == 'error'], [])

    def test_duplicate_labels_rejected(self):
        text = CARS_TEXT.replace('CAR_LABEL=jdk26', 'CAR_LABEL=jdk25')
        problems = C.validate_cars(C.parse_cars(text))
        self.assertTrue(any('CAR_LABEL 重复' in m for s, m in problems if s == 'error'))

    def test_es_car_with_jdk_dir_rejected(self):
        text = CARS_TEXT.replace('CAR_LABEL=es819', 'CAR_LABEL=es819 JDK_DIR=/opt/jdks/jdk-26')
        problems = C.validate_cars(C.parse_cars(text))
        self.assertTrue(any('es 车' in m and 'JDK_DIR' in m for s, m in problems if s == 'error'))

    def test_missing_engine_rejected(self):
        cars = C.parse_cars('bare CAR_LABEL=x\n')
        self.assertTrue(any('ENGINE_NAME' in m for s, m in C.validate_cars(cars) if s == 'error'))


class TestInterleave(unittest.TestCase):
    """交错序 == runner 非排程主循环的执行序（复刻 order_for_round）。"""

    def test_matches_order_for_round_per_round(self):
        cars = C.parse_cars(CARS_TEXT)
        rows = C.interleave(cars, ['ds-j1024', 'ds-g960'], 3)
        names = car_names(cars)
        # 每一轮内的车序必须等于 order_for_round(r, n) 给出的索引序
        pos = 0
        for tk in ['ds-j1024', 'ds-g960']:
            for r in range(1, 4):
                expect = [names[i] for i in Q.order_for_round(r, len(cars))]
                got = [a for a, t, _ in rows[pos:pos + 3]]
                self.assertEqual(got, expect, 'track=%s round=%d' % (tk, r))
                self.assertEqual([t for _, t, _ in rows[pos:pos + 3]], [tk] * 3)
                pos += 3
        self.assertEqual(pos, len(rows))

    def test_runner_header_example_3x3(self):
        # run-bench.sh 头部注释的例子：3 配置 × 3 轮 = 9 场，r1: A,B,C / r2: C,A,B / r3: B,C,A
        cars = C.parse_cars('a ENGINE_NAME=x\nb ENGINE_NAME=y\nc ENGINE_NAME=z\n')
        rows = C.interleave(cars, ['geo'], 3)
        self.assertEqual([a for a, _, _ in rows],
                         ['a', 'b', 'c', 'c', 'a', 'b', 'b', 'c', 'a'])

    def test_track_major_ordering(self):
        cars = C.parse_cars('a ENGINE_NAME=x\nb ENGINE_NAME=y\n')
        rows = C.interleave(cars, ['ds-j1024', 'ds-g960'], 2)
        # 每轮 n 车 = n 行：赛道外层先打满该赛道所有轮次，再到下一条赛道
        self.assertEqual([(t, r) for _, t, r in rows],
                         [('ds-j1024', 1), ('ds-j1024', 1),
                          ('ds-j1024', 2), ('ds-j1024', 2),
                          ('ds-g960', 1), ('ds-g960', 1),
                          ('ds-g960', 2), ('ds-g960', 2)])

    def test_single_car_trivial_rotation(self):
        cars = C.parse_cars('only ENGINE_NAME=x\n')
        rows = C.interleave(cars, ['ds-j512'], 3)
        self.assertEqual([(a, r) for a, _, r in rows], [('only', 1), ('only', 2), ('only', 3)])


class TestRenderSchedule(unittest.TestCase):
    def test_fields_and_no_hash_in_data_lines(self):
        cars = C.parse_cars(CARS_TEXT)
        rows = C.interleave(cars, ['ds-j1024'], 2)
        text = C.render_schedule(rows, cars, 't1')
        data = [l for l in text.splitlines() if l and not l.startswith('#')]
        self.assertEqual(len(data), len(rows))
        for line, (a, t, r) in zip(data, rows):
            parts = line.split()
            self.assertEqual(len(parts), 5, line)
            self.assertEqual(parts[0], a)
            self.assertEqual(parts[1], t)
            self.assertEqual(int(parts[2]), r)
            self.assertIn(parts[3], ('recall', 'norecall'))
            self.assertNotIn('#', line)  # runner 整行剥 #，数据行绝不能带

    def test_round_trip_preserves_order(self):
        cars = C.parse_cars(CARS_TEXT)
        rows = C.interleave(cars, ['ds-j1024', 'ds-g960'], 3)
        text = C.render_schedule(rows, cars, 't1')
        back = [(a, t, r) for a, t, r, _, _ in C.schedule_rows_from_text(text)]
        self.assertEqual(back, rows)

    def test_recall_flag_and_est(self):
        cars = C.parse_cars(CARS_TEXT)
        rows = C.interleave(cars, ['ds-j1024', 'ds-g960'], 1)
        text = C.render_schedule(rows, cars, 't1', est_map={'ds-g960': 34}, est_default=45, recall=False)
        by_track = {t: line.split()[3:] for _, t, _, _, _ in C.schedule_rows_from_text(text)
                    for line in [l for l in text.splitlines() if l.startswith(t)]}
        # 不查 too clever：直接按行断言
        lines = [l for l in text.splitlines() if l and not l.startswith('#')]
        for line in lines:
            parts = line.split()
            if parts[1] == 'ds-g960':
                self.assertEqual(parts[3:], ['norecall', '34'])
            else:
                self.assertEqual(parts[3:], ['norecall', '45'])

    def test_inline_comment_stripped_like_runner(self):
        # schedule_rows_from_text 与 runner schedule_lines 同规则：# 后全剥
        rows = C.schedule_rows_from_text('jdk25 ds-j1024 1 recall 35 # 行尾注释\n')
        self.assertEqual(rows, [('jdk25', 'ds-j1024', 1, 'recall', 35)])


class TestEstOverrides(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(C.parse_est_overrides(['ds-g960=34', 'ds-j1024=35']),
                         {'ds-g960': 34, 'ds-j1024': 35})

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            C.parse_est_overrides(['ds-g960'])
        with self.assertRaises(ValueError):
            C.parse_est_overrides(['ds-g960=abc'])


class TestControlConsole(unittest.TestCase):
    """三合一控制台（serve）的纯函数与页面模板完整性。"""

    def test_cid_whitelist(self):
        self.assertIsNone(C.check_cid('ds2e-20261022-231940'))
        self.assertIsNone(C.check_cid('jdk3-x_1.2'))
        # 路径穿越 / 注入面：campaign 名进远端 shell 与路径拼接，必须挡死
        for bad in ('', '../etc', 'a b', 'a;b', 'a\nb', '$(x)', 'a/b', '.'):
            self.assertIsNotNone(C.check_cid(bad), '应拒绝: %r' % bad)

    def test_est_total_min(self):
        rows = [('a', 'ds-j1024', 1), ('b', 'ds-j1024', 1), ('a', 'geo', 1)]
        est_map = {'ds-j1024': 35, 'geo': 30}
        self.assertEqual(C.est_total_min(rows, est_map), 100)
        # 未指定的赛道用默认值
        self.assertEqual(C.est_total_min([('a', 'nested', 1)], est_map, est_default=45), 45)

    def test_what_for(self):
        self.assertEqual(C.what_for(['ds-j1024', 'ds-g960']), 'ds')
        self.assertEqual(C.what_for(['geo']), 'geo')
        self.assertEqual(C.what_for(['ds-j1024', 'geo']), 'all')
        self.assertEqual(C.what_for([]), C.DEFAULT_WHAT)

    def test_api_render_returns_est_total_and_what(self):
        """GUI 的 render 响应要带 est_total_min / what —— 起跑前"要跑多久"和
        runner 分支参数（历史 bug：serve 模式 what=None 让 dry-run 必失败）。"""
        import tempfile
        cars_path = tempfile.mktemp(suffix='.conf')
        with open(cars_path, 'w', encoding='utf-8') as f:
            f.write('jdk25 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=jdk25\n'
                    'es819 ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819\n')
        args = types.SimpleNamespace(root=tempfile.mkdtemp(), ssh=None, cars_file=cars_path,
                                     es_auth=None, es_url=None, no_history=True, what=None,
                                     cars=None, tracks='', rounds=1, est=[], est_default=45,
                                     tag=None, recall=True, include_covered=False)
        st = C.ComposeState(args)
        code, payload = C.api_render(st, {'cars': ['jdk25', 'es819'],
                                          'tracks': ['ds-j1024', 'geo'], 'rounds': 2,
                                          'recall': 'recall'})
        self.assertEqual(code, 200, payload)
        # 2 车 × 2 赛道 × 2 轮 = 8 场；est = (35 + 30) × 2 = 130
        self.assertEqual(payload['rows'], 8)
        # 每场按各自赛道的 est 累加：ds-j1024(35)×4 行 + geo(30)×4 行
        self.assertEqual(payload['est_total_min'], 260)
        self.assertEqual(payload['what'], 'all')
        self.assertEqual(st.last['est_total_min'], 260)

    def test_api_compare_rejects_same_and_bad_cid(self):
        import tempfile
        args = types.SimpleNamespace(root=tempfile.mkdtemp(), ssh=None,
                                     cars_file=create_temp_cars_file(),
                                     es_auth=None, es_url=None, no_history=True)
        st = C.ComposeState(args)
        code, payload = C.api_compare(st, {'a': 'x', 'b': 'x', 'car': ''})
        self.assertEqual(code, 400)
        self.assertIn('同一个', payload['error'])
        code, payload = C.api_compare(st, {'a': '../etc', 'b': 'y', 'car': ''})
        self.assertEqual(code, 400)

    def test_page_template_has_three_tabs_and_no_format_leftovers(self):
        # 页面改用 .replace 注入（不是 % 格式化），模板里不能残留占位符/转义
        self.assertNotIn('%s', C._PAGE)
        self.assertNotIn('%%', C._PAGE)
        self.assertNotIn('__DS_TIERS__', C._PAGE)
        for frag in ('tab-compose', 'tab-progress', 'tab-history',
                     '/api/campaigns', '/api/queue', '/api/logtail', '/api/stop',
                     'renderProgress', 'reorderCamp', 'doStop', 'restorePrefs'):
            self.assertIn(frag, C._PAGE)

    def test_page_ids_referenced_by_js_exist(self):
        """JS 里 getElementById 的每个 id 都必须在模板里出现（防改 HTML 漏改 JS）。"""
        import re as _re
        ids = set(_re.findall(r"getElementById\('([a-z]+)'\)", C._PAGE))
        # querySelector('.carck') 等动态拼的不在此列
        missing = [i for i in ids if ('id="%s"' % i) not in C._PAGE]
        self.assertEqual(missing, [])

    def test_page_has_compare_and_est_ui(self):
        for frag in ('/api/compare', 'doCompare', 'est_total_min', 'hfilter', 'pnotify',
                     'fmtMin', 'windowHint'):
            self.assertIn(frag, C._PAGE)

    def test_page_has_doctor_ui(self):
        for frag in ('/api/doctor', 'doDoctor', '环境体检'):
            self.assertIn(frag, C._PAGE)

    def test_doctor_script_bash_syntax(self):
        """doctor.sh 必须能过 bash -n；且不硬性依赖 vm-bench.env（体检对象就是它）。"""
        import shutil
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('本机没有 bash')
        p = subprocess.run([bash, '-n', os.path.join(repo, 'doctor.sh')],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(repo, 'doctor.sh'), encoding='utf-8') as f:
            src = f.read()
        # doctor 的第一梯队检查必须在 env 缺失时也能跑（env 不存在不能让脚本半途死掉）
        self.assertIn('if [ -f vm-bench.env ]', src)
        # 纯只读自证：不许出现会改状态的命令
        for forbidden in (' rm ', ' systemctl stop', ' systemctl disable', ' mkdir -p $MON_DIR'):
            self.assertNotIn(forbidden, src, 'doctor 应保持只读，不应包含 %r' % forbidden)

    def test_parse_env_summary(self):
        """ENV-SUMMARY.csv → 逐场最坏判定 + steal 峰值（进度页的证据链展示）。"""
        csv = (
            'fid,phase,node,ip,samples,steal_avg,steal_max,foreign_avg,foreign_max,swapin,env_verdict\n'
            'jdk25-geo-r1,w,n1,10.0.0.1,100,0.10,0.40,0.00,0.00,0,OK\n'
            'jdk25-geo-r1,q,n1,10.0.0.1,80,0.50,2.30,0.10,1.00,0,CONTAMINATED\n'
            'jdk25-geo-r1,q,n2,10.0.0.2,80,0.20,1.00,0.00,0.00,0,OK\n'
            'jdk25-geo-r2,w,n1,10.0.0.1,100,0.00,0.00,0.00,0.00,0,OK\n'
            'jdk25-geo-r2,q,n1,10.0.0.1,80,0.00,0.00,0.00,0.00,0,WARN\n'
            'jdk25-geo-r3,w,n1,10.0.0.1,0,,,,,0,NO-DATA\n')
        env = C.parse_env_summary(csv)
        self.assertEqual(env['jdk25-geo-r1'], {'verdict': 'CONTAMINATED', 'steal_max': 2.3})
        self.assertEqual(env['jdk25-geo-r2'], {'verdict': 'WARN', 'steal_max': 0.0})
        self.assertEqual(env['jdk25-geo-r3'], {'verdict': 'NO-DATA', 'steal_max': None})
        self.assertEqual(C.parse_env_summary(''), {})
        self.assertEqual(C.parse_env_summary(None), {})

    def test_api_queue_attaches_env_verdicts(self):
        import tempfile
        args = types.SimpleNamespace(root=tempfile.mkdtemp(), ssh=None,
                                     cars_file=create_temp_cars_file(),
                                     es_auth=None, es_url=None, no_history=True)
        st = C.ComposeState(args)
        # 造一个带 monitors/ENV-SUMMARY.csv 的 campaign（legacy 模式，1 场 VALID）
        cid = 'env-camp'
        d = os.path.join(args.root, 'campaigns', cid)
        os.makedirs(os.path.join(d, 'monitors'))
        with open(os.path.join(d, 'PROGRESS.txt'), 'w', encoding='utf-8') as f:
            f.write('[2026-10-20 10:00:00] campaign %s 启动: cars=[a] tracks=geo rounds=1\n'
                    '[2026-10-20 10:01:00] ===== [1] a-geo-r1 开始 =====\n'
                    '[2026-10-20 10:30:00] [1] a-geo-r1 完成: VALID（29 分钟）\n' % cid)
        with open(os.path.join(d, 'monitors', 'ENV-SUMMARY.csv'), 'w', encoding='utf-8') as f:
            f.write('fid,phase,node,ip,samples,steal_avg,steal_max,foreign_avg,foreign_max,swapin,env_verdict\n'
                    'a-geo-r1,w,n1,10.0.0.1,100,0.10,1.50,0,0,0,OK\n'
                    'a-geo-r1,q,n1,10.0.0.1,80,0.40,3.20,0,0,0,CONTAMINATED\n')
        code, payload = C.api_queue(st, cid)
        self.assertEqual(code, 200)
        f0 = payload['fields'][0]
        self.assertEqual(f0['env_verdict'], 'CONTAMINATED')
        self.assertEqual(f0['steal_max'], 3.2)

    def test_page_script_parses(self):
        """控制台页面的 <script> 必须是合法 JS（python 三引号里写 JS 很容易转义翻车）。"""
        import re as _re
        import shutil
        import subprocess
        import tempfile
        node = shutil.which('node')
        if not node:
            self.skipTest('本机没有 node，跳过 JS 语法检查')
        m = _re.search(r'<script>(.*)</script>', C._PAGE, _re.S)
        self.assertTrue(m, '页面里没有 <script> 块')
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8', newline='\n') as f:
            f.write(m.group(1))
            path = f.name
        p = subprocess.run([node, '--check', path], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_api_start_reports_campaign(self):
        # 起跑成功后的 JSON 要带 campaign 名 —— 进度页靠它自动选中
        import inspect
        src = inspect.getsource(C.api_start)
        self.assertIn("'campaign': camp", src)


if __name__ == '__main__':
    unittest.main()
