#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-compose.py — raceway 排程编排台（PLAN v1：矩阵 → 排程文件 → 预览 → 起跑）

设计要点（见 PLAN-raceway队列可视化与编排台.md §四 v1）：
- 交错必须复刻 order_for_round（赛道外层 → 轮次 → 臂右旋），直接 import
  bench-queue 的实现，不写第二份；
- 预览直接来自 `run-bench.sh --dry-run`（用 runner 自己的语义），compose
  不复刻 resolve_engine / 包解析；
- 起跑前：本地校验（ARM_LABEL 唯一、es 臂不带 JDK_DIR）+ 远端互斥预检
  （无活跃 runner）+ dry-run 必须先通过；
- compose 不提供任何绕过 runner precheck 的手段（heap/port/宿主安静卡点
  全在 runner 侧）。

用法：
  python3 bench-compose.py arms    [--arms-file F] [--ssh ...]        # 看臂候选
  python3 bench-compose.py gen     --arms-file F --tracks ds-j1024,ds-g960 --rounds 2
  python3 bench-compose.py preview ... --ssh root@HOST                  # 生成+上传+DRY 预览
  python3 bench-compose.py start   ... [--ds-hours 12] [--camp-prefix ds2] [--yes]
  python3 bench-compose.py serve   [--port 8802] [--ssh ...]            # 本地 Web 界面
"""
import argparse
import datetime
import importlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Q = importlib.import_module('bench-queue')  # 文件名带连字符
order_for_round = Q.order_for_round
H = importlib.import_module('bench-history')  # 已入库历史覆盖（指标库 bench-run）

DEFAULT_ROOT = '/opt/raceway'
DEFAULT_WHAT = 'ds'
# 与 run-bench.sh:117 的 ds) 分支保持同步
DS_TIERS = ['ds-g1920', 'ds-j2048', 'ds-j1024', 'ds-g960', 'ds-j768', 'ds-j512', 'ds-j1536']
DEFAULT_EST = 45

Arm = namedtuple('Arm', 'name label engine build jdk_dir config')

# ============================================================ 纯函数（可测）

def parse_arms(text):
    """解析臂定义文件：每行 `<臂名> <配置行...>`，# 注释与空行跳过。"""
    arms = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        name = parts[0]
        config = parts[1] if len(parts) > 1 else ''
        kv = dict()
        for tok in config.split():
            if '=' in tok:
                k, v = tok.split('=', 1)
                kv[k] = v
        arms.append(Arm(name=name, label=kv.get('ARM_LABEL', name),
                        engine=kv.get('ENGINE_NAME', ''), build=kv.get('ENGINE_BUILD', ''),
                        jdk_dir=kv.get('JDK_DIR', ''), config=config))
    return arms


def validate_arms(arms):
    """本地能查的校验。返回 [(严重度, 消息)]，空列表 = 通过。

    - ARM_LABEL 必须唯一（重复 → 场次名冲突，runner 侧非排程模式同规则）
    - ENGINE_NAME 必填
    - elasticsearch 臂不带 JDK_DIR（用户自管 JDK 只为 easysearch 臂接线）
    """
    problems = []
    seen = {}
    for a in arms:
        if a.label in seen:
            problems.append(('error', 'ARM_LABEL 重复: %s（%s 与 %s）' % (a.label, seen[a.label], a.name)))
        else:
            seen[a.label] = a.name
        if not a.engine:
            problems.append(('error', '臂 %s 缺 ENGINE_NAME' % a.name))
        if a.engine == 'elasticsearch' and a.jdk_dir:
            problems.append(('error', 'es 臂 %s 不应带 JDK_DIR（用户自管 JDK 只为 easysearch 臂接线）' % a.name))
    return problems


def interleave(arms, tracks, rounds):
    """生成场次行序：赛道外层 → 轮次 → 臂右旋（复刻 run-bench.sh 非排程主循环）。"""
    rows = []
    for tk in tracks:
        for r in range(1, rounds + 1):
            for idx in order_for_round(r, len(arms)):
                rows.append((arms[idx].name, tk, r))
    return rows


def parse_est_overrides(pairs):
    """['ds-g1920=52', ...] → {'ds-g1920': 52}"""
    out = {}
    for p in pairs or []:
        k, _, v = p.partition('=')
        if not k or not v.isdigit():
            raise ValueError('--est 期望 <赛道>=<分钟>，得到: %r' % p)
        out[k] = int(v)
    return out


def render_schedule(rows, arms, tag, est_map=None, est_default=DEFAULT_EST, recall=True):
    """渲染排程文件文本。字段：`<臂> <赛道> <轮次> <recall|norecall> <est分钟>`。

    注意：runner 的 schedule_lines 会整行剥 `#` 之后的内容，所以行内
    不能有任何 `#`；说明性文字全部放在行首注释区。
    """
    est_map = est_map or {}
    by_name = {a.name: a for a in arms}
    now = datetime.datetime.now().strftime('%F %T')
    head = [
        '# raceway compose 排程：tag=%s' % tag,
        '# 生成：bench-compose.py @ %s' % now,
        '# 臂序（order_for_round 右旋基准序）： %s' % ' '.join(a.name for a in arms),
        '# 交错：赛道外层 → 轮次 → 臂右旋（复刻 run-bench.sh 非排程主循环，方法学不变）',
        '# 每行：<臂> <赛道> <轮次> <recall|norecall> <est分钟>',
        '',
    ]
    lines = list(head)
    for arm_name, tk, r in rows:
        a = by_name[arm_name]
        lines.append('%s %s %d %s %d' % (a.name, tk, r,
                                         'recall' if recall else 'norecall',
                                         est_map.get(tk, est_default)))
    return '\n'.join(lines) + '\n'


def schedule_rows_from_text(text):
    """按 runner schedule_lines 的规则剥注释后还原行（round-trip 校验用）。"""
    rows = []
    for raw in text.splitlines():
        line = re.sub(r'#.*', '', raw).strip()
        if not line:
            continue
        arm, tk, rnd, probe, est = line.split()
        rows.append((arm, tk, int(rnd), probe, int(est)))
    return rows


def render_arms(arms, tag):
    """把臂候选固化为 <tag>-arms.conf（campaign 自带臂定义，可指纹化）。"""
    now = datetime.datetime.now().strftime('%F %T')
    head = [
        '# raceway compose 臂定义：tag=%s' % tag,
        '# 生成：bench-compose.py @ %s（源自所选臂定义文件）' % now,
        '# 每行：<臂名> <配置行...>   配置行语法与 CONF_FILE 一致',
        '',
    ]
    return '\n'.join(head) + '\n'.join('%s %s' % (a.name, a.config) for a in arms) + '\n'


# ---- 赛道清单（两层：基础赛道 → 压缩 × source_reuse 变体网格）-----------------
# 压缩与 source_reuse 是**索引级设置**，对任何基础赛道都成立 → 不再只给 geonames
# 做变体，而是让**每个**基础赛道都能派生 6 个变体（3 压缩 × 2 sr）。
#
# 变体 id = <基础赛道id>-codec-<def|zstd|bc>[-sr]  （与变体目录名一致）
# 引擎兼容不是查表而是**规则推导**（与压测机 vm-bench-lib.sh 同一套）→
# 不存在两处表格漂移的可能。
#
# 能不能起跑看 run-bench.sh:109-118 的赛道分支；库里的 hvtp200k-2s/3s 是旧版
# runner 跑的，当前 runner 的 track_dir 没有这两个分支 → 只能看历史，排不了。
#
# est 取各 campaign 的 RACES.md 实测耗时上界 + 余量（窗底守卫靠它算"这场塞不塞
# 得进剩余窗口"，估小了会把排得下的场次误判成 DEADLINE-SKIP）。
# ds-* 用现成排程 dimsweep-24.conf 里的值 —— 那是在用口径，别另起一套。
# 名字 → (展示名, est 默认分钟, 数据集标注)
#
# ds-* 族有**两套不同的数据集**（赛道名里的血统字母，见 prepare-dimsweep-track.sh
# "ds-<血统><维度>；j=jina 系，g=GIST 系"）：
#   jina 系：ds-j512 / ds-j768 / ds-j1024 / ds-j1536 / ds-j2048
#   GIST 系：ds-g960 / ds-g1920
# 两套都是 1M docs、float、dot_product HNSW，差的是**向量数据集本身** ——
# 分布不同，召回与吞吐的绝对值不能跨数据集直接比。编排时必须看得见是哪一套。
BASE_TRACKS = {
    'geo':      ('geonames', 30, None),
    'hnsw':     ('hnsw 2m', 25, None),
    'hv1024':   ('hv 1024 维', 30, None),
    'nested':   ('nested', 35, None),
    'ds-g1920': ('1920 维', 56, 'GIST'),
    'ds-j2048': ('2048 维', 49, 'jina'),
    'ds-j1024': ('1024 维', 35, 'jina'),
    'ds-g960':  ('960 维', 30, 'GIST'),
    'ds-j768':  ('768 维', 26, 'jina'),
    'ds-j512':  ('512 维', 21, 'jina'),
    'ds-j1536': ('1536 维', 44, 'jina'),
}
TRACK_GROUPS = [('ds', '维度扫描 ds-*（两套数据集：jina 系 / GIST 系）'),
                ('geo', 'geonames'),
                ('vector', '向量 hnsw / hv1024'), ('nested', 'nested')]
BASE_GROUP = {'geo': 'geo', 'hnsw': 'vector', 'hv1024': 'vector', 'nested': 'nested'}
# 压缩 token（与 vm-bench-lib.sh 的 track_compression 同一套）
CODEC_TOKENS = ['def', 'zstd', 'bc']
TOKEN_LABEL = {'def': 'default(LZ4)', 'zstd': 'ZSTD', 'bc': 'best_compression'}
TOKEN_CODEC = {'def': 'default', 'zstd': 'ZSTD', 'bc': 'best_compression'}
# 变体 est 系数（相对基础赛道）：
#   （default,关）1.0 —— 基线
#   （ZSTD,开）  1.25 —— 来自 geo 实测（ZSTD+sr 中位 30min / 基座 24min）
#   其余取中间值 —— 压缩对总耗时的影响**没有实测**，这里是保守估计，
#   跑完一轮后按实测值用 --est 覆盖。
VARIANT_EST_FACTOR = {
    ('def', False): 1.0, ('def', True): 1.05,
    ('zstd', False): 1.2, ('zstd', True): 1.25,
    ('bc', False): 1.0, ('bc', True): 1.05,
}


def build_catalog():
    """完整赛道清单：每个基础赛道 + 它的 6 个压缩变体。

    返回 {id: {name, label, est, group, base, compression, source_reuse, engines}}
    """
    cat = {}
    for base, (label, est, dataset) in BASE_TRACKS.items():
        grp = BASE_GROUP.get(base, 'ds' if base.startswith('ds-') else 'geo')
        cat[base] = {'name': base, 'label': label, 'est': est, 'group': grp,
                     'base': base, 'compression': None, 'source_reuse': None,
                     'engines': H.engines_of(base), 'variant': False,
                     'dataset': dataset}
        for tok in CODEC_TOKENS:
            for sr in (False, True):
                vid = '%s-codec-%s%s' % (base, tok, '-sr' if sr else '')
                cat[vid] = {
                    'name': vid,
                    'label': '%s · sr %s' % (TOKEN_LABEL[tok], '开' if sr else '关'),
                    'est': int(round(est * VARIANT_EST_FACTOR[(tok, sr)])),
                    'group': grp, 'base': base,
                    'compression': TOKEN_CODEC[tok], 'source_reuse': sr,
                    'engines': H.engines_of(vid), 'variant': True,
                    'dataset': dataset,
                }
    return cat


TRACK_CATALOG = build_catalog()


def track_est_map():
    """赛道 → est 分钟。排程里没单独指定的赛道用它。"""
    return {k: v['est'] for k, v in TRACK_CATALOG.items()}


def compat_problems(arms, rows):
    """检查 (臂, 变体) 的引擎兼容性。

    ZSTD 系列压缩与 index.source_reuse 都是 **Easysearch 专有** —— ES 上
    create-index 直接拒（unknown setting），要等建索引那步才炸，白烧一台机器
    的建库时间。编排时就拦下来。
    返回 [(臂, 变体, 说明)]，空列表 = 全兼容。
    """
    by_name = {a.name: a for a in arms}
    out = []
    for arm, tk, rnd in rows:
        allow = H.engines_of(tk)
        if not allow:
            continue
        a = by_name.get(arm)
        if not a:
            continue
        if (a.engine or '').lower() != allow:
            out.append((arm, tk, '臂 %s 的引擎是 %s，变体 %s 只支持 %s'
                        % (arm, a.engine or '?', tk, allow)))
    return out


def what_for(tracks):
    """给 runner 的赛道分支参数（--what）。

    排程模式下 runner 会用**排程文件里的赛道集合**覆盖 TRACKS
    （run-bench.sh:973-980），所以 --what 只需要是个合法关键字；但给语义正确的
    值更好：单赛道直接用它自己，全 ds-* 用 ds，混排用 all。
    """
    ts = [t for t in tracks if t]
    if not ts:
        return DEFAULT_WHAT
    uniq = sorted(set(ts))
    if all(t.startswith('ds-') for t in uniq):
        return 'ds'
    if len(uniq) == 1:
        return uniq[0]
    return 'all'          # 混排：runner 的 all 分支覆盖 geo/hnsw/nested，
                          # 排程模式会再用排程文件的集合覆盖它


# ============================================================ ssh / 远端

def ssh_run(ssh_target, cmd, input_text=None, timeout=120):
    """跑一条远端命令，返回 (returncode, stdout)。ssh 约定与 bench-queue 相同。"""
    full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
    full += (ssh_target or '').split()
    full += [cmd]
    p = subprocess.run(full, input=input_text, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout


def read_arms_file(args):
    """臂定义文本：本地路径或远端路径（--ssh 时）。"""
    if args.ssh:
        rc, out = ssh_run(args.ssh, 'cat %s' % shlex.quote(args.arms_file))
        if rc != 0:
            raise SystemExit('读不到远端臂定义 %s: %s' % (args.arms_file, out.strip()))
        return out
    with open(args.arms_file, encoding='utf-8') as f:
        return f.read()


def upload(args, tag, schedule_text, arms_text):
    """排程 + 臂定义落到 <root>/schedules/<tag>.conf / <tag>-arms.conf。"""
    sdir = posixpath.join(args.root, 'schedules')
    rc, out = ssh_run(args.ssh, 'mkdir -p %s' % shlex.quote(sdir))
    if rc != 0:
        raise SystemExit('远端建目录失败: %s' % out.strip())
    for name, text in (('%s.conf' % tag, schedule_text), ('%s-arms.conf' % tag, arms_text)):
        dst = posixpath.join(sdir, name)
        rc, out = ssh_run(args.ssh, 'cat > %s' % shlex.quote(dst), input_text=text)
        if rc != 0:
            raise SystemExit('上传失败 %s: %s' % (dst, out.strip()))
    return sdir


def remote_mutex_check(args):
    """互斥预检：远端已有活跃 runner 时拒绝起跑。返回 None 或原因。"""
    rc, out = ssh_run(args.ssh, "pgrep -af '[r]un-bench' || true")
    pids = [l for l in out.splitlines() if l.strip()]
    if pids:
        return '远端已有活跃 runner，拒绝起跑：\n  ' + '\n  '.join(pids)
    return None


def remote_dry_run(args, tag, ds_hours):
    """跑 runner 自己的 dry-run（排程校验 + 逐场引擎身份）。"""
    sdir = posixpath.join(args.root, 'schedules')
    cmd = ('cd %s && SCHEDULE_FILE=%s ARMS_FILE=%s DS_HOURS=%d ./%s --dry-run %s'
           % (shlex.quote(args.root),
              shlex.quote(posixpath.join('schedules', '%s.conf' % tag)),
              shlex.quote(posixpath.join('schedules', '%s-arms.conf' % tag)),
              ds_hours, 'run-bench.sh', shlex.quote(args.what)))
    return ssh_run(args.ssh, cmd, timeout=180)


def remote_start(args, tag, ds_hours, camp_prefix):
    """互斥预检通过后 setsid nohup 起跑；稍候回读新 campaign 目录。"""
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    sdir = posixpath.join(args.root, 'schedules')
    env = ('SCHEDULE_FILE=%s ARMS_FILE=%s DS_HOURS=%d'
           % (shlex.quote(posixpath.join('schedules', '%s.conf' % tag)),
              shlex.quote(posixpath.join('schedules', '%s-arms.conf' % tag)), ds_hours))
    if camp_prefix:
        env += ' CAMP_PREFIX=%s' % shlex.quote(camp_prefix)
    log = 'compose-%s.log' % tag
    cmd = ('cd %s && %s setsid nohup ./run-bench.sh %s </dev/null >%s 2>&1 & echo STARTED'
           % (shlex.quote(args.root), env, shlex.quote(args.what), shlex.quote(log)))
    rc, out = ssh_run(args.ssh, cmd)
    if 'STARTED' not in out:
        raise SystemExit('起跑命令未确认（rc=%d）: %s' % (rc, out.strip()))
    # 给 runner 一点时间建 campaign 目录，再按前缀抓最新的
    prefix = camp_prefix or 'jdk3'
    time.sleep(6)
    rc, out = ssh_run(args.ssh, 'ls -dt %s 2>/dev/null | head -1'
                      % shlex.quote(posixpath.join(args.root, 'campaigns', '%s-*' % prefix)))
    camp = out.strip().split('/')[-1] if out.strip() else '(未知，稍后用 bench-queue list 确认)'
    return camp, log


# ============================================================ 子命令

def tracks_of(args):
    tracks = [t.strip() for t in args.tracks.split(',') if t.strip()]
    if not tracks:
        raise SystemExit('--tracks 不能为空（如 ds-g1920,ds-j1024）')
    return tracks


def load_history(args):
    """读指标库的覆盖表（已入库跑过哪些 臂×赛道×轮次 格）。

    指标库不可用（没起 / 连不上 / 没口令）时返回 unavailable 表 —— 编排照常
    进行，但界面会显示"指标库未运行"。**绝不能**把空表当成"什么都没跑过"：
    那会让编排台把已经跑过的场次重排一遍，白烧机器还把时序摊薄的前提搞脏。
    """
    if getattr(args, 'no_history', False):
        return H.Coverage(available=False, error='已按参数关闭历史覆盖检查')
    auth = getattr(args, 'es_auth', None) or os.environ.get('RACEWAY_ES_AUTH')
    if not auth:
        return H.Coverage(available=False,
                          error='未配置指标库口令（--es-auth 或环境变量 RACEWAY_ES_AUTH）')
    return H.fetch_coverage(ssh=args.ssh, auth=auth,
                            es_url=getattr(args, 'es_url', None) or H.DEFAULT_ES_URL)


def report_skipped(plan):
    """把"剔除了哪些格、为什么"说清楚 —— 编排结果必须可追溯。"""
    skipped = plan['skipped']
    if not skipped:
        return ''
    out = ['已剔除 %d 格（指标库里当前 build 已跑过，不再重排）：' % len(skipped)]
    for c in skipped:
        out.append('  %-8s %-10s r%d  %s' % (c['arm'], c['track'], c['round'], c['reason']))
    return '\n'.join(out)


def build_rows(args, arms):
    tracks = tracks_of(args)
    unknown = [t for t in tracks if t not in TRACK_CATALOG]
    if unknown:
        raise SystemExit(
            '这些赛道当前 runner 跑不了（run-bench.sh 没有对应分支）：%s\n'
            '可排的赛道：%s\n'
            '库里有历史数据的其他赛道只能看不能排（例如 hvtp200k-2s/3s 是旧版 runner 跑的）。'
            % ('、'.join(unknown), '、'.join(sorted(TRACK_CATALOG))))
    rows = interleave(arms, tracks, args.rounds)
    # est：先取赛道清单的实测值，再让 --est 覆盖（--est 优先）
    est_map = track_est_map()
    est_map.update(parse_est_overrides(args.est))
    return rows, est_map


def compose_all(args):
    """arms 解析 + 校验 + 历史覆盖剔除 + 渲染。

    返回 (arms, schedule_text, arms_text, rows, tag, plan)。
    """
    arms = parse_arms(read_arms_file(args))
    if not arms:
        raise SystemExit('臂定义里没有可用臂: %s' % args.arms_file)
    # --arms 过滤（GUI 里是勾选臂；CLI 给同样的能力）。
    # 有了臂×变体兼容矩阵后这个开关是必需的 —— 比如只跑 easysearch 臂的
    # ZSTD/source_reuse 变体，必须能把 ES 臂摘掉。
    only = [x.strip() for x in (getattr(args, 'arms', '') or '').split(',') if x.strip()]
    if only:
        known = {a.name for a in arms}
        unknown = [x for x in only if x not in known]
        if unknown:
            raise SystemExit('--arms 里有臂定义中不存在的臂: %s\n可用: %s'
                             % ('、'.join(unknown), '、'.join(sorted(known))))
        arms = [a for a in arms if a.name in only]
    problems = validate_arms(arms)
    hard = [m for sev, m in problems if sev == 'error']
    if hard:
        raise SystemExit('臂定义校验失败：\n  ' + '\n  '.join(hard))
    tracks = tracks_of(args)
    rows, est_map = build_rows(args, arms)

    # --what 没显式给就按赛道集合推导（排程模式下 runner 会用排程文件的集合覆盖它，
    # 但给个语义正确的关键字更稳：单赛道用它自己、全 ds-* 用 ds、混排用 all）
    if not getattr(args, 'what', None):
        args.what = what_for(tracks)

    # 已入库历史覆盖：默认剔除"当前 build 上已经跑过"的格
    cov = load_history(args)
    plan = H.plan_cells(cov, arms, tracks, args.rounds,
                         include_covered=getattr(args, 'include_covered', False))
    if plan['skipped']:
        rows = H.filter_rows(plan, rows)
        if not rows:
            raise SystemExit(
                '矩阵里的每一格都已在指标库跑过（当前 build），无事可排。\n'
                '要重跑请加 --include-covered。')

    # 引擎 × 变体兼容性：ZSTD / source_reuse 是 Easysearch 专有，ES 上建索引就拒。
    # 编排时拦 —— 别排了再白跑一场建库。
    bad = compat_problems(arms, rows)
    if bad:
        raise SystemExit('排程里有引擎与变体不兼容的组合：\n  '
                         + '\n  '.join(x[2] for x in bad)
                         + '\n（EASYSEARCH 专有：ZSTD 压缩、index.source_reuse；'
                           'ES 的 index.codec 只有 default/best_compression，也不认 source_reuse）')

    tag = args.tag or ('compose-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    sched = render_schedule(rows, arms, tag, est_map, args.est_default, recall=args.recall)
    # round-trip 自校验：渲染出来的文件按 runner 规则读回去必须等于生成序
    back = [(a, t, r) for a, t, r, _, _ in schedule_rows_from_text(sched)]
    if back != rows:
        raise SystemExit('内部错误：渲染结果与生成序不一致（round-trip 失败）')
    return arms, sched, render_arms(arms, tag), rows, tag, plan


def cmd_arms(args):
    arms = parse_arms(read_arms_file(args))
    for a in arms:
        print('%-8s label=%-10s engine=%-14s jdk_dir=%s' % (a.name, a.label, a.engine, a.jdk_dir or '-'))
    for sev, msg in validate_arms(arms):
        print('[%s] %s' % (sev, msg), file=sys.stderr)


def cmd_gen(args):
    arms, sched, _, rows, _, plan = compose_all(args)
    rep = report_skipped(plan)
    if rep:
        print(rep, file=sys.stderr)
    sys.stdout.write(sched)


def cmd_preview(args):
    arms, sched, arms_text, rows, tag, plan = compose_all(args)
    upload(args, tag, sched, arms_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours)
    rep = report_skipped(plan)
    if rep:
        print(rep, file=sys.stderr)
    print('== 排程文件（tag=%s，%d 场）==' % (tag, len(rows)))
    sys.stdout.write(sched)
    print('== run-bench.sh --dry-run（rc=%d）==' % rc)
    sys.stdout.write(out)
    if rc != 0:
        raise SystemExit(2)


def cmd_start(args):
    if not args.yes:
        raise SystemExit('起跑需显式确认：加 --yes（先 preview 检查过 dry-run 输出）')
    arms, sched, arms_text, rows, tag, plan = compose_all(args)
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    upload(args, tag, sched, arms_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours)
    if rc != 0:
        print(out)
        raise SystemExit('dry-run 未通过，拒绝起跑（rc=%d）' % rc)
    camp, log = remote_start(args, tag, args.ds_hours, args.camp_prefix)
    rep = report_skipped(plan)
    if rep:
        print(rep)
    print('已起跑 tag=%s（%d 场）' % (tag, len(rows)))
    print('campaign: %s' % camp)
    print('日志: %s@%s:%s' % (args.ssh, args.root, log))
    print('观测: python3 bench-queue.py status %s --root %s --ssh %s' % (camp, args.root, args.ssh))


# ============================================================ serve（本地 Web 界面）

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>raceway 排程编排台</title>
<style>
 body{font:14px/1.6 -apple-system,'PingFang SC',sans-serif;margin:24px;max-width:1240px;color:#222}
 h2{border-bottom:2px solid #3a7;padding-bottom:4px}
 table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 10px;text-align:left}
 .tracks label{margin-right:12px;white-space:nowrap}
 button{padding:6px 18px;margin-right:10px}
 pre{background:#f6f6f6;border:1px solid #ddd;padding:10px;max-height:340px;overflow:auto}
 .err{color:#b00}.ok{color:#080}
 fieldset{border:1px solid #bbb;margin-bottom:14px}
 .hist-sum{font-size:13px;margin:4px 0 8px}
 .hmatrix{border-collapse:collapse;font-size:12px;max-width:100%%;overflow:auto;display:block;max-height:420px}
 .hmatrix td,.hmatrix th{border:1px solid #ddd;padding:2px 6px;white-space:nowrap}
 .hmatrix th{background:#f2f2f2;position:sticky;top:0}
 .hmatrix tr>th:first-child{position:sticky;left:0;z-index:2;background:#f2f2f2}
 .v-ok{background:#dcfce7;color:#14532d;font-weight:600}
 .v-deg{background:#fef9c3;color:#713f12;font-weight:600}
 .v-half{background:#ffedd5;color:#7c2d12}
 .v-skip{background:#e5e7eb;color:#374151}
 .v-fail{background:#fee2e2;color:#7f1d1d;font-weight:600}
 .v-none{background:#fafafa;color:#bbb}
 .hmatrix th.cur{background:#e8f4ff}
 .hmatrix th.dim{background:#f7f7f7;color:#aaa;font-weight:400}
 .hmatrix td.past{opacity:.42}
 .legend span{margin-right:14px;font-size:12px}
 .skipbox{font-size:12px;color:#666;margin-top:6px;max-height:120px;overflow:auto;word-break:break-all;white-space:normal}
</style></head><body>
<h1>raceway 排程编排台（compose）</h1>
<p>矩阵勾选 → <b>自动剔除指标库里已跑过的格</b> → 生成排程 → dry-run 预览（runner 自己的语义）→ 互斥预检后起跑。
交错由服务端复刻 <code>order_for_round</code>；本页不做任何绕过 precheck 的事。</p>

<fieldset><legend>臂（来自臂定义文件）</legend><div id="arms" class="tracks"></div></fieldset>
<fieldset><legend>赛道（全部可起跑的赛道，按族分组）</legend><div id="tracks"></div>
 自定义（逗号分隔，清单外的赛道）<input id="custom" size="30" placeholder="ds-hv1024,..."></fieldset>
<fieldset><legend>轮次与口径</legend>
 轮次 <input id="rounds" type="number" value="1" min="1" max="9" style="width:4em">
 探针 <select id="recall"><option value="recall">recall（带召回探针）</option>
   <option value="norecall">norecall</option></select>
 est 覆盖 <input id="estov" size="30" placeholder="ds-g1920=52,nested=40">（赛道=分钟；留空用赛道清单的实测值）
 DS_HOURS <input id="dsh" type="number" value="12" min="0" style="width:5em">（0=不启用窗底守卫）
 tag <input id="tag" size="22" placeholder="留空自动生成"></fieldset>

<fieldset><legend>已入库历史（指标库 bench-run）</legend>
 <div id="histbox" class="hist-sum">加载中…</div>
 <div class="legend">
  <span class="v-ok">✓已跑</span><span class="v-deg">~降级</span><span class="v-half">半(缺相位)</span>
  <span class="v-skip">跳过</span><span class="v-fail">✗失败</span><span class="v-none">未跑</span>
 </div>
 <div id="histmatrix"></div>
 <div style="margin-top:8px">
  <label><input type="checkbox" id="incov"> 编排时<b>含</b>已跑过的格（默认剔除，避免重跑已做过的测试）</label>
  <button onclick="loadHist()">刷新历史</button>
 </div>
 <div id="skipbox" class="skipbox"></div>
</fieldset>

<fieldset><legend>臂 × 变体 兼容性</legend>
 <div id="compatbox" class="hist-sum">…</div>
 <div style="font-size:12px;color:#666">ZSTD 压缩与 <code>index.source_reuse</code> 是 Easysearch 专有
 （ES 的 <code>index.codec</code> 只有 default/best_compression，也不认 source_reuse）——
 编排台与 runner 都会在起跑前拦下不兼容的组合。</div>
</fieldset>

<button onclick="doRender()">生成排程</button>
<button onclick="doPreview()">上传 + dry-run 预览</button>
<button onclick="doStart()">互斥预检并起跑</button>
<span id="msg"></span>

<h2>排程</h2><pre id="sched">（未生成）</pre>
<h2>dry-run 输出 / 起跑结果</h2><pre id="out">（空）</pre>

<script>
const DS_TIERS = %s;
let ARMS = [];
let TRACKS = [];
let TRACK_GROUPS = [];
let BASE_TRACKS = [];
let CODEC_TOKENS = [];
let TOKEN_LABEL = {};
let TOKEN_CODEC = {};
let HIST = null;
const VT = {VALID:'✓已跑',DEGRADED:'~降级',HALF:'半',SKIP:'跳过',FAIL:'✗失败',UNKNOWN:'?'};
const VC = {VALID:'v-ok',DEGRADED:'v-deg',HALF:'v-half',SKIP:'v-skip',FAIL:'v-fail',UNKNOWN:'v-none'};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function boot(){
  const r = await fetch('/api/bootstrap'); const j = await r.json();
  ARMS = j.arms; TRACKS = j.tracks||[]; TRACK_GROUPS = j.track_groups||[];
  BASE_TRACKS = (j.tracks||[]).filter(function(t){return !t.variant;});
  CODEC_TOKENS = j.codec_tokens||[]; TOKEN_LABEL = j.token_label||{};
  TOKEN_CODEC = j.token_codec||{};
  document.getElementById('arms').innerHTML = ARMS.map(a =>
    `<label><input type="checkbox" class="armck" value="${a.name}" checked> ${a.name} <small>(${a.engine}${a.build?'@'+a.build:''}${a.jdk_dir? ' + '+a.jdk_dir.split('/').pop():''})</small></label>`).join('');
  // 赛道按族渲染，**两层**：基础赛道 + 它的 压缩 × source_reuse 变体网格
  // （压缩与 source_reuse 是两个独立维度，正交可比 —— 网格就是那张二维表）
  let h='';
  TRACK_GROUPS.forEach(function(g){
    const bases=BASE_TRACKS.filter(t=>t.group===g[0]);
    if(!bases.length) return;
    h+='<div style="margin:8px 0"><b style="font-size:12px;color:#555">'+esc(g[1])+'</b></div>';
    bases.forEach(function(b){
      h+='<div style="margin:6px 0 10px 8px">';
      const dsTxt = b.dataset ? ' · <span style="color:#0a7">'+esc(b.dataset)+' 语料</span>' : '';
      h+='<label title="'+esc(b.name+(b.dataset?(' · '+b.dataset+' 语料'):''))+' · 基座（不带压缩变体）· ~'+b.est+'min"><input type="checkbox" class="tkck" value="'+b.name+'" checked> <b>'+esc(b.label)+'</b> <small style="color:#888">('+b.name+', ~'+b.est+'min)</small>'+dsTxt+'</label>';
      // 该基础赛道的变体网格：行=压缩，列=source_reuse
      // **默认折叠** —— 11 个基础赛道 × 6 变体平铺出来没法看，展开才看细节
      const vs=TRACKS.filter(t=>t.base===b.name && t.variant);
      if(vs.length){
        h+='<details style="margin:2px 0 0 16px"><summary style="cursor:pointer;font-size:12px;color:#555">'
          + '变体网格（压缩 × source_reuse）· '+vs.length+' 个</summary>';
        h+='<table style="border-collapse:collapse;font-size:12px;margin:3px 0 4px 0">';
        h+='<tr><th style="border:1px solid #ddd;padding:2px 8px"></th>';
        h+='<th style="border:1px solid #ddd;padding:2px 8px">source_reuse 关</th>';
        h+='<th style="border:1px solid #ddd;padding:2px 8px">source_reuse 开</th></tr>';
        CODEC_TOKENS.forEach(function(tok){
          h+='<tr><th style="border:1px solid #ddd;padding:2px 8px;text-align:left">'+esc(TOKEN_LABEL[tok])+'</th>';
          [false,true].forEach(function(sr){
            const t=vs.find(x=>x.compression===TOKEN_CODEC[tok] && x.source_reuse===sr);
            if(t){
              const eng=t.engines||'';
              h+='<td style="border:1px solid #ddd;padding:2px 8px"><label title="'+esc(
                t.name+' · '+(eng?('只支持 '+eng):'两侧通用')+' · ~'+t.est+'min')+
                '"><input type="checkbox" class="tkck" value="'+t.name+'" checked> '+esc(t.name)+
                ' <small style="color:#888">(~'+t.est+'min)</small></label></td>';
            } else {
              h+='<td style="border:1px solid #ddd;padding:2px 8px;color:#bbb">—</td>';
            }
          });
          h+='</tr>';
        });
        h+='</table></details>';
      }
      h+='</div>';
    });
  });
  document.getElementById('tracks').innerHTML=h;
  ['arms','tracks','rounds','incov'].forEach(function(id){
    document.getElementById(id).addEventListener('change',function(){loadHist();});
  });
  loadHist();
}
function paintCompat(){
  // 臂的引擎 × 变体的兼容性：ZSTD 压缩与 index.source_reuse 是 Easysearch 专有，
  // ES 上 create-index 直接拒 —— 提前标出来，别排了再白跑一场建库。
  const m=matrix();
  const bad=[];
  ARMS.forEach(function(a){
    if(m.arms.indexOf(a.name)<0) return;
    m.tracks.forEach(function(t){
      const info=TRACKS.find(function(x){return x.name===t;});
      const allow=info?(info.engines||''):'';
      if(allow && (a.engine||'').toLowerCase()!==allow)
        bad.push(a.name+'（'+a.engine+'）× '+t+'（只支持 '+allow+'）');
    });
  });
  const box=document.getElementById('compatbox');
  if(!bad.length){ box.innerHTML='<span style="color:#080">✔ 当前矩阵的臂×变体组合全部兼容</span>'; return; }
  box.innerHTML='<span class="err">✘ '+bad.length+' 个组合不兼容（起跑会被拦下）：<br>'
    + bad.map(esc).join('<br>')+'</span>';
}
function matrix(){
  const arms=[...document.querySelectorAll('.armck:checked')].map(e=>e.value);
  const tracks=[...document.querySelectorAll('.tkck:checked')].map(e=>e.value);
  const custom=document.getElementById('custom').value.trim();
  if(custom) custom.split(',').forEach(t=>{t=t.trim(); if(t&&!tracks.includes(t))tracks.push(t);});
  return {arms, tracks, rounds:+document.getElementById('rounds').value,
          recall:document.getElementById('recall').value,
          est:document.getElementById('estov').value.trim(),
          ds_hours:+document.getElementById('dsh').value,
          include_covered:document.getElementById('incov').checked,
          tag:document.getElementById('tag').value.trim()};
}
async function post(path){ const r=await fetch(path,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(matrix())});
  return {status:r.status, j:await r.json()}; }
function show(el,text){document.getElementById(el).textContent=text;}
function setMsg(ok,text){const m=document.getElementById('msg');
  m.className=ok?'ok':'err'; m.textContent=' '+text;}

async function loadHist(){
  const box=document.getElementById('histbox');
  box.innerHTML='查询指标库…';
  let j;
  try{ const r=await fetch('/api/history'); j=await r.json(); }
  catch(e){ box.innerHTML='<span class="err">查询失败: '+esc(e)+'</span>'; return; }
  if(!j.available){
    box.innerHTML='<span class="err">指标库不可用：'+esc(j.error||'未知')+
      '　—— 编排照常可用，但无法自动剔除已跑过的格（压测窗口内指标库停着是常态）。</span>';
    document.getElementById('histmatrix').innerHTML='';
    document.getElementById('skipbox').textContent='';
    HIST=null; paintCompat(); return;
  }
  HIST=j;
  const s=j.summary;
  box.innerHTML='指标库已连接：<b>'+s.races+'</b> 场次 / <b>'+s.rows+'</b> 行 / <b>'+
    s.campaigns+'</b> 个 campaign / <b>'+s.tracks+'</b> 条赛道 / <b>'+s.arms+'</b> 个臂';
  paintMatrix(); paintCompat();
}
function cellOf(arm,track,round){
  if(!HIST) return null;
  return HIST.cells[arm+'|'+track+'|'+round] || null;
}
function roundsOf(arm,track){
  // 该格在库里出现过哪些轮次（全量，不受当前矩阵的轮次限制）
  if(!HIST) return [];
  const pre=arm+'|'+track+'|', out=[];
  for(const k in HIST.cells){
    if(k.indexOf(pre)===0) out.push(parseInt(k.slice(pre.length),10));
  }
  return out.sort(function(x,y){return x-y;});
}
function paintMatrix(){
  if(!HIST){ document.getElementById('histmatrix').innerHTML=''; return; }
  const m=matrix();
  const selArms=new Set(m.arms), selTracks=new Set(m.tracks);
  const arms=HIST.arms||[], tracks=HIST.tracks||[];
  let h='<table class="hmatrix"><tr><th>臂 \\ 赛道</th>';
  tracks.forEach(function(t){
    const info=TRACKS.find(function(x){return x.name===t;});
    const runnable = TRACKS.some(function(x){return x.name===t;});
    // ds-* 族有两套数据集（jina 系 / GIST 系）—— 跨数据集的绝对值不能直接比，
    // 列头标出来，避免把两套数据的点看成一条线上的。
    const dsTxt = (info && info.dataset) ? '<br><small style="font-weight:400;color:#0a7">'+esc(info.dataset)+' 语料</small>' : '';
    h+='<th class="'+(selTracks.has(t)?'cur':'dim')+'" title="'+esc(t)+'">'+esc(t)+dsTxt+
       (runnable?'':'<br><small style="font-weight:400">仅历史</small>')+'</th>';
  });
  h+='</tr>';
  const skip=[];
  arms.forEach(function(a){
    const armDef=ARMS.find(function(x){return x.name===a;})||{};
    const want=armDef.build? (a+'@'+armDef.build) : a;
    h+='<tr><th class="'+(selArms.has(a)?'cur':'dim')+'">'+esc(a)+
       '<br><small style="font-weight:400">'+esc(want)+'</small></th>';
    tracks.forEach(function(t){
      let cell='';
      roundsOf(a,t).forEach(function(r){
        const c=cellOf(a,t,r);
        let v='UNKNOWN', title='未跑过';
        if(c){
          const pb=c.per_build||{};
          if(pb[want]){ v=pb[want].verdict; title=(pb[want].campaigns||[]).join(',')+' '+v; }
          else { v='UNKNOWN'; title='别的 build: '+(c.builds||[]).join(','); }
        }
        const inMatrix = selArms.has(a) && selTracks.has(t) && r<=m.rounds;
        cell+='<td class="'+VC[v]+(inMatrix?'':' past')+'" title="'+esc(
          a+' '+t+' r'+r+' · '+title+(inMatrix?'（在本次矩阵内）':'（本次矩阵外）'))+'">r'+r+' '+VT[v]+'</td>';
        if(inMatrix && c && pb_covered(c,want)) skip.push(a+' '+t+' r'+r);
      });
      h+='<td>'+(cell||'<span class="v-none">—</span>')+'</td>';
    });
    h+='</tr>';
  });
  h+='</table>';
  document.getElementById('histmatrix').innerHTML=h;
  const incov=document.getElementById('incov').checked;
  document.getElementById('skipbox').innerHTML = incov
    ? '<b>已勾选"含已跑过的格"</b>：本次会重跑全部已跑过的格。'
    : ('本次矩阵将<b>剔除</b> '+skip.length+' 格（指标库里当前 build 已跑过）：<br>'+
       (skip.length? skip.join('、') : '（无）'));
}
function pb_covered(c,want){
  const pb=(c.per_build||{})[want];
  if(!pb) return false;
  return pb.verdict==='VALID' || pb.verdict==='DEGRADED';
}

async function doRender(){ const {status,j}=await post('/api/render');
  if(status!=200){ show('sched','错误: '+(j.error||'未知')); setMsg(false,'生成失败'); return; }
  show('sched', j.schedule); setMsg(true,'生成 '+j.rows+' 场'); }
async function doPreview(){ setMsg(true,'dry-run 运行中…'); const {status,j}=await post('/api/preview');
  if(status!=200){show('out','错误: '+(j.error||'未知'));setMsg(false,'dry-run 失败');return;}
  show('sched',j.schedule); show('out',j.dryrun); setMsg(j.rc==0, j.rc==0?'dry-run 通过，可起跑':'dry-run 非零，勿起跑');}
async function doStart(){ if(!confirm('确认起跑？会占用压测集群。'))return;
  setMsg(true,'校验并起跑…'); const {status,j}=await post('/api/start');
  if(status!=200){show('out','错误: '+(j.error||'未知'));setMsg(false,'未起跑');return;}
  show('out',j.result); setMsg(true,'已起跑');}
boot();
</script></body></html>
""" % json.dumps(DS_TIERS)


class ComposeState:
    """serve 模式的共享状态：启动参数 + 已解析臂。线程锁保护远端操作串行。"""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        text = read_arms_file(args)
        self.arms = parse_arms(text)
        self.arms_src_text = text
        self.problems = validate_arms(self.arms)
        self._hist = None
        self._hist_at = 0.0
        self._hist_lock = threading.Lock()

    def history(self, max_age=30):
        """指标库覆盖表（带 TTL 缓存，避免每次点按钮都打一次 ES）。

        指标库不可用时返回 unavailable 表 —— 界面据此显示"未运行"，
        不会把空表当成"什么都没跑过"。
        """
        with self._hist_lock:
            now = time.time()
            if self._hist is not None and (now - self._hist_at) < max_age:
                return self._hist
            self._hist = load_history(self.args)
            self._hist_at = now
            return self._hist

    def build_of(self, arm_name):
        for a in self.arms:
            if a.name == arm_name:
                return a.build
        return ''


def api_render(st, body):
    arms = [a for a in st.arms if a.name in body['arms']] or st.arms
    order = {a.name: i for i, a in enumerate(st.arms)}
    arms = sorted(arms, key=lambda a: order[a.name])
    problems = validate_arms(arms)
    hard = [m for s, m in problems if s == 'error']
    if hard:
        return 400, {'error': '；'.join(hard)}
    if not body['tracks']:
        return 400, {'error': '未选赛道'}
    tracks = list(body['tracks'])
    rounds = max(1, int(body['rounds']))
    rows = interleave(arms, tracks, rounds)

    # 已入库历史覆盖：默认剔除"当前 build 上已经跑过"的格
    incov = bool(body.get('include_covered'))
    cov = st.history()
    plan = H.plan_cells(cov, arms, tracks, rounds, include_covered=incov)
    if plan['skipped']:
        rows = H.filter_rows(plan, rows)
    if not rows:
        return 400, {'error': '矩阵里的每一格都已在指标库跑过（当前 build），无事可排。'
                             '要重跑请勾选"含已跑过的格"。',
                     'plan': plan}

    # 引擎 × 变体兼容性（ZSTD / source_reuse 是 Easysearch 专有）
    bad = compat_problems(arms, rows)
    if bad:
        return 400, {'error': '矩阵里有引擎与变体不兼容的组合：\n'
                             + '\n'.join(x[2] for x in bad)
                             + '\n（EASYSEARCH 专有：ZSTD 压缩、index.source_reuse；'
                               'ES 的 index.codec 只有 default/best_compression，也不认 source_reuse）',
                     'plan': plan}

    tag = body.get('tag') or ('compose-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    # est：赛道清单的实测值 → 界面/CLI 的覆盖值
    est_map = track_est_map()
    est_over = body.get('est') or ''
    if est_over:
        est_map.update(parse_est_overrides([p for p in est_over.split(',') if p.strip()]))
    sched = render_schedule(rows, arms, tag, est_map,
                            int(body.get('est_default') or DEFAULT_EST),
                            recall=(body.get('recall') != 'norecall'))
    st.last = {'arms': arms, 'schedule': sched, 'tag': tag, 'plan': plan}
    return 200, {'schedule': sched, 'rows': len(rows), 'plan': plan}


def api_preview(st, body):
    rc_, out = api_render(st, body)
    if rc_ != 200:
        return rc_, out
    with st.lock:
        upload(st.args, st.last['tag'], st.last['schedule'], render_arms(st.last['arms'], st.last['tag']))
        drc, dry = remote_dry_run(st.args, st.last['tag'], int(body.get('ds_hours') or 0))
    return 200, {'schedule': st.last['schedule'], 'dryrun': dry, 'rc': drc}


def api_start(st, body):
    with st.lock:
        why = remote_mutex_check(st.args)
        if why:
            return 400, {'error': why}
        rc_, out = api_render(st, body)
        if rc_ != 200:
            return rc_, out
        upload(st.args, st.last['tag'], st.last['schedule'], render_arms(st.last['arms'], st.last['tag']))
        drc, dry = remote_dry_run(st.args, st.last['tag'], int(body.get('ds_hours') or 0))
        if drc != 0:
            return 400, {'error': 'dry-run 未通过，拒绝起跑\n' + dry}
        try:
            # remote_start 内部会再做一次互斥检查（防 preview/起跑竞态窗口）
            camp, log = remote_start(st.args, st.last['tag'],
                                     int(body.get('ds_hours') or 0), None)
        except SystemExit as e:
            return 400, {'error': str(e)}
    return 200, {'result': '已起跑 tag=%s\ncampaign: %s\n日志: %s\n观测: bench-queue.py status %s'
                 % (st.last['tag'], camp, log, camp)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/':
            self._send(200, 'text/html; charset=utf-8', _PAGE)
        elif self.path == '/api/bootstrap':
            st = self.server.state
            self._send(200, 'application/json', json.dumps({
                'arms': [a._asdict() for a in st.arms],
                'problems': st.problems, 'ds_tiers': DS_TIERS,
                # 全部可起跑的赛道（不是只有 ds-*）：geo/codec 变体、hnsw、hv1024、nested
                'tracks': [dict(v, name=k) for k, v in TRACK_CATALOG.items()],
                'track_groups': TRACK_GROUPS,
                'base_tracks': sorted(BASE_TRACKS),
                'codec_tokens': CODEC_TOKENS,
                'token_label': TOKEN_LABEL,
                'token_codec': TOKEN_CODEC,
            }, ensure_ascii=False))
        elif self.path == '/api/history':
            st = self.server.state
            cov = st.history()
            self._send(200, 'application/json', json.dumps(cov.to_dict(), ensure_ascii=False))
        else:
            self._send(404, 'text/plain', 'not found')

    def do_POST(self):
        st = self.server.state
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self._send(400, 'application/json', '{"error":"bad json"}')
        try:
            if self.path == '/api/render':
                code, payload = api_render(st, body)
            elif self.path == '/api/preview':
                code, payload = api_preview(st, body)
            elif self.path == '/api/start':
                code, payload = api_start(st, body)
            else:
                code, payload = 404, {'error': 'not found'}
        except SystemExit as e:
            code, payload = 400, {'error': str(e)}
        except Exception as e:  # 远端命令失败等，页面要能看到原因
            code, payload = 500, {'error': '%s: %s' % (type(e).__name__, e)}
        self._send(code, 'application/json', json.dumps(payload, ensure_ascii=False))


def cmd_serve(args):
    state = ComposeState(args)
    hard = [m for s, m in state.problems if s == 'error']
    if hard:
        print('警告：臂定义有未解决问题（渲染时会拦截）：', file=sys.stderr)
        for m in hard:
            print('  ' + m, file=sys.stderr)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.state = state
    print('编排台: http://127.0.0.1:%d  （远端 %s，臂源 %s）'
          % (args.port, args.ssh or '本地', args.arms_file))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['arms', 'gen', 'preview', 'start', 'serve'])
    ap.add_argument('--arms-file', default=None, help='臂定义文件（本地路径；--ssh 时为远端路径）')
    ap.add_argument('--arms', default=None,
                    help='只用这些臂（逗号分隔）；缺省用臂定义文件里的全部')
    ap.add_argument('--tracks', default='ds-g1920,ds-j2048,ds-j1024,ds-g960,ds-j768,ds-j512,ds-j1536')
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--recall', dest='recall', action='store_true', default=True)
    ap.add_argument('--norecall', dest='recall', action='store_false')
    ap.add_argument('--est', action='append', default=[], help='赛道=est分钟，可重复')
    ap.add_argument('--est-default', type=int, default=DEFAULT_EST)
    ap.add_argument('--tag', default=None)
    ap.add_argument('--what', default=None,
                    help='runner 的赛道分支参数；缺省按所选赛道自动推导'
                         '（单赛道用它自己 / 全 ds-* 用 ds / 混排用 all）')
    ap.add_argument('--ds-hours', type=int, default=0)
    ap.add_argument('--camp-prefix', default=None)
    ap.add_argument('--root', default=DEFAULT_ROOT, help='远端 raceway 目录')
    ap.add_argument('--ssh', default=None, help='user@host；缺省操作本地')
    ap.add_argument('--port', type=int, default=8802)
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--es-auth', default=None,
                    help='指标库 basic auth（user:pass）；缺省读环境变量 RACEWAY_ES_AUTH。'
                         '用于查已入库历史，自动剔除已跑过的格')
    ap.add_argument('--es-url', default=None,
                    help='指标库地址（默认 https://127.0.0.1:9237）。'
                         '--ssh 模式下这个地址是**服务器那边**的地址')
    ap.add_argument('--include-covered', action='store_true',
                    help='把指标库里已跑过的格也排进去（默认剔除，避免重跑已做过的测试）')
    ap.add_argument('--no-history', action='store_true',
                    help='不查指标库历史（默认会查；查不到时编排照常，只是不剔除）')
    args = ap.parse_args(argv)

    if args.arms_file is None:
        if args.ssh:
            args.arms_file = posixpath.join(args.root, 'schedules', 'dimsweep-arms.conf')
        else:
            # 本地模式的臂定义默认取仓库内 schedules/dimsweep-arms.conf。
            # 该文件属内部资料，不随公共库公开（仓库里没有这个目录），
            # 所以按「环境变量 → 仓库内默认 → 明确报错」三级回退，
            # 免得直接抛一个看不懂的 FileNotFoundError。
            here = os.path.dirname(os.path.abspath(__file__))
            args.arms_file = (os.environ.get('RACEWAY_ARMS_FILE')
                               or os.path.join(here, 'schedules', 'dimsweep-arms.conf'))
            if not os.path.isfile(args.arms_file):
                raise SystemExit(
                    '找不到臂定义文件: %s\n'
                    '臂定义属内部资料，未随公共库公开 —— 用 --arms-file 指定路径，'
                    '或设环境变量 RACEWAY_ARMS_FILE 指向它。\n'
                    '远端模式（--ssh）不受影响：默认读远端 %s'
                    % (args.arms_file,
                       posixpath.join(args.root, 'schedules', 'dimsweep-arms.conf')))

    {'arms': cmd_arms, 'gen': cmd_gen, 'preview': cmd_preview,
     'start': cmd_start, 'serve': cmd_serve}[args.cmd](args)


if __name__ == '__main__':
    main()
