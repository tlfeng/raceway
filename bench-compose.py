#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-compose.py — raceway 控制台（启动排程 + 运行进度 + 历史结果）

serve 是三合一 Web 控制台（原"排程编排台"扩容）：
- ① 启动排程（PLAN v1：矩阵 → 排程文件 → 预览 → 起跑）
- ② 运行进度：campaign 队列实时视图（复用 bench-queue 的推导），自动选中
  最新没收尾的 campaign；显示当前场次/相位/已进行时长、进度条、剩余预估、
  PROGRESS.txt 尾部日志；可停止（远端 stop-campaign.sh）
- ③ 历史与结果：全部 campaign 清单（一次远端调用取摘要），任一场可"看进度"、
  可"按此重排"（预填启动矩阵）；外链 Kibana 看板

设计要点（见 PLAN-raceway队列可视化与编排台.md §四 v1）：
- 交错必须复刻 order_for_round（赛道外层 → 轮次 → 车右旋），直接 import
  bench-queue 的实现，不写第二份；
- 预览直接来自 `run-bench.sh --dry-run`（用 runner 自己的语义），compose
  不复刻 resolve_engine / 包解析；
- 起跑前：本地校验（CAR_LABEL 唯一、es 车不带 JDK_DIR）+ 远端互斥预检
  （无活跃 runner）+ dry-run 必须先通过；
- compose 不提供任何绕过 runner precheck 的手段（heap/port/宿主安静卡点
  全在 runner 侧）；
- 进度/历史只读 bench-queue 的推导（纯读文件），不碰集群；停止是唯一
  的写操作，走远端已有的 stop-campaign.sh，界面二次确认。

用法：
  python3 bench-compose.py cars    [--cars-file F] [--ssh ...]        # 看车候选
  python3 bench-compose.py gen     --cars-file F --tracks ds-j1024,ds-g960 --rounds 2
  python3 bench-compose.py preview ... --ssh root@HOST                  # 生成+上传+DRY 预览
  python3 bench-compose.py start   ... [--ds-hours 12] [--camp-prefix ds2] [--yes]
  python3 bench-compose.py serve   [--port 8802] [--ssh ...]            # 控制台 Web 界面
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
import webbrowser
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Q = importlib.import_module('bench-queue')  # 文件名带连字符
order_for_round = Q.order_for_round
H = importlib.import_module('bench-history')  # 已入库历史覆盖（指标库 bench-run）

DEFAULT_ROOT = '/opt/raceway'
DEFAULT_WHAT = 'ds'
# 与 run-bench.sh:117 的 ds) 分支保持同步
DS_TIERS = ['ds-g1920', 'ds-j2048', 'ds-j1024', 'ds-g960', 'ds-j768', 'ds-j512', 'ds-j1536']
DEFAULT_EST = 45

Car = namedtuple('Car', 'name label engine build jdk_dir config')

# ============================================================ 纯函数（可测）

def parse_cars(text):
    """解析车定义文件：每行 `<车名> <配置行...>`，# 注释与空行跳过。"""
    cars = []
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
        cars.append(Car(name=name, label=kv.get('CAR_LABEL', name),
                        engine=kv.get('ENGINE_NAME', ''), build=kv.get('ENGINE_BUILD', ''),
                        jdk_dir=kv.get('JDK_DIR', ''), config=config))
    return cars


def validate_cars(cars):
    """本地能查的校验。返回 [(严重度, 消息)]，空列表 = 通过。

    - CAR_LABEL 必须唯一（重复 → 场次名冲突，runner 侧非排程模式同规则）
    - ENGINE_NAME 必填
    - elasticsearch 车不带 JDK_DIR（用户自管 JDK 只为 easysearch 车接线）
    """
    problems = []
    seen = {}
    for a in cars:
        if a.label in seen:
            problems.append(('error', 'CAR_LABEL 重复: %s（%s 与 %s）' % (a.label, seen[a.label], a.name)))
        else:
            seen[a.label] = a.name
        if not a.engine:
            problems.append(('error', '车 %s 缺 ENGINE_NAME' % a.name))
        if a.engine == 'elasticsearch' and a.jdk_dir:
            problems.append(('error', 'es 车 %s 不应带 JDK_DIR（用户自管 JDK 只为 easysearch 车接线）' % a.name))
    return problems


def interleave(cars, tracks, rounds):
    """生成场次行序：赛道外层 → 轮次 → 车右旋（复刻 run-bench.sh 非排程主循环）。"""
    rows = []
    for tk in tracks:
        for r in range(1, rounds + 1):
            for idx in order_for_round(r, len(cars)):
                rows.append((cars[idx].name, tk, r))
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


def render_schedule(rows, cars, tag, est_map=None, est_default=DEFAULT_EST, recall=True):
    """渲染排程文件文本。字段：`<车> <赛道> <轮次> <recall|norecall> <est分钟>`。

    注意：runner 的 schedule_lines 会整行剥 `#` 之后的内容，所以行内
    不能有任何 `#`；说明性文字全部放在行首注释区。
    """
    est_map = est_map or {}
    by_name = {a.name: a for a in cars}
    now = datetime.datetime.now().strftime('%F %T')
    head = [
        '# raceway compose 排程：tag=%s' % tag,
        '# 生成：bench-compose.py @ %s' % now,
        '# 车序（order_for_round 右旋基准序）： %s' % ' '.join(a.name for a in cars),
        '# 交错：赛道外层 → 轮次 → 车右旋（复刻 run-bench.sh 非排程主循环，方法学不变）',
        '# 每行：<车> <赛道> <轮次> <recall|norecall> <est分钟>',
        '',
    ]
    lines = list(head)
    for car_name, tk, r in rows:
        a = by_name[car_name]
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
        car, tk, rnd, probe, est = line.split()
        rows.append((car, tk, int(rnd), probe, int(est)))
    return rows


def render_cars(cars, tag):
    """把车候选固化为 <tag>-cars.conf（campaign 自带车定义，可指纹化）。"""
    now = datetime.datetime.now().strftime('%F %T')
    head = [
        '# raceway compose 车定义：tag=%s' % tag,
        '# 生成：bench-compose.py @ %s（源自所选车定义文件）' % now,
        '# 每行：<车名> <配置行...>   配置行语法与 CONF_FILE 一致',
        '',
    ]
    return '\n'.join(head) + '\n'.join('%s %s' % (a.name, a.config) for a in cars) + '\n'


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


def est_total_min(rows, est_map, est_default=DEFAULT_EST):
    """整份排程的预计总耗时（分钟）：逐行按赛道的 est 累加。

    起跑前"要跑多久"的第一直觉 —— 窗口（DS_HOURS）塞不塞得下也应该看它。
    """
    return sum(est_map.get(tk, est_default) for _, tk, _ in rows)


def compat_problems(cars, rows):
    """检查 (车, 变体) 的引擎兼容性。

    ZSTD 系列压缩与 index.source_reuse 都是 **Easysearch 专有** —— ES 上
    create-index 直接拒（unknown setting），要等建索引那步才炸，白烧一台机器
    的建库时间。编排时就拦下来。
    返回 [(车, 变体, 说明)]，空列表 = 全兼容。
    """
    by_name = {a.name: a for a in cars}
    out = []
    for car, tk, rnd in rows:
        allow = H.engines_of(tk)
        if not allow:
            continue
        a = by_name.get(car)
        if not a:
            continue
        if (a.engine or '').lower() != allow:
            out.append((car, tk, '车 %s 的引擎是 %s，变体 %s 只支持 %s'
                        % (car, a.engine or '?', tk, allow)))
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
    """跑一条远端命令，返回 (returncode, stdout)。ssh 约定与 bench-queue 相同。

    stderr 合并进 stdout：dry-run 的报错全在 stderr，丢了就是"rc=1 空输出"没法排障。
    """
    full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
    full += (ssh_target or '').split()
    full += [cmd + ' 2>&1']
    p = subprocess.run(full, input=input_text, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout


def read_cars_file(args):
    """车定义文本：本地路径或远端路径（--ssh 时）。"""
    if args.ssh:
        rc, out = ssh_run(args.ssh, 'cat %s' % shlex.quote(args.cars_file))
        if rc != 0:
            raise SystemExit('读不到远端车定义 %s: %s' % (args.cars_file, out.strip()))
        return out
    with open(args.cars_file, encoding='utf-8') as f:
        return f.read()


def upload(args, tag, schedule_text, cars_text):
    """排程 + 车定义落到 <root>/schedules/<tag>.conf / <tag>-cars.conf。"""
    sdir = posixpath.join(args.root, 'schedules')
    rc, out = ssh_run(args.ssh, 'mkdir -p %s' % shlex.quote(sdir))
    if rc != 0:
        raise SystemExit('远端建目录失败: %s' % out.strip())
    for name, text in (('%s.conf' % tag, schedule_text), ('%s-cars.conf' % tag, cars_text)):
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


def remote_dry_run(args, tag, ds_hours, what=None):
    """跑 runner 自己的 dry-run（排程校验 + 逐场引擎身份）。"""
    sdir = posixpath.join(args.root, 'schedules')
    cmd = ('cd %s && SCHEDULE_FILE=%s CARS_FILE=%s DS_HOURS=%d ./%s --dry-run %s'
           % (shlex.quote(args.root),
              shlex.quote(posixpath.join('schedules', '%s.conf' % tag)),
              shlex.quote(posixpath.join('schedules', '%s-cars.conf' % tag)),
              ds_hours, 'run-bench.sh', shlex.quote(what or DEFAULT_WHAT)))
    return ssh_run(args.ssh, cmd, timeout=180)


def remote_start(args, tag, ds_hours, camp_prefix, what=None):
    """互斥预检通过后 setsid nohup 起跑；稍候回读新 campaign 目录。"""
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    sdir = posixpath.join(args.root, 'schedules')
    env = ('SCHEDULE_FILE=%s CARS_FILE=%s DS_HOURS=%d'
           % (shlex.quote(posixpath.join('schedules', '%s.conf' % tag)),
              shlex.quote(posixpath.join('schedules', '%s-cars.conf' % tag)), ds_hours))
    if camp_prefix:
        env += ' CAMP_PREFIX=%s' % shlex.quote(camp_prefix)
    log = 'compose-%s.log' % tag
    cmd = ('cd %s && %s setsid nohup ./run-bench.sh %s </dev/null >%s 2>&1 & echo STARTED'
           % (shlex.quote(args.root), env, shlex.quote(what or DEFAULT_WHAT),
              shlex.quote(log)))
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
    """读指标库的覆盖表（已入库跑过哪些 车×赛道×轮次 格）。

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
        out.append('  %-8s %-10s r%d  %s' % (c['car'], c['track'], c['round'], c['reason']))
    return '\n'.join(out)


def build_rows(args, cars):
    tracks = tracks_of(args)
    unknown = [t for t in tracks if t not in TRACK_CATALOG]
    if unknown:
        raise SystemExit(
            '这些赛道当前 runner 跑不了（run-bench.sh 没有对应分支）：%s\n'
            '可排的赛道：%s\n'
            '库里有历史数据的其他赛道只能看不能排（例如 hvtp200k-2s/3s 是旧版 runner 跑的）。'
            % ('、'.join(unknown), '、'.join(sorted(TRACK_CATALOG))))
    rows = interleave(cars, tracks, args.rounds)
    # est：先取赛道清单的实测值，再让 --est 覆盖（--est 优先）
    est_map = track_est_map()
    est_map.update(parse_est_overrides(args.est))
    return rows, est_map


def compose_all(args):
    """cars 解析 + 校验 + 历史覆盖剔除 + 渲染。

    返回 (cars, schedule_text, cars_text, rows, tag, plan)。
    """
    cars = parse_cars(read_cars_file(args))
    if not cars:
        raise SystemExit('车定义里没有可用车: %s' % args.cars_file)
    # --cars 过滤（GUI 里是勾选车；CLI 给同样的能力）。
    # 有了车×变体兼容矩阵后这个开关是必需的 —— 比如只跑 easysearch 车的
    # ZSTD/source_reuse 变体，必须能把 ES 车摘掉。
    only = [x.strip() for x in (getattr(args, 'cars', '') or '').split(',') if x.strip()]
    if only:
        known = {a.name for a in cars}
        unknown = [x for x in only if x not in known]
        if unknown:
            raise SystemExit('--cars 里有车定义中不存在的车: %s\n可用: %s'
                             % ('、'.join(unknown), '、'.join(sorted(known))))
        cars = [a for a in cars if a.name in only]
    problems = validate_cars(cars)
    hard = [m for sev, m in problems if sev == 'error']
    if hard:
        raise SystemExit('车定义校验失败：\n  ' + '\n  '.join(hard))
    tracks = tracks_of(args)
    rows, est_map = build_rows(args, cars)

    # --what 没显式给就按赛道集合推导（排程模式下 runner 会用排程文件的集合覆盖它，
    # 但给个语义正确的关键字更稳：单赛道用它自己、全 ds-* 用 ds、混排用 all）
    if not getattr(args, 'what', None):
        args.what = what_for(tracks)

    # 已入库历史覆盖：默认剔除"当前 build 上已经跑过"的格
    cov = load_history(args)
    plan = H.plan_cells(cov, cars, tracks, args.rounds,
                         include_covered=getattr(args, 'include_covered', False))
    if plan['skipped']:
        rows = H.filter_rows(plan, rows)
        if not rows:
            raise SystemExit(
                '矩阵里的每一格都已在指标库跑过（当前 build），无事可排。\n'
                '要重跑请加 --include-covered。')

    # 引擎 × 变体兼容性：ZSTD / source_reuse 是 Easysearch 专有，ES 上建索引就拒。
    # 编排时拦 —— 别排了再白跑一场建库。
    bad = compat_problems(cars, rows)
    if bad:
        raise SystemExit('排程里有引擎与变体不兼容的组合：\n  '
                         + '\n  '.join(x[2] for x in bad)
                         + '\n（EASYSEARCH 专有：ZSTD 压缩、index.source_reuse；'
                           'ES 的 index.codec 只有 default/best_compression，也不认 source_reuse）')

    tag = args.tag or ('compose-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    sched = render_schedule(rows, cars, tag, est_map, args.est_default, recall=args.recall)
    # round-trip 自校验：渲染出来的文件按 runner 规则读回去必须等于生成序
    back = [(a, t, r) for a, t, r, _, _ in schedule_rows_from_text(sched)]
    if back != rows:
        raise SystemExit('内部错误：渲染结果与生成序不一致（round-trip 失败）')
    return cars, sched, render_cars(cars, tag), rows, tag, plan


def cmd_cars(args):
    cars = parse_cars(read_cars_file(args))
    for a in cars:
        print('%-8s label=%-10s engine=%-14s jdk_dir=%s' % (a.name, a.label, a.engine, a.jdk_dir or '-'))
    for sev, msg in validate_cars(cars):
        print('[%s] %s' % (sev, msg), file=sys.stderr)


def cmd_gen(args):
    cars, sched, _, rows, _, plan = compose_all(args)
    rep = report_skipped(plan)
    if rep:
        print(rep, file=sys.stderr)
    sys.stdout.write(sched)


def cmd_preview(args):
    cars, sched, cars_text, rows, tag, plan = compose_all(args)
    upload(args, tag, sched, cars_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours, what=args.what)
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
    cars, sched, cars_text, rows, tag, plan = compose_all(args)
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    upload(args, tag, sched, cars_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours, what=args.what)
    if rc != 0:
        print(out)
        raise SystemExit('dry-run 未通过，拒绝起跑（rc=%d）' % rc)
    camp, log = remote_start(args, tag, args.ds_hours, args.camp_prefix, what=args.what)
    rep = report_skipped(plan)
    if rep:
        print(rep)
    print('已起跑 tag=%s（%d 场）' % (tag, len(rows)))
    print('campaign: %s' % camp)
    print('日志: %s@%s:%s' % (args.ssh, args.root, log))
    print('观测: python3 bench-compose.py serve --ssh %s   # 控制台「运行进度」页' % (args.ssh or '本机'))


# ============================================================ serve（本地 Web 界面）

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>raceway 控制台</title>
<style>
 body{font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:20px;max-width:1280px;color:#222;background:#fff;color-scheme:light}
 h1{font-size:20px;margin:0 0 2px}
 h2{border-bottom:2px solid #3a7;padding-bottom:4px;font-size:16px;margin:18px 0 8px}
 .sub{color:#666;font-size:12px}
 table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 10px;text-align:left}
 .tracks label{margin-right:12px;white-space:nowrap}
 button{padding:5px 14px;margin-right:8px;cursor:pointer}
 pre{background:#f6f6f6;border:1px solid #ddd;padding:10px;max-height:340px;overflow:auto}
 .err{color:#b00}.ok{color:#080}
 fieldset{border:1px solid #bbb;margin-bottom:14px}
 .tabs{margin:12px 0 0;border-bottom:2px solid #3a7}
 .tabs button{border:none;background:none;font-size:15px;padding:8px 18px;margin:0;color:#666;border-bottom:3px solid transparent}
 .tabs button.on{color:#111;font-weight:600;border-bottom-color:#3a7}
 .tab{display:none}.tab.on{display:block}
 .conn{color:#777;font-size:12px;margin:6px 0 0}
 #msg{margin-left:6px}
 /* ---- ② 运行进度 ---- */
 .pbar{height:10px;background:#e8e8e8;border-radius:5px;overflow:hidden;margin:10px 0}
 .pbar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);transition:width .4s}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
 .card{background:#f4f7f5;border:1px solid #dde5df;border-radius:8px;padding:8px 14px;min-width:86px}
 .card b{display:block;font-size:19px}
 .card span{color:#777;font-size:11px}
 .pcur{background:#eef6ff;border:1px solid #bcd8f5;border-radius:8px;padding:8px 14px;margin:10px 0 6px}
 table.q{width:100%;font-size:13px}
 table.q td,table.q th{border:none;border-bottom:1px solid #e3e3e3;padding:5px 8px;text-align:left;white-space:nowrap}
 tr.now td{background:#e8f4ff}
 .st-ok{color:#0a0;font-weight:600}.st-run{color:#06c;font-weight:600}.st-pend{color:#999}
 .st-skip{color:#b80}.st-bad{color:#b00;font-weight:600}
 .chip-run{background:#dbeafe;color:#1d4ed8;font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap}
 .chip-end{background:#e5e7eb;color:#4b5563;font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap}
 .warn{background:#fff7e6;border:1px solid #e7c98a;border-left:3px solid #d97706;border-radius:6px;padding:6px 12px;margin:6px 0;color:#7c4a03;font-size:13px}
 select,input{font:inherit}
 /* ---- ① 启动排程（沿用编排台样式） ---- */
 .hist-sum{font-size:13px;margin:4px 0 8px}
 .hmatrix{border-collapse:collapse;font-size:12px;max-width:100%;overflow:auto;display:block;max-height:420px}
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
<h1>raceway 控制台</h1>
<div class="conn" id="connrow"><span id="conn">连接中…</span>
 <button onclick="doDoctor()" title="远端跑 doctor.sh：基础工具/env 配置/拓扑/虚机连通/语料/指标库，纯只读">🩺 环境体检</button>
</div>
<div class="tabs">
 <button id="tb-compose" class="on" onclick="switchTab('compose')">① 启动排程</button>
 <button id="tb-progress" onclick="switchTab('progress')">② 运行进度</button>
 <button id="tb-history" onclick="switchTab('history')">③ 历史与结果</button>
</div>
<span id="msg"></span>

<div id="tab-compose" class="tab on">
<p>矩阵勾选 → <b>自动剔除指标库里已跑过的格</b> → 生成排程 → dry-run 预览（runner 自己的语义）→ 互斥预检后起跑。
交错由服务端复刻 <code>order_for_round</code>；本页不做任何绕过 precheck 的事。勾选会记住（localStorage），下次打开不用重勾。</p>

<fieldset><legend>车（来自车定义文件）</legend><div id="cars" class="tracks"></div></fieldset>
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

<fieldset><legend>车 × 变体 兼容性</legend>
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
<h2>输出（dry-run / 起跑 / 环境体检）</h2><pre id="out">（空）</pre>
</div>

<div id="tab-progress" class="tab">
 <fieldset><legend>campaign</legend>
  <select id="pcamp" style="min-width:360px" onchange="CURCID=this.value;pollNow()"></select>
  <button onclick="loadCampaigns()">刷新列表</button>
  <label style="margin-left:10px"><input type="checkbox" id="pauto" checked> 自动刷新（5s）</label>
  <label style="margin-left:10px"><input type="checkbox" id="pnotify"> 完成提醒</label>
  <button onclick="doStop()" style="float:right;background:#fee2e2;border:1px solid #fca5a5;color:#b00">■ 停止该 campaign</button>
 </fieldset>
 <div id="pcur" class="pcur">（未选择）</div>
 <div class="pbar"><i id="pbar" style="width:0"></i></div>
 <div class="cards" id="pcards"></div>
 <div id="pwarns"></div>
 <table class="q"><thead><tr><th>seq</th><th>场次</th><th>状态</th><th>用时</th><th>recall</th><th>说明</th></tr></thead>
 <tbody id="prows"><tr><td colspan="6" class="sub">（等待选择 campaign）</td></tr></tbody></table>
 <h2>PROGRESS.txt 尾部（实时日志）</h2><pre id="ptail">（空）</pre>
</div>

<div id="tab-history" class="tab">
 <fieldset><legend>外部结果入口</legend>
  <a id="kibana" href="#" target="_blank">Kibana 看板 →</a>
  <div class="sub" id="kibanahint"></div>
 </fieldset>
 <fieldset><legend>两场对比（car-compare：逐轮实测 + 中位差值 Δ + 轮间极差）</legend>
  A <select id="cmpa" style="max-width:250px"></select> 车 <input id="cmpcara" size="8" placeholder="自动">
  &nbsp;vs&nbsp;
  B <select id="cmpb" style="max-width:250px"></select> 车 <input id="cmpcarb" size="8" placeholder="自动">
  <button onclick="doCompare()">对比</button>
  <span class="sub">两个 campaign 作为两侧的取数池；车代号留空自动取（B 侧尽量取与 A 不同的车）。典型用法：两场各跑了 jdk25/kona25，对比 jdk25 vs kona25。</span>
  <pre id="cmpout" style="max-height:320px">（未运行）</pre>
 </fieldset>
 <p class="sub">「看进度」跳到运行进度页看该场逐场明细；「按此重排」把该场的 车×赛道×轮次 预填进启动排程（可调整后重新生成）。列表按最近活动排序。</p>
 <div>筛选 <input id="hfilter" size="24" placeholder="输入 campaign 名关键字过滤" oninput="renderHistory()"></div>
 <table class="q"><thead><tr><th>campaign</th><th>状态</th><th>VALID / 数据行</th><th>最近活动</th><th>最后一条进度</th><th>操作</th></tr></thead>
 <tbody id="hrows"><tr><td colspan="6" class="sub">（加载中…）</td></tr></tbody></table>
</div>

<script>
let CARS = [];
let TRACKS = [];
let TRACK_GROUPS = [];
let BASE_TRACKS = [];
let CODEC_TOKENS = [];
let TOKEN_LABEL = {};
let TOKEN_CODEC = {};
let HIST = null;
// 控制台状态：当前看的 campaign、轮询定时器、campaign 摘要列表、上一轮"是否已结束"（完成提醒用）
let CURCID = null, POLLT = null, CAMPS = [], PREV_ENDED = null;
const VT = {VALID:'✓已跑',DEGRADED:'~降级',HALF:'半',SKIP:'跳过',FAIL:'✗失败',UNKNOWN:'?'};
const VC = {VALID:'v-ok',DEGRADED:'v-deg',HALF:'v-half',SKIP:'v-skip',FAIL:'v-fail',UNKNOWN:'v-none'};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function boot(){
  const r = await fetch('/api/bootstrap'); const j = await r.json();
  CARS = j.cars; TRACKS = j.tracks||[]; TRACK_GROUPS = j.track_groups||[];
  BASE_TRACKS = (j.tracks||[]).filter(function(t){return !t.variant;});
  CODEC_TOKENS = j.codec_tokens||[]; TOKEN_LABEL = j.token_label||{};
  TOKEN_CODEC = j.token_codec||{};
  document.getElementById('cars').innerHTML = CARS.map(a =>
    `<label><input type="checkbox" class="carck" value="${a.name}" checked> ${a.name} <small>(${a.engine}${a.build?'@'+a.build:''}${a.jdk_dir? ' + '+a.jdk_dir.split('/').pop():''})</small></label>`).join('');
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
  document.getElementById('conn').textContent =
    '远端 '+(j.ssh||'本地')+' · raceway 根 '+j.root+' · 车源 '+j.cars_file;
  const kb=document.getElementById('kibana');
  kb.href=j.kibana_url+'/app/dashboards';
  kb.textContent='Kibana 看板（'+j.kibana_url+'）→';
  document.getElementById('kibanahint').textContent =
    'Kibana 若只听远端 127.0.0.1，先建隧道：ssh -L 5601:127.0.0.1:5601 '+(j.ssh||'user@host');
  ['cars','tracks','rounds','recall','estov','dsh','custom','tag','incov'].forEach(function(id){
    document.getElementById(id).addEventListener('change',function(){savePrefs();loadHist();});
  });
  document.getElementById('pnotify').addEventListener('change',function(){
    if(this.checked && typeof Notification!=='undefined' && Notification.permission!=='granted')
      Notification.requestPermission();
  });
  restorePrefs();
  loadHist();
  loadCampaigns();
}
function paintCompat(){
  // 车的引擎 × 变体的兼容性：ZSTD 压缩与 index.source_reuse 是 Easysearch 专有，
  // ES 上 create-index 直接拒 —— 提前标出来，别排了再白跑一场建库。
  const m=matrix();
  const bad=[];
  CARS.forEach(function(a){
    if(m.cars.indexOf(a.name)<0) return;
    m.tracks.forEach(function(t){
      const info=TRACKS.find(function(x){return x.name===t;});
      const allow=info?(info.engines||''):'';
      if(allow && (a.engine||'').toLowerCase()!==allow)
        bad.push(a.name+'（'+a.engine+'）× '+t+'（只支持 '+allow+'）');
    });
  });
  const box=document.getElementById('compatbox');
  if(!bad.length){ box.innerHTML='<span style="color:#080">✔ 当前矩阵的车×变体组合全部兼容</span>'; return; }
  box.innerHTML='<span class="err">✘ '+bad.length+' 个组合不兼容（起跑会被拦下）：<br>'
    + bad.map(esc).join('<br>')+'</span>';
}
function matrix(){
  const cars=[...document.querySelectorAll('.carck:checked')].map(e=>e.value);
  const tracks=[...document.querySelectorAll('.tkck:checked')].map(e=>e.value);
  const custom=document.getElementById('custom').value.trim();
  if(custom) custom.split(',').forEach(t=>{t=t.trim(); if(t&&!tracks.includes(t))tracks.push(t);});
  return {cars, tracks, rounds:+document.getElementById('rounds').value,
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
    s.campaigns+'</b> 个 campaign / <b>'+s.tracks+'</b> 条赛道 / <b>'+s.cars+'</b> 个车';
  paintMatrix(); paintCompat();
}
function cellOf(car,track,round){
  if(!HIST) return null;
  return HIST.cells[car+'|'+track+'|'+round] || null;
}
function roundsOf(car,track){
  // 该格在库里出现过哪些轮次（全量，不受当前矩阵的轮次限制）
  if(!HIST) return [];
  const pre=car+'|'+track+'|', out=[];
  for(const k in HIST.cells){
    if(k.indexOf(pre)===0) out.push(parseInt(k.slice(pre.length),10));
  }
  return out.sort(function(x,y){return x-y;});
}
function paintMatrix(){
  if(!HIST){ document.getElementById('histmatrix').innerHTML=''; return; }
  const m=matrix();
  const selCars=new Set(m.cars), selTracks=new Set(m.tracks);
  const cars=HIST.cars||[], tracks=HIST.tracks||[];
  let h='<table class="hmatrix"><tr><th>车 \\ 赛道</th>';
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
  cars.forEach(function(a){
    const carDef=CARS.find(function(x){return x.name===a;})||{};
    const want=carDef.build? (a+'@'+carDef.build) : a;
    h+='<tr><th class="'+(selCars.has(a)?'cur':'dim')+'">'+esc(a)+
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
        const inMatrix = selCars.has(a) && selTracks.has(t) && r<=m.rounds;
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

function fmtMin(m){
  m=Math.round(m||0);
  if(!m) return '0 分钟';
  return (m>=60? Math.floor(m/60)+' 小时 ' : '')+(m%60)+' 分钟';
}
function windowHint(estTotal){
  const ds=+document.getElementById('dsh').value||0;
  if(ds>0 && estTotal>ds*60)
    return '；⚠ 预计 '+fmtMin(estTotal)+' 超出 DS_HOURS='+ds+'h 窗口，放不下的场次会被 DEADLINE-SKIP';
  return '；预计总耗时约 '+fmtMin(estTotal)+(ds>0?'（DS_HOURS='+ds+'h 窗口内）':'');
}
async function doRender(){ const {status,j}=await post('/api/render');
  if(status!=200){ show('sched','错误: '+(j.error||'未知')); setMsg(false,'生成失败'); return; }
  show('sched', j.schedule);
  setMsg(true,'生成 '+j.rows+' 场'+windowHint(j.est_total_min)); }
async function doPreview(){ setMsg(true,'dry-run 运行中…'); const {status,j}=await post('/api/preview');
  if(status!=200){show('out','错误: '+(j.error||'未知'));setMsg(false,'dry-run 失败');return;}
  show('sched',j.schedule); show('out',j.dryrun);
  setMsg(j.rc==0, j.rc==0?('dry-run 通过，可起跑；'+j.rows+' 场'+windowHint(j.est_total_min||0)):'dry-run 非零，勿起跑');}
async function doStart(){ if(!confirm('确认起跑？会占用压测集群。'))return;
  setMsg(true,'校验并起跑…'); const {status,j}=await post('/api/start');
  if(status!=200){show('out','错误: '+(j.error||'未知'));setMsg(false,'未起跑');return;}
  show('out',j.result); setMsg(true,'已起跑');
  // 起跑成功 → 直接带到进度页盯它
  if(j.campaign){ loadCampaigns(j.campaign); switchTab('progress'); }}
// ==================== 控制台：标签页 / 进度 / 历史 / 停止 / 重排 ====================
function onTab(name){
  return document.getElementById('tab-'+name).className.indexOf('on')>=0;
}
function switchTab(name){
  ['compose','progress','history'].forEach(function(t){
    document.getElementById('tab-'+t).className='tab'+(t===name?' on':'');
    document.getElementById('tb-'+t).className=(t===name?'on':'');
  });
  if(name==='progress') pollNow();
  if(name==='history') loadCampaigns();   // 回历史页顺带刷新活动状态
}

async function loadCampaigns(selectCid){
  let j;
  try{ const r=await fetch('/api/campaigns',{cache:'no-store'}); j=await r.json(); }
  catch(e){ return; }
  CAMPS=j.campaigns||[];
  const sel=document.getElementById('pcamp');
  sel.innerHTML=CAMPS.map(function(c){
    return '<option value="'+esc(c.cid)+'">'+esc(c.cid)+'（'+
      (c.ended?'已结束':'运行中')+' '+c.valid+'/'+c.total+'）</option>';
  }).join('');
  if(selectCid) CURCID=selectCid;
  if(!CURCID) CURCID=j.active||(CAMPS[0]?CAMPS[0].cid:null);
  const has=[...sel.options].some(function(o){return o.value===CURCID;});
  if(has){ sel.value=CURCID; } else { CURCID=sel.value||null; }
  ['cmpa','cmpb'].forEach(function(id){
    const s=document.getElementById(id);
    const cur=s.value;
    s.innerHTML=CAMPS.map(function(c){
      return '<option value="'+esc(c.cid)+'">'+esc(c.cid)+'</option>';}).join('');
    if([...s.options].some(function(o){return o.value===cur;})) s.value=cur;
  });
  renderHistory();
  pollNow();
}

async function pollNow(){
  clearTimeout(POLLT);
  // 只在进度页可见时轮询（切回进度页时 switchTab 会立刻触发一次）；
  // 一次轮询 = 队列推导 + 日志尾两次远端读，别在后台空转。
  if(!CURCID || !onTab('progress')) return;
  try{
    const enc=encodeURIComponent(CURCID);
    const [rq,rt]=await Promise.all([
      fetch('/api/queue?cid='+enc,{cache:'no-store'}),
      fetch('/api/logtail?cid='+enc+'&n=40',{cache:'no-store'})]);
    if(rq.ok) renderProgress(await rq.json());
    if(rt.ok){ const t=await rt.json(); show('ptail',t.lines||'（空）'); }
  }catch(e){/* 网络抖动下一轮再试 */}
  if(document.getElementById('pauto').checked) POLLT=setTimeout(pollNow,5000);
}

function renderProgress(d){
  const s=d.stats||{};
  const run=(d.fields||[]).find(function(f){return f.status==='RUNNING';});
  // 标签页标题当状态栏用：切到别的页也能瞄一眼进度
  if(run){ document.title='▶ '+run.fid+(run.phase?(' · '+run.phase):''); }
  else if(d.ended){ document.title='✔ '+d.campaign+'（'+s.valid+'/'+s.total+'）'; }
  else { document.title='raceway 控制台'; }
  // 完成提醒：上一轮还没结束、这一轮结束了 → 系统通知（需授权）
  if(PREV_ENDED===false && d.ended){
    if(document.getElementById('pnotify').checked
       && typeof Notification!=='undefined' && Notification.permission==='granted'){
      try{ new Notification('raceway: '+d.campaign+' 已结束',
        {body:s.valid+'/'+s.total+' 场有效'+(s.failed?('，失败 '+s.failed+' 场'):'')}); }catch(e){}
    }
  }
  PREV_ENDED=d.ended;
  let cur;
  if(run){
    cur='▶ ['+run.seq+'] <b>'+esc(run.fid)+'</b>'+
      (run.phase?(' · 相位 <b>'+esc(run.phase)+'</b>'):'')+
      (run.elapsed_min?(' · 已进行 <b>'+run.elapsed_min.toFixed(1)+'</b> 分钟（起 '+esc(run.started)+'）'):'');
  } else if(d.ended){
    cur='✔ 已结束：'+s.valid+'/'+s.total+' 场有效（起跑 '+esc(d.started_at||'-')+'）';
  } else if(d.started_at){
    cur='… runner 在跑，但当前没有进行中的场次（可能在部署 / 卡点 / 收尾阶段）';
  } else { cur='（尚无进度数据）'; }
  document.getElementById('pcur').innerHTML=cur;
  const done=s.total-s.pending;
  document.getElementById('pbar').style.width=(s.total?100*done/s.total:0)+'%';
  const eta=(s.pending>0&&s.est_remaining_min>0)
    ? new Date(Date.now()+s.est_remaining_min*60000).toLocaleTimeString() : '-';
  const cards=[['总场次',s.total],['有效',s.valid],['失败',s.failed],['跳过',s.skipped],
    ['待跑',s.pending],['剩余约(分)',Math.round(s.est_remaining_min||0)],['预计完成',eta]];
  document.getElementById('pcards').innerHTML=cards.map(function(c){
    return '<div class="card"><b>'+esc(c[1])+'</b><span>'+c[0]+'</span></div>';}).join('');
  // 没收尾但进度文件长期无更新 → runner 大概率已被杀（页面标出来，别让人干等）
  const warns=(d.warnings||[]).slice();
  const camp=CAMPS.find(function(x){return x.cid===d.campaign;});
  if(camp && !camp.ended && camp.mtime){
    const staleMin=(Date.now()/1000-camp.mtime)/60;
    if(staleMin>30)
      warns.unshift('进度文件已 '+Math.round(staleMin)+' 分钟无更新 —— runner 可能已停止'
        +'（杀掉不写结束行；确认没人跑可用 stop-campaign.sh 清场后断点续跑）');
  }
  document.getElementById('pwarns').innerHTML=warns
    .map(function(w){return '<div class="warn">⚠ '+esc(w)+'</div>';}).join('');
  document.getElementById('prows').innerHTML=(d.fields||[]).map(function(f){
    const cls=f.status==='VALID'?'st-ok':f.status==='RUNNING'?'st-run':
      (f.status==='SKIP'||f.status==='DEADLINE-SKIP')?'st-skip':
      f.status==='PENDING'?'st-pend':'st-bad';
    let note='';
    if(f.conflicts&&f.conflicts.length) note='⚠ 矛盾判定: '+f.conflicts.join('/');
    if(f.status==='RUNNING'&&f.phase)
      note=(note?note+' · ':'')+'相位 '+f.phase+
        (f.elapsed_min?(' · 已 '+f.elapsed_min.toFixed(1)+' 分钟'):'');
    // 数据可信度证据链：runner 分相位采样的 ENV 判定（最坏相位×节点）
    if(f.env_verdict){
      const ev=f.env_verdict;
      const tag=ev==='CONTAMINATED'?'ENV⛔'+(f.steal_max!=null?('steal峰值'+f.steal_max+'%'):'')
        :ev==='WARN'?'ENV⚠':ev==='OK'?'ENV✓':'';
      if(tag) note=(note?note+'  ':'')+tag;
    }
    if(f.detail) note=(note?note+'  ':'')+f.detail;
    return '<tr class="'+(f.status==='RUNNING'?'now':'')+'"><td>'+f.seq+'</td><td>'+esc(f.fid)+'</td>'+
      '<td class="'+cls+'">'+esc(f.status)+'</td><td>'+esc(f.duration||'-')+'</td>'+
      '<td>'+esc(f.recall||'-')+'</td><td>'+esc(note)+'</td></tr>';
  }).join('');
}

function renderHistory(){
  const f=document.getElementById('hfilter').value.trim().toLowerCase();
  const list=f?CAMPS.filter(function(c){return c.cid.toLowerCase().indexOf(f)>=0;}):CAMPS;
  document.getElementById('hrows').innerHTML=list.map(function(c){
    const chip=c.ended?'<span class="chip-end">已结束</span>':'<span class="chip-run">运行中</span>';
    const t=c.mtime?new Date(c.mtime*1000).toLocaleString():'-';
    return '<tr><td>'+esc(c.cid)+'</td><td>'+chip+'</td><td>'+c.valid+' / '+c.total+'</td>'+
      '<td>'+esc(t)+'</td><td style="white-space:normal;max-width:340px">'+esc(c.last||'')+'</td>'+
      '<td><button data-act="view" data-cid="'+esc(c.cid)+'">看进度</button>'+
      '<button data-act="reorder" data-cid="'+esc(c.cid)+'">按此重排</button></td></tr>';
  }).join('');
}
// 历史表按钮的事件委托（行是 innerHTML 重建的，监听器挂在容器上只挂一次）
document.getElementById('hrows').addEventListener('click', function(e){
  const b=e.target.closest('button[data-act]');
  if(!b) return;
  if(b.dataset.act==='view') viewCamp(b.dataset.cid);
  if(b.dataset.act==='reorder') reorderCamp(b.dataset.cid);
});
function viewCamp(cid){
  CURCID=cid;
  const sel=document.getElementById('pcamp');
  if([...sel.options].some(function(o){return o.value===cid;})) sel.value=cid;
  switchTab('progress'); pollNow();
}

// 「按此重排」：拿该场campaign的 车×赛道×轮次 预填启动排程
async function reorderCamp(cid){
  let d;
  try{ const r=await fetch('/api/queue?cid='+encodeURIComponent(cid)); d=await r.json(); }
  catch(e){ setMsg(false,'读取 campaign 失败: '+e); return; }
  if(d.error){ setMsg(false,d.error); return; }
  // 排程模式下起跑头 configs=[]，车名从逐场明细里取
  const cars=[...new Set((d.fields||[]).map(function(f){return f.car;}).filter(Boolean))];
  document.querySelectorAll('.carck').forEach(function(cb){ cb.checked=cars.includes(cb.value); });
  const tracks=(d.tracks&&d.tracks.length)
    ? d.tracks
    : [...new Set((d.fields||[]).map(function(f){return f.track;}).filter(Boolean))];
  const cust=[];
  document.querySelectorAll('.tkck').forEach(function(cb){ cb.checked=false; });
  tracks.forEach(function(t){
    const cb=document.querySelector('.tkck[value="'+t+'"]');
    if(cb) cb.checked=true; else cust.push(t);
  });
  document.getElementById('custom').value=cust.join(',');
  document.getElementById('rounds').value=d.rounds||1;
  const m=(cid||'').match(/^([A-Za-z][A-Za-z0-9]*)-\d{8}-\d{6}/);
  document.getElementById('tag').value=m?m[1]:'';
  savePrefs(); loadHist(); switchTab('compose');
  setMsg(true,'已按 '+cid+' 预填（车 '+(cars.join('/')||'?')+' × '+tracks.length+' 赛道 × '+
    (d.rounds||'?')+' 轮）—— 检查后点「生成排程」');
}

async function doStop(){
  if(!CURCID){ setMsg(false,'未选择 campaign'); return; }
  if(!confirm('确认停止 '+CURCID+'？\\n将杀掉远端 runner 与 esrally 进程；campaign 目录保留，可断点续跑。'))return;
  setMsg(true,'停止中…');
  try{
    const r=await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid:CURCID})});
    const j=await r.json();
    if(r.status!=200){ show('ptail','停止失败: '+(j.error||'未知')); setMsg(false,'停止失败'); return; }
    show('ptail',j.result||'已停止'); setMsg(true,'已发送停止命令');
    loadCampaigns(CURCID);
  }catch(e){ setMsg(false,'停止请求失败: '+e); }
}

async function doDoctor(){
  setMsg(true,'环境体检中…（远端 doctor.sh，约 30-60 秒）');
  switchTab('compose');
  show('out','体检中…');
  scrollOut();
  try{
    const r=await fetch('/api/doctor',{method:'POST'});
    const j=await r.json();
    if(r.status!=200){ show('out','体检失败: '+(j.error||'未知')); setMsg(false,'体检失败'); return; }
    show('out',j.result);
    setMsg(j.rc==0, j.rc==0?'环境体检全部通过 ✔':'体检有失败项（见输出里的 ✘）');
    scrollOut();   // 结果在页面很深处，不做这一步用户只看到顶上一句话
  }catch(e){ show('out','体检请求失败: '+e); setMsg(false,'体检请求失败'); }
}
function scrollOut(){
  const el=document.getElementById('out');
  if(el) el.scrollIntoView({behavior:'smooth',block:'center'});
}

async function doCompare(){
  const a=document.getElementById('cmpa').value, b=document.getElementById('cmpb').value;
  if(!a||!b||a===b){ setMsg(false,'请选择两个不同的 campaign'); return; }
  const car_a=document.getElementById('cmpcara').value.trim();
  const car_b=document.getElementById('cmpcarb').value.trim();
  show('cmpout','对比中…（远端 car-compare，可能要几十秒）');
  try{
    const r=await fetch('/api/compare',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({a:a,b:b,car_a:car_a,car_b:car_b})});
    const j=await r.json();
    if(r.status!=200){ show('cmpout','对比失败: '+(j.error||'未知')); setMsg(false,'对比失败'); return; }
    show('cmpout',(j.rc!=0?('（退出码 '+j.rc+'）\\n'):'')+j.result);
    setMsg(!!j.result,'对比完成：'+a+' 的 '+j.car.split(' vs ')[0]+' vs '+b+' 的 '+j.car.split(' vs ')[1]);
  }catch(e){ show('cmpout','对比请求失败: '+e); setMsg(false,'对比请求失败'); }
}

// ---- 勾选偏好记忆（localStorage）：下次打开不用重勾 ----
function savePrefs(){
  try{ localStorage.setItem('raceway.compose', JSON.stringify(matrix())); }catch(e){}
}
function restorePrefs(){
  let p;
  try{ p=JSON.parse(localStorage.getItem('raceway.compose')||'null'); }catch(e){ return; }
  if(!p) return;
  document.querySelectorAll('.carck').forEach(function(cb){
    cb.checked=p.cars?p.cars.includes(cb.value):cb.checked; });
  document.querySelectorAll('.tkck').forEach(function(cb){
    cb.checked=p.tracks?p.tracks.includes(cb.value):cb.checked; });
  if(p.custom!=null) document.getElementById('custom').value=p.custom;
  if(p.rounds) document.getElementById('rounds').value=p.rounds;
  if(p.recall) document.getElementById('recall').value=p.recall;
  if(p.est!=null) document.getElementById('estov').value=p.est;
  if(p.ds_hours!=null) document.getElementById('dsh').value=p.ds_hours;
  if(p.tag!=null) document.getElementById('tag').value=p.tag;
}

boot();
</script></body></html>
"""


class ComposeState:
    """serve 模式的共享状态：启动参数 + 已解析车。线程锁保护远端操作串行。"""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        text = read_cars_file(args)
        self.cars = parse_cars(text)
        self.cars_src_text = text
        self.problems = validate_cars(self.cars)
        # 进度/历史页的 campaign 目录读取（本地或 ssh），与 bench-queue 同一抽象。
        # Source 的 root 语义是 campaigns 目录，而 --root 是 raceway 根 —— 补一级。
        self.src = Q.Source(root=posixpath.join(args.root, 'campaigns'), ssh=args.ssh)
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

    def build_of(self, car_name):
        for a in self.cars:
            if a.name == car_name:
                return a.build
        return ''


def api_render(st, body):
    cars = [a for a in st.cars if a.name in body['cars']] or st.cars
    order = {a.name: i for i, a in enumerate(st.cars)}
    cars = sorted(cars, key=lambda a: order[a.name])
    problems = validate_cars(cars)
    hard = [m for s, m in problems if s == 'error']
    if hard:
        return 400, {'error': '；'.join(hard)}
    if not body['tracks']:
        return 400, {'error': '未选赛道'}
    tracks = list(body['tracks'])
    rounds = max(1, int(body['rounds']))
    rows = interleave(cars, tracks, rounds)

    # 已入库历史覆盖：默认剔除"当前 build 上已经跑过"的格
    incov = bool(body.get('include_covered'))
    cov = st.history()
    plan = H.plan_cells(cov, cars, tracks, rounds, include_covered=incov)
    if plan['skipped']:
        rows = H.filter_rows(plan, rows)
    if not rows:
        return 400, {'error': '矩阵里的每一格都已在指标库跑过（当前 build），无事可排。'
                             '要重跑请勾选"含已跑过的格"。',
                     'plan': plan}

    # 引擎 × 变体兼容性（ZSTD / source_reuse 是 Easysearch 专有）
    bad = compat_problems(cars, rows)
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
    sched = render_schedule(rows, cars, tag, est_map,
                            int(body.get('est_default') or DEFAULT_EST),
                            recall=(body.get('recall') != 'norecall'))
    # runner 的赛道分支参数：serve 模式没有 CLI 的 compose_all 推导，这里补上
    # （此前 GUI 的 preview/start 把 what=None 传给 runner，dry-run 必失败）
    what = body.get('what') or what_for(tracks)
    est_total = est_total_min(rows, est_map, int(body.get('est_default') or DEFAULT_EST))
    st.last = {'cars': cars, 'schedule': sched, 'tag': tag, 'plan': plan, 'what': what,
               'est_total_min': est_total}
    return 200, {'schedule': sched, 'rows': len(rows), 'plan': plan, 'what': what,
                 'est_total_min': est_total}


def api_preview(st, body):
    rc_, out = api_render(st, body)
    if rc_ != 200:
        return rc_, out
    with st.lock:
        upload(st.args, st.last['tag'], st.last['schedule'], render_cars(st.last['cars'], st.last['tag']))
        drc, dry = remote_dry_run(st.args, st.last['tag'], int(body.get('ds_hours') or 0),
                                  what=st.last['what'])
    n_rows = sum(1 for l in st.last['schedule'].splitlines()
                 if l.strip() and not l.startswith('#'))
    return 200, {'schedule': st.last['schedule'], 'dryrun': dry, 'rc': drc,
                 'what': st.last['what'], 'rows': n_rows,
                 'est_total_min': st.last['est_total_min']}


def api_start(st, body):
    with st.lock:
        why = remote_mutex_check(st.args)
        if why:
            return 400, {'error': why}
        rc_, out = api_render(st, body)
        if rc_ != 200:
            return rc_, out
        upload(st.args, st.last['tag'], st.last['schedule'], render_cars(st.last['cars'], st.last['tag']))
        drc, dry = remote_dry_run(st.args, st.last['tag'], int(body.get('ds_hours') or 0),
                                  what=st.last['what'])
        if drc != 0:
            return 400, {'error': 'dry-run 未通过，拒绝起跑\n' + dry}
        try:
            # remote_start 内部会再做一次互斥检查（防 preview/起跑竞态窗口）
            camp, log = remote_start(st.args, st.last['tag'],
                                     int(body.get('ds_hours') or 0), None,
                                     what=st.last['what'])
        except SystemExit as e:
            return 400, {'error': str(e)}
    return 200, {'result': '已起跑 tag=%s\ncampaign: %s\n日志: %s\n观测: 本页「运行进度」标签'
                 % (st.last['tag'], camp, log),
                 'campaign': camp, 'tag': st.last['tag']}


# ---------------------------------------------------------------- 控制台 API（进度 / 历史 / 停止）

CID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def check_cid(cid):
    """campaign 名白名单：路径片段与 shell 参数都靠它兜底。"""
    if not cid or not CID_RE.match(cid):
        return '非法 campaign 名: %r（只允许 字母/数字/._-）' % (cid,)
    return None


def api_campaigns(st):
    """全部 campaign 的轻量摘要 + 默认该看的（最新没收尾的）。"""
    with st.lock:
        sums = Q.campaign_summaries(st.src)
    return 200, {'campaigns': sums[:300], 'active': Q.pick_active(sums)}


def api_queue(st, cid):
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    d = Q.campaign_json(Q.build_campaign(st.src, cid))
    # 数据可信度证据链上屏：monitors/ENV-SUMMARY.csv（runner 收尾时拍平）→
    # 逐场最坏 ENV verdict + steal 峰值。读不到（旧 campaign 没跑监控）就不加列。
    env = parse_env_summary(st.src.read_abs(
        posixpath.join(st.args.root, 'campaigns', cid, 'monitors', 'ENV-SUMMARY.csv')) or '')
    if env:
        for f in d['fields']:
            e = env.get(f['fid'])
            if e:
                f['env_verdict'] = e['verdict']
                f['steal_max'] = e['steal_max']
    return 200, d


# ENV verdict 的严重度序（越大越坏）；NO-DATA 只说明没采到，不算脏
ENV_VERDICT_RANK = {'CONTAMINATED': 3, 'WARN': 2, 'OK': 1, 'NO-DATA': 0}


def parse_env_summary(text):
    """ENV-SUMMARY.csv → {fid: {'verdict': 最坏判定, 'steal_max': 峰值}}。

    一场 = 多相位 × 多节点多行；判定取最坏（CONTAMINATED > WARN > OK > NO-DATA），
    steal_max 取跨相位跨节点最大（报告"这场最脏的时候有多脏"）。
    """
    out = {}
    lines = (text or '').splitlines()
    for line in lines[1:]:
        parts = (line.split(',') + [''] * 11)[:11]
        fid = parts[0].strip()
        if not fid:
            continue
        try:
            steal = float(parts[6]) if parts[6] else None
        except ValueError:
            steal = None
        verdict = parts[10].strip() or 'NO-DATA'
        e = out.setdefault(fid, {'verdict': 'NO-DATA', 'steal_max': None})
        if ENV_VERDICT_RANK.get(verdict, 0) > ENV_VERDICT_RANK.get(e['verdict'], 0):
            e['verdict'] = verdict
        if steal is not None and (e['steal_max'] is None or steal > e['steal_max']):
            e['steal_max'] = steal
    return out


def api_logtail(st, cid, n):
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    n = max(1, min(int(n or 50), 400))
    return 200, {'lines': st.src.tail(cid, 'PROGRESS.txt', n)}


def api_stop(st, body):
    """停止一个 campaign：调远端 stop-campaign.sh（仓库已收编该脚本）。

    这是控制台唯一会杀进程的操作：二次确认在页面侧，参数白名单在这里。
    """
    cid = (body.get('cid') or '').strip()
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    script = posixpath.join(st.args.root, 'stop-campaign.sh')
    with st.lock:
        rc, out = ssh_run(st.args.ssh, 'bash %s %s' % (shlex.quote(script), shlex.quote(cid)),
                          timeout=120)
    if rc != 0:
        return 400, {'error': '停止脚本返回码 %d：\n%s\n'
                     '（检查远端 %s 是否存在且可执行；可从仓库拷贝 stop-campaign.sh 过去）'
                     % (rc, out.strip(), script)}
    return 200, {'result': out.strip() or '已发送停止命令'}


def api_doctor(st):
    """新环境体检：远端跑 doctor.sh（纯只读），回答"离能起跑还差什么"。

    doctor.sh 随仓库分发 —— 远端没有时给部署提示，而不是裸报错。
    """
    cmd = 'cd %s && if [ -f doctor.sh ]; then bash doctor.sh; else echo MISSING; fi' \
          % shlex.quote(st.args.root)
    with st.lock:
        rc, out = ssh_run(st.args.ssh, cmd, timeout=300)
    if 'MISSING' in out:
        return 400, {'error': '远端 %s 没有 doctor.sh —— 新环境体检脚本随仓库分发，'
                     '先拷过去：scp doctor.sh %s:%s/' % (st.args.root, st.args.ssh, st.args.root)}
    return 200, {'result': out, 'rc': rc}


def api_compare(st, body):
    """两场对比：远端执行仓库版 car-compare.py（stdin 喂脚本，不受远端旧拷贝影响）。

    car-compare 的语义是 **A 车 vs B 车**（campaign 是取数池），所以两侧各带一个
    车代号：留空时 A 取该场第一个车，B 尽量取与 A 不同的第一个车（两场各跑了
    jdk25/kona25 的典型场景直接默认出 jdk25 vs kona25）。
    """
    ca = (body.get('a') or '').strip()
    cb = (body.get('b') or '').strip()
    for c in (ca, cb):
        why = check_cid(c)
        if why:
            return 400, {'error': why}
    if ca == cb:
        return 400, {'error': '两个 campaign 是同一个，A/B 池合在一起没有可比性'}
    car_a = (body.get('car_a') or '').strip()
    car_b = (body.get('car_b') or '').strip()
    with st.lock:
        if not car_a or not car_b:
            cars_a = sorted({f.car for f in Q.build_campaign(st.src, ca).fields if f.car})
            cars_b = sorted({f.car for f in Q.build_campaign(st.src, cb).fields if f.car})
            if not car_a:
                if not cars_a:
                    return 400, {'error': 'A 场（%s）里没有可用的车代号，请显式填写' % ca}
                car_a = cars_a[0]
            if not car_b:
                diff = [c for c in cars_b if c != car_a]
                car_b = diff[0] if diff else (cars_b or [car_a])[0]
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, 'car-compare.py'), encoding='utf-8') as f:
                script = f.read()
        except OSError as e:
            return 400, {'error': '读不到仓库里的 car-compare.py: %s' % e}
        cmd = ('cd %s && python3 - %s %s --a %s --b %s'
               % (shlex.quote(st.args.root),
                  shlex.quote(posixpath.join('campaigns', ca)),
                  shlex.quote(posixpath.join('campaigns', cb)),
                  shlex.quote(car_a), shlex.quote(car_b)))
        rc, out = ssh_run(st.args.ssh, cmd, input_text=script, timeout=300)
    if rc != 0 and not out.strip():
        return 400, {'error': 'car-compare 返回码 %d 且无输出（远端 python3 可用？）' % rc}
    return 200, {'result': out, 'rc': rc, 'car': '%s vs %s' % (car_a, car_b)}


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
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = self.server.state
        try:
            self._route_get(u.path, q, st)
        except Exception as e:  # 参数/远端失败等，页面要能看到原因而不是断连
            self._send(500, 'application/json',
                       json.dumps({'error': '%s: %s' % (type(e).__name__, e)},
                                  ensure_ascii=False))

    def _route_get(self, path, q, st):
        if path == '/':
            self._send(200, 'text/html; charset=utf-8', _PAGE)
        elif path == '/api/bootstrap':
            self._send(200, 'application/json', json.dumps({
                'cars': [a._asdict() for a in st.cars],
                'problems': st.problems, 'ds_tiers': DS_TIERS,
                # 全部可起跑的赛道（不是只有 ds-*）：geo/codec 变体、hnsw、hv1024、nested
                'tracks': [dict(v, name=k) for k, v in TRACK_CATALOG.items()],
                'track_groups': TRACK_GROUPS,
                'base_tracks': sorted(BASE_TRACKS),
                'codec_tokens': CODEC_TOKENS,
                'token_label': TOKEN_LABEL,
                'token_codec': TOKEN_CODEC,
                'ssh': st.args.ssh or '', 'root': st.args.root,
                'cars_file': st.args.cars_file,
                'kibana_url': st.args.kibana_url,
            }, ensure_ascii=False))
        elif path == '/api/history':
            cov = st.history()
            self._send(200, 'application/json', json.dumps(cov.to_dict(), ensure_ascii=False))
        elif path == '/api/campaigns':
            code, payload = api_campaigns(st)
            self._send(code, 'application/json', json.dumps(payload, ensure_ascii=False))
        elif path == '/api/queue':
            code, payload = api_queue(st, (q.get('cid') or [''])[0])
            self._send(code, 'application/json', json.dumps(payload, ensure_ascii=False))
        elif path == '/api/logtail':
            code, payload = api_logtail(st, (q.get('cid') or [''])[0],
                                        (q.get('n') or ['50'])[0])
            self._send(code, 'application/json', json.dumps(payload, ensure_ascii=False))
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
            elif self.path == '/api/stop':
                code, payload = api_stop(st, body)
            elif self.path == '/api/compare':
                code, payload = api_compare(st, body)
            elif self.path == '/api/doctor':
                code, payload = api_doctor(st)
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
        print('警告：车定义有未解决问题（渲染时会拦截）：', file=sys.stderr)
        for m in hard:
            print('  ' + m, file=sys.stderr)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.state = state
    url = 'http://127.0.0.1:%d' % args.port
    print('raceway 控制台: %s  （① 启动排程 · ② 运行进度 · ③ 历史与结果）' % url)
    print('  远端 %s · raceway 根 %s · 车源 %s' % (args.ssh or '本地', args.root, args.cars_file))
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['cars', 'gen', 'preview', 'start', 'serve'])
    ap.add_argument('--cars-file', default=None, help='车定义文件（本地路径；--ssh 时为远端路径）')
    ap.add_argument('--cars', default=None,
                    help='只用这些车（逗号分隔）；缺省用车定义文件里的全部')
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
    ap.add_argument('--ssh', default=os.environ.get('RACEWAY_SSH'),
                    help='user@host；缺省读环境变量 RACEWAY_SSH，再缺省操作本地')
    ap.add_argument('--port', type=int, default=8802)
    ap.add_argument('--kibana-url', default='http://127.0.0.1:5601',
                    help='历史页外链的 Kibana 地址（默认 http://127.0.0.1:5601；'
                         'Kibana 只听远端 127.0.0.1 时需先建 ssh 隧道）')
    ap.add_argument('--no-open', action='store_true',
                    help='serve 起来后不自动打开浏览器')
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

    if args.cars_file is None:
        if args.ssh:
            args.cars_file = posixpath.join(args.root, 'schedules', 'dimsweep-cars.conf')
        else:
            # 本地模式的车定义默认取仓库内 schedules/dimsweep-cars.conf。
            # 该文件属内部资料，不随公共库公开（仓库里没有这个目录），
            # 所以按「环境变量 → 仓库内默认 → 明确报错」三级回退，
            # 免得直接抛一个看不懂的 FileNotFoundError。
            here = os.path.dirname(os.path.abspath(__file__))
            args.cars_file = (os.environ.get('RACEWAY_CARS_FILE')
                               or os.path.join(here, 'schedules', 'dimsweep-cars.conf'))
            if not os.path.isfile(args.cars_file):
                # 旧名兼容：改名前的文件叫 dimsweep-arms.conf
                _leg = args.cars_file.replace('cars.conf', 'arms.conf')
                if _leg != args.cars_file and os.path.isfile(_leg):
                    args.cars_file = _leg
            if not os.path.isfile(args.cars_file):
                raise SystemExit(
                    '找不到车定义文件: %s\n'
                    '车定义属内部资料，未随公共库公开 —— 用 --cars-file 指定路径，'
                    '或设环境变量 RACEWAY_CARS_FILE 指向它。\n'
                    '远端模式（--ssh）不受影响：默认读远端 %s'
                    % (args.cars_file,
                       posixpath.join(args.root, 'schedules', 'dimsweep-cars.conf')))

    {'cars': cmd_cars, 'gen': cmd_gen, 'preview': cmd_preview,
     'start': cmd_start, 'serve': cmd_serve}[args.cmd](args)


if __name__ == '__main__':
    main()
