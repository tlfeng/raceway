#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩变体的两套推导必须一致（压测机 bash ↔ 编排台 python）

"压缩 × source_reuse" 的变体推导有两套实现：

| 实现 | 文件 | 谁用 |
|---|---|---|
| bash | `vm-bench-lib.sh` 的 track_canonical / track_base / track_compression / track_source_reuse / track_engines | runner（起跑前 fail-fast）、prepare 脚本 |
| python | `bench-history.py` 的同名函数 | 编排台（臂×变体兼容矩阵）、入库/覆盖判定 |

两边**必须**给出同样的答案，否则会出现"编排台说能跑、runner 跑不了"，
或者"覆盖判定把变体认错"—— 后者在压测里就是**结论不可信**。

本测试**直接执行 bash 那套**（不是解析源码，解析会随写法变化而脆），
再与 python 侧逐项比对。跑法：
    python3 -m unittest discover -s tests -t . -v
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

H = importlib.import_module('bench-history')    # noqa: E402
C = importlib.import_module('bench-compose')    # noqa: E402
E = importlib.import_module('bench-export')     # noqa: E402  （codec 两维度拆分的实现在这）


def bash_derive(fn, ids):
    """在 bash 里跑 vm-bench-lib.sh 的推导函数 → {id: stdout}。

    用临时目录放一份 lib + 空 vm-bench.env（真身不入库），避免依赖现场配置。
    """
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
        with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8') as f:
            f.write('ROOT=/opt/raceway\n')
        quoted = ' '.join("'%s'" % i for i in ids)
        script = (
            "source '%s/vm-bench-lib.sh' 2>/dev/null\n"
            "for i in %s; do printf '%%s\\t' \"$i\"; %s \"$i\"; echo; done\n"
            % (d, quoted, fn))
        p = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
        out = {}
        for line in p.stdout.splitlines():
            if '\t' in line:
                k, _, v = line.partition('\t')
                out[k] = v.strip()
        return out


class TestDerivationParity(unittest.TestCase):
    """bash 与 python 的推导结果必须逐项一致。"""

    def setUp(self):
        self.ids = sorted(set(list(C.TRACK_CATALOG) + list(H.CANONICAL_ALIAS)))

    def test_canonical_id(self):
        b = bash_derive('track_canonical', self.ids)
        for i in self.ids:
            self.assertEqual(b.get(i), H.canonical_id(i), 'canonical 不一致: %s' % i)

    def test_base_of(self):
        b = bash_derive('track_base', self.ids)
        for i in self.ids:
            self.assertEqual(b.get(i), H.base_of(i), 'base 不一致: %s' % i)

    def test_compression(self):
        b = bash_derive('track_compression', self.ids)
        for i in self.ids:
            want = H.compression_of(i) or ''
            self.assertEqual(b.get(i, ''), want, '压缩不一致: %s' % i)

    def test_source_reuse(self):
        b = bash_derive('track_source_reuse', self.ids)
        for i in self.ids:
            want = '' if H.source_reuse_of(i) is None else (
                'true' if H.source_reuse_of(i) else 'false')
            self.assertEqual(b.get(i, ''), want, 'source_reuse 不一致: %s' % i)

    def test_engines(self):
        b = bash_derive('track_engines', self.ids)
        for i in self.ids:
            want = H.engines_of(i) or ''
            self.assertEqual(b.get(i, ''), want, '引擎约束不一致: %s' % i)


class TestCatalog(unittest.TestCase):
    """编排台的两层清单。"""

    def test_every_base_has_six_variants(self):
        for base in C.BASE_TRACKS:
            vs = [v for v in C.TRACK_CATALOG.values()
                  if v['base'] == base and v['variant']]
            self.assertEqual(len(vs), 6, '基础赛道 %s 的变体数不对' % base)
            combos = set((v['compression'], v['source_reuse']) for v in vs)
            for c in ('default', 'ZSTD', 'best_compression'):
                for sr in (False, True):
                    self.assertIn((c, sr), combos,
                                  '基础赛道 %s 缺少组合 压缩=%s sr=%s' % (base, c, sr))

    def test_variant_id_matches_dir_naming(self):
        """变体 id 必须与变体目录名一致（runner 按 id 找目录）。"""
        for name, v in C.TRACK_CATALOG.items():
            if not v['variant']:
                continue
            self.assertEqual(name, '%s-codec-%s%s' % (
                v['base'],
                {'default': 'def', 'ZSTD': 'zstd', 'best_compression': 'bc'}[v['compression']],
                '-sr' if v['source_reuse'] else ''),
                '变体 id 与目录命名规则不符: %s' % name)

    def test_historical_aliases_preserved(self):
        """历史短名不能改：旧 campaign 的场次名（…-gzstd-r1）依赖它。"""
        self.assertEqual(H.canonical_id('gdef'), 'geo-codec-def')
        self.assertEqual(H.canonical_id('gzstd'), 'geo-codec-zstd-sr')
        self.assertEqual(H.canonical_id('gbc'), 'geo-codec-bc')
        self.assertEqual(H.compression_of('gzstd'), 'ZSTD')
        self.assertTrue(H.source_reuse_of('gzstd'))

    def test_engine_constraint_rules(self):
        """default / best_compression + sr 关 = 两侧通用；其余仅 Easysearch。"""
        self.assertIsNone(H.engines_of('geo-codec-def'))
        self.assertIsNone(H.engines_of('geo-codec-bc'))
        self.assertEqual(H.engines_of('geo-codec-def-sr'), 'easysearch')
        self.assertEqual(H.engines_of('geo-codec-zstd'), 'easysearch')
        self.assertEqual(H.engines_of('geo-codec-zstd-sr'), 'easysearch')
        self.assertEqual(H.engines_of('geo-codec-bc-sr'), 'easysearch')
        # 基础赛道（非变体）无约束
        self.assertIsNone(H.engines_of('geo'))
        self.assertIsNone(H.engines_of('ds-j1024'))


class TestPrepareDefaults(unittest.TestCase):
    """prepare 脚本的默认变体集合要与清单里的 geo 族一致。"""

    def test_default_variants_match_catalog(self):
        with open(os.path.join(ROOT, 'prepare-codec-track.sh'), encoding='utf-8') as f:
            text = f.read()
        line = [l for l in text.splitlines() if l.startswith('DEFAULT_VARIANTS=')][0]
        defaults = line.split('=', 1)[1].strip().strip('"').split()
        geo_family = sorted(n for n, v in C.TRACK_CATALOG.items()
                            if v['base'] == 'geo' and v['variant'])
        self.assertEqual(sorted(defaults), geo_family,
                         'prepare 默认变体集合与清单里的 geo 族不一致')

    def test_prepare_accepts_any_base(self):
        """prepare 必须支持任意基础赛道（不只是 geonames）。"""
        with open(os.path.join(ROOT, 'prepare-codec-track.sh'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('track_base', text)      # 基础赛道由规则推导
        self.assertIn('track_canonical', text)  # 历史短名归一
        self.assertIn('targets=("$@")', text)   # 可传变体 id


class TestDatasetLabels(unittest.TestCase):
    """ds-* 族的两套数据集标注：血统字母必须与数据集对上。"""

    def test_every_ds_track_has_dataset(self):
        for name, v in C.TRACK_CATALOG.items():
            if v['variant'] or not name.startswith('ds-'):
                continue
            self.assertTrue(v.get('dataset'), 'ds 赛道 %s 没标数据集' % name)

    def test_lineage_letter_matches_dataset(self):
        """赛道名里的血统字母（j=jina 系 / g=GIST 系）必须与标注一致。"""
        for name, v in C.TRACK_CATALOG.items():
            if v['variant'] or not name.startswith('ds-'):
                continue
            letter = name[3]
            ds = (v.get('dataset') or '').lower()
            self.assertTrue(
                (letter == 'j' and 'jina' in ds) or (letter == 'g' and 'gist' in ds),
                '赛道 %s 的血统字母与数据集标注对不上: %s' % (name, v.get('dataset')))

    def test_dataset_inherited_by_variants(self):
        """变体的数据集与其基础赛道一致。"""
        for name, v in C.TRACK_CATALOG.items():
            if not v['variant']:
                continue
            base = C.TRACK_CATALOG.get(v['base'])
            self.assertEqual(v.get('dataset'), base.get('dataset'),
                             '变体 %s 的数据集标注与基础赛道不一致' % name)


class TestComposeCompat(unittest.TestCase):
    """编排台的臂 × 变体兼容检查。"""

    def test_es_arm_cannot_use_easysearch_only_variants(self):
        from collections import namedtuple
        Arm = namedtuple('Arm', 'name label engine build jdk_dir config')
        es = Arm('es819', 'es819', 'elasticsearch', '', '', 'x')
        ez = Arm('jdk25', 'jdk25', 'easysearch', '3015', '', 'x')
        rows = [('es819', 'geo-codec-zstd-sr', 1), ('es819', 'geo-codec-def', 1),
                ('jdk25', 'geo-codec-zstd-sr', 1)]
        bad = C.compat_problems([es, ez], rows)
        self.assertEqual([b[1] for b in bad], ['geo-codec-zstd-sr'])
        self.assertIn('easysearch', bad[0][2])

    def test_common_variants_allow_both_engines(self):
        from collections import namedtuple
        Arm = namedtuple('Arm', 'name label engine build jdk_dir config')
        es = Arm('es819', 'es819', 'elasticsearch', '', '', 'x')
        ez = Arm('jdk25', 'jdk25', 'easysearch', '3015', '', 'x')
        rows = [('es819', 'geo-codec-def', 1), ('jdk25', 'geo-codec-def', 1),
                ('es819', 'geo-codec-bc', 1), ('jdk25', 'geo-codec-bc', 1)]
        self.assertEqual(C.compat_problems([es, ez], rows), [])

    def test_non_variant_tracks_have_no_constraint(self):
        from collections import namedtuple
        Arm = namedtuple('Arm', 'name label engine build jdk_dir config')
        es = Arm('es819', 'es819', 'elasticsearch', '', '', 'x')
        rows = [('es819', 'ds-j1024', 1), ('es819', 'nested', 1), ('es819', 'geo', 1)]
        self.assertEqual(C.compat_problems([es], rows), [])


class TestHistoryVariantDimension(unittest.TestCase):
    """覆盖判定必须按变体区分（否则 6 个变体的历史混成一格）。"""

    def _row(self, **kw):
        d = {'campaign': 'c1', 'fid': 'x', 'arm': 'jdk25', 'arm_build': 'jdk25@3015',
             'track': 'geo', 'round': 1, 'status': 'VALID', 'quality': 'ok',
             'phase': 'w', '@timestamp': '2026-10-21T02:30:00Z'}
        d.update(kw)
        return d

    def test_base_and_default_variant_are_distinguishable(self):
        """基座 geo 与 geo-codec-def 的**实测 codec 相同**（引擎默认就是 LZ4），
        只能靠 track_raw（目录名）区分 —— 靠 codec 字段会把两者混成同一格。"""
        rows = [self._row(track_raw='track', codec='default', source_reuse='false'),
                self._row(track_raw='geo-codec-def', codec='default', source_reuse='false')]
        cov = H.build_coverage(rows)
        self.assertIn(('jdk25', 'geo', 1), cov.cells)
        self.assertIn(('jdk25', 'geo-codec-def', 1), cov.cells)
        self.assertEqual(len(cov.cells), 2)

    def test_different_compression_are_different_cells(self):
        rows = [self._row(track_raw='geo-codec-def', codec='default',
                          source_reuse='false', phase='w'),
                self._row(track_raw='geo-codec-def', codec='default',
                          source_reuse='false', phase='q'),
                self._row(track_raw='geo-codec-zstd-sr', codec='ZSTD',
                          source_reuse='true', phase='w'),
                self._row(track_raw='geo-codec-zstd-sr', codec='ZSTD',
                          source_reuse='true', phase='q')]
        cov = H.build_coverage(rows)
        self.assertIn(('jdk25', 'geo-codec-def', 1), cov.cells)
        self.assertIn(('jdk25', 'geo-codec-zstd-sr', 1), cov.cells)
        self.assertEqual(len(cov.cells), 2, '不同压缩的变体被混到同一格了')

    def test_source_reuse_is_separate_dimension(self):
        rows = [self._row(track_raw='geo-codec-def', codec='default', source_reuse='false'),
                self._row(track_raw='geo-codec-def-sr', codec='default', source_reuse='true')]
        cov = H.build_coverage(rows)
        self.assertIn(('jdk25', 'geo-codec-def', 1), cov.cells)
        self.assertIn(('jdk25', 'geo-codec-def-sr', 1), cov.cells)

    def test_legacy_combined_value_maps_to_legacy_variant(self):
        """旧数据 user_tags.codec="ZSTD+source_reuse" → geo-codec-zstd-sr。"""
        comp, sr = E.codec_dims({'user-tags': {'codec': 'ZSTD+source_reuse'}}, None)
        self.assertEqual(comp, 'ZSTD')
        self.assertEqual(sr, 'true')
        self.assertEqual(H.variant_of('geo', comp, sr), 'geo-codec-zstd-sr')

    def test_non_codec_row_keeps_base_track(self):
        cov = H.build_coverage([self._row(track='ds-j1024', track_raw='ds-j1024',
                                         codec=None, source_reuse=None)])
        self.assertIn(('jdk25', 'ds-j1024', 1), cov.cells)

    def test_legacy_combined_codec_value_is_split(self):
        """旧数据 codec="ZSTD+source_reuse" → 拆成 (ZSTD, true)。"""
        self.assertEqual(H.split_codec('ZSTD+source_reuse'), ('ZSTD', 'true'))
        self.assertEqual(H.split_codec('default'), ('default', None))
        self.assertEqual(H.split_codec('default', 'true'), ('default', 'true'))


if __name__ == '__main__':
    unittest.main()
