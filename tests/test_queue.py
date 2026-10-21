#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""queue.py 的回归测试（纯 stdlib，不碰集群、不碰被测引擎）

跑法：
    python3 -m unittest discover -s tests -t . -v
    # 额外跑真实数据回归（需要一个 campaigns 目录）：
    RACEWAY_CAMPAIGNS=/path/to/campaigns python3 -m unittest discover -s tests -t . -v

真实数据回归验证的是最要紧的一条不变式：
    **推导出来的队列 == runner 实际跑出来的队列**（逐场、逐顺序）
这保证了"待跑清单"不是拍脑袋算出来的。
"""
import datetime
import importlib
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 文件名带连字符，只能用 importlib 导入
Q = importlib.import_module('bench-queue')  # noqa: E402


# ------------------------------------------------------------------ 夹具
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def make_legacy_campaign(root, cid, cars, tracks, rounds, fields):
    """造一个非排程模式的 campaign 目录。

    fields: [(fid, status, minutes)]，按实际顺序给。
    """
    d = os.path.join(root, cid)
    prog = ['[%s] campaign %s 启动: cars=[%s] tracks=%s rounds=%d'
            % ('2026-10-20 10:00:00', cid, ' '.join(cars), ' '.join(tracks), rounds)]
    races = ['# RACES — %s' % cid, '| 场次 | 状态 | 赛前卡点 | 用时 |', '|---|---|---|---|']
    for i, (fid, status, mins) in enumerate(fields, 1):
        prog.append('[2026-10-20 10:%02d:00] ===== [%d] %s 开始 =====' % (i % 60, i, fid))
        if status == 'VALID':
            prog.append('[2026-10-20 10:%02d:30] [%d] %s 完成: VALID（%d 分钟）' % (i % 60, i, fid, mins))
            races.append('| %s | VALID | precheck=PASS | %dmin |' % (fid, mins))
        else:
            prog.append('[2026-10-20 10:%02d:30] [%d] %s %s' % (i % 60, i, fid, status))
            races.append('| %s | %s | precheck=PASS | - |' % (fid, status))
    prog.append('[2026-10-20 11:00:00] campaign 结束: %d/%d 场有效。' % (
        sum(1 for f in fields if f[1] == 'VALID'), len(fields)))
    write(os.path.join(d, 'PROGRESS.txt'), '\n'.join(prog) + '\n')
    write(os.path.join(d, 'RACES.md'), '\n'.join(races) + '\n')
    return d


def make_schedule_campaign(root, cid, sched_rows, races_rows, progress_extra=None):
    """造一个排程模式的 campaign 目录。

    sched_rows: [(car, track, round, recall, est)]
    races_rows: [(fid, status, dur)]
    progress_extra: 追加到 PROGRESS 的行
    """
    d = os.path.join(root, cid)
    sched = os.path.join(root, '_sched', '%s.conf' % cid)
    write(sched, '\n'.join('%s %s %s %s %s' % r for r in sched_rows) + '\n')
    # 排程文件的 mtime 落在 campaign 起跑之前 —— 否则"排程在运行期间被改过"的
    # 告警会在每个夹具上误报
    ts = datetime.datetime(2026, 10, 20, 2, 11, 51).timestamp()
    os.utime(sched, (ts, ts))

    prog = ['[2026-10-20 02:11:51] campaign %s 启动: configs=[] tracks=%s rounds=1（轮内右旋配对交错）'
            % (cid, ' '.join(sorted({r[1] for r in sched_rows}))),
            '[2026-10-20 02:12:04] 窗底守卫: 12h（截止 2026-10-20 13:52:04 之前放不下的场次 DEADLINE-SKIP）',
            '[2026-10-20 02:12:05] 排程模式: _sched/%s.conf（车定义 _sched/%s-cars.conf）' % (cid, cid)]
    if progress_extra:
        prog += progress_extra
    races = ['# RACES — %s' % cid, '| 场次 | 状态 | 赛前卡点 | 用时 |', '|---|---|---|---|']
    for fid, status, dur in races_rows:
        races.append('| %s | %s | precheck=PASS | %s |' % (fid, status, dur))
    write(os.path.join(d, 'PROGRESS.txt'), '\n'.join(prog) + '\n')
    write(os.path.join(d, 'RACES.md'), '\n'.join(races) + '\n')
    return d


# ------------------------------------------------------------------ 单测
class TestOrderForRound(unittest.TestCase):
    """右旋配对交错必须和 run-bench.sh:199-204 完全一致。"""

    def test_rotation(self):
        n = 3
        self.assertEqual(Q.order_for_round(1, n), [0, 1, 2])
        self.assertEqual(Q.order_for_round(2, n), [2, 0, 1])
        self.assertEqual(Q.order_for_round(3, n), [1, 2, 0])
        self.assertEqual(Q.order_for_round(4, n), [0, 1, 2])   # 轮次循环

    def test_two_cars(self):
        self.assertEqual(Q.order_for_round(1, 2), [0, 1])
        self.assertEqual(Q.order_for_round(2, 2), [1, 0])

    def test_single_car(self):
        self.assertEqual(Q.order_for_round(1, 1), [0])
        self.assertEqual(Q.order_for_round(9, 1), [0])


class TestParse(unittest.TestCase):
    def test_schedule_lines(self):
        text = "# 注释行\njdk25 ds-g1920 1 recall 52\n\n  es819 ds-j1024 1 norecall 42  \n"
        rows = Q.parse_schedule(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {'car': 'jdk25', 'track': 'ds-g1920', 'rnd': 1,
                                   'recall': 'recall', 'est': 52})
        self.assertEqual(rows[1]['est'], 42)

    def test_state(self):
        st = Q.parse_state('OK 1\nFAIL 2\nOK 3\nFAIL 3\n')
        self.assertEqual(st[1], ['OK'])
        self.assertEqual(st[3], ['OK', 'FAIL'])      # 同一行两种结果都要留住

    def test_split_fid(self):
        cars, tracks = ['es819', 'jdk25'], ['ds-g1920', 'ds-j1024', 'geo']
        self.assertEqual(Q.split_fid('jdk25-ds-j1024-r2', cars, tracks)[:3],
                         ('jdk25', 'ds-j1024', 2))
        self.assertEqual(Q.split_fid('es819-ds-g1920-r1', cars, tracks)[:3],
                         ('es819', 'ds-g1920', 1))
        self.assertEqual(Q.split_fid('jdk25-geo-r1-try3', cars, tracks)[3], 3)


class TestLegacyCampaign(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rwq-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_derived_plan_matches_observed_order(self):
        """核心不变式：推导出的队列 == 实际跑出来的顺序（含右旋交错）。"""
        cars = ['ez240', 'jdk25', 'kona25']
        tracks = ['geo', 'hnsw', 'nested']
        rounds = 3
        # 按 run-bench.sh 的展平规则造真实顺序：赛道外层 → 轮次 → 车右旋
        expected = []
        for tk in tracks:
            for r in range(1, rounds + 1):
                for idx in Q.order_for_round(r, len(cars)):
                    expected.append('%s-%s-r%d' % (cars[idx], tk, r))
        self.assertEqual(expected[:3], ['ez240-geo-r1', 'jdk25-geo-r1', 'kona25-geo-r1'])
        self.assertEqual(expected[3:6], ['kona25-geo-r2', 'ez240-geo-r2', 'jdk25-geo-r2'])
        self.assertEqual(expected[6:9], ['jdk25-geo-r3', 'kona25-geo-r3', 'ez240-geo-r3'])

        fields = [(fid, 'VALID', 20) for fid in expected]
        make_legacy_campaign(self.root, 'c1', cars, tracks, rounds, fields)

        c = Q.build_campaign(Q.Source(self.root), 'c1')
        self.assertEqual(c.mode, 'legacy')
        self.assertTrue(c.plan_known)
        self.assertEqual([f.fid for f in c.fields], expected)
        self.assertEqual(c.valid, 27)
        self.assertEqual(c.pending, 0)
        self.assertEqual(c.warnings, [])

    def test_resume_skip_is_not_a_skip_verdict(self):
        """续跑跳过（STATE 已有 OK）不能把 VALID 场改成 SKIP。"""
        d = make_legacy_campaign(self.root, 'c2', ['a1'], ['geo'], 1,
                                 [('a1-geo-r1', 'VALID', 20)])
        prog = os.path.join(d, 'PROGRESS.txt')
        with open(prog, 'a', encoding='utf-8') as f:
            f.write('[2026-10-20 12:00:00] [1] a1-geo-r1 跳过（STATE 已有 OK 1）\n')
        c = Q.build_campaign(Q.Source(self.root), 'c2')
        self.assertEqual(c.fields[0].status, 'VALID')
        self.assertEqual(c.skipped, 0)
        self.assertEqual(c.valid, 1)

    def test_retry_does_not_break_legacy_plan(self):
        """重跑会把同一 fid 再插回时间线中间；legacy 推导的自校验必须按首次出现去重，
        否则会把本来正确的排程误判成"推导不可信"（实测 ds2 campaign 被这么误判过）。"""
        cars = ['jdk26', 'jdk27']
        tracks = ['ds-j1024']
        rounds = 2
        expected = ['jdk26-ds-j1024-r1', 'jdk27-ds-j1024-r1',
                    'jdk27-ds-j1024-r2', 'jdk26-ds-j1024-r2']
        d = make_legacy_campaign(self.root, 'c4', cars, tracks, rounds,
                                 [(f, 'VALID', 20) for f in expected])
        # 时间线：jdk26-r1 / jdk27-r1 先失败，修好后重跑 → 同一 fid 在中间再出现一次
        events = [('jdk26-ds-j1024-r1', 1), ('jdk27-ds-j1024-r1', 1),
                  ('jdk26-ds-j1024-r1', 1), ('jdk27-ds-j1024-r1', 1),
                  ('jdk27-ds-j1024-r2', 3), ('jdk26-ds-j1024-r2', 4)]
        lines = ['[2026-10-20 10:00:00] campaign c4 启动: cars=[%s] tracks=%s rounds=%d'
                 % (' '.join(cars), ' '.join(tracks), rounds)]
        for i, (fid, seq) in enumerate(events):
            lines.append('[2026-10-20 10:%02d:00] ===== [%d] %s 开始 =====' % (i, seq, fid))
        lines.append('[2026-10-20 11:00:00] campaign 结束: 4/4 场有效。')
        with open(os.path.join(d, 'PROGRESS.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        c = Q.build_campaign(Q.Source(self.root), 'c4')
        self.assertTrue(c.plan_known, c.warnings)
        self.assertEqual([f.fid for f in c.fields], expected)

    def test_real_precheck_skip_is_skip(self):
        """赛前卡点没过才是真 SKIP。"""
        d = make_legacy_campaign(self.root, 'c3', ['a1'], ['geo'], 1,
                                 [('a1-geo-r1', 'SKIP', 0)])
        prog = os.path.join(d, 'PROGRESS.txt')
        with open(prog, 'a', encoding='utf-8') as f:
            f.write('[2026-10-20 12:00:00] [1] a1-geo-r1 SKIP: 赛前负载检测未通过\n')
        c = Q.build_campaign(Q.Source(self.root), 'c3')
        self.assertEqual(c.fields[0].status, 'SKIP')
        self.assertEqual(c.skipped, 1)


class TestScheduleCampaign(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rwq-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_plan_from_schedule_file(self):
        rows = [('jdk25', 'ds-g1920', 1, 'recall', 52),
                ('jdk25', 'ds-j1024', 1, 'recall', 35),
                ('es819', 'ds-j1024', 1, 'recall', 42)]
        make_schedule_campaign(self.root, 's1', rows,
                               [('jdk25-ds-g1920-r1', 'VALID', '45min')])
        c = Q.build_campaign(Q.Source(self.root), 's1')
        self.assertEqual(c.mode, 'schedule')
        self.assertTrue(c.plan_known)
        self.assertEqual(c.total, 3)
        self.assertEqual(c.pending, 2)
        self.assertEqual(c.fields[0].est, 52)
        self.assertEqual(c.fields[0].recall, 'recall')

    def test_duplicate_verdict_is_flagged(self):
        """同一 fid 两条矛盾判定（排程被改后重跑没清表）必须报出来。"""
        rows = [('jdk26', 'ds-j1024', 1, 'norecall', 36),
                ('jdk26', 'ds-j1024', 2, 'recall', 31)]
        make_schedule_campaign(
            self.root, 's2', rows,
            [('jdk26-ds-j1024-r1', 'COLD-START-FAIL', '-'),
             ('jdk26-ds-j1024-r1', 'VALID', '24min'),
             ('jdk26-ds-j1024-r2', 'COLD-START-FAIL', '-')])
        c = Q.build_campaign(Q.Source(self.root), 's2')
        self.assertTrue(any('矛盾的判定' in w for w in c.warnings), c.warnings)
        # 最后一条判定生效
        f1 = [f for f in c.fields if f.fid == 'jdk26-ds-j1024-r1'][0]
        self.assertEqual(f1.status, 'VALID')

    def test_state_conflict_is_flagged(self):
        rows = [('jdk25', 'geo', 1, 'recall', 20)]
        d = make_schedule_campaign(self.root, 's3', rows, [])
        write(os.path.join(d, 'STATE.txt'), 'FAIL 1\nOK 1\n')
        c = Q.build_campaign(Q.Source(self.root), 's3')
        self.assertTrue(any('同时有' in w for w in c.warnings), c.warnings)

    def test_deadline_drift_is_flagged(self):
        rows = [('jdk25', 'geo', 1, 'recall', 20)]
        make_schedule_campaign(
            self.root, 's4', rows, [],
            progress_extra=['[2026-10-20 03:00:00] 窗底守卫: 12h（截止 2026-10-20 14:00:00 之前放不下的场次 DEADLINE-SKIP）'])
        c = Q.build_campaign(Q.Source(self.root), 's4')
        self.assertTrue(any('窗底被 runner 重启推后' in w for w in c.warnings), c.warnings)
        self.assertEqual(c.deadline, '2026-10-20 14:00:00')

    def test_attempts_are_merged_into_one_field(self):
        """-tryN 是同一场的尝试，不该变成独立场次。"""
        rows = [('jdk26', 'ds-j1024', 1, 'norecall', 36)]
        make_schedule_campaign(
            self.root, 's5', rows,
            [('jdk26-ds-j1024-r1', 'COLD-START-FAIL', '-'),
             ('jdk26-ds-j1024-r1-try2', 'COLD-START-FAIL', '-'),
             ('jdk26-ds-j1024-r1-try3', 'COLD-START-FAIL', '-')])
        c = Q.build_campaign(Q.Source(self.root), 's5')
        self.assertEqual(c.total, 1)
        self.assertEqual(c.fields[0].attempts, 3)
        self.assertEqual(c.failed, 1)


class TestRealCampaigns(unittest.TestCase):
    """真实数据回归：需要 RACEWAY_CAMPAIGNS 环境变量。"""

    def test_derived_plan_matches_observed_timeline(self):
        root = os.environ.get('RACEWAY_CAMPAIGNS')
        if not root or not os.path.isdir(root):
            self.skipTest('未设置 RACEWAY_CAMPAIGNS，跳过真实数据回归')
        src = Q.Source(root)
        checked = 0
        untrusted = 0
        for cid in src.list_campaigns():
            text = src.read(cid, 'PROGRESS.txt')
            if not text:
                continue
            observed = re.findall(r'=====\s+\[(\d+)\]\s+(\S+)\s+开始\s+=====', text)
            if len(observed) < 2:
                continue
            c = Q.build_campaign(src, cid)
            if not c.plan_known:
                # 推导不可信时，必须**说明原因**，不能默默给一个错的清单
                self.assertTrue(c.warnings, '%s: 计划未知却没有给出原因' % cid)
                untrusted += 1
                continue
            got = [f.fid for f in c.fields if f.seq]
            # 时间线里一次重试会有多行（-try2/-try3，或同 fid 重跑），
            # 归并到基础 fid 后按**首次出现**去重 —— 重跑是同一场的又一次尝试，
            # 不能当成"插在别处的新场次"，否则顺序比对必然对不上。
            want = []
            seen = set()
            for _, fid in observed:
                b = Q.base_fid(fid)
                if b in seen:
                    continue
                seen.add(b)
                want.append(b)
            self.assertEqual(got[:len(want)], want[:len(got)],
                             '%s: 推导队列与实际顺序不一致' % cid)
            checked += 1
        self.assertGreater(checked, 0, '没有可比对的 campaign')
        self.assertGreater(untrusted, 0,
                           '真实数据里应当存在"推导不可信"的旧版 campaign（否则校验形同虚设）')


if __name__ == '__main__':
    unittest.main()
