#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geonames 缩量探针（lite / lite-encrypted）的同源与预算闸门

背景：`encrypted-store` 与 `full` 的查询段按 target-throughput 定死（≈ 78 分钟，
与数据量无关），"只砍数据量"救不了耗时。`challenges/lite.json` 用**同一份 schedule**
经 `_enc` 开关生成两臂（常规 / 加密），只改 create-index 的 store 设置 —— 这里盯三件事：

  1. 必须真的是"一份定义出两臂"（source 里只有一个 schedule，两臂逐任务同序同预算）。
     复制成两个文件就会漂：改了其中一个臂的 iterations，另一臂悄悄不同口径。
  2. 加密臂的键必须落在 `{% if _enc %}` 分支里，且常规臂把 store.type 钉在 operation
     settings —— 否则残留的 `encrypted_store:true` 会把常规臂静默跑成加密臂（结论全废）。
  3. 查询段墙钟 = Σ(warmup + iterations) / target-throughput，必须落在 2-3 分钟：
     上限 180 s 保住 5-10 分钟档，下限 120 s 防止有人把它砍成没有统计意义的空跑。
     写段与 force-merge 随机器浮动，不在断言范围。
  4. runner/lib 真的接了这两个臂（track_challenge / track_base / track_dir /
     track_params 的 LITE_PCT / 引擎与装机门）—— 挑战文件写得再对，
     没人把它映射成挑战名也是白搭（之前只能手工 esrally 跑，编排页看不见）。

没装 jinja2 的机器只跑 source 级断言（渲染类用例 skip）；跑赛道的机器上全量执行。
跑法：
    python3 -m unittest tests.test_geonames_lite -v
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHALLENGE = os.path.join(ROOT, 'tracks', 'geonames', 'challenges', 'lite.json')

ENC = {'index.store.type': 'encrypted', 'index.encryption.profile': 'index'}
DECLARED_PCT = 5          # 与 README / description 对齐
BUDGET_MIN_S, BUDGET_MAX_S = 120, 180

try:
    import jinja2
except ImportError:                                   # 没装就 skip 渲染类用例，不当失败
    jinja2 = None


def source():
    with open(CHALLENGE, encoding='utf-8') as f:
        return f.read()


def rendered(**params):
    """真 jinja2 渲染成两臂列表（track.json 里 collect 之后就是数组里的两个对象）。"""
    env = jinja2.Environment(undefined=jinja2.Undefined)
    out = env.from_string(source()).render(**params)
    return json.loads('[' + out + ']')


def task_name(task):
    op = task['operation']
    return task.get('name') or (op if isinstance(op, str)
                                else op.get('name') or op.get('operation-type'))


def create_settings(doc):
    for task in doc['schedule']:
        op = task['operation']
        if isinstance(op, dict) and op.get('operation-type') == 'create-index':
            return op['settings']
    raise AssertionError('缺少 create-index')


def bulk_op(doc):
    for task in doc['schedule']:
        op = task['operation']
        if isinstance(op, dict) and op.get('operation-type') == 'bulk':
            return op
    raise AssertionError('缺少 bulk')


def query_budget(doc):
    """受 target-throughput 约束的任务墙钟（秒）。"""
    total = 0.0
    for task in doc['schedule']:
        if 'iterations' not in task:
            continue
        tput = task['target-throughput']          # 缺了会在下面 KeyError → 闸门红
        total += (int(task['iterations']) + int(task.get('warmup-iterations', 0))) / float(tput)
    return total


class SourceTest(unittest.TestCase):
    """不依赖 jinja2：盯"一份定义"和几个烘死的坑。"""

    def test_single_schedule_definition(self):
        self.assertEqual(source().count('"schedule"'), 1,
                         'lite 两臂必须共用一份 schedule（复制成两个文件会漂口径）')

    def test_loop_covers_both_arms(self):
        src = source()
        self.assertIn('{% for _enc in [false, true] %}', src)
        self.assertIn('"lite{{ \'-encrypted\' if _enc else \'\' }}"', src)

    def test_arms_separated_by_comma(self):
        """collect 只负责文件之间补逗号；文件内部两臂的逗号得自己出。"""
        self.assertIn("{{ ',' if not loop.last }}", source())

    def test_encrypted_keys_stay_behind_the_flag(self):
        src = source()
        flag = src.find('{% if _enc %}')
        self.assertGreaterEqual(flag, 0, '找不到 _enc 开关')
        self.assertGreater(src.find('"index.store.type": p_enc_store_type'), flag,
                           '加密键必须落在 {% if _enc %} 分支内')
        self.assertGreater(src.find('"index.encryption.profile": "index"'), flag,
                           'profile 键必须落在 {% if _enc %} 分支内')

    def test_default_data_volume_is_small(self):
        self.assertIn('lite_ingest_percentage | default(%d)' % DECLARED_PCT, source())

    def test_no_bulk_warmup_period(self):
        """上游 70 s 预热在 5% 语料下会把整段吞进 warmup → 测量样本为零。"""
        self.assertNotIn('warmup-time-period', source())


@unittest.skipIf(jinja2 is None, '没装 jinja2（跑赛道的机器上会执行）')
class RenderTest(unittest.TestCase):
    def setUp(self):
        self.arms = rendered()
        self.plain = next(c for c in self.arms if c['name'] == 'lite')
        self.enc = next(c for c in self.arms if c['name'] == 'lite-encrypted')

    def test_two_arms_with_expected_names(self):
        self.assertEqual([c['name'] for c in self.arms], ['lite', 'lite-encrypted'])

    def test_arms_identical_except_store_settings(self):
        def normalized(doc):
            out = []
            for task in doc['schedule']:
                t = json.loads(json.dumps(task))
                op = t['operation']
                if isinstance(op, dict) and op.get('operation-type') == 'create-index':
                    op.pop('settings', None)
                out.append(t)
            return out
        self.assertEqual(normalized(self.plain), normalized(self.enc),
                         '两臂除 store 设置外必须逐任务同序同预算')

    def test_store_settings_per_arm(self):
        self.assertEqual(create_settings(self.plain)['index.store.type'], 'fs')
        self.assertNotIn('index.encryption.profile', create_settings(self.plain))
        for k, v in ENC.items():
            self.assertEqual(create_settings(self.enc)[k], v)

    def test_opensource_cryptofs_arm(self):
        """OpenSearch storage-encryption：store.type=cryptofs + key_provider，
        且不得带 Easysearch 的 index.encryption.profile（OpenSearch 会拒收未知设置）。"""
        arms = rendered(enc_store_type='cryptofs',
                        enc_index_settings={'index.store.crypto.key_provider': 'dummy'})
        plain = next(c for c in arms if c['name'] == 'lite')
        enc = next(c for c in arms if c['name'] == 'lite-encrypted')
        self.assertEqual(create_settings(plain)['index.store.type'], 'fs')
        s = create_settings(enc)
        self.assertEqual(s['index.store.type'], 'cryptofs')
        self.assertEqual(s['index.store.crypto.key_provider'], 'dummy')
        self.assertNotIn('index.encryption.profile', s)

    def test_cryptofs_bakes_default_key_provider_without_json_params(self):
        """rally CLI 传不了 JSON：只传标量 enc_store_type=cryptofs 时，模板必须自己
        烘出 key_provider 默认值（dummy），crypto_key_provider 标量旋钮可覆盖，
        enc_index_settings（渲染侧）优先级最高。"""
        enc = next(c for c in rendered(enc_store_type='cryptofs') if c['name'] == 'lite-encrypted')
        self.assertEqual(create_settings(enc)['index.store.crypto.key_provider'], 'dummy')
        enc = next(c for c in rendered(enc_store_type='cryptofs',
                                       crypto_key_provider='aws-kms')
                   if c['name'] == 'lite-encrypted')
        self.assertEqual(create_settings(enc)['index.store.crypto.key_provider'], 'aws-kms')

    def test_encrypted_profile_still_baked_for_easysearch_arm(self):
        """Easysearch 默认语义不回归：encrypted + profile=index，无 cryptofs 杂键。"""
        enc = next(c for c in rendered() if c['name'] == 'lite-encrypted')
        s = create_settings(enc)
        self.assertEqual(s['index.store.type'], 'encrypted')
        self.assertEqual(s['index.encryption.profile'], 'index')
        self.assertNotIn('index.store.crypto.key_provider', s)

    def test_stray_encrypted_param_cannot_poison_plain_arm(self):
        """index.json 会按 encrypted_store 写加密键，常规臂必须用 operation settings 压回去。"""
        plain = next(c for c in rendered(encrypted_store=True) if c['name'] == 'lite')
        self.assertEqual(create_settings(plain)['index.store.type'], 'fs')

    def test_data_volume_is_parametrized(self):
        self.assertEqual(bulk_op(self.plain)['ingest-percentage'], DECLARED_PCT)
        arm = next(c for c in rendered(lite_ingest_percentage=2) if c['name'] == 'lite')
        self.assertEqual(bulk_op(arm)['ingest-percentage'], 2)

    def test_query_budget_stays_in_the_5_10_min_window(self):
        for doc in (self.plain, self.enc):
            with self.subTest(arm=doc['name']):
                sec = query_budget(doc)
                self.assertGreaterEqual(sec, BUDGET_MIN_S, '查询段太短，没有统计意义：%.0fs' % sec)
                self.assertLessEqual(sec, BUDGET_MAX_S,
                                     '查询段 %.0fs 超预算（写段+合并另算，总时长会越过 10 分钟）' % sec)

    def test_no_unthrottled_measuring_task(self):
        for doc in (self.plain, self.enc):
            for task in doc['schedule']:
                if 'iterations' in task:
                    self.assertIn('target-throughput', task,
                                  '%s: %s 有 iterations 却没节流，墙钟不可控' % (doc['name'], task_name(task)))

    def test_no_search_after_ops(self):
        """search_after 烘死 [1000000]/[5000000]，5% 语料下命中为空，延迟无意义。"""
        for doc in (self.plain, self.enc):
            for task in doc['schedule']:
                self.assertNotIn('_with_after_', task_name(task))

    def test_task_names_unique(self):
        for doc in (self.plain, self.enc):
            names = [task_name(t) for t in doc['schedule']]
            self.assertEqual(len(names), len(set(names)), '%s 任务名重复：%s' % (doc['name'], names))


def _runner_function(name):
    """从 run-bench.sh 抽一个函数全文（供独立执行）。"""
    with open(os.path.join(ROOT, 'run-bench.sh'), encoding='utf-8') as f:
        text = f.read()
    m = re.search(r'^' + re.escape(name) + r'\(\).*?^\}', text, re.M | re.S)
    if not m:
        raise AssertionError('run-bench.sh 缺函数: %s' % name)
    return m.group(0) + '\n'


def _lib_probe(script, env_extra=None):
    """在临时目录里 source vm-bench-lib.sh 跑一段 bash（自带最小 vm-bench.env）。"""
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
        with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('ROOT=%s\n' % ROOT)
        env = dict(os.environ)
        env.update(env_extra or {})
        sp = os.path.join(d, '_probe.sh')
        with open(sp, 'w', encoding='utf-8', newline='\n') as f:
            f.write("cd '%s' || exit 9\nsource ./vm-bench-lib.sh || exit 8\n%s" % (d, script))
        return subprocess.run(['bash', sp], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env, timeout=30)


class RunnerWiringTests(unittest.TestCase):
    """lite 两个臂要能从编排页排进 runner，而不是只能手工 esrally。"""

    def test_challenge_mapping(self):
        """geo-lite → lite；geo-lite-enc → lite-encrypted；geo-enc 仍是 full。"""
        p = subprocess.run(['bash'], input=(
            "set -e\n" + _runner_function('track_challenge') +
            '[ "$(track_challenge geo-lite)" = lite ] || exit 1\n'
            '[ "$(track_challenge geo-lite-enc)" = lite-encrypted ] || exit 1\n'
            '[ "$(track_challenge geo-enc)" = full ] || exit 1\n'),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_base_dir_and_default_percentage(self):
        p = _lib_probe(
            'for t in geo-lite geo-lite-enc; do\n'
            '  [ "$(track_base $t)" = geo ] || { echo "base $t -> $(track_base $t)" >&2; exit 1; }\n'
            '  [ "$(track_dir $t)" = "$ROOT/tracks/geonames" ] || { echo "dir $t" >&2; exit 1; }\n'
            '  case "$(track_params $t)" in *lite_ingest_percentage:%d*) ;; '
            '*) echo "pct $t -> $(track_params $t)" >&2; exit 1;; esac\n'
            'done\n' % DECLARED_PCT)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_lite_pct_knob_moves_both_arms_together(self):
        """百分比只留一个旋钮：两臂填不同值就没有 A/B 意义了。"""
        p = _lib_probe(
            'for t in geo-lite geo-lite-enc; do\n'
            '  case "$(track_params $t)" in *lite_ingest_percentage:7*) ;; '
            '*) echo "pct $t -> $(track_params $t)" >&2; exit 1;; esac\n'
            'done\n', env_extra={'LITE_PCT': '7'})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_engines_and_enc_gate(self):
        """明文臂两侧通用；加密臂双引擎（Easysearch + OpenSearch cryptofs）且跑前要装加密套件。
        消费方必须词匹配（allow 是空格词表），这里也按词表断言。"""
        p = _lib_probe(
            '[ -z "$(track_engines geo-lite)" ] || { echo "geo-lite 不该限引擎" >&2; exit 1; }\n'
            'allow="$(track_engines geo-lite-enc)"\n'
            'case " $allow " in *" easysearch "*) ;; '
            '*) echo "geo-lite-enc 缺 easysearch: $allow" >&2; exit 1;; esac\n'
            'case " $allow " in *" opensearch "*) ;; '
            '*) echo "geo-lite-enc 缺 opensearch: $allow" >&2; exit 1;; esac\n'
            'track_needs_enc geo-lite && { echo "明文臂不该装加密套件" >&2; exit 1; }\n'
            'track_needs_enc geo-lite-enc || { echo "加密臂漏了装机门" >&2; exit 1; }\n')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_track_params_engine_aware_enc_arm(self):
        """geo-lite-enc 的 store 类型按引擎选通：OpenSearch 注入 enc_store_type:cryptofs
        （标量，CLI 可传）；Easysearch/缺省不注入（走模板默认 encrypted 语义）。"""
        p = _lib_probe(
            'os="$(track_params geo-lite-enc opensearch)"\n'
            'ez="$(track_params geo-lite-enc easysearch)"\n'
            'case "$os" in *enc_store_type:cryptofs*) ;; *) echo "OS 缺 cryptofs: $os" >&2; exit 1;; esac\n'
            'case "$ez" in *enc_store_type*) echo "EZ 不该注入 enc_store_type: $ez" >&2; exit 1;; esac\n'
            'case "$(track_params geo-lite)" in *enc_store_type*) echo "明文臂不该有" >&2; exit 1;; esac\n'
            'for t in "geo-lite-enc opensearch" "geo-lite easysearch"; do\n'
            '  set -- $t\n'
            '  case "$(track_params $1 $2)" in *lite_ingest_percentage:*) ;; '
            '*) echo "pct 丢了: $t" >&2; exit 1;; esac\n'
            'done\n')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_availability_table_lists_lite_arms(self):
        with open(os.path.join(ROOT, 'vm-bench-lib.sh'), encoding='utf-8') as f:
            lib = f.read()
        m = re.search(r'for _t in ([^)]+); do', lib)
        self.assertIsNotNone(m, '可用性表结构变了')
        for t in ('geo-lite', 'geo-lite-enc'):
            self.assertIn(' %s ' % t, m.group(1),
                          '可用性表缺 %s（run-bench.sh list / doctor 会漏）' % t)


if __name__ == '__main__':
    unittest.main()
