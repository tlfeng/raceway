#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-compose.py 的回归测试（纯 stdlib，不碰集群、不碰被测引擎）。

跑法：
    python3 -m unittest discover -s tests -t . -v

覆盖的最要紧的不变式：
    **compose 生成的排程行序 == runner 非排程主循环的实际执行序**
    （赛道外层 → 轮次 → 臂右旋，交错直接复刻 bench-queue.order_for_round）
排程模式下 runner 不再自行交错，交错是排程文件的义务——这里钉住它。
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

C = importlib.import_module('bench-compose')  # noqa: E402
Q = importlib.import_module('bench-queue')  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARMS_TEXT = """
# 注释行与空行都跳过

jdk25 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 ARM_LABEL=jdk25 JDK_WANT=25|OpenJDK
jdk26 ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 ARM_LABEL=jdk26 JDK_DIR=/opt/jdks/jdk-26 JDK_WANT=26|OpenJDK
es819 ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ARM_LABEL=es819
"""


def arm_names(arms):
    return [a.name for a in arms]


class TestParseArms(unittest.TestCase):
    def test_skips_comments_and_blanks(self):
        arms = C.parse_arms(ARMS_TEXT)
        self.assertEqual(arm_names(arms), ['jdk25', 'jdk26', 'es819'])

    def test_label_defaults_to_name_and_fields_extracted(self):
        arms = C.parse_arms(ARMS_TEXT)
        by = {a.name: a for a in arms}
        self.assertEqual(by['jdk25'].label, 'jdk25')
        self.assertEqual(by['jdk26'].jdk_dir, '/opt/jdks/jdk-26')
        self.assertEqual(by['es819'].engine, 'elasticsearch')
        # config 原文保留（渲染 <tag>-arms.conf 时要原样回写）
        self.assertIn('JDK_DIR=/opt/jdks/jdk-26', by['jdk26'].config)

    def test_real_dimsweep_arms_file(self):
        # 臂定义属内部资料，不随公共库公开：按与 bench-compose.py 相同的顺序找，
        # 找不到就跳过（本地留存副本用 RACEWAY_ARMS_FILE 指过来即可跑）。
        path = (os.environ.get('RACEWAY_ARMS_FILE')
                or os.path.join(REPO, 'schedules', 'dimsweep-arms.conf'))
        if not os.path.isfile(path):
            self.skipTest('臂定义不在公共库里（%s），跳过；'
                          '用 RACEWAY_ARMS_FILE 指向本地留存副本可跑' % path)
        with open(path, encoding='utf-8') as f:
            arms = C.parse_arms(f.read())
        self.assertEqual(arm_names(arms), ['jdk25', 'jdk26', 'jdk27', 'es819'])
        self.assertFalse([m for s, m in C.validate_arms(arms) if s == 'error'])


class TestValidateArms(unittest.TestCase):
    def test_clean_file_has_no_errors(self):
        self.assertEqual([m for s, m in C.validate_arms(C.parse_arms(ARMS_TEXT)) if s == 'error'], [])

    def test_duplicate_labels_rejected(self):
        text = ARMS_TEXT.replace('ARM_LABEL=jdk26', 'ARM_LABEL=jdk25')
        problems = C.validate_arms(C.parse_arms(text))
        self.assertTrue(any('ARM_LABEL 重复' in m for s, m in problems if s == 'error'))

    def test_es_arm_with_jdk_dir_rejected(self):
        text = ARMS_TEXT.replace('ARM_LABEL=es819', 'ARM_LABEL=es819 JDK_DIR=/opt/jdks/jdk-26')
        problems = C.validate_arms(C.parse_arms(text))
        self.assertTrue(any('es 臂' in m and 'JDK_DIR' in m for s, m in problems if s == 'error'))

    def test_missing_engine_rejected(self):
        arms = C.parse_arms('bare ARM_LABEL=x\n')
        self.assertTrue(any('ENGINE_NAME' in m for s, m in C.validate_arms(arms) if s == 'error'))


class TestInterleave(unittest.TestCase):
    """交错序 == runner 非排程主循环的执行序（复刻 order_for_round）。"""

    def test_matches_order_for_round_per_round(self):
        arms = C.parse_arms(ARMS_TEXT)
        rows = C.interleave(arms, ['ds-j1024', 'ds-g960'], 3)
        names = arm_names(arms)
        # 每一轮内的臂序必须等于 order_for_round(r, n) 给出的索引序
        pos = 0
        for tk in ['ds-j1024', 'ds-g960']:
            for r in range(1, 4):
                expect = [names[i] for i in Q.order_for_round(r, len(arms))]
                got = [a for a, t, _ in rows[pos:pos + 3]]
                self.assertEqual(got, expect, 'track=%s round=%d' % (tk, r))
                self.assertEqual([t for _, t, _ in rows[pos:pos + 3]], [tk] * 3)
                pos += 3
        self.assertEqual(pos, len(rows))

    def test_runner_header_example_3x3(self):
        # run-bench.sh 头部注释的例子：3 配置 × 3 轮 = 9 场，r1: A,B,C / r2: C,A,B / r3: B,C,A
        arms = C.parse_arms('a ENGINE_NAME=x\nb ENGINE_NAME=y\nc ENGINE_NAME=z\n')
        rows = C.interleave(arms, ['geo'], 3)
        self.assertEqual([a for a, _, _ in rows],
                         ['a', 'b', 'c', 'c', 'a', 'b', 'b', 'c', 'a'])

    def test_track_major_ordering(self):
        arms = C.parse_arms('a ENGINE_NAME=x\nb ENGINE_NAME=y\n')
        rows = C.interleave(arms, ['ds-j1024', 'ds-g960'], 2)
        # 每轮 n 臂 = n 行：赛道外层先打满该赛道所有轮次，再到下一条赛道
        self.assertEqual([(t, r) for _, t, r in rows],
                         [('ds-j1024', 1), ('ds-j1024', 1),
                          ('ds-j1024', 2), ('ds-j1024', 2),
                          ('ds-g960', 1), ('ds-g960', 1),
                          ('ds-g960', 2), ('ds-g960', 2)])

    def test_single_arm_trivial_rotation(self):
        arms = C.parse_arms('only ENGINE_NAME=x\n')
        rows = C.interleave(arms, ['ds-j512'], 3)
        self.assertEqual([(a, r) for a, _, r in rows], [('only', 1), ('only', 2), ('only', 3)])


class TestRenderSchedule(unittest.TestCase):
    def test_fields_and_no_hash_in_data_lines(self):
        arms = C.parse_arms(ARMS_TEXT)
        rows = C.interleave(arms, ['ds-j1024'], 2)
        text = C.render_schedule(rows, arms, 't1')
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
        arms = C.parse_arms(ARMS_TEXT)
        rows = C.interleave(arms, ['ds-j1024', 'ds-g960'], 3)
        text = C.render_schedule(rows, arms, 't1')
        back = [(a, t, r) for a, t, r, _, _ in C.schedule_rows_from_text(text)]
        self.assertEqual(back, rows)

    def test_recall_flag_and_est(self):
        arms = C.parse_arms(ARMS_TEXT)
        rows = C.interleave(arms, ['ds-j1024', 'ds-g960'], 1)
        text = C.render_schedule(rows, arms, 't1', est_map={'ds-g960': 34}, est_default=45, recall=False)
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


if __name__ == '__main__':
    unittest.main()
