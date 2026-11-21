"""tests/test_geo_write.py — geo-write / geo-write-enc（纯写入挑战，双引擎加密臂）的接线与渲染守卫。

守卫四件事：
  1. index.json 的 store 条件块三分支齐备（cryptofs / encrypted / fs），缺分支 = 对应引擎
     的加密臂**静默跑成明文**（rally 不报错，比失败更糟）。
  2. write-only.json 挑战在位且形状正确：bulk 带预热（冷启动段滤出测量）、force-merge 单段、
     无查询类操作（写入专场的口径就是"没有查询"）。
  3. runner 接线：挑战映射 write-only、引擎词表（明文臂通用 / 加密臂双引擎）、
     track_params 按引擎选通（EZ=encrypted_store:true；OS=enc_store_type:cryptofs）、
     装机门、超时旋钮、可用性表。
  4. track_params 选通的参数 index.json 真的认（渲染验证闭环，防"参数传了但模板不认"）。

跑法：
    python3 -m unittest tests.test_geo_write -v
没装 jinja2 的机器只跑 source 级断言（渲染类用例 skip）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_JSON = os.path.join(ROOT, 'tracks', 'geonames', 'index.json')
WRITE_ONLY = os.path.join(ROOT, 'tracks', 'geonames', 'challenges', 'write-only.json')
RUN_BENCH = os.path.join(ROOT, 'run-bench.sh')

try:
    import jinja2
except ImportError:
    jinja2 = None


def index_source():
    with open(INDEX_JSON, encoding='utf-8') as f:
        return f.read()


def write_only_doc():
    """write-only.json 是 Jinja 片段（collect 约定）：先渲染成单对象数组再解析。"""
    with open(WRITE_ONLY, encoding='utf-8') as f:
        src = f.read()
    env = jinja2.Environment(undefined=jinja2.Undefined)
    return json.loads('[' + env.from_string(src).render() + ']')[0]


def _render_index(**params):
    env = jinja2.Environment(undefined=jinja2.Undefined)
    return json.loads(env.from_string(index_source()).render(**params))['settings']


def _lib_probe(script, env_extra=None):
    """临时目录里 source vm-bench-lib.sh 跑一段 bash（自带最小 vm-bench.env）。"""
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


def _runner_function(name):
    with open(RUN_BENCH, encoding='utf-8') as f:
        text = f.read()
    m = re.search(r'^' + re.escape(name) + r'\(\).*?^\}', text, re.M | re.S)
    if not m:
        raise AssertionError('run-bench.sh 缺函数: %s' % name)
    return m.group(0) + '\n'


class IndexStoreBranchesTest(unittest.TestCase):
    """index.json 的 store 条件块三分支 —— 缺一 = 该引擎的加密臂静默跑成明文。"""

    def setUp(self):
        self.src = index_source()

    def test_all_three_branches_present(self):
        self.assertIn("enc_store_type is defined and enc_store_type == 'cryptofs'", self.src,
                      '缺 cryptofs 分支 —— OpenSearch 加密臂会静默跑成明文')
        self.assertIn('{%- elif encrypted_store is defined and encrypted_store %}', self.src,
                      '缺 encrypted 分支 —— Easysearch 加密臂会静默跑成明文')
        self.assertIn('"index.store.type": "{{store_type | default(\'fs\')}}"', self.src,
                      '缺 fs 兜底分支')

    def test_cryptofs_branch_bakes_key_provider(self):
        self.assertIn("{{crypto_key_provider | default('dummy')}}", self.src,
                      'cryptofs 分支必须烘 key_provider（rally CLI 传不了 JSON）')

    @unittest.skipIf(jinja2 is None, '需要 jinja2')
    def test_rendered_settings_per_engine(self):
        # OpenSearch 加密臂：runner 传 enc_store_type:cryptofs
        s = _render_index(enc_store_type='cryptofs')
        self.assertEqual(s['index.store.type'], 'cryptofs')
        self.assertEqual(s['index.store.crypto.key_provider'], 'dummy')
        self.assertNotIn('index.encryption.profile', s)
        self.assertNotIn('encrypted_store', ' '.join(s))
        # crypto_key_provider 覆盖旋钮
        s = _render_index(enc_store_type='cryptofs', crypto_key_provider='aws-kms')
        self.assertEqual(s['index.store.crypto.key_provider'], 'aws-kms')
        # Easysearch 加密臂：encrypted_store:true
        s = _render_index(encrypted_store=True)
        self.assertEqual(s['index.store.type'], 'encrypted')
        self.assertEqual(s['index.encryption.profile'], 'index')
        self.assertNotIn('index.store.crypto.key_provider', s)
        # 明文臂：两个加密参数都不传
        s = _render_index()
        self.assertEqual(s['index.store.type'], 'fs')
        self.assertNotIn('index.encryption.profile', s)
        self.assertNotIn('index.store.crypto.key_provider', s)


@unittest.skipIf(jinja2 is None, '需要 jinja2（write-only.json 是 Jinja 片段）')
class WriteOnlyChallengeTest(unittest.TestCase):
    """write-only 挑战形状：纯写入口径，预热滤冷启动。"""

    def setUp(self):
        self.doc = write_only_doc()

    def test_challenge_name(self):
        self.assertEqual(self.doc['name'], 'write-only')

    def test_no_query_operations(self):
        for task in self.doc['schedule']:
            op = task['operation']
            ot = op.get('operation-type') if isinstance(op, dict) else op
            self.assertNotIn(ot, ('search', 'scroll-search', 'indices-stats'),
                             'write-only 混入了查询类操作: %s' % ot)

    def test_bulk_has_warmup_period(self):
        # write-only 的 bulk 是按名字引用的操作（"index-append"），warmup 挂在 task 级
        bulks = [t for t in self.doc['schedule'] if t['operation'] == 'index-append']
        self.assertTrue(bulks, '缺 index-append 任务')
        for b in bulks:
            self.assertIn('warmup-time-period', b,
                          'bulk 缺 warmup —— 冷启动段会污染写入中位数（上一轮方差的主要来源）')

    def test_force_merge_to_single_segment(self):
        fms = [t for t in self.doc['schedule']
               if isinstance(t['operation'], dict)
               and t['operation'].get('operation-type') == 'force-merge']
        self.assertTrue(fms, '缺 force-merge（写入族口径 = 单段收官）')
        self.assertIn('max-num-segments', fms[0]['operation'])


class RunnerWiringTest(unittest.TestCase):
    """geo-write / geo-write-enc 要能从编排页排进 runner。"""

    def test_challenge_mapping(self):
        p = subprocess.run(['bash'], input=(
            "set -e\n" + _runner_function('track_challenge') +
            '[ "$(track_challenge geo-write)" = write-only ] || exit 1\n'
            '[ "$(track_challenge geo-write-enc)" = write-only ] || exit 1\n'
            '[ "$(track_challenge geo)" = full ] || exit 1\n'),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_base_dir_and_engines_and_gate(self):
        p = _lib_probe(
            'for t in geo-write geo-write-enc; do\n'
            '  [ "$(track_base $t)" = geo ] || { echo "base $t" >&2; exit 1; }\n'
            '  [ "$(track_dir $t)" = "$ROOT/tracks/geonames" ] || { echo "dir $t" >&2; exit 1; }\n'
            'done\n'
            '[ -z "$(track_engines geo-write)" ] || { echo "明文写入臂不该限引擎" >&2; exit 1; }\n'
            'allow="$(track_engines geo-write-enc)"\n'
            'case " $allow " in *" easysearch "*) ;; *) echo "缺 easysearch: $allow" >&2; exit 1;; esac\n'
            'case " $allow " in *" opensearch "*) ;; *) echo "缺 opensearch: $allow" >&2; exit 1;; esac\n'
            'track_needs_enc geo-write && { echo "明文写入臂不该装机" >&2; exit 1; }\n'
            'track_needs_enc geo-write-enc || { echo "加密写入臂漏了装机门" >&2; exit 1; }\n')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_track_params_engine_selection(self):
        p = _lib_probe(
            'os="$(track_params geo-write-enc opensearch)"\n'
            'ez="$(track_params geo-write-enc easysearch)"\n'
            'plain="$(track_params geo-write)"\n'
            'case "$os" in *enc_store_type:cryptofs*) ;; *) echo "OS 缺 cryptofs: $os" >&2; exit 1;; esac\n'
            'case "$ez" in *encrypted_store:true*) ;; *) echo "EZ 缺 encrypted_store: $ez" >&2; exit 1;; esac\n'
            'case "$ez" in *enc_store_type*) echo "EZ 不该有 cryptofs" >&2; exit 1;; esac\n'
            'case "$plain" in *enc_store_type*|*encrypted_store*) echo "明文臂不该有加密参数" >&2; exit 1;; esac\n')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_availability_and_timeout_wiring(self):
        with open(os.path.join(ROOT, 'vm-bench-lib.sh'), encoding='utf-8') as f:
            lib = f.read()
        m = re.search(r'for _t in ([^)]+); do', lib)
        self.assertIsNotNone(m, '可用性表结构变了')
        for t in ('geo-write', 'geo-write-enc'):
            self.assertIn(' %s ' % t, m.group(1), '可用性表缺 %s' % t)
        with open(RUN_BENCH, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('TIMEOUT_GEO_WRITE_W:=3600', src, '缺写入族超时旋钮')
        self.assertIn('geo-write|geo-write-enc) echo "$TIMEOUT_GEO_WRITE_W"', src,
                      'track_tmo_w 没接写入族')
        tmo_w = _runner_function('track_tmo_w')
        self.assertIn('geo-write', tmo_w, 'track_tmo_w 缺写入族分支')

    def test_storage_param_guard_covers_write_family(self):
        p = _lib_probe(
            'track_assert_storage_params geo-write || exit 1\n'
            'track_assert_storage_params geo-write-enc || exit 1\n')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_expected_tasks_terminal_rule_for_write_only(self):
        """expected_tasks 的无查询挑战规则：末任务 index-stats = 生命周期证据（terminal）。

        write-only 挑战没有查询段 —— 硬断言"必须声明查询任务"会让整个写入族
        TASKS-FAIL（20261122 实测 28 场全灭）。规则泛化后 upsert 的 final-stats
        特例同样被覆盖。"""
        body = self._expected_tasks_body()
        spec = {
            'operations': [
                {'name': 'index-append', 'operation-type': 'bulk'},
                {'name': 'wait-until-merges-finish', 'operation-type': 'index-stats'},
            ],
            'challenges': [{'name': 'write-only', 'schedule': [
                {'operation': 'index-append'},
                {'operation': 'wait-until-merges-finish'},
            ]}],
        }
        out = self._run_expected_tasks(body, spec, 'write-only')
        self.assertTrue(out['write'], '写任务必须进 expected')
        self.assertEqual(out['terminal'], ['wait-until-merges-finish'],
                         '末任务 index-stats 必须记为 terminal 证据')

    def test_expected_tasks_query_challenge_keeps_terminal_empty(self):
        body = self._expected_tasks_body()
        spec = {
            'operations': [
                {'name': 'index-append', 'operation-type': 'bulk'},
                {'name': 'q1', 'operation-type': 'search'},
            ],
            'challenges': [{'name': 'full', 'schedule': [
                {'operation': 'index-append'},
                {'operation': 'q1'},
            ]}],
        }
        out = self._run_expected_tasks(body, spec, 'full')
        self.assertTrue(out['query'], '有查询任务时不该走 terminal 规则')
        self.assertEqual(out['terminal'], [])

    def _expected_tasks_body(self):
        with open(RUN_BENCH, encoding='utf-8') as f:
            text = f.read()
        m = re.search(r"^expected_tasks\(\).*?<<'PY'.*?^(.*?)^PY$", text, re.M | re.S)
        self.assertIsNotNone(m, 'expected_tasks 的内嵌 python 找不到')
        return m.group(1)

    def _run_expected_tasks(self, body, spec, challenge):
        loader = types.ModuleType('esrally.track.loader')
        loader.render_template_from_file = lambda path, params: json.dumps(spec)
        opts = types.ModuleType('esrally.utils.opts')
        opts.to_dict = lambda raw: {}
        modules = {'esrally': types.ModuleType('esrally'),
                   'esrally.track': types.ModuleType('esrally.track'),
                   'esrally.track.loader': loader,
                   'esrally.utils': types.ModuleType('esrally.utils'),
                   'esrally.utils.opts': opts}
        fd, out = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.unlink(out)          # expected_tasks 用 'x' 独占创建，必须先移除空壳
        try:
            with patch.dict(sys.modules, modules), \
                 patch.object(sys, 'argv', ['et', '/fake/track', '', out, challenge]):
                exec(compile(body, '<expected_tasks>', 'exec'), {})
            return json.load(open(out, encoding='utf-8'))
        finally:
            os.unlink(out)


if __name__ == '__main__':
    unittest.main()
