#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-history.py 的回归测试（纯 stdlib，不连指标库、不碰集群）

跑法：
    python3 -m unittest discover -s tests -t . -v

钉住的是三条最容易悄悄错掉的不变式：

1. **一场次两行**：写赛/查赛各一行，两行都 VALID 才算"跑过"；
   只有写赛 VALID 是**半场**，仍需重跑（不能当成已覆盖）。
2. **build 隔离**：jdk25@3009 跑过 ≠ jdk25@3015 跑过。同一格在两个 build 上
   各有一次跑动时，必须**按 build 分别**判定 —— 取"最坏"会把后来成功的
   重跑也一起抹掉（ds2 之前就在 3009 上 SKIP、后来 3015 上 VALID）。
3. **指标库不可用 ≠ 什么都没跑过**：查不到库时覆盖表必须标 unavailable，
   编排照常但**不剔除**任何格；把空表当"没跑过"会让编排台重排已做过的测试。
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

H = importlib.import_module('bench-history')  # noqa: E402


def row(campaign='c1', fid='jdk25-ds-j1024-r1', arm='jdk25', arm_build='jdk25@3015',
        track='ds-j1024', rnd=1, status='VALID', quality='ok', phase='w',
        challenge=None, ts='2026-10-21T02:30:00Z', warnings=None):
    d = {
        'campaign': campaign, 'fid': fid, 'arm': arm, 'arm_build': arm_build,
        'track': track, 'round': rnd, 'status': status, 'quality': quality,
        'phase': phase, '@timestamp': ts,
    }
    if challenge is not None:
        d['challenge'] = challenge
    if warnings:
        d['caliber_warnings'] = warnings
    return d


class TestVerdict(unittest.TestCase):
    """一场次（多行）合并成一个判定。"""

    def test_write_and_query_both_valid_is_valid(self):
        rows = [row(phase='w'), row(phase='q')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_VALID)

    def test_half_run_is_not_covered(self):
        """只有写赛 VALID、查赛没跑 → 半场，不算跑过。"""
        rows = [row(phase='w')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_HALF)
        cov = H.build_coverage(rows)
        cell = cov.get('jdk25', 'ds-j1024', 1)
        self.assertFalse(cell.covered_for('jdk25@3015'))

    def test_fail_beats_valid(self):
        rows = [row(phase='w'), row(phase='q', status='QUERY-FAIL')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_FAIL)

    def test_skip_beats_valid(self):
        rows = [row(phase='w', status='SKIP'), row(phase='q', status='SKIP')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_SKIP)

    def test_degraded_is_covered_but_flagged(self):
        rows = [row(phase='w', quality='degraded'), row(phase='q')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_DEGRADED)
        cov = H.build_coverage(rows)
        self.assertTrue(cov.get('jdk25', 'ds-j1024', 1).covered_for('jdk25@3015'))

    def test_write_only_track_expects_single_phase(self):
        """write-only 赛道只有一个相位也算完整。"""
        rows = [row(phase='w', challenge='write-only')]
        self.assertEqual(H._verdict_of(rows), H.VERDICT_VALID)


class TestBuildIsolation(unittest.TestCase):
    """同一格在不同 build 上各跑一次 → 必须按 build 分别判定。"""

    def setUp(self):
        # 3009 上 SKIP（ds3 当时被窗底砍掉），3015 上后来跑成 VALID（ds2）
        rows = [
            row(campaign='ds3', arm_build='jdk25@3009', status='SKIP', phase='w'),
            row(campaign='ds3', arm_build='jdk25@3009', status='SKIP', phase='q'),
            row(campaign='ds2', arm_build='jdk25@3015', phase='w'),
            row(campaign='ds2', arm_build='jdk25@3015', phase='q'),
        ]
        self.cov = H.build_coverage(rows)
        self.cell = self.cov.get('jdk25', 'ds-j1024', 1)

    def test_same_build_covered(self):
        self.assertTrue(self.cell.covered_for('jdk25@3015'))
        self.assertEqual(self.cell.verdict_for('jdk25@3015'), H.VERDICT_VALID)

    def test_other_build_not_covered(self):
        """3009 上是 SKIP → 对 3009 不算跑过。"""
        self.assertFalse(self.cell.covered_for('jdk25@3009'))
        self.assertEqual(self.cell.verdict_for('jdk25@3009'), H.VERDICT_SKIP)

    def test_overall_verdict_is_worst_across_builds(self):
        """整格判定是跨 build 取最坏（仅用于展示）。"""
        self.assertEqual(self.cell.verdict, H.VERDICT_SKIP)

    def test_per_build_breakdown(self):
        pb = self.cell.per_build()
        self.assertEqual(pb['jdk25@3015']['verdict'], H.VERDICT_VALID)
        self.assertEqual(pb['jdk25@3009']['verdict'], H.VERDICT_SKIP)
        self.assertEqual(pb['jdk25@3015']['campaigns'], ['ds2'])


class TestPlanCells(unittest.TestCase):
    """编排矩阵里每一格该不该排。"""

    def setUp(self):
        rows = [
            row(campaign='ds2', arm='jdk25', arm_build='jdk25@3015', track='ds-j1024', rnd=1, phase='w'),
            row(campaign='ds2', arm='jdk25', arm_build='jdk25@3015', track='ds-j1024', rnd=1, phase='q'),
            row(campaign='ds3', arm='jdk25', arm_build='jdk25@3009', track='ds-j1024', rnd=2, phase='w', status='SKIP'),
            row(campaign='ds3', arm='jdk25', arm_build='jdk25@3009', track='ds-j1024', rnd=2, phase='q', status='SKIP'),
        ]
        self.cov = H.build_coverage(rows)
        self.arms = [{'name': 'jdk25', 'build': '3015'}]

    def plan(self, **kw):
        return H.plan_cells(self.cov, self.arms, ['ds-j1024'], 2, **kw)

    def test_covered_cell_is_skipped(self):
        p = self.plan()
        self.assertEqual(len(p['skipped']), 1)
        self.assertEqual(p['skipped'][0]['round'], 1)
        self.assertTrue(p['skipped'][0]['same_build'])
        self.assertIn('已跑过', p['skipped'][0]['reason'])

    def test_skip_cell_still_runs(self):
        """3009 上 SKIP 的那格，对 3015 来说没跑过 → 仍要排。"""
        p = self.plan()
        self.assertEqual([c['round'] for c in p['to_run']], [2])

    def test_include_covered_runs_everything(self):
        p = self.plan(include_covered=True)
        self.assertEqual(len(p['skipped']), 0)
        self.assertEqual(len(p['to_run']), 2)

    def test_other_build_only_is_explained(self):
        """只被别的 build 跑过 → 仍要跑，且理由要说清是哪个 build。"""
        arms = [{'name': 'jdk25', 'build': '3020'}]
        p = H.plan_cells(self.cov, arms, ['ds-j1024'], 1)
        self.assertEqual(len(p['to_run']), 1)
        self.assertIn('别的 build', p['to_run'][0]['reason'])

    def test_missing_build_info_is_flagged(self):
        """臂没有 build 信息 → 无从判断同 build，必须说出来而不是当成已跑过。"""
        arms = [{'name': 'jdk25', 'build': ''}]
        p = H.plan_cells(self.cov, arms, ['ds-j1024'], 1)
        self.assertEqual(len(p['to_run']), 1)
        self.assertIn('没有 build 信息', p['to_run'][0]['reason'])

    def test_namedtuple_arms_do_not_mistake_label_for_build(self):
        """Arm 命名元组的第 2 个字段是 label，不是 build —— 不能解包错。"""
        from collections import namedtuple
        Arm = namedtuple('Arm', 'name label engine build jdk_dir config')
        arm = Arm('jdk25', 'jdk25', 'easysearch', '3015', '', 'x')
        p = H.plan_cells(self.cov, [arm], ['ds-j1024'], 1)
        self.assertEqual(len(p['skipped']), 1)

    def test_unavailable_coverage_never_skips(self):
        """指标库不可用 → 一格都不剔除（空表不等于"什么都没跑过"）。"""
        cov = H.Coverage(available=False, error='连不上')
        p = H.plan_cells(cov, self.arms, ['ds-j1024'], 2)
        self.assertEqual(len(p['skipped']), 0)
        self.assertEqual(len(p['to_run']), 2)

    def test_filter_rows_drops_skipped_only(self):
        p = self.plan()
        rows = [('jdk25', 'ds-j1024', 1), ('jdk25', 'ds-j1024', 2)]
        self.assertEqual(H.filter_rows(p, rows), [('jdk25', 'ds-j1024', 2)])


class TestCoverageTable(unittest.TestCase):
    def test_rows_fold_into_cells(self):
        rows = [row(phase='w'), row(phase='q'),
                row(fid='jdk26-ds-j1024-r1', arm='jdk26', arm_build='jdk26@3015', phase='w'),
                row(fid='jdk26-ds-j1024-r1', arm='jdk26', arm_build='jdk26@3015', phase='q')]
        cov = H.build_coverage(rows)
        self.assertEqual(len(cov.cells), 2)
        self.assertEqual(cov.campaigns, ['c1'])
        self.assertEqual(cov.tracks, ['ds-j1024'])
        self.assertEqual(cov.arms, ['jdk25', 'jdk26'])
        self.assertEqual(cov.summary()['races'], 2)

    def test_rows_without_arm_or_track_are_dropped(self):
        cov = H.build_coverage([row(arm=''), row(track='')])
        self.assertEqual(len(cov.cells), 0)

    def test_none_round_becomes_zero(self):
        cov = H.build_coverage([row(rnd=None, phase='w')])
        self.assertIn(('jdk25', 'ds-j1024', 0), cov.cells)

    def test_expected_arm_build(self):
        self.assertEqual(H.expected_arm_build('jdk25', '3015'), 'jdk25@3015')
        self.assertEqual(H.expected_arm_build('es819', ''), 'es819')


if __name__ == '__main__':
    unittest.main()
