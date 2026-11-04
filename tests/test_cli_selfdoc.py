#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 自解释闸门：不问人也能把 raceway 用起来

目标用户是「拿到压测机、没人能问」的操作者。所以 runner 必须自己说清：
能跑哪些赛道、每个赛道定义在不在、参数怎么写、出错该看哪。

这里把这些**口头承诺**钉成可执行断言，防止文案与实现漂移（仓库里同类漂移已发生过多次：
用法提示列了 6 个历史短名，实际 case 只认 3 个）。

跑法：python3 -m unittest tests.test_cli_selfdoc -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_BASH_IS_WSL = None


def _bash_path(p):
    """把本地路径转成 bash 看得见的路径（Windows 上的 bash 通常是 WSL）。"""
    global _BASH_IS_WSL
    if os.name != 'nt':
        return p
    if _BASH_IS_WSL is None:
        probe = subprocess.run(['bash', '-c', 'test -d /mnt && echo wsl || echo native'],
                               capture_output=True, text=True)
        _BASH_IS_WSL = 'wsl' in probe.stdout
    if not _BASH_IS_WSL:
        return p
    d = os.path.abspath(p).replace('\\', '/')
    if len(d) > 2 and d[1] == ':':
        return '/mnt/' + d[0].lower() + d[2:]
    return d


def _run_bash(script, cwd=None):
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                          cwd=cwd, timeout=120)


class CliSelfDocTest(unittest.TestCase):
    """runner 的自我说明能力（--help / list / 错参提示 / 作用域）"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='raceway-cli-')
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        for f in ('run-bench.sh', 'vm-bench-lib.sh'):
            shutil.copy(os.path.join(ROOT, f), self.d)
        # 假 env：路径都落在临时目录里（绝不碰真机器）
        root = os.path.join(self.d, 'root')
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(self.d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('ROOT=%s\nRESULTS_ROOT=%s/campaigns\nIBS_BASE=/infini\n'
                    'IBS_ROOT=%s/ibs\nPKG_DIR=%s/pkg\n'
                    % (root, root, root, root))
        # 假安装包（只求存在且非空，resolve_engine 只查这两件事）
        pkg = os.path.join(root, 'pkg')
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, 'easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz'), 'w') as f:
            f.write('fake')
        # 假 ibs 副本（ibs_selfcheck 只 grep 两个特征串）
        ops = os.path.join(root, 'ibs', 'ops')
        os.makedirs(ops, exist_ok=True)
        with open(os.path.join(ops, 'status.py'), 'w') as f:
            f.write('# expected_core\n')
        with open(os.path.join(ops, 'clean.py'), 'w') as f:
            f.write('# STILL_RUNNING\n')

    def _run(self, *args):
        argv = ' '.join("'%s'" % a for a in args)
        script = ("cd '%s' || exit 1\n"
                  "bash ./run-bench.sh %s 2>&1\n" % (_bash_path(self.d), argv))
        return _run_bash(script).stdout

    # ---- --help ------------------------------------------------------------
    def test_help_is_available_and_self_sufficient(self):
        """--help 要真能打开，且内容覆盖「怎么跑 / 赛道 / 前置」。"""
        out = self._run('--help')
        self.assertNotIn('用法: $0', out, '--help 没生效：%r' % out[:200])
        for kw in ('用法', '三种编排方式', '赛道', '前置条件', 'list', '--dry-run'):
            self.assertIn(kw, out, '--help 里缺「%s」' % kw)
        self.assertIn('tracks/', out, '--help 没交代仓库同步要带 tracks/')

    def test_help_lists_the_hv1024x_family(self):
        """量化/压缩四臂要能在帮助里被看见（否则用户不知道它们存在）。"""
        out = self._run('--help')
        for name in ('hv1024x', 'hv1024x-int8', 'hv1024x-bbq', 'hv1024x-codec-zstd-sr'):
            self.assertIn(name, out, '--help 里没有 %s' % name)

    # ---- list --------------------------------------------------------------
    def test_list_shows_tracks_and_availability(self):
        """list 要给「有哪些赛道 + 现在能不能跑」，不能只列名字。"""
        out = self._run('list')
        self.assertIn('可用赛道:', out)
        for name in ('geo', 'hv1024', 'nested', 'hv1024x-int8'):
            self.assertIn(name, out, 'list 里没有 %s' % name)
        self.assertIn('现在能不能跑', out, 'list 没标可用性')

    def test_list_menu_names_are_all_accepted(self):
        """list 菜单里列的每个名字，runner 都得认（菜单也是一份承诺）。"""
        out = self._run('list')
        names = out.split('可用赛道:', 1)[1].splitlines()[0].split()
        self.assertTrue(names, 'list 菜单里没解析出赛道名: %r' % out)
        for n in names:
            r = self._run(n)
            self.assertNotIn('track 参数必须是', r, 'list 菜单里的 %s 被拒' % n)

    # ---- 错参 --------------------------------------------------------------
    def test_typo_is_rejected_with_a_suggestion(self):
        """打错名字要当场说清，且给个最接近的（别让人去翻源码）。"""
        out = self._run('hv1024x-int88')
        self.assertIn('track 参数必须是', out)
        self.assertIn('你可能想跑的是：hv1024x-int8', out,
                      '错参没给建议: %r' % out)
        self.assertIn('--help', out, '错参没指向 --help')

    # ---- 作用域：跑 geo 不该被 hnsw 拖累 ------------------------------------
    def test_non_hnsw_track_does_not_require_hnsw_base(self):
        """机器上缺 hnsw 基座定义时，跑 geo 不该起不来（那是别人的坑）。"""
        lib = os.path.join(self.d, 'vm-bench-lib.sh')
        script = ("cd '%s' || exit 1\n"
                  "source ./vm-bench-lib.sh >/dev/null 2>&1\n"
                  "if tracks_need_hnsw_prep geo; then echo 'NEED'; else echo 'NO'; fi\n"
                  "if tracks_need_hnsw_prep geo hv1024x-int8 nested; then echo 'NEED'; else echo 'NO'; fi\n"
                  "if tracks_need_hnsw_prep hnsw; then echo 'NEED'; else echo 'NO'; fi\n"
                  "if tracks_need_hnsw_prep hnsw-codec-zstd; then echo 'NEED'; else echo 'NO'; fi\n"
                  % _bash_path(self.d))
        out = _run_bash(script).stdout.split()
        self.assertEqual(out, ['NO', 'NO', 'NEED', 'NEED'],
                         'hnsw 变体准备的作用域判错了: %r' % out)


class AuditToolTest(unittest.TestCase):
    """audit_track_settings.py：只审计不修复，且不认识 --fix"""

    def _run(self, *args):
        script = ("cd '%s' || exit 1\n"
                  "python3 audit_track_settings.py %s 2>&1\n"
                  % (_bash_path(ROOT), ' '.join("'%s'" % a for a in args)))
        return _run_bash(script)

    def test_fix_flag_is_rejected(self):
        """--fix 必须被明确拒绝：打错旗标最怕得到一句"全部通过"的假 OK。"""
        p = self._run('--fix', 'tracks')
        self.assertNotEqual(p.returncode, 0, '--fix 没被拒绝：%r' % p.stdout)
        self.assertIn('未知参数', p.stdout, '报错没说明为什么：%r' % p.stdout)

    def test_check_passes_on_repo_tracks(self):
        p = self._run('--check', 'tracks')
        self.assertEqual(p.returncode, 0, '仓库里的赛道定义没过结构审计：%r' % p.stdout)
        self.assertIn('全部通过', p.stdout)


class DocsSyncCommandTest(unittest.TestCase):
    """文档里的同步命令必须带 tracks/ —— 漏了就跑不起来"""

    FILES = ('README.md', os.path.join('docs', 'GUIDE.md'))

    def _sync_blocks(self, path):
        """取出所有「同步命令」相关的行及其后两行。"""
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        out = []
        for i, ln in enumerate(lines):
            if re.search(r'\b(scp|rsync|git archive)\b', ln):
                out.append('\n'.join(lines[i:i + 3]))
        return '\n'.join(out)

    def test_sync_command_carries_tracks_dir(self):
        for rel in self.FILES:
            path = os.path.join(ROOT, rel)
            blocks = self._sync_blocks(path)
            self.assertTrue(blocks, '%s 里没有同步命令' % rel)
            self.assertIn('tracks/', blocks,
                          '%s 的同步命令没带 tracks/（赛道定义在 git 里，漏同步就起不来）' % rel)


if __name__ == '__main__':
    unittest.main()
