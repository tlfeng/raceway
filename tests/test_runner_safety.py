#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runner 安全回归：仅临时夹具和命令替身，不访问 SSH/ES，不发真实信号。

Linux/WSL: python3 -B -m unittest tests.test_runner_safety -v
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(file, name):
    text = (ROOT / file).read_text(encoding="utf-8")
    match = re.search(r"^" + re.escape(name) + r"\(\).*?^\}", text, re.M | re.S)
    if not match:
        raise AssertionError(name)
    return match.group(0) + "\n"


@unittest.skipUnless(os.name == "posix" and Path("/proc/self/stat").exists(), "需 Linux/WSL bash 与 /proc")
class RunnerSafetyTest(unittest.TestCase):
    def setUp(self):
        base = (Path('/mnt/c/Users/user/AppData/Local/Temp/raceway-review/runner-main')
                if Path('/mnt/c/Users/user').exists() else None)
        if base:
            base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runner-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)

    def shell(self, code, ok=True, env=None):
        prefix = "set -uo pipefail\nRACEWAY_ENV_OPTIONAL=1\n"
        prefix += "source '%s'\n" % (ROOT / 'vm-bench-lib.sh')
        p = subprocess.run(['bash'], input=prefix + code, text=True, encoding='utf-8',
                           capture_output=True, cwd=self.d, env=dict(os.environ, **(env or {})), timeout=20)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def test_quant_codec_cartesian_product(self):
        code = ""
        for quant, expected in [('int8', 'int8_hnsw'), ('bbq', 'bbq_hnsw')]:
            for codec in ['def', 'zstd', 'bc']:
                for suffix in ['', '-sr']:
                    name = 'hv1024x-%s-codec-%s%s' % (quant, codec, suffix)
                    code += '[[ "$(track_quant %s)" = %s ]] || exit 1\n' % (name, expected)
        self.shell(code)

    def test_stop_and_wipe_remote_guards_and_no_unrelated_kill(self):
        engine = self.d / 'install/easysearch'
        java = engine / 'jdk/bin/java'
        java.parent.mkdir(parents=True)
        java.write_text('fixture')
        other = self.d / 'other/bin/java'
        other.parent.mkdir(parents=True)
        other.write_text('fixture')
        proc = self.d / 'proc'
        for pid, exe, home in [(101, java, engine), (202, other, self.d / 'other')]:
            p = proc / str(pid)
            p.mkdir(parents=True)
            (p / 'exe').symlink_to(exe)
            (p / 'cmdline').write_bytes(b'java\0-Des.path.home=' + str(home).encode() + b'\0')
            (p / 'stat').write_text(str(pid) + ' (java) S ' + ' '.join(['0'] * 18 + ['123']))
        code = r'''
IBS_BASE="$FIX/install"
FAKE_PROC="$FIX/proc/"
export FIX FAKE_PROC
systemctl() { return 0; }
sleep() { :; }
kill() { printf '%s\n' "$*" >> "$FIX/signals"; mv "$FAKE_PROC$2/exe" "$FAKE_PROC$2/exe.gone"; }
export -f systemctl sleep kill
vm() { local cmd="${2//\/proc\//$FAKE_PROC}"; bash -c "$cmd"; }
stop_engine_on_vm fixture easysearch || exit 1
[[ "$(cat "$FIX/signals")" = '-TERM 101' ]] || exit 2
[ -L "$FIX/proc/202/exe" ] || exit 3
'''
        self.shell(code, env={'FIX': str(self.d)})
        self.assertTrue((proc / '202/exe').exists())

    def test_fail_closed_port_and_empty_topology(self):
        p = self.shell('HTTP_PORT=9200\nvm() { return 255; }\nport_free_on_vm explicit\n', ok=False)
        self.assertNotEqual(p.returncode, 0)
        p = self.shell('VM_IPS=()\ncpu_affinity_apply 8\n', ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('空节点', p.stderr)
        self.shell("IBS_BASE=/; ! engine_root easysearch\n")

    def test_clean_runs_only_after_successful_stop(self):
        body = function('run-bench.sh', 'run_field')
        # 实际函数前置失败即返回，不会抵达任何部署/采样命令。
        fake = self.d / 'bin'
        fake.mkdir()
        (fake / 'reset-env.sh').write_text('#!/bin/bash\nexit 7\n')
        (fake / 'reset-env.sh').chmod(0o700)
        code = body + r'''
CONFIGS=('x'); CAR_LABEL=a; ENGINE_NAME=easysearch
LIB_DIR="$FIX/bin"; CAMP_DIR="$FIX"; RACES="$FIX/RACES.md"; PCTAG=PASS
mkdir -p "$CAMP_DIR/results" "$CAMP_DIR/logs"
apply_config() { :; }; track_dir() { printf track; }; track_index() { printf idx; }
target_hosts() { printf hosts; }; track_params() { :; }; PROGSTEP() { :; }
ADMIN_USER=u; ADMIN_PW=p
purge_stale_index_data() { printf 'unsafe\n' > "$FIX/deleted"; }
run_field 0 geo 1 1 && exit 8
[ ! -f "$FIX/deleted" ] && grep -q STOP-FAIL "$RACES"
'''
        self.shell(code, env={'FIX': str(self.d)})

    def test_expected_tasks_real_templates_all_static_tracks(self):
        import contextlib
        import io
        import sys
        import types
        from unittest.mock import patch
        import jinja2
        from markupsafe import Markup

        body = function('run-bench.sh', 'expected_tasks')
        code = body.split("<<'PY'", 1)[1].split('\n', 1)[1].rsplit('\nPY', 1)[0]
        for source in sorted((ROOT / 'tracks').iterdir()):
            if not (source / 'track.json').exists():
                continue
            with self.subTest(track=source.name):
                target = self.d / source.name
                shutil.copytree(source, target, ignore=shutil.ignore_patterns('documents*', 'queries*', 'truth*', '__pycache__'))
                # 只在临时副本派生 full，绝不改仓库赛道。
                self.shell('derive_full_challenge "%s"\n' % target)
                full_source = (target / 'challenges/full.json').read_text()
                for sleep_enabled in (False, True):
                    rendered = jinja2.Environment().from_string(full_source).render(post_ingest_sleep=sleep_enabled)
                    parsed = json.loads(rendered)
                    self.assertTrue(parsed['schedule'])
                    if source.name == 'geonames':
                        sleep_tasks = [task for task in parsed['schedule'] if task.get('name') == 'post-ingest-sleep']
                        self.assertEqual(bool(sleep_tasks), sleep_enabled)
                env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(target)))
                def collect(parts):
                    return Markup(','.join(value for p in sorted(target.glob(parts))
                                           if (value := env.get_template(str(p.relative_to(target))).render().strip())))
                env.globals['collect_fixture'] = collect
                def render(path, params):
                    # 真实 Jinja 逐文件展开；仅用离线 collect 替代 Rally 的模板 helper。
                    text = Path(path).read_text(encoding='utf-8')
                    text = re.sub(r'{%\s*import\s+"rally.helpers".*?%}', '', text)
                    text = text.replace('rally.collect(', 'collect_fixture(')
                    previous = dict(env.globals)
                    env.globals.update(params)
                    try:
                        return env.from_string(text).render(**params)
                    finally:
                        env.globals.clear()
                        env.globals.update(previous)
                loader = types.ModuleType('esrally.track.loader')
                loader.render_template_from_file = render
                opts = types.ModuleType('esrally.utils.opts')
                opts.to_dict = lambda raw: {}
                modules = {'esrally': types.ModuleType('esrally'),
                           'esrally.track': types.ModuleType('esrally.track'),
                           'esrally.track.loader': loader,
                           'esrally.utils': types.ModuleType('esrally.utils'),
                           'esrally.utils.opts': opts}
                output = target / 'expected.json'
                with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['expected', str(target), '', str(output)]):
                    try:
                        exec(compile(code, '<expected_tasks>', 'exec'), {})
                    except json.JSONDecodeError as exc:
                        self.fail(str(exc) + '\n' + '\n'.join(exc.doc.splitlines()[exc.lineno-4:exc.lineno+3]))
                result = json.loads(output.read_text())
                self.assertTrue(result['write'])
                if source.name == 'geonames-upsert':
                    self.assertEqual(result['query'], [])
                    self.assertEqual(result['terminal'], ['final-stats'])
                    self.assertEqual(len(result['write']), 5)
                else:
                    self.assertTrue(result['query'])
                    self.assertEqual(result['terminal'], [])

    def test_metric_all_expected_queries_and_finite_zero_errors(self):
        (self.d / 'results').mkdir()
        expected = {'write': ['bulk'], 'query': ['q1', 'q2']}
        (self.d / 'results/f-expected-tasks.json').write_text(json.dumps(expected))
        good = [{'task': t, 'throughput': {'median': 1, 'unit': u}, 'error_rate': 0}
                for t, u in [('bulk', 'docs/s'), ('q1', 'ops/s'), ('q2', 'ops/s')]]
        code = function('run-bench.sh', 'race_metric_ok')
        code += 'CAMP_DIR="$FIX"\nlog() { :; }\nrace_metric_ok f wq\n'
        variants = [good, good[:-1]]
        for value in [None, float('nan'), float('inf'), '1', True, 0]:
            rows = json.loads(json.dumps(good))
            rows[-1]['throughput']['median'] = value
            variants.append(rows)
        for error in [None, .01, float('nan'), float('inf')]:
            rows = json.loads(json.dumps(good))
            rows[-1]['error_rate'] = error
            variants.append(rows)
        for i, rows in enumerate(variants):
            with self.subTest(i=i):
                (self.d / 'results/f-wq-race.json').write_text(json.dumps({'results': {'op_metrics': rows}}))
                p = self.shell(code, ok=False, env={'FIX': str(self.d)})
                self.assertEqual(p.returncode == 0, i == 0, p.stdout + p.stderr)

    def test_env_requires_each_node_positive_samples_and_ok(self):
        mon = self.d / 'monitors'
        mon.mkdir()
        code = function('run-bench.sh', 'env_verdict_ok')
        code += 'CAMP_DIR="$FIX"\nVM_NAMES=(node-a node-b)\nenv_verdict_ok f-wq\n'
        for status in ['OK', 'WARN', 'CONTAMINATED', 'NO-DATA', 'MISSING']:
            for node in ['node-a', 'node-b']:
                f = mon / ('f-wq-' + node + '-summary.txt')
                f.write_text('node=%s ip=fixture phase=wq\nsamples=1\nENV-VERDICT: OK\n' % node)
            if status == 'MISSING':
                f.unlink()
            else:
                f.write_text('node=node-b ip=fixture phase=wq\nsamples=1\nENV-VERDICT: ' + status + '\n')
            p = self.shell(code, ok=False, env={'FIX': str(self.d)})
            self.assertEqual(p.returncode == 0, status == 'OK')

    def test_attempts_monotonic_success_retry_and_literal_fid(self):
        (self.d / 'attempts').mkdir()
        (self.d / 'attempts/a.b-geo-r1-try12').write_text('reserved')
        (self.d / 'RACES.md').write_text('| a.b-geo-r1-try12 | VALID | x | 1min |\n')
        code = function('run-bench.sh', 'field_valid') + function('run-bench.sh', 'next_attempt')
        code += r'''
CAMP_DIR="$FIX"; RACES="$FIX/RACES.md"
field_valid a.b-geo-r1 || exit 1
field_valid axb-geo-r1 && exit 2
[[ "$(next_attempt a.b-geo-r1)" = 13 ]]
'''
        self.shell(code, env={'FIX': str(self.d)})

    def test_plan_digest_deadline_and_atomic_stable_state(self):
        code = function('run-bench.sh', 'persist_plan') + function('run-bench.sh', 'state_write')
        code += r'''
CAMP_DIR="$FIX"; RACES="$FIX/RACES.md"; STATE="$FIX/STATE.txt"
PLAN_FIDS=(a-geo-r1); PLAN_CONFIGS=('ENGINE_NAME=easysearch'); PLAN_TRACKS=(geo)
PLAN_ROUNDS=(1); PLAN_RECALL=(norecall); PLAN_EST=(2)
DS_HOURS=2; SHARDS=3; REPLICAS=0; HEAP=16g; PRECHECK_MODE=each
ENV_ID=fixture; VERDICT_STEAL_MAX=2; VERDICT_FOREIGN_MAX=5
persist_plan
first=$DEADLINE
persist_plan
[ "$DEADLINE" = "$first" ] || exit 1
state_write FAIL a-geo-r1
state_write OK a-geo-r1
[[ "$(cat "$STATE")" = 'OK a-geo-r1' ]] || exit 2
PLAN_RECALL=(recall)
(persist_plan) && exit 3
exit 0
'''
        self.shell(code, env={'FIX': str(self.d)})
        self.assertEqual((self.d / 'PLAN.tsv').read_text().splitlines()[0], 'fid\tconfig\ttrack\tround\trecall\test')

    def test_mutual_exclusion_is_atomic(self):
        self.shell(r'''
exec 9> lock
flock -n 9 || exit 1
bash -c 'exec 9> lock; flock -n 9' && exit 2
exit 0
''')

    def test_external_daemon_cannot_inherit_runner_lock(self):
        text = (ROOT / 'run-bench.sh').read_text(encoding='utf-8')
        self.assertIn('> "$rlog" 2>&1 9>&-', text)
        for log in ['ibs-deploy', 'ibs-wipe', 'ibs-uninstall']:
            self.assertIn('logs/' + log + '-$eng.log" 2>&1 9>&-', text)
        self.shell(r'''
(
  exec 9> daemon-lock
  flock -n 9 || exit 1
  bash -c 'sleep 0.2 </dev/null >/dev/null 2>&1 &' 9>&-
) || exit 2
exec 9> daemon-lock
flock -n 9
''')

    def test_sampler_stop_checks_only_requested_tag(self):
        code = function('load-monitor.sh', 'valid_tag') + function('load-monitor.sh', 'stop_sampler')
        code += r'''
MON_DIR="$FIX"
printf '123 456 boot\n' > "$MON_DIR/mon-own.pid"
printf '234 567 boot\n' > "$MON_DIR/mon-other.pid"
sampler_owned() { return 1; }
kill() { printf 'unsafe'; exit 4; }
stop_sampler own
[ -f "$MON_DIR/mon-other.pid" ] && [ ! -e "$MON_DIR/mon-own.pid" ]
'''
        self.shell(code, env={'FIX': str(self.d)})

    def test_empty_host_escape_hatches_and_monitor_path_forwarded(self):
        text = (ROOT / 'precheck.sh').read_text(encoding='utf-8')
        self.assertIn('${HOST_QUIET_AUTOSTART-on}', text)
        self.assertIn('${HOST_QUIET_PORTS-"9202 9237 5601"}', text)
        for name in ['monitor_start', 'monitor_stop']:
            self.assertIn('RACEWAY_MON_DIR=', function('run-bench.sh', name))


if __name__ == '__main__':
    unittest.main()
