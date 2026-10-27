#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""close-campaign.sh 的回归测试（本地 bash 跑脚本，不碰集群）

跑法：python3 -m unittest tests.test_close_campaign -v

守的不变式：
1. 补写的结束行必须被 bench-queue 的两套判定（build_campaign 位置语义 /
   campaign_summaries 计数语义）同时认成"已结束"，N/M 来自 RACES.md 实测计数；
2. 已有结束行的不动、还活着的不动（RUNNER.pid 活进程 / 宿主机有 runner 且进度刚更新）；
3. PROGRESS.txt 的 mtime 不变（历史页按 mtime 排序）。
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
Q = importlib.import_module('bench-queue')  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT_DIR, 'close-campaign.sh')

HEADER = '[2026-10-21 02:11:51] campaign %s 启动: configs=[a b] tracks=ds-j2048 rounds=1\n'
END_NATURAL = '[2026-10-21 08:00:00] campaign 结束: 1/1 场有效。报告: x；汇总: y\n'
RACES = ('| 场次 | 状态 | 卡点 | 用时 |\n|---|---|---|---|\n'
         '| f1 | VALID | PASS | 10m |\n| f2 | VALID | PASS | 10m |\n| f3 | WRITE-FAIL | PASS | - |\n')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


@unittest.skipUnless(shutil.which('bash'), '本机没有 bash')
class CloseCampaignTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='close-camp-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.kill()
            self.fake.wait()

    # ---- 夹具
    def camp(self, cid, progress, races=None, mtime=None, pid=None):
        d = os.path.join(self.root, cid)
        write(os.path.join(d, 'PROGRESS.txt'), progress)
        if races is not None:
            write(os.path.join(d, 'RACES.md'), races)
        if pid is not None:
            write(os.path.join(d, 'RUNNER.pid'), '%d\n' % pid)
        if mtime is not None:
            os.utime(os.path.join(d, 'PROGRESS.txt'), (mtime, mtime))
        return d

    def run_close(self, *args):
        p = subprocess.run(['bash', SCRIPT, '--root', self.root] + list(args),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def progress(self, cid):
        with open(os.path.join(self.root, cid, 'PROGRESS.txt'), encoding='utf-8') as f:
            return f.read()

    def start_fake_runner(self):
        """起一个真 bash 执行名叫 run-bench.sh 的脚本（comm=bash，命令行含文件名），
        和宿主机上 `./run-bench.sh ds 1` 的进程形态一致。"""
        fake_dir = tempfile.mkdtemp(prefix='fake-runner-')
        self.addCleanup(shutil.rmtree, fake_dir, True)
        script = os.path.join(fake_dir, 'run-bench.sh')
        write(script, 'sleep 60\n')
        self.fake = subprocess.Popen(['bash', script])
        time.sleep(0.3)
        return self.fake.pid

    def start_decoy(self):
        """命令行里带 run-bench.sh 但不是 shell（像 vim/tail/ssh 那样）—— 不能算 runner。"""
        p = subprocess.Popen(['bash', '-c', 'exec -a "vim run-bench.sh" sleep 60'])
        self._decoy_proc = p
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.3)
        return p.pid

    # ---- 用例
    def test_closes_killed_campaign_with_real_counts_and_keeps_mtime(self):
        old = time.time() - 6 * 86400
        self.camp('killed', HEADER % 'killed' + '[2026-10-21 09:00:57] [2] x write race 开始\n',
                  races=RACES, mtime=old)
        self.camp('done', HEADER % 'done' + END_NATURAL, races=RACES, mtime=old)

        out = self.run_close('--dry-run')
        self.assertIn('WOULD killed  2/3', out)
        self.assertNotIn('done', out.split('----')[0])            # 扫描模式不刷已收尾的
        self.assertFalse(Q.parse_progress(self.progress('killed'))['ended'])  # 预演不落盘

        out = self.run_close('--reason', '单测')
        self.assertIn('CLOSE killed  2/3', out)
        prog = self.progress('killed')
        p = Q.parse_progress(prog)
        self.assertTrue(p['ended'])
        self.assertEqual((p['end_valid'], p['end_total']), (2, 3))
        self.assertIn('中止: 单测', prog)
        self.assertIn('CAMP_ID=killed', prog)
        self.assertAlmostEqual(os.path.getmtime(os.path.join(self.root, 'killed', 'PROGRESS.txt')),
                               old, delta=1)
        # 已自然结束的一行没多
        self.assertEqual(self.progress('done'), HEADER % 'done' + END_NATURAL)

        # 摘要（计数语义）与 pick_active 也认：全收尾 → 默认看最新的当历史
        sums = {s['cid']: s for s in Q.campaign_summaries(Q.Source(self.root))}
        self.assertTrue(sums['killed']['ended'] and sums['done']['ended'])
        self.assertEqual((sums['killed']['valid'], sums['killed']['total']), (2, 3))

        # 幂等：再跑一次没有可收尾的
        self.assertIn('没有待收尾', self.run_close())

    def test_resumed_then_killed_needs_new_end_line(self):
        """起跑头 → 结束 → 再起跑头（续跑）→ 被杀：旧结束行不算，要补新的。"""
        text = HEADER % 'resume' + END_NATURAL + HEADER % 'resume' + '[..] ibs wipe 完成\n'
        self.camp('resume', text, races=RACES, mtime=time.time() - 86400)
        self.assertFalse(Q.parse_progress(text)['ended'])
        self.run_close()
        self.assertTrue(Q.parse_progress(self.progress('resume'))['ended'])

    def test_explicit_cids_report_ended_and_missing(self):
        self.camp('done', HEADER % 'done' + END_NATURAL)
        out = self.run_close('done', 'nope')
        self.assertIn('SKIP  done：已有结束行', out)
        self.assertIn('SKIP  nope：没有 PROGRESS.txt', out)

    def test_dead_pid_file_is_closed_without_races(self):
        self.camp('deadpid', HEADER % 'deadpid', pid=999999)
        out = self.run_close()
        self.assertIn('CLOSE deadpid  0/0', out)
        p = Q.parse_progress(self.progress('deadpid'))
        self.assertTrue(p['ended'])
        self.assertEqual((p['end_valid'], p['end_total']), (0, 0))

    def test_live_runner_is_protected(self):
        pid = self.start_fake_runner()
        self.camp('live', HEADER % 'live', pid=pid)                       # pid 文件指向活 runner
        self.camp('fresh', HEADER % 'fresh')                              # 旧式无 pid 文件、刚更新
        self.camp('stale', HEADER % 'stale', mtime=time.time() - 3600)    # 旧式无 pid 文件、早停了
        out = self.run_close()
        self.assertIn('LIVE  live：RUNNER.pid', out)
        self.assertIn('LIVE  fresh：宿主机有 run-bench.sh', out)
        self.assertIn('CLOSE stale', out)
        self.assertFalse(Q.parse_progress(self.progress('live'))['ended'])
        self.assertFalse(Q.parse_progress(self.progress('fresh'))['ended'])
        # --force 只放开"进度刚更新"这条启发式，pid 活着的仍然不动
        out = self.run_close('--force')
        self.assertIn('CLOSE fresh', out)
        self.assertIn('LIVE  live', out)

    def test_prefix_mode_only_touches_prefix(self):
        self.camp('ds2c-20261021-1', HEADER % 'ds2c-20261021-1', mtime=time.time() - 3600)
        self.camp('ds2-20261021-1', HEADER % 'ds2-20261021-1', mtime=time.time() - 3600)
        out = self.run_close('--prefix', 'ds2c-')
        self.assertIn('CLOSE ds2c-20261021-1', out)
        self.assertNotIn('ds2-20261021-1', out)
        self.assertFalse(Q.parse_progress(self.progress('ds2-20261021-1'))['ended'])

    def test_decoy_with_filename_in_cmdline_is_not_a_runner(self):
        """vim/tail/ssh 的命令行里带 run-bench.sh 不算 runner：刚更新的旧式 campaign 照常收尾。"""
        self.start_decoy()
        self.camp('fresh', HEADER % 'fresh')
        self.assertIn('CLOSE fresh', self.run_close())

    def test_rejects_bad_args(self):
        for args in (['../etc'], ['--stale-min', 'abc'], ['--prefix', 'a-', 'b']):
            p = subprocess.run(['bash', SCRIPT, '--root', self.root] + args,
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, args)
        p = subprocess.run(['bash', SCRIPT, '--root', self.root, '--print-root'],
                           capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), self.root)

    def test_stop_campaign_kills_runner_and_closes(self):
        """stop-campaign.sh 端到端：杀真 runner、放过 decoy、停完补结束行（约 9 秒）。"""
        pid = self.start_fake_runner()
        decoy = self.start_decoy()
        self.camp('ds9-20261027-1', HEADER % 'ds9-20261027-1', races=RACES, pid=pid)
        env = dict(os.environ, RESULTS_ROOT=self.root)
        p = subprocess.run(['bash', os.path.join(ROOT_DIR, 'stop-campaign.sh'), 'ds9-20261027-1'],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('TERM %d' % pid, p.stdout)
        self.assertNotIn('TERM %d' % decoy, p.stdout)
        self.assertNotIn('KILL %d' % decoy, p.stdout)
        self.assertIn('无残留', p.stdout)
        self.assertIn('CLOSE ds9-20261027-1  2/3', p.stdout)
        self.assertIsNotNone(self.fake.poll())                       # runner 真死了
        self.assertIsNone(self._decoy_proc.poll())                   # decoy 还活着
        prog = self.progress('ds9-20261027-1')
        self.assertTrue(Q.parse_progress(prog)['ended'])
        self.assertIn('stop-campaign.sh 停止', prog)


if __name__ == '__main__':
    unittest.main()
