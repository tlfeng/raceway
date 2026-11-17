#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geo-snap 族（快照探针）runner 支持的回归闸门

背景：geo-snap / geo-snap-enc 是 geonames 的快照探针变体（挑战 encrypted-store-snapshot，
仓库 p_repo_settings 三臂：明文 s3 / 加密+s3 委托 / 默认加密+fs）。runner 侧要打通五件事，
缺一件就白烧一场（1-1.5h）：

  1. lib 推导：track_base/track_dir 指回 geonames；track_params 按臂传仓库参数；
     track_engines 锁 easysearch；track_needs_enc 认全三族
  2. run-bench 的 WHAT_ARGS / case / 可用性表收 geo-snap 族
  3. track_challenge：geo-snap 族 → encrypted-store-snapshot，其余 full
  4. run_field 链路：enc 装机（wipe 前）→ enc 门（/_encryption/status）→ MinIO 拉起，
     失败态 ENC-SETUP-FAIL / ENC-GATE-FAIL / S3-BACKEND-FAIL 进观测层词表
  5. MinIO 生命周期：minio_ensure 幂等拉起 + SigV4 建桶；_close_on_exit / campaign 收尾必关

跑法：python3 -m unittest tests.test_geo_snap_runner -v
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_IDS = ('geo-snap', 'geo-snap-enc')


def bash_run(script, env_extra=None):
    """在仓库根跑一段 bash（自带空 vm-bench.env，免依赖现场配置）。"""
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(ROOT, 'vm-bench-lib.sh'), d)
        with open(os.path.join(d, 'vm-bench.env'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('ROOT=%s\n' % ROOT)
        env = dict(os.environ)
        env.update(env_extra or {})
        sp = os.path.join(d, '_probe.sh')
        with open(sp, 'w', encoding='utf-8', newline='\n') as f:
            f.write("cd '%s' || exit 9\nsource ./vm-bench-lib.sh || exit 8\n%s" % (d, script))
        return subprocess.run(['bash', sp], capture_output=True, text=True, env=env, timeout=30)


def runner_function(name):
    """从 run-bench.sh 抽一个函数全文（供独立执行）。"""
    text = open(os.path.join(ROOT, 'run-bench.sh'), encoding='utf-8').read()
    m = re.search(r'^' + re.escape(name) + r'\(\).*?^\}', text, re.M | re.S)
    if not m:
        raise AssertionError('run-bench.sh 缺函数: %s' % name)
    return m.group(0) + '\n'


class TestSnapTrackDerivation(unittest.TestCase):
    """lib 推导：geo-snap 族必须指回 geonames 基座并按臂传参。"""

    def test_base_and_dir(self):
        p = bash_run(
            "for t in geo-snap geo-snap-enc; do\n"
            "  b=$(track_base \"$t\"); d=$(track_dir \"$t\")\n"
            "  [ \"$b\" = geo ] || { echo \"base: $t → $b\" >&2; exit 1; }\n"
            "  [ \"$d\" = '%s' ] || { echo \"dir: $t → $d\" >&2; exit 1; }\n"
            "done\n" % os.path.join(ROOT, 'tracks', 'geonames'))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_params_per_arm(self):
        p = bash_run(
            "p=$(track_params geo-snap)\n"
            "case \"$p\" in *snapshot_repo_type:s3*) ;; *) echo \"geo-snap params: $p\" >&2; exit 1;; esac\n"
            "case \"$p\" in *encrypted_store*|*snapshot_repo_delegate*) echo \"geo-snap 不应带加密仓库参数: $p\" >&2; exit 1;; esac\n"
            "p=$(track_params geo-snap-enc)\n"
            "case \"$p\" in *snapshot_repo_delegate:s3*) ;; *) echo \"geo-snap-enc 缺委托: $p\" >&2; exit 1;; esac\n"
            "case \"$p\" in *snapshot_repo_profile:snapshot*) ;; *) echo \"geo-snap-enc 缺快照 profile: $p\" >&2; exit 1;; esac\n"
            "p=$(track_params geo)\n"
            "case \"$p\" in *snapshot_repo*|*encrypted_store*) echo \"geo 基座不应带加密/快照参数: $p\" >&2; exit 1;; esac\n")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_params_bucket_follows_knob(self):
        p = bash_run(
            "p=$(track_params geo-snap)\n"
            "case \"$p\" in *snapshot_repo_bucket:bucket-x*|*snapshot_repo_base_path:bp-x*) ;; *) echo \"$p\" >&2; exit 1;; esac\n",
            env_extra={'S3_BUCKET': 'bucket-x', 'S3_BASE_PATH': 'bp-x'})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_engines_locked_to_easysearch(self):
        p = bash_run(
            "for t in geo-snap geo-snap-enc geo-enc; do\n"
            "  [ \"$(track_engines \"$t\")\" = easysearch ] || { echo \"$t: $(track_engines \"$t\")\" >&2; exit 1; }\n"
            "done\n")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_needs_enc_covers_family_only(self):
        p = bash_run(
            "for t in geo-snap geo-snap-enc geo-enc; do track_needs_enc \"$t\" || exit 1; done\n"
            "track_needs_enc geo && exit 1\n"
            "track_needs_enc hnsw && exit 1\n"
            "exit 0\n")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)


class TestSnapRunnerWiring(unittest.TestCase):
    """run-bench.sh 接线：入口/挑战/失败态/MinIO 生命周期。"""

    def setUp(self):
        self.src = open(os.path.join(ROOT, 'run-bench.sh'), encoding='utf-8').read()

    def test_what_args_and_case_accept_snap_family(self):
        self.assertIn('geo-snap|geo-snap-enc', self.src.split('WHAT_ARGS=')[1].split('\n')[0])
        m = re.search(r"^  geo-snap\|geo-snap-enc\) TRACKS=.*$", self.src, re.M)
        self.assertIsNotNone(m, 'case 缺 geo-snap 族分支')

    def test_availability_table_lists_snap_family(self):
        lib = open(os.path.join(ROOT, 'vm-bench-lib.sh'), encoding='utf-8').read()
        m = re.search(r'for _t in ([^)]+); do', lib)
        self.assertIsNotNone(m)
        for t in SNAP_IDS:
            self.assertIn(' %s ' % t, m.group(1), '可用性表缺 %s' % t)

    def test_challenge_mapping(self):
        """track_challenge：geo-snap 族 → encrypted-store-snapshot；geo/geo-enc → full。"""
        p = subprocess.run(['bash'], input=(
            "set -e\n" + runner_function('track_challenge') +
            "[ \"$(track_challenge geo-snap)\" = encrypted-store-snapshot ] || exit 1\n"
            "[ \"$(track_challenge geo-snap-enc)\" = encrypted-store-snapshot ] || exit 1\n"
            "[ \"$(track_challenge geo)\" = full ] || exit 1\n"
            "[ \"$(track_challenge geo-enc)\" = full ] || exit 1\n"),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_race_wires_challenge_into_rally_and_sidecar(self):
        """rally --challenge 与 sidecar --challenge 必须吃同一个 track_challenge 结果。"""
        self.assertIn('--challenge="$ch"', self.src)
        self.assertIn('sidecar_capture "$fid" wq "$tk" "$rnd" "$att" "$track" "$tp" "$ch"', self.src)
        self.assertIn('expected_tasks "$track" "$tp" "$CAMP_DIR/results/$fid-expected-tasks.json" "$ch"', self.src)

    def test_full_challenge_derivation_skipped_for_snap_family(self):
        """快照族不能跑 derive_full_challenge（挑战文件已定版，派生会把它当缺失）。"""
        m = re.search(r'if \[ "\$ch" = full \]; then\n(.*?)\n  fi\n', self.src, re.S)
        self.assertIsNotNone(m, 'derive_full_challenge 缺挑战守卫')
        self.assertIn('derive_full_challenge', m.group(1))

    def test_enc_setup_and_gate_in_run_field(self):
        """enc 装机在 wipe 前、enc 门在集群卡点后，且失败态写进 RACES.md。"""
        body = re.search(r'^run_field\(\).*?^\}', self.src, re.M | re.S).group(0)
        # 装机（2c）必须先于 wipe；门（3b）必须在 wipe 之后
        self.assertLess(body.index('ops.enc setup'), body.index('ibs_wipe2 "$eng"'),
                        'enc 装机必须在 ibs wipe 之前（配置落盘后冷启动才生效）')
        self.assertLess(body.index('ibs_wipe2 "$eng"'), body.index('_encryption/status'),
                        'enc 门必须在集群就绪（wipe+卡点）之后')
        for status in ('ENC-SETUP-FAIL', 'ENC-GATE-FAIL', 'S3-BACKEND-FAIL'):
            self.assertIn('| %s |' % status, body, 'run_field 缺失败态 %s' % status)
        # geo-snap 族装机必须带 s3 客户端参数；geo-enc 不带
        self.assertIn('ops.enc setup $s3args --yes', body)
        self.assertIn('case "$tk" in geo-snap*) s3args=', body)

    def test_minio_lifecycle(self):
        """MinIO：幂等拉起 + 建桶；campaign 收尾与 _close_on_exit 都要关。"""
        self.assertIn('minio_ensure', self.src)
        close = re.search(r'^_close_on_exit\(\).*?^\}', self.src, re.M | re.S).group(0)
        self.assertIn('minio_stop', close, '_close_on_exit 必须关 MinIO')
        m = re.search(r'env_summary_csv.*?PROGSTEP "campaign 结束', self.src, re.S)
        self.assertIsNotNone(m, 'campaign 收尾段落找不到')
        self.assertIn('minio_stop', m.group(0), 'campaign 自然结束必须关 MinIO')
        fn = runner_function('minio_ensure')
        self.assertIn('MINIO_ROOT_USER="$S3_ACCESS_KEY" MINIO_ROOT_PASSWORD="$S3_SECRET_KEY"', fn,
                      'MinIO root 账号必须=业务 s3 凭证（免 mc 建户）')
        self.assertIn('/minio/health/live', fn, '拉起后必须等健康检查')
        self.assertIn('urllib.request', fn, '建桶必须走内联 SigV4（不依赖 mc）')

    def test_minio_binds_bridge_ip_by_default(self):
        self.assertIn(': "${MINIO_ADDR:=10.10.10.1:9000}"', self.src,
                      '默认只绑网桥 IP —— 弱凭证 MinIO 不能暴露公网接口')

    def test_snap_snapshot_name_unique_per_field(self):
        """同仓库同名快照二次 create 会被拒 → 快照名必须带 CAMP_ID+fid（跨 campaign/轮次/重试唯一）。"""
        self.assertIn('snapshot_name:snap-${CAMP_ID}-${fid}', self.src,
                      '快照名必须跨 campaign 唯一（PARTIAL 残留会让 wait-for-snapshot-create 死循环）')

    def test_timeouts_allow_snapshot_loop(self):
        """快照族写超时独立放宽（写段之外还要注册/快照/恢复）。"""
        self.assertIn('TIMEOUT_GEO_SNAP_W', self.src)
        m = re.search(r'geo\) case "\$1" in geo-snap\|geo-snap-enc\) echo "\$TIMEOUT_GEO_SNAP_W"', self.src)
        self.assertIsNotNone(m, 'track_tmo_w 未给快照族放宽')


if __name__ == '__main__':
    unittest.main()
