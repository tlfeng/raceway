#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩变体的两套推导必须一致（压测机 bash ↔ 编排台 python）

"压缩 × source_reuse" 的变体推导有两套实现：

| 实现 | 文件 | 谁用 |
|---|---|---|
| bash | `vm-bench-lib.sh` 的 track_canonical / track_base / track_compression / track_source_reuse / track_engines | runner（起跑前 fail-fast）、prepare 脚本 |
| python | `bench-history.py` 的同名函数 | 编排台（车×变体兼容矩阵）、入库/覆盖判定 |

两边**必须**给出同样的答案，否则会出现"编排台说能跑、runner 跑不了"，
或者"覆盖判定把变体认错"—— 后者在压测里就是**结论不可信**。

本测试**直接执行 bash 那套**（不是解析源码，解析会随写法变化而脆），
再与 python 侧逐项比对。跑法：
    python3 -m unittest discover -s tests -t . -v
"""
import importlib
import os
import re
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


_BASH_IS_WSL = None


def _bash_path(p):
    """把本地路径转成 bash 看得见的路径。

    Windows 上的 bash 通常是 WSL：它看不见 `C:\\...`，得写成 `/mnt/c/...`，
    否则 source 静默失败、函数全部没定义（测试表现为"函数返回空"）。
    Git Bash 看得见 Windows 路径，所以先问 bash 自己是哪一种。
    """
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


def _run_bash(script, workdir):
    """把脚本写成文件再执行。

    不能用 `bash -c "<脚本>"`：在 Windows 上这一层传参会吃掉脚本里的引号
    （`"$i"` 变成空串），bash 收到的脚本被改了样 —— 表现为变量全空、
    函数返回空。写成文件走 `bash <file>` 才不会走那层转义。
    """
    sp = os.path.join(workdir, '_probe.sh')
    with open(sp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(script)
    return subprocess.run(['bash', _bash_path(sp)], capture_output=True, text=True)


def bash_derive(fn, ids):
    """在 bash 里跑 vm-bench-lib.sh 的推导函数 → {id: stdout}。

    用临时目录放一份 lib + 空 vm-bench.env（真身不入库），避免依赖现场配置。
    """
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
        with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('ROOT=/opt/raceway\n')
        quoted = ' '.join("'%s'" % i for i in ids)
        script = (
            "cd '%s' || exit 1\n"
            "source ./vm-bench-lib.sh 2>/dev/null\n"
            "for i in %s; do printf '%%s\\t' \"$i\"; %s \"$i\"; echo; done\n"
            % (_bash_path(d), quoted, fn))
        p = _run_bash(script, d)
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


class TestTrackParams(unittest.TestCase):
    """分片口径：canonical 变体与 legacy 别名必须拿到同一份 track-params。

    geonames 的 index.json 是 Jinja 模板，分片数写的是
    `{{number_of_shards | default(5)}}` —— 不传 track-params 就落到模板默认的
    **5 分片**，而本项目其余赛道都是 3 分片。一旦漏传，同一条赛道的新旧场次
    分片数就不同（跨变体/跨赛道的数字直接不可比），而且**不会报错**。

    这条曾经真的漏过：runner 原来按 id 名单判断（只列了 geo/nested 与几个
    legacy 短别名），canonical 名（geo-codec-*）不在名单里 → 静默走 5 分片。
    """

    PARAMS = 'number_of_shards:3,number_of_replicas:0'

    def test_geo_family_gets_shard_params(self):
        """模板类基座（geo 族 + nested）必须传分片口径。"""
        ids = ['geo', 'nested',
               'gdef', 'gzstd', 'gbc',
               'geo-codec-def', 'geo-codec-def-sr',
               'geo-codec-zstd', 'geo-codec-zstd-sr',
               'geo-codec-bc', 'geo-codec-bc-sr']
        b = bash_derive('track_params', ids)
        for i in ids:
            self.assertIn(i, b, 'bash function did not run: %s' % i)
            self.assertEqual(b[i], self.PARAMS, '分片口径不对: %s' % i)

    def test_all_codec_names_share_one_params(self):
        """6 个 canonical 变体 + 3 个 legacy 别名，分片口径必须完全一致。

        这里断言的是**具体值**，不是"canonical 与 legacy 相等" —— 只比相等的话，
        两边同时为空（函数根本没跑起来）也会通过，等于没测。
        """
        ids = ['gdef', 'gzstd', 'gbc',
               'geo-codec-def', 'geo-codec-def-sr',
               'geo-codec-zstd', 'geo-codec-zstd-sr',
               'geo-codec-bc', 'geo-codec-bc-sr']
        b = bash_derive('track_params', ids)
        for i in ids:
            self.assertIn(i, b, 'bash function did not run: %s' % i)
            self.assertEqual(b[i], self.PARAMS, '分片口径不对: %s' % i)

    def test_baked_tracks_get_nothing(self):
        """分片已烘进 index.json 的赛道不传 track-params。"""
        ids = ['hnsw', 'hv1024', 'ds-j1024', 'ds-g1920',
               'hnsw-codec-zstd', 'hnsw-codec-def']
        b = bash_derive('track_params', ids)
        for i in ids:
            self.assertIn(i, b, 'bash function did not run: %s' % i)
            self.assertEqual(b[i], '', '不该传分片参数: %s' % i)

    def test_shards_knob_is_honored(self):
        """SHARDS / REPLICAS 旋钮要真的生效（不能写死）。"""
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
            with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
                f.write('ROOT=/opt/raceway\n')
            script = (
                "cd '%s' || exit 1\n"
                "source ./vm-bench-lib.sh 2>/dev/null\n"
                "SHARDS=5; REPLICAS=1\n"
                "track_params geo-codec-def\n" % _bash_path(d))
            p = _run_bash(script, d)
        self.assertEqual(p.stdout.strip(), 'number_of_shards:5,number_of_replicas:1')


class TestTrackNameHelpText(unittest.TestCase):
    """文案承诺的赛道名，必须真的能被 runner 接受。

    踩过的坑：文案列了 6 个历史短名，实际 case 只认 3 个 —— 另外 3 个传进去
    直接 die，文案却写着能用。这里做**行为测**（不解析源码）：从 runner 自己的
    文案里抠出它承诺的名字，逐个跑一遍，确认没有"说了不算"的。

    文案有三份（文件头注释 / 无参用法提示 / 错参报错消息），必须列同一份名单。
    """

    REJECT = 'track 参数必须是'
    PH = '\x00'          # 占位符哨兵：<> 里是形态说明，不是能直接跑的名字

    def _prepare(self, d):
        shutil.copy(os.path.join(ROOT, 'run-bench.sh'), d)
        shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
        with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('ROOT=/opt/raceway\nRESULTS_ROOT=/opt/raceway/campaigns\n')

    # ---- 跑 runner --------------------------------------------------------
    def _run(self, d, *args):
        """跑一次 runner，返回 stdout+stderr 合并后的输出。"""
        argv = ' '.join("'%s'" % a for a in args)
        script = ("cd '%s' || exit 1\n"
                  "bash ./run-bench.sh %s 2>&1\n" % (_bash_path(d), argv))
        return _run_bash(script, d).stdout

    def _run_many(self, d, names):
        """一次 bash 里跑完多个名字 → {名字: 输出}。

        逐个起 bash 在 WSL 上不便宜（每次 ~0.15s），一次循环跑完省一个数量级。
        """
        lst = ' '.join("'%s'" % n for n in names)
        script = ("cd '%s' || exit 1\n"
                  "for n in %s; do\n"
                  "  out=$(bash ./run-bench.sh \"$n\" 2>&1)\n"
                  "  printf '===%%s\\n' \"$n\"\n"
                  "  printf '%%s\\n' \"$out\"\n"
                  "done\n" % (_bash_path(d), lst))
        out = _run_bash(script, d).stdout
        res, cur, buf = {}, None, []
        for line in out.splitlines():
            if line.startswith('==='):
                if cur is not None:
                    res[cur] = '\n'.join(buf)
                cur, buf = line[3:], []
            elif cur is not None:
                buf.append(line)
        if cur is not None:
            res[cur] = '\n'.join(buf)
        missing = [n for n in names if n not in res]
        self.assertEqual(missing, [], '批量执行没回收到这些名字的输出: %s' % missing)
        return res

    # ---- 解析文案里的名单 --------------------------------------------------
    def _literal_names(self, listed):
        """从 `a|b|<x>-<y>|<z>` 里取出**字面**名字。

        占位符里也可能带 `|`（如 `<def|zstd|bc>`），所以先把 `<...>` 整段换成
        哨兵再按 `|` 拆 —— 带哨兵的片段是占位符拼出来的形态，不是能直接跑的名字。
        """
        clean = re.sub(r'<[^<>]*>', self.PH, listed)
        out = []
        for tok in clean.split('|'):
            tok = tok.strip()
            if not tok or self.PH in tok:
                continue
            out.append(tok)
        return out

    def _names_in_angle(self, seg):
        """取 `<...>` 里的名单（第一个 `<` 到最后一个 `>` 之间）。"""
        return seg[seg.index('<') + 1: seg.rindex('>')]

    def _usage_names(self, usage):
        """无参用法提示里的名单。"""
        seg = usage.split('用法:', 1)[1].split(' [rounds]', 1)[0]
        return self._literal_names(self._names_in_angle(seg))

    def _error_names(self, err):
        """错参报错消息里的名单。"""
        listed = err.split(self.REJECT, 1)[1].splitlines()[0]
        return self._literal_names(listed)

    def _header_names(self):
        """文件头注释里那行用法里的名单。"""
        with open(os.path.join(ROOT, 'run-bench.sh'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('#   ./run-bench.sh <'):
                    seg = line.split(' [rounds', 1)[0]
                    return self._literal_names(self._names_in_angle(seg))
        self.fail('run-bench.sh 文件头没找到用法行')

    # ---- 用例 --------------------------------------------------------------
    def test_help_texts_agree(self):
        """文件头注释 / 用法提示 / 报错消息必须列同一份名字。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._prepare(d)
            usage = self._run(d)
            err = self._run(d, '__no_such_track__')
        self.assertIn('用法:', usage)
        self.assertIn(self.REJECT, err)
        header = self._header_names()
        self.assertEqual(self._usage_names(usage), header,
                         '文件头注释与用法提示列的名字不一致')
        self.assertEqual(self._error_names(err), header,
                         '报错消息与用法提示列的名字不一致')

    def test_every_name_in_help_is_actually_accepted(self):
        """文案里承诺的每个字面名，都要真的不被赛道名校验拦下。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._prepare(d)
            err = self._run(d, '__no_such_track__')
            self.assertIn(self.REJECT, err)
            names = self._error_names(err)
            self.assertTrue(names, '报错消息里没解析出赛道名: %r' % err)
            res = self._run_many(d, names)
        bad = [n for n in names if self.REJECT in res.get(n, '')]
        self.assertEqual(bad, [], '文案承诺了但实际会被 die 拦下: %s' % bad)

    def test_canonical_codec_form_is_accepted(self):
        """canonical 形态（<赛道>-codec-<def|zstd|bc>[-sr]）是跑单个变体的正确写法。"""
        geo_family = sorted(n for n, v in C.TRACK_CATALOG.items()
                            if v['base'] == 'geo' and v['variant'])
        self.assertEqual(len(geo_family), 6, 'geo 族的 canonical 变体数不对')
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._prepare(d)
            res = self._run_many(d, geo_family)
        bad = [n for n in geo_family if self.REJECT in res.get(n, '')]
        self.assertEqual(bad, [], 'canonical 变体名被拒: %s' % bad)

    def test_removed_legacy_names_are_rejected(self):
        """gdefsr/gzstdns/gbcsr 从未实现过（只进过文案）—— 文案不许再承诺它们。"""
        dead = ['gdefsr', 'gzstdns', 'gbcsr']
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._prepare(d)
            res = self._run_many(d, dead)
        for n in dead:
            self.assertIn(self.REJECT, res.get(n, ''), '%s 本该被拒绝' % n)

    def test_list_menu_names_are_accepted(self):
        """`list` 菜单里列的赛道，也要真的能跑（菜单也是一份文案）。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._prepare(d)
            out = self._run(d, 'list')
            self.assertIn('可用赛道:', out)
            names = out.split('可用赛道:', 1)[1].splitlines()[0].split()
            self.assertTrue(names, 'list 菜单里没解析出赛道名: %r' % out)
            res = self._run_many(d, names)
        bad = [n for n in names if self.REJECT in res.get(n, '')]
        self.assertEqual(bad, [], 'list 菜单里的赛道名被拒: %s' % bad)

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
    """编排台的车 × 变体兼容检查。"""

    def test_es_car_cannot_use_easysearch_only_variants(self):
        from collections import namedtuple
        Car = namedtuple('Car', 'name label engine build jdk_dir config')
        es = Car('es819', 'es819', 'elasticsearch', '', '', 'x')
        ez = Car('jdk25', 'jdk25', 'easysearch', '3015', '', 'x')
        rows = [('es819', 'geo-codec-zstd-sr', 1), ('es819', 'geo-codec-def', 1),
                ('jdk25', 'geo-codec-zstd-sr', 1)]
        bad = C.compat_problems([es, ez], rows)
        self.assertEqual([b[1] for b in bad], ['geo-codec-zstd-sr'])
        self.assertIn('easysearch', bad[0][2])

    def test_common_variants_allow_both_engines(self):
        from collections import namedtuple
        Car = namedtuple('Car', 'name label engine build jdk_dir config')
        es = Car('es819', 'es819', 'elasticsearch', '', '', 'x')
        ez = Car('jdk25', 'jdk25', 'easysearch', '3015', '', 'x')
        rows = [('es819', 'geo-codec-def', 1), ('jdk25', 'geo-codec-def', 1),
                ('es819', 'geo-codec-bc', 1), ('jdk25', 'geo-codec-bc', 1)]
        self.assertEqual(C.compat_problems([es, ez], rows), [])

    def test_non_variant_tracks_have_no_constraint(self):
        from collections import namedtuple
        Car = namedtuple('Car', 'name label engine build jdk_dir config')
        es = Car('es819', 'es819', 'elasticsearch', '', '', 'x')
        rows = [('es819', 'ds-j1024', 1), ('es819', 'nested', 1), ('es819', 'geo', 1)]
        self.assertEqual(C.compat_problems([es], rows), [])


class TestHistoryVariantDimension(unittest.TestCase):
    """覆盖判定必须按变体区分（否则 6 个变体的历史混成一格）。"""

    def _row(self, **kw):
        d = {'campaign': 'c1', 'fid': 'x', 'car': 'jdk25', 'car_build': 'jdk25@3015',
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
