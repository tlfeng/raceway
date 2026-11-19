#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pkg/ 自动车推导（pkg_auto_cars）：新包放进 pkg/ 即可编排，不用手写车定义。

守的契约：
  - 包名 → 车名的确定性推导（同包同名，跨 campaign 身份稳定、结果可合池）
  - 命名规则与 runner resolve_engine / api_assets 三条一一对应
  - 已被车定义覆盖的 (engine, ver, build) 不重复出现；车定义同名车文件优先
  - JDK 包 / 认不出的文件不成车；非目录内容不炸
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
C = importlib.import_module('bench-compose')

EZ = 'easysearch-2.6.0-3020-linux-amd64-bundle.tar.gz'
EZ_OLD = 'easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz'
ES = 'elasticsearch-8.19.23-linux-x86_64.tar.gz'
OS = 'opensearch-3.3.2-linux-x64.tar.gz'
JDK = 'openjdk-27_linux-x64_bin.tar.gz'
JUNK = 'agent-patched-1.18.15.jar'


def car(name, config):
    """与 parse_cars 同口径：从配置串解出 engine/build 等字段（pkg_auto_cars 读字段）。"""
    kv = dict(t.split('=', 1) for t in config.split() if '=' in t)
    return C.Car(name=name, label=kv.get('CAR_LABEL', name), engine=kv.get('ENGINE_NAME', ''),
                 build=kv.get('ENGINE_BUILD', ''), jdk_dir=kv.get('JDK_DIR', ''), config=config)


def cfg(**kv):
    return ' '.join('%s=%s' % t for t in kv.items())


def mkpkg(*names):
    """造一个最小仓库根：<root>/pkg/ 下放包文件。"""
    root = tempfile.mkdtemp(prefix='rw-root-')
    d = os.path.join(root, 'pkg')
    os.mkdir(d)
    for n in names:
        with open(os.path.join(d, n), 'w') as f:
            f.write('x')
    return root


class TestPkgAutoCars(unittest.TestCase):
    def test_derives_engine_packages_deterministically(self):
        d = mkpkg(EZ, ES, OS)
        cars = C.pkg_auto_cars(d, [])
        self.assertEqual([a.name for a in cars], ['ez260-3020', 'es81923', 'os332'])
        self.assertEqual(cars[0].engine, 'easysearch')
        self.assertEqual(cars[0].build, '3020')
        self.assertEqual(cars[0].config,
                         'ENGINE_NAME=easysearch ENGINE_VERSION=2.6.0 ENGINE_BUILD=3020 CAR_LABEL=ez260-3020')
        self.assertEqual(cars[1].config,
                         'ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es81923')
        self.assertEqual(cars[1].build, '')

    def test_jdk_and_unknown_files_are_not_cars(self):
        d = mkpkg(JDK, JUNK)
        self.assertEqual(C.pkg_auto_cars(d, []), [])

    def test_file_car_covers_same_engine_version_build(self):
        d = mkpkg(EZ, EZ_OLD)
        # 车定义里已有 easysearch 2.6.0-3020（显式）→ 只有老包冒出来
        cars = C.pkg_auto_cars(d, [car('ez260', cfg(ENGINE_NAME='easysearch', ENGINE_VERSION='2.6.0',
                                                     ENGINE_BUILD='3020', CAR_LABEL='ez260'))])
        self.assertEqual([a.name for a in cars], ['ez250-3015'])

    def test_file_car_default_version_counts_as_covered(self):
        d = mkpkg(EZ_OLD)
        # runner 口径：easysearch 缺省 2.5.0/3015 —— 省略键也等于已覆盖
        cars = C.pkg_auto_cars(d, [car('ez3015', cfg(ENGINE_NAME='easysearch', CAR_LABEL='ez3015'))])
        self.assertEqual(C.pkg_auto_cars(d, [car('x', cfg(ENGINE_NAME='easysearch',
                                                          ENGINE_VERSION='2.5.0'))]), [])
        self.assertEqual(cars, [])

    def test_name_collision_file_wins(self):
        d = mkpkg(EZ)
        # 车定义里恰好有一台叫 ez260-3020 的车：包不再自动出现（文件优先）
        cars = C.pkg_auto_cars(d, [car('ez260-3020', cfg(ENGINE_NAME='easysearch', ENGINE_VERSION='9.9.9',
                                                         ENGINE_BUILD='1', CAR_LABEL='ez260-3020'))])
        self.assertEqual(cars, [])

    def test_empty_or_missing_pkg_dir(self):
        self.assertEqual(C.pkg_auto_cars(tempfile.mkdtemp(prefix='rw-empty-'), []), [])
        self.assertEqual(C.pkg_auto_cars(os.path.join(tempfile.mkdtemp(), 'nope'), []), [])

    def test_snapshot_accepts_auto_car(self):
        """自动车走同一套 make_snapshot/validate_cars：render_cars 能渲染出车定义文件。"""
        d = mkpkg(EZ)
        auto = C.pkg_auto_cars(d, [])
        rows = C.interleave(auto, ['geonames'], 1)
        snap = C.make_snapshot(auto, rows, None, {}, 40, True, 'geo', 0)
        self.assertIn('ez260-3020', snap.cars_text)
        self.assertIn('ENGINE_VERSION=2.6.0', snap.cars_text)
        self.assertFalse([m for s, m in C.validate_cars(auto) if s == 'error'])


if __name__ == '__main__':
    unittest.main()
