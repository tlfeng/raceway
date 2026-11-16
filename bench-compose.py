#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-compose.py — raceway 控制台（启动排程 + 运行进度 + 历史结果 + 趋势 + 入库）

serve 是五合一 Web 控制台（原"排程编排台"逐步扩容）：
- 前端在 web/ 目录（Preact+HTM 轻量单页，风格对齐 logpilot web）；web/ 缺失时回退内联旧页
- ① 启动排程（PLAN v1：矩阵 → 排程文件 → 预览 → 起跑）
- ② 运行进度：campaign 队列实时视图（复用 bench-queue 的推导），自动选中
  最新没收尾的 campaign；显示当前场次/相位/已进行时长、进度条、剩余预估、
  PROGRESS.txt 尾部日志；可停止（远端 stop-campaign.sh）；窄屏可看（值班场景）
- ③ 历史与结果：全部 campaign 清单（一次远端调用取摘要），任一场可"看进度"、
  可"按此重排"（预填启动矩阵）；两场对比为结构化表格（Δ 按方向着色）+
  原始文本可折叠；外链 Kibana 看板
- ④ 趋势：跨 campaign 的指标走势（只读指标库，echarts 折线；库缺失降级表格）
- ⑤ 赛道与入库：远端 run-bench.sh list 的赛道目录 + exports/ 入库
  （bench-apply 预检不过不发任何请求，断言状态先展示，--drop 需页面二次确认）

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
import hashlib
import importlib
import secrets
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
from dataclasses import dataclass
from types import MappingProxyType
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Q = importlib.import_module('bench-queue')  # 文件名带连字符
order_for_round = Q.order_for_round
H = importlib.import_module('bench-history')  # 已入库历史覆盖（指标库 benchmark-result-race）

DEFAULT_ROOT = '/opt/raceway'
DEFAULT_WHAT = 'ds'
# 与 run-bench.sh:117 的 ds) 分支保持同步
DS_TIERS = ['ds-g1920', 'ds-j2048', 'ds-j1024', 'ds-g960', 'ds-j768', 'ds-j512', 'ds-j1536']
DEFAULT_EST = 45

Car = namedtuple('Car', 'name label engine build jdk_dir config')
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')


def safe_name(value, label='tag'):
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise ValueError('%s 只能是安全单文件名（1–80 位字母/数字/._-，首位为字母或数字）' % label)
    return value


def integer(value, label, minimum=0, maximum=100000):
    if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
        raise ValueError('%s 必须是整数' % label)
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError('%s 必须在 %d–%d 之间' % (label, minimum, maximum))
    return result


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class ComposeSnapshot:
    """单请求的不可变执行材料；上传、dry-run、起跑不得再读共享页面状态。"""
    cars: tuple
    rows: tuple
    tag: str
    schedule: str
    cars_text: str
    what: str
    ds_hours: int
    camp_prefix: str
    digest: str
    est_total_min: int

    def payload(self):
        return {'tag': self.tag, 'schedule': self.schedule, 'cars': self.cars_text,
                'what': self.what, 'ds_hours': self.ds_hours,
                'camp_prefix': self.camp_prefix, 'digest': self.digest}


def make_snapshot(cars, rows, tag, est_map, est_default, recall, what, ds_hours=0,
                  camp_prefix=None):
    ds_hours = integer(ds_hours, 'DS_HOURS', 0, 8760)
    prefix = safe_name(camp_prefix or 'jdk3', 'campaign 前缀')
    if not rows:
        raise ValueError('排程不能为空')
    if not isinstance(what, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', what):
        raise ValueError('非法 runner 赛道分支')
    # 自动 tag 取内容摘要而非时钟；跨 CLI preview/start 仍能复用相同材料。
    seed = {'cars': [list(a) for a in cars], 'rows': rows,
            'est': {tk: est_map.get(tk, est_default) for _, tk, _ in rows},
            'est_default': est_default, 'recall': recall, 'what': what,
            'ds_hours': ds_hours, 'camp_prefix': prefix}
    tag = safe_name(tag or ('compose-' + hashlib.sha256(canonical_json(seed).encode('utf-8')).hexdigest()[:20]))
    schedule = render_schedule(rows, cars, tag, est_map, est_default, recall)
    cars_text = render_cars(cars, tag)
    back = [(a, t, r) for a, t, r, _, _ in schedule_rows_from_text(schedule)]
    if back != list(rows):
        raise ValueError('渲染结果与生成序不一致')
    material = {'tag': tag, 'schedule': schedule, 'cars': cars_text, 'what': what,
                'ds_hours': ds_hours, 'camp_prefix': prefix}
    digest = hashlib.sha256(canonical_json(material).encode('utf-8')).hexdigest()
    return ComposeSnapshot(tuple(cars), tuple(rows), tag, schedule, cars_text, what,
                           ds_hours, prefix, digest, est_total_min(rows, est_map, est_default))

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
    names = set()
    for a in cars:
        for value, label in ((a.name, '车名'), (a.label, 'CAR_LABEL')):
            try:
                safe_name(value, label)
            except ValueError as e:
                problems.append(('error', str(e)))
        if a.name in names:
            problems.append(('error', '车名重复: %s' % a.name))
        names.add(a.name)
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
        out[k] = integer(v, '--est 分钟', 1, 100000)
    return out


def render_schedule(rows, cars, tag, est_map=None, est_default=DEFAULT_EST, recall=True):
    """渲染排程文件文本。字段：`<车> <赛道> <轮次> <recall|norecall> <est分钟>`。

    注意：runner 的 schedule_lines 会整行剥 `#` 之后的内容，所以行内
    不能有任何 `#`；说明性文字全部放在行首注释区。
    """
    est_map = est_map or {}
    by_name = {a.name: a for a in cars}
    head = [
        '# raceway compose 排程：tag=%s' % tag,
        '# 生成：bench-compose.py（确定性快照）',
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
    head = [
        '# raceway compose 车定义：tag=%s' % tag,
        '# 生成：bench-compose.py（确定性快照；源自所选车定义文件）',
        '# 每行：<车名> <配置行...>   配置行语法与 CONF_FILE 一致',
        '',
    ]
    return '\n'.join(head) + '\n'.join('%s %s' % (a.name, a.config) for a in cars) + '\n'


# ---- 赛道清单（两层：基础赛道 → 压缩 × source_reuse 变体网格）-----------------
# 压缩与 source_reuse 是**索引级设置**，对任何基础赛道都成立 → 每个基础赛道派生
# 6 个变体（3 压缩 × 2 sr）。
#
# 变体 id = <基础赛道id>-codec-<def|zstd|bc>[-sr]  （与变体目录名一致）
# 引擎兼容不是查表而是**规则推导**（与压测机 vm-bench-lib.sh 同一套）→
# 不存在两处表格漂移的可能。
#
# 能不能起跑看 run-bench.sh:109-118 的赛道分支；hvtp200k-2s/3s 当前 runner 的
# track_dir 没有这两个分支 → 只能看历史，排不了。
#
# est 取各 campaign 的 RACES.md 实测耗时上界 + 余量（窗底守卫靠它算"这场塞不塞
# 得进剩余窗口"，估小了会把排得下的场次误判成 DEADLINE-SKIP）。
# ds-* 用现成排程 dimsweep-24.conf 里的值 —— 那是在用口径，别另起一套。
# 名字 → (展示名, est 默认分钟, 数据集标注)
#
# ds-* 族有**两套不同的数据集**（赛道名里的血统字母，j=jina 系，g=GIST 系）：
#   jina 系：ds-j512 / ds-j768 / ds-j1024 / ds-j1536 / ds-j2048
#   GIST 系：ds-g960 / ds-g1920
# 两套都是 1M docs、float、dot_product HNSW，差的是**向量数据集本身** ——
# 分布不同，召回与吞吐的绝对值不能跨数据集直接比。编排时必须看得见是哪一套。
#
# 值的第 4 位（可选，默认 True）= 该基座**支持压缩变体**：index.json 里得真有
# index_codec / source_reuse 条件块，变体才能落进 settings。os 族 / bbqdisk /
# geonames-upsert 的 index.json 是**烘焙定值**（无 Jinja 条件块）→ 变体排了也会
# 在起跑前被 track_assert_storage_params 挡下 —— 干脆不生成，别让用户排了再失败。
# hvtp200k-2s/3s 有历史数据但 runner 无分支 → 干脆不进清单（能看不能排，见 CLI 报错文案）。
BASE_TRACKS = {
    'geo':      ('geonames', 30, None),
    'geonames-upsert': ('geonames upsert/update', 40, 'geonames', False),
    'hnsw':     ('hnsw 2m', 25, None),
    'hv1024':   ('hv 1024 维', 30, None),
    'hv1024x':  ('hv1024x 四臂（1024 维）', 20, None),
    'hv1024x-bbqdisk': ('hv1024x ES bbq_disk', 20, None, False),
    'hv1024x-os':      ('1024 维 OS · lucene hnsw', 20, None, False),
    'hv1024x-os-bbq':  ('1024 维 OS · BBQ(32x)', 20, None, False),
    'hv1024x-os-disk': ('1024 维 OS · on_disk(faiss)', 20, None, False),
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
                ('geo', 'geonames（基座 / upsert 对比）'),
                ('vector', '向量 hnsw / hv1024 / hv1024x 家族'), ('nested', 'nested')]
BASE_GROUP = {'geo': 'geo', 'geonames-upsert': 'geo', 'hnsw': 'vector',
              'hv1024': 'vector', 'hv1024x': 'vector', 'hv1024x-bbqdisk': 'vector',
              'hv1024x-os': 'vector', 'hv1024x-os-bbq': 'vector',
              'hv1024x-os-disk': 'vector', 'nested': 'nested'}
# 压缩 token（与 vm-bench-lib.sh 的 track_compression 同一套）
CODEC_TOKENS = ['def', 'zstd', 'bc']
TOKEN_LABEL = {'def': 'default(LZ4)', 'zstd': 'ZSTD', 'bc': 'best_compression'}
TOKEN_CODEC = {'def': 'default', 'zstd': 'ZSTD', 'bc': 'best_compression'}
# 变体 est 系数（相对基础赛道）：
#   （default,关）1.0 —— 基线
#   （ZSTD,开）  1.25 —— 来自 geo 实测
#   其余取中间值 —— 压缩对总耗时的影响**没有实测**，这里是保守估计，
#   跑完一轮后按实测值用 --est 覆盖。
VARIANT_EST_FACTOR = {
    ('def', False): 1.0, ('def', True): 1.05,
    ('zstd', False): 1.2, ('zstd', True): 1.25,
    ('bc', False): 1.0, ('bc', True): 1.05,
}


def build_catalog():
    """完整赛道清单：每个基础赛道 + 它的 6 个压缩变体（仅支持变体的基座）。

    返回 {id: {name, label, est, group, base, compression, source_reuse, engines}}
    """
    cat = {}
    for base, spec in BASE_TRACKS.items():
        label, est, dataset = spec[0], spec[1], spec[2]
        can_codec = spec[3] if len(spec) > 3 else True   # 4 位缺省 = 支持变体（旧条目兼容）
        grp = BASE_GROUP.get(base, 'ds' if base.startswith('ds-') else 'geo')
        cat[base] = {'name': base, 'label': label, 'est': est, 'group': grp,
                     'base': base, 'compression': None, 'source_reuse': None,
                     'engines': H.engines_of(base), 'variant': False,
                     'dataset': dataset}
        if not can_codec:
            continue
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
    """执行远端命令（或 Unix 本机）；返回码与 stderr 都必须保留。"""
    if ssh_target:
        full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        full += ssh_target.split()
        full += [cmd + ' 2>&1']
    else:
        if os.name == 'nt':
            raise SystemExit('Windows 上执行 runner 操作必须指定 --ssh；gen/cars 可离线使用')
        full = ['/bin/bash', '-c', cmd + ' 2>&1']
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


# 远端只执行这一份结构化辅助程序。参数是 JSON，文件内容经 JSON 传入而不是拼 shell。
# runner 的原子锁是最终互斥边界；这里的 mutex 仅作起跑前预检。
_REMOTE = r'''
import fcntl, hashlib, json, os, pathlib, re, subprocess, sys, tempfile, time
mode, p = sys.argv[1], json.load(sys.stdin)
root = pathlib.Path.cwd().resolve()
results = pathlib.Path(os.environ['RESULTS_ROOT'])
if not results.is_absolute():
    results = root / results
results = results.resolve()

def fail(message):
    raise RuntimeError(message)

def safe_track_path(base, rel):
    """赛道文件相对路径 → 绝对路径；拒绝越界、空段、绝对/反斜杠/符号链接。"""
    if not isinstance(rel, str) or not rel or len(rel) > 200:
        fail('非法赛道路径')
    if '\\' in rel or rel.startswith('/') or '\0' in rel:
        fail('非法赛道路径')
    parts = rel.split('/')
    if any(x in ('', '.', '..') for x in parts):
        fail('赛道路径不能含空段、. 或 ..')
    if any(not re.fullmatch(r'[A-Za-z0-9._-]+', x) for x in parts):
        fail('赛道路径只允许字母数字与 ._-')
    tracks = (base / 'tracks').resolve()
    fp = (tracks / rel).resolve()
    if fp != tracks and tracks not in fp.parents:
        fail('赛道路径越界')
    if fp.is_symlink():
        fail('赛道文件不允许符号链接')
    return fp

def proc_identity(pid):
    text = pathlib.Path('/proc/%d/stat' % pid).read_text()
    fields = text[text.rfind(')') + 2:].split()
    if fields[0] == 'Z':
        fail('runner 已成为僵尸进程')
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    cmd = pathlib.Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
    return fields[19], boot, [os.fsdecode(x) for x in cmd if x]

def files():
    tag = p['tag']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', tag):
        fail('非法 tag')
    material = {k: p[k] for k in ('tag', 'schedule', 'cars', 'what', 'ds_hours', 'camp_prefix')}
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    if digest != p['digest']:
        fail('请求摘要不匹配')
    manifest = json.dumps({'digest': digest, 'what': p['what'], 'ds_hours': p['ds_hours'],
                           'camp_prefix': p['camp_prefix']}, sort_keys=True) + '\n'
    return {tag + '.conf': p['schedule'].encode(), tag + '-cars.conf': p['cars'].encode(),
            tag + '.compose.json': manifest.encode()}

def check_files(expected):
    d = root / 'schedules'
    if d.is_symlink() or not d.is_dir():
        fail('schedules 必须是真实目录')
    for name, data in expected.items():
        path = d / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            fail('排程快照不存在或内容已变更: ' + name)
    return d

try:
    if mode == 'inspect':
        reply = {'results_root': str(results)}
    elif mode == 'mutex':
        # 与 runner 共用 RESULTS_ROOT/.runner.lock；预检释放后最终加锁仍由 runner 执行。
        results.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(results / '.runner.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail('远端已有活跃 runner（.runner.lock 被占用）')
        finally:
            os.close(fd)
        reply = {'unlocked': True, 'results_root': str(results)}
    elif mode == 'upload':
        expected = files()
        d = root / 'schedules'
        # 先判存在再建：某些平台的 mkdir(exist_ok=True) 对已存在目录仍会抛 EEXIST
        # （实测 macOS 的 python 3.13 构建），这里不依赖那个开关的容错语义。
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
        if d.is_symlink():
            fail('schedules 不允许符号链接')
        fd = os.open(str(d / '.compose-upload.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            present = [os.path.lexists(d / n) for n in expected]
            if any(present):
                check_files(expected)  # 只能精确同内容幂等，不补写/覆盖部分旧文件。
            else:
                created = []
                with tempfile.TemporaryDirectory(prefix='.compose-', dir=d) as temp:
                    try:
                        for name, data in expected.items():
                            staged = pathlib.Path(temp) / name
                            with open(staged, 'xb') as f:
                                f.write(data)
                                f.flush()
                                os.fsync(f.fileno())
                            os.chmod(staged, 0o444)
                            os.link(staged, d / name)  # 不覆盖，逐文件原子发布。
                            created.append(d / name)
                        directory = os.open(str(d), os.O_RDONLY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    except BaseException:
                        for path in created:
                            path.unlink()
                        raise
        reply = {'digest': p['digest']}
    elif mode == 'verify':
        check_files(files())
        reply = {'digest': p['digest']}
    elif mode == 'launch':
        d = check_files(files())
        cid = p['cid']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', cid):
            fail('非法 campaign 名')
        camp = results / cid
        if os.path.lexists(camp):
            fail('campaign 已存在，禁止隐式续跑')
        log_name = 'compose-' + cid + '.log'
        env = dict(os.environ, SCHEDULE_FILE=str(d / (p['tag'] + '.conf')),
                   CARS_FILE=str(d / (p['tag'] + '-cars.conf')), DS_HOURS=str(p['ds_hours']),
                   CAMP_ID=cid, CAMP_PREFIX=p['camp_prefix'], COMPOSE_DIGEST=p['digest'], DRY='0')
        # runner exec 快照前后 PID 不变；锁路径必须与本次解析的 RESULTS_ROOT 相同。
        env.pop('RACEWAY_SNAPSHOT_ROOT', None)
        env.pop('RACEWAY_SOURCE_DIR', None)
        env['RACEWAY_ENV_FILE'] = str(root / 'vm-bench.env')
        # Popen 的真实子进程 PID 不经 shell $! / setsid 二次 fork 猜测。
        with open(root / log_name, 'xb') as log, open(os.devnull, 'rb') as null:
            child = subprocess.Popen([str(root / 'run-bench.sh'), p['what']], cwd=root,
                                     env=env, stdin=null, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child.poll() is not None:
                fail('runner 提前退出 rc=%d，见 %s' % (child.returncode, log_name))
            pidfile, meta = camp / 'RUNNER.pid', camp / 'RUNNER.meta'
            if pidfile.is_file() and meta.is_file():
                if pidfile.is_symlink() or meta.is_symlink() or camp.is_symlink():
                    fail('campaign 身份文件不能是符号链接')
                values = meta.read_text().strip().split('\t')
                if len(values) != 4:
                    fail('RUNNER.meta 格式错误')
                pid, ticks, boot, script = values
                now_ticks, now_boot, argv = proc_identity(child.pid)
                if pid != str(child.pid) or pidfile.read_text().strip() != pid:
                    fail('RUNNER.pid/meta 与启动 PID 不一致')
                if ticks != now_ticks or boot != now_boot or not os.path.isabs(script) or script not in argv:
                    fail('RUNNER.meta 与实际进程身份不一致')
                reply = {'campaign': cid, 'pid': child.pid, 'log': log_name,
                         'results_root': str(results), 'digest': p['digest'],
                         'start_ticks': now_ticks, 'boot_id': now_boot, 'script': script}
                break
            time.sleep(0.2)
        else:
            fail('30 秒内未确认 runner 身份；可能已启动，禁止盲目重试，见 ' + log_name)
    elif mode == 'track-list':
        base = root / 'tracks'
        if not base.is_dir():
            fail('仓库下没有 tracks/ 目录')
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fn in filenames:
                if fn.startswith('.'):
                    continue
                fp = pathlib.Path(dirpath) / fn
                if fp.is_symlink():
                    continue
                st = fp.stat()
                out.append({'path': fp.relative_to(base).as_posix(),
                            'size': st.st_size, 'mtime': int(st.st_mtime)})
        out.sort(key=lambda x: x['path'])
        reply = {'files': out}

    elif mode == 'track-read':
        fp = safe_track_path(root, p['path'])
        if not fp.is_file():
            fail('赛道文件不存在: ' + p['path'])
        reply = {'path': p['path'], 'content': fp.read_text(encoding='utf-8', errors='replace'),
                 'sha256': hashlib.sha256(fp.read_bytes()).hexdigest()}

    elif mode == 'track-write':
        fp = safe_track_path(root, p['path'])
        if fp.exists() and not fp.is_file():
            fail('目标不是普通文件')
        content = p['content']
        if not isinstance(content, str):
            fail('文件内容必须是文本')
        old = fp.read_text(encoding='utf-8', errors='replace') if fp.is_file() else None
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if old == content:
            reply = {'path': p['path'], 'sha256': digest, 'unchanged': True}
        else:
            if p.get('expect') and p['expect'] != (hashlib.sha256(old.encode('utf-8')).hexdigest()
                                                    if old is not None else None):
                fail('文件内容已被他人修改，请刷新后重试（期望摘要不匹配）')
            fp.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.track-', dir=str(fp.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, fp)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            reply = {'path': p['path'], 'sha256': digest, 'unchanged': False}

    elif mode == 'track-delete':
        fp = safe_track_path(root, p['path'])
        if not fp.is_file():
            fail('赛道文件不存在: ' + p['path'])
        old = fp.read_text(encoding='utf-8', errors='replace')
        if p.get('expect') and p['expect'] != hashlib.sha256(old.encode('utf-8')).hexdigest():
            fail('文件内容已变更，请刷新后重试（期望摘要不匹配）')
        fp.unlink()
        reply = {'path': p['path'], 'deleted': True}

    elif mode == 'export-gen':
        tag = p['tag']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', tag):
            fail('非法导出 tag')
        cids = p.get('campaigns') or []
        if not isinstance(cids, list) or not cids:
            fail('请至少选择一个 campaign 生成导出')
        for cid in cids:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', cid):
                fail('非法 campaign 名: %r' % cid)
        import shutil
        export_root = root / 'exports' / tag
        if export_root.exists():
            fail('导出 tag 已存在: %s —— 换一个 tag，已归档导出不覆盖' % tag)
        staging = root / 'exports' / ('.gen-' + tag)
        if staging.exists():
            fail('上一次生成残留: %s —— 请先清理' % staging)
        store = staging / 'race-store'
        camps = staging / 'campaigns'
        try:
            staging.mkdir(parents=True)
            camps.mkdir()
            for cid in cids:
                src = results / cid
                if not src.is_dir():
                    fail('campaign 不存在: ' + cid)
                os.symlink(src, camps / cid)
            env = dict(os.environ, RESULTS_ROOT=str(results))
            r1 = subprocess.run([sys.executable, str(root / 'build-race-store.py'),
                                 '--campaigns', str(camps), '--out', str(store)],
                                cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
            if r1.returncode != 0:
                fail('build-race-store 失败(rc=%d): %s' % (r1.returncode, (r1.stderr or r1.stdout)[-1200:]))
            r2 = subprocess.run([sys.executable, str(root / 'bench-export.py'),
                                 '--out', str(staging / 'out'), '--races', str(store),
                                 '--campaigns', str(camps)],
                                cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
            if r2.returncode != 0:
                fail('bench-export 失败(rc=%d): %s' % (r2.returncode, (r2.stderr or r2.stdout)[-1200:]))
            if not (staging / 'out' / 'manifest.json').is_file():
                fail('导出产物缺 manifest.json，拒绝发布')
            os.replace(staging / 'out', export_root)
            shutil.rmtree(staging, ignore_errors=True)
            reply = {'tag': tag, 'campaigns': cids,
                     'log': (r1.stdout + r2.stdout).strip()[-2000:]}
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    elif mode == 'close':
        cid = p.get('cid') or ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', cid):
            fail('非法 campaign 名')
        dry = p.get('dry_run', True)
        cmd = ['bash', str(root / 'close-campaign.sh')]
        if dry:
            cmd.append('--dry-run')
        cmd += ['--reason', '控制台收尾', cid]
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=120)
        reply = {'result': (r.stdout or '') + (r.stderr or ''), 'rc': r.returncode,
                 'dry_run': bool(dry)}

    else:
        fail('未知远端操作')
    print(json.dumps(reply, ensure_ascii=False))
except Exception as e:
    print(json.dumps({'error': str(e)}, ensure_ascii=False))
    sys.exit(1)
'''


def remote_operation(args, mode, payload=None, timeout=120):
    """只在子 shell 中读取 runner 配置；RESULTS_ROOT 与 runner 同一解析环境。"""
    root = args.root
    if args.ssh and ('\\' in root or not posixpath.isabs(root)):
        raise SystemExit('--root 必须是远端绝对 POSIX 路径，不能含反斜杠')
    shell = ('set -e; cd "$1"; set -a; source ./vm-bench.env >/dev/null; '
             ': "${ROOT:=$PWD}"; : "${RESULTS_ROOT:=$ROOT/campaigns}"; '
             'export ROOT RESULTS_ROOT; exec python3 -B -c "$2" "$3"')
    cmd = 'bash -c %s -- %s %s %s' % (
        shlex.quote(shell), shlex.quote(root), shlex.quote(_REMOTE), shlex.quote(mode))
    rc, out = ssh_run(args.ssh, cmd, input_text=canonical_json(payload or {}), timeout=timeout)
    if rc != 0:
        raise SystemExit('远端 %s 失败（rc=%d）: %s' % (mode, rc, out.strip()))
    try:
        reply = json.loads(out)
    except ValueError:
        raise SystemExit('远端 %s 未返回有效 JSON，拒绝继续: %s' % (mode, out.strip()))
    if not isinstance(reply, dict) or reply.get('error'):
        raise SystemExit('远端 %s 响应无效: %s' % (mode, out.strip()))
    return reply


def upload(args, snapshot):
    """同 tag 只能复用精确同内容；不同内容须换 tag，预览不得覆写正在使用的排程。"""
    reply = remote_operation(args, 'upload', snapshot.payload())
    if reply.get('digest') != snapshot.digest:
        raise SystemExit('上传摘要未确认，拒绝继续')


def remote_mutex_check(args):
    """传输、配置读取、锁检查失败均 fail closed；最终互斥仍由 runner 原子锁保证。"""
    try:
        reply = remote_operation(args, 'mutex')
        if reply.get('unlocked') is not True:
            return '远端互斥状态未确认，拒绝起跑'
    except (SystemExit, OSError, subprocess.TimeoutExpired) as e:
        return '远端互斥预检失败，拒绝起跑: %s' % e
    return None


def remote_dry_run(args, snapshot):
    """验证远端材料摘要后，dry-run 使用与起跑完全相同的排程及窗口。"""
    verified = remote_operation(args, 'verify', snapshot.payload())
    if verified.get('digest') != snapshot.digest:
        raise SystemExit('远端快照摘要未确认')
    cmd = ('cd %s && SCHEDULE_FILE=%s CARS_FILE=%s DS_HOURS=%d COMPOSE_DIGEST=%s '
           './run-bench.sh --dry-run %s' % (
               shlex.quote(args.root), shlex.quote('schedules/%s.conf' % snapshot.tag),
               shlex.quote('schedules/%s-cars.conf' % snapshot.tag), snapshot.ds_hours,
               shlex.quote(snapshot.digest), shlex.quote(snapshot.what)))
    return ssh_run(args.ssh, cmd, timeout=180)


def remote_start(args, snapshot):
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    # 明确且唯一的 campaign 身份，不以 latest/glob 猜测别人的 campaign。
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    cid = '%s-%s-%s' % (snapshot.camp_prefix, stamp, secrets.token_hex(6))
    payload = dict(snapshot.payload(), cid=cid)
    reply = remote_operation(args, 'launch', payload, timeout=60)
    if (reply.get('campaign') != cid or reply.get('digest') != snapshot.digest
            or not isinstance(reply.get('pid'), int) or reply['pid'] <= 1
            or not reply.get('results_root') or not reply.get('log')):
        raise SystemExit('起跑身份未确认；可能已启动，请检查远端日志，禁止盲目重试')
    return cid, reply['log']


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
    if len(set(tracks)) != len(tracks):
        raise ValueError('赛道不能重复')
    rounds = integer(args.rounds, '轮次', 1, 100)
    rows = interleave(cars, tracks, rounds)
    # est：先取赛道清单的实测值，再让 --est 覆盖（--est 优先）
    est_map = track_est_map()
    est_map.update(parse_est_overrides(args.est))
    return rows, est_map


def compose_all(args):
    """cars 解析 + 校验 + 历史覆盖剔除 + 渲染。

    返回 (ComposeSnapshot, plan)。
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

    snapshot = make_snapshot(cars, rows, args.tag, est_map,
                             integer(args.est_default, 'est 默认分钟', 1), args.recall,
                             args.what, args.ds_hours, args.camp_prefix)
    return snapshot, plan


def cmd_cars(args):
    cars = parse_cars(read_cars_file(args))
    for a in cars:
        print('%-8s label=%-10s engine=%-14s jdk_dir=%s' % (a.name, a.label, a.engine, a.jdk_dir or '-'))
    for sev, msg in validate_cars(cars):
        print('[%s] %s' % (sev, msg), file=sys.stderr)


def cmd_gen(args):
    snapshot, plan = compose_all(args)
    rep = report_skipped(plan)
    if rep:
        print(rep, file=sys.stderr)
    sys.stdout.write(snapshot.schedule)


def cmd_preview(args):
    snapshot, plan = compose_all(args)
    upload(args, snapshot)
    rc, out = remote_dry_run(args, snapshot)
    rep = report_skipped(plan)
    if rep:
        print(rep, file=sys.stderr)
    print('== 排程文件（tag=%s，%d 场，sha256=%s）==' % (snapshot.tag, len(snapshot.rows), snapshot.digest))
    sys.stdout.write(snapshot.schedule)
    print('== run-bench.sh --dry-run（rc=%d）==' % rc)
    sys.stdout.write(out)
    if rc != 0:
        raise SystemExit(2)


def cmd_start(args):
    if not args.yes:
        raise SystemExit('起跑需显式确认：加 --yes（先 preview 检查过 dry-run 输出）')
    snapshot, plan = compose_all(args)
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    upload(args, snapshot)
    rc, out = remote_dry_run(args, snapshot)
    if rc != 0:
        print(out)
        raise SystemExit('dry-run 未通过，拒绝起跑（rc=%d）' % rc)
    camp, log = remote_start(args, snapshot)
    rep = report_skipped(plan)
    if rep:
        print(rep)
    print('已起跑 tag=%s（%d 场，sha256=%s）' % (snapshot.tag, len(snapshot.rows), snapshot.digest))
    print('campaign: %s' % camp)
    print('日志: %s@%s:%s' % (args.ssh, args.root, log))
    print('观测: python3 bench-compose.py serve --ssh %s   # 控制台「运行进度」页' % (args.ssh or '本机'))


# ============================================================ serve（本地 Web 界面）

# 前端都在 web/（Preact+HTM 单页 + 旧页 legacy.html 兜底）；URL→(文件, MIME) 白名单静态服务，防路径穿越。
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
_JS_MIME = 'application/javascript; charset=utf-8'
_WEB_STATIC = {
    '/web/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/web/app.js': ('app.js', _JS_MIME),
    '/web/extras.js': ('extras.js', _JS_MIME),
    '/web/vendor/preact.umd.js': ('vendor/preact.umd.js', _JS_MIME),
    '/web/vendor/hooks.umd.js': ('vendor/hooks.umd.js', _JS_MIME),
    '/web/vendor/htm.umd.js': ('vendor/htm.umd.js', _JS_MIME),
    '/legacy': ('legacy.html', 'text/html; charset=utf-8'),  # 旧页面独立入口
}


class ComposeState:
    """serve 模式的共享状态：启动参数 + 已解析车。线程锁保护远端操作串行。"""

    def __init__(self, args):
        self.args = args
        # st.lock 只保护**快操作**（状态快照、CSRF、车表刷新）。入库/体检这类
        # 分钟级操作走 long_lock —— 否则进度页与"停止"会被一起堵住。
        self.lock = threading.Lock()
        self.long_lock = threading.Lock()
        try:
            text = read_cars_file(args)
        except (OSError, SystemExit) as e:
            # serve 降级：车定义读不到（本地缺文件 / 远端不可达）时起空车表，
            # 页面提示原因；进度/历史/趋势/入库板块照常工作。
            print('警告：车定义读取失败（%s）—— 排程板块不可用，其余板块不受影响'
                  % e, file=sys.stderr)
            text = ''
        self.cars = tuple(parse_cars(text))
        self.problems = validate_cars(self.cars)
        if not self.cars:
            self.problems = list(self.problems) + [('error',
                '车定义不可用: %s —— 排程板块不可用（其余板块不受影响）；'
                '用 --cars-file / RACEWAY_CARS_FILE 指向车定义后重启恢复'
                % args.cars_file)]
        # 远端从 vm-bench.env 子 shell 解析 RESULTS_ROOT；本地只读页面可显式提供。
        if args.ssh:
            self.results_root = remote_operation(args, 'inspect')['results_root']
        else:
            self.results_root = (getattr(args, 'results_root', None)
                                 or posixpath.join(args.root, 'campaigns'))
        self.src = Q.Source(root=self.results_root, ssh=args.ssh)
        self.last = MappingProxyType({})  # 仅展示诊断；执行路径绝不读取它。
        self.csrf_token = secrets.token_urlsafe(32)
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


def api_snapshot(st, body):
    if not isinstance(body, dict):
        raise ValueError('请求必须是 JSON 对象')
    selected = body.get('cars')
    if not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected):
        raise ValueError('请至少选择一台车')
    known = {a.name for a in st.cars}
    unknown = set(selected) - known
    if unknown or len(set(selected)) != len(selected):
        raise ValueError('未知或重复车名: %s' % '、'.join(sorted(unknown or set(selected))))
    cars = tuple(a for a in st.cars if a.name in selected)
    hard = [m for s, m in validate_cars(cars) if s == 'error']
    if hard:
        raise ValueError('；'.join(hard))
    tracks = body.get('tracks')
    if (not isinstance(tracks, list) or not tracks
            or not all(isinstance(t, str) for t in tracks)):
        raise ValueError('请至少选择一条赛道')
    unknown = set(tracks) - set(TRACK_CATALOG)
    if unknown or len(set(tracks)) != len(tracks):
        raise ValueError('未知或重复赛道: %s' % '、'.join(sorted(unknown or set(tracks))))
    rounds = integer(body.get('rounds', 1), '轮次', 1, 100)
    rows = interleave(cars, tracks, rounds)
    if 'include_covered' in body and not isinstance(body['include_covered'], bool):
        raise ValueError('include_covered 必须是布尔值')
    plan = H.plan_cells(st.history(), cars, tracks, rounds,
                        include_covered=body.get('include_covered', False))
    rows = H.filter_rows(plan, rows)
    if not rows:
        raise ValueError('矩阵里的每一格都已在指标库跑过（当前 build），无事可排。'
                         '要重跑请勾选"含已跑过的格"。')
    bad = compat_problems(cars, rows)
    if bad:
        raise ValueError('矩阵里有引擎与变体不兼容的组合：\n' + '\n'.join(x[2] for x in bad))
    est_map = track_est_map()
    overrides = body.get('est') or ''
    if not isinstance(overrides, str):
        raise ValueError('est 必须是 赛道=分钟 文本')
    est_map.update(parse_est_overrides([p.strip() for p in overrides.split(',') if p.strip()]))
    recall = body.get('recall', 'recall')
    if recall not in ('recall', 'norecall'):
        raise ValueError('探针必须是 recall 或 norecall')
    snapshot = make_snapshot(cars, rows, body.get('tag'), est_map,
                             integer(body.get('est_default', DEFAULT_EST), 'est 默认分钟', 1),
                             recall != 'norecall', body.get('what') or what_for(tracks),
                             body.get('ds_hours', getattr(st.args, 'ds_hours', 0)),
                             body.get('camp_prefix') or getattr(st.args, 'camp_prefix', None))
    expected_digest = body.get('digest')
    if expected_digest is not None and expected_digest != snapshot.digest:
        raise ValueError('矩阵或历史覆盖已变更，摘要与预览不一致；请重新预览后起跑')
    return snapshot, plan


def snapshot_response(snapshot, plan):
    return {'schedule': snapshot.schedule, 'rows': len(snapshot.rows), 'plan': plan,
            'what': snapshot.what, 'est_total_min': snapshot.est_total_min,
            'tag': snapshot.tag, 'digest': snapshot.digest}


def api_render(st, body):
    try:
        snapshot, plan = api_snapshot(st, body)
    except (ValueError, TypeError) as e:
        return 400, {'error': str(e)}
    # 兼容控制台诊断读取，但这不是后续 preview/start 的执行材料。
    st.last = MappingProxyType({'est_total_min': snapshot.est_total_min,
                                'tag': snapshot.tag, 'digest': snapshot.digest})
    return 200, snapshot_response(snapshot, plan)


def api_preview(st, body):
    try:
        snapshot, plan = api_snapshot(st, body)
    except (ValueError, TypeError) as e:
        return 400, {'error': str(e)}
    with st.lock:
        upload(st.args, snapshot)
        drc, dry = remote_dry_run(st.args, snapshot)
    return 200, dict(snapshot_response(snapshot, plan), dryrun=dry, rc=drc)


def api_start(st, body):
    try:
        snapshot, plan = api_snapshot(st, body)
    except (ValueError, TypeError) as e:
        return 400, {'error': str(e)}
    with st.lock:
        why = remote_mutex_check(st.args)
        if why:
            return 400, {'error': why}
        upload(st.args, snapshot)
        drc, dry = remote_dry_run(st.args, snapshot)
        if drc != 0:
            return 400, {'error': 'dry-run 未通过，拒绝起跑\n' + dry}
        try:
            camp, log = remote_start(st.args, snapshot)
        except SystemExit as e:
            return 400, {'error': str(e)}
    return 200, {'result': '已起跑 tag=%s\nsha256: %s\ncampaign: %s\n日志: %s\n观测: 本页「运行进度」标签'
                 % (snapshot.tag, snapshot.digest, camp, log),
                 'campaign': camp, 'tag': snapshot.tag, 'digest': snapshot.digest}


# ---------------------------------------------------------------- 控制台 API（进度 / 历史 / 停止）

CID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def check_cid(cid):
    """campaign 名白名单：路径片段与 shell 参数都靠它兜底。"""
    if not isinstance(cid, str) or not CID_RE.fullmatch(cid):
        return '非法 campaign 名: %r（只允许 字母/数字/._-）' % (cid,)
    return None


def api_campaigns(st):
    """全部 campaign 的轻量摘要 + 默认该看的（最新没收尾的）。"""
    with st.lock:
        sums = Q.campaign_summaries(st.src)
    shown = sums[:300]
    # active 必须落在返回的这一段里：summaries 有上限，pick_active 用的是全量，
    # 选中被截掉的那一条会让前端静默回退到第一项（显示的是另一个 campaign）。
    active = Q.pick_active(sums)
    if active not in {s['cid'] for s in shown}:
        active = shown[0]['cid'] if shown else None
    return 200, {'campaigns': shown, 'active': active, 'total': len(sums)}


def api_queue(st, cid):
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    d = Q.campaign_json(Q.build_campaign(st.src, cid))
    # 数据可信度证据链上屏：monitors/ENV-SUMMARY.csv（runner 收尾时拍平）→
    # 逐场最坏 ENV verdict + steal 峰值。读不到（旧 campaign 没跑监控）就不加列。
    env = parse_env_summary(st.src.read_abs(
        posixpath.join(st.src.root, cid, 'monitors', 'ENV-SUMMARY.csv')) or '')
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
                     '（脚本以 bash 执行、无需可执行位；检查远端 %s 是否存在 —— '
                     '没有就从仓库拷过去：scp stop-campaign.sh %s:%s/）'
                     % (rc, out.strip(), script, st.args.ssh or '<host>', st.args.root)}
    return 200, {'result': out.strip() or '已发送停止命令'}


def api_doctor(st):
    """新环境体检：远端跑 doctor.sh（纯只读），回答"离能起跑还差什么"。

    doctor.sh 随仓库分发 —— 远端没有时给部署提示，而不是裸报错。
    """
    cmd = 'cd %s && if [ -f doctor.sh ]; then bash doctor.sh; else echo MISSING; fi' \
          % shlex.quote(st.args.root)
    # 体检最长 5 分钟且只读：走 long_lock，不挡进度页与"停止"
    with st.long_lock:
        rc, out = ssh_run(st.args.ssh, cmd, timeout=300)
    if 'MISSING' in out:
        return 400, {'error': '远端 %s 没有 doctor.sh —— 新环境体检脚本随仓库分发，'
                     '先拷过去：scp doctor.sh %s:%s/' % (st.args.root, st.args.ssh, st.args.root)}
    return 200, {'result': out, 'rc': rc}


def comparison_bundle():
    """把本机审定版本及依赖一起送入远端内存，不 import 远端旧 helper。"""
    here = os.path.dirname(os.path.abspath(__file__))
    sources = {}
    for name in ('identity', 'result_selection', 'car-compare'):
        with open(os.path.join(here, name + '.py'), encoding='utf-8') as f:
            sources[name] = f.read()
    return ("import json, sys, types\n"
            "sources = json.loads(%r)\n"
            "for name in ('identity', 'result_selection'):\n"
            "    module = types.ModuleType(name)\n"
            "    module.__file__ = '<compose-bundle>/' + name + '.py'\n"
            "    sys.modules[name] = module\n"
            "    exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)\n"
            "exec(compile(sources['car-compare'], '<compose-bundle>/car-compare.py', 'exec'), "
            "{'__name__':'__main__', '__file__':'<compose-bundle>/car-compare.py'})\n") % canonical_json(sources)


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
    if ca == cb and not (body.get('car_a') or body.get('car_b')):
        return 400, {'error': '同一 campaign 对比请显式选择两台不同车'}
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
        if ca == cb and car_a == car_b:
            return 400, {'error': '同一个 campaign 的同一台车不能与自身对比'}
        try:
            script = comparison_bundle()
        except OSError as e:
            return 400, {'error': '读不到本机对比程序或其依赖: %s' % e}
        cmd = ('cd %s && python3 -B - --a %s --b %s --a-campaign %s --b-campaign %s%s'
               % (shlex.quote(st.args.root), shlex.quote(car_a), shlex.quote(car_b),
                  shlex.quote(posixpath.join(st.src.root, ca)),
                  shlex.quote(posixpath.join(st.src.root, cb)),
                  ' --json' if body.get('json') else ''))
        rc, out = ssh_run(st.args.ssh, cmd, input_text=script, timeout=300)
    if rc != 0 and not out.strip():
        return 400, {'error': 'car-compare 返回码 %d 且无输出（远端 python3 可用？）' % rc}
    data = None
    if body.get('json'):
        try:
            data = json.loads(out.strip())
            if not isinstance(data, dict) or 'tracks' not in data:
                data = None          # stderr 混入导致解析失败 → 降级为文本展示
        except ValueError:
            data = None
    return 200, {'result': out, 'rc': rc, 'car': '%s vs %s' % (car_a, car_b), 'data': data}


def api_tracks(st):
    """赛道目录：远端 run-bench.sh list（定义在不在位、语料、近期 campaign）。"""
    cmd = ('cd %s && if [ -f run-bench.sh ]; then ./run-bench.sh list; else echo MISSING; fi'
           % shlex.quote(st.args.root))
    with st.lock:
        rc, out = ssh_run(st.args.ssh, cmd, timeout=120)
    if 'MISSING' in out:
        return 400, {'error': '远端 %s 没有 run-bench.sh —— 仓库同步时记得带全部 *.sh' % st.args.root}
    return 200, {'result': out, 'rc': rc}


def api_exports(st):
    """exports/ 清单：tag、导出断言状态、三索引行数（远端只读，供入库页选择）。"""
    script = (
        "import json, os\n"
        "root = %s\n"
        "exp = os.path.join(root, 'exports')\n"
        "out = []\n"
        "for tag in (sorted(os.listdir(exp)) if os.path.isdir(exp) else []):\n"
        "    d = os.path.join(exp, tag)\n"
        "    m = os.path.join(d, 'manifest.json')\n"
        "    if not os.path.isdir(d) or not os.path.isfile(m):\n"
        "        continue\n"
        "    try:\n"
        "        doc = json.load(open(m, encoding='utf-8'))\n"
        "    except (ValueError, OSError):\n"
        "        continue\n"
        "    out.append({'tag': tag,\n"
        "                'assertions': (doc.get('assertions') or {}).get('status'),\n"
        "                'counts': {k: doc.get(k) for k in ('bench_env', 'bench_run', 'bench_metric')},\n"
        "                'mtime': int(os.path.getmtime(m))})\n"
        "print(json.dumps(out))\n" % json.dumps(st.args.root))
    with st.lock:
        rc, out = ssh_run(st.args.ssh, 'cd %s && python3 -B -' % shlex.quote(st.args.root),
                          input_text=script, timeout=60)
    if rc != 0:
        return 400, {'error': '读 exports/ 失败（rc=%d）：\n%s' % (rc, out.strip()[:400])}
    try:
        exports = json.loads(out.strip().splitlines()[-1])
    except (ValueError, AttributeError, IndexError):
        return 500, {'error': 'exports 清单输出不可解析：\n%s' % out.strip()[:400]}
    if not isinstance(exports, list):
        return 500, {'error': 'exports 清单格式异常'}
    return 200, {'exports': exports}


def _trend_series_keys(st, car, track, auth, es_url, limit=500):
    """该车在该赛道的身份线（series_key）集合。

    `benchmark-result-metric` 没有 car_code 字段，趋势页要按车筛选，只能先用
    race 索引（有 car_code + series_key）定位身份线，再到指标索引取数。
    超过 limit 说明同一车号挂了太多 build —— 报错让使用者收窄口径，不静默截断。
    """
    body = {'size': 2000, '_source': ['series_key'],
            'query': {'bool': {'filter': [{'term': {'car_code': car}},
                                          {'term': {'track': track}}]}}}
    resp = H.es_query(st.args.ssh, body, auth=auth, es_url=es_url,
                      path='benchmark-result-race/_search', timeout=60)
    keys = sorted({(h.get('_source') or {}).get('series_key')
                   for h in (resp.get('hits') or {}).get('hits') or []} - {None, ''})
    if len(keys) > limit:
        raise H.HistoryUnavailable('该车在该赛道有 %d 条身份线，超过上限 %d —— '
                                   '请先按 build/JDK 收窄口径' % (len(keys), limit))
    return keys


def api_trend(st, q):
    """跨 campaign 指标趋势：只读指标库（bench-history 同一条 ssh/curl 取数路径）。

    指标库没跑/没配口令时返回 available=False —— 界面显示原因，绝不把空数据
    当"没有趋势"。分组键 = series_key | 变体 | 操作 | stat，同车多 build 天然分线。

    车代号怎么落到指标行上：`benchmark-result-metric` 里**没有 car_code 字段**
    （只有 series_key / comparison_key / track），所以先在 race 索引定位这辆车的
    series_key 集合，再用 terms 查指标。早先直接在指标索引上 term car_code，
    对未映射字段查询不报错、只是零命中 —— 整页永远画不出线。
    """
    car = (q.get('car') or [''])[0].strip()
    track = (q.get('track') or [''])[0].strip()
    family = (q.get('family') or [''])[0].strip()
    stat = (q.get('stat') or [''])[0].strip()
    task = (q.get('task') or [''])[0].strip()
    if not car or not track:
        return 400, {'error': 'car 与 track 必填'}
    for label, val in (('car', car), ('track', track), ('family', family),
                       ('stat', stat), ('task', task)):
        if val and not re.fullmatch(r'[A-Za-z0-9._@+-]{1,120}', val):
            return 400, {'error': '%s 参数非法: %r' % (label, val)}
    auth = getattr(st.args, 'es_auth', None) or os.environ.get('RACEWAY_ES_AUTH')
    if not auth:
        return 200, {'available': False,
                     'error': '未配置指标库口令（--es-auth 或环境变量 RACEWAY_ES_AUTH）'}
    es_url = getattr(st.args, 'es_url', None) or H.DEFAULT_ES_URL
    try:
        keys = _trend_series_keys(st, car, track, auth, es_url)
    except H.HistoryUnavailable as e:
        return 200, {'available': False, 'error': str(e)}
    if not keys:
        return 200, {'available': True, 'n': 0, 'unit': None, 'direction': None,
                     'series': [],
                     'note': '这辆车在这条赛道上还没有入库场次（先跑一场并入库）'}
    filt = [{'terms': {'series_key': keys}}, {'term': {'track': track}},
            {'term': {'trusted': True}}]
    if family:
        filt.append({'term': {'family': family}})
    if stat:
        filt.append({'term': {'stat': stat}})
    if task:
        filt.append({'term': {'task': task}})
    body = {'size': 2000, 'query': {'bool': {'filter': filt}},
            '_source': ['run_start', 'campaign', 'series_key', 'task', 'family', 'stat',
                        'value', 'unit', 'direction', 'track_variant'],
            'sort': [{'run_start': 'asc'}]}
    try:
        resp = H.es_query(st.args.ssh, body, auth=auth, es_url=es_url,
                          path='benchmark-result-metric/_search', timeout=60)
    except H.HistoryUnavailable as e:
        return 200, {'available': False, 'error': str(e)}
    groups, unit, direction, total = {}, None, None, 0
    for hit in resp.get('hits', {}).get('hits', []):
        s = hit.get('_source') or {}
        val = s.get('value')
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        key = '%s | %s | %s | %s' % (s.get('series_key') or car,
                                     s.get('track_variant') or s.get('track') or '',
                                     s.get('task') or '?', s.get('stat') or '?')
        groups.setdefault(key, []).append({'t': s.get('run_start'), 'v': val,
                                           'campaign': s.get('campaign')})
        total += 1
        unit = unit or s.get('unit')
        direction = direction or s.get('direction')
    return 200, {'available': True, 'n': total, 'unit': unit, 'direction': direction,
                 'series': [{'key': k, 'points': v} for k, v in sorted(groups.items())]}


# 远端固定 bootstrap：从 stdin 读 JSON（源码 + 参数 + 凭据）再执行。
# 这样口令不会出现在远端 ps 的 argv 里，参数也不需要经过一层 shell 引号拼接。
# 只有这段固定串会出现在 ssh 命令行上，它不含任何机密。
APPLY_BOOTSTRAP = (
    'import json, sys\n'
    'p = json.load(sys.stdin)\n'
    'sys.argv = ["bench-apply.py"] + p["argv"]\n'
    'exec(compile(p["script"], "<bench-apply.py>", "exec"),\n'
    '     {"__name__": "__main__", "__file__": "<raceway-stdin>",'
    ' "RACEWAY_AUTH": p.get("auth")})\n'
)


def api_apply(st, body):
    """入库执行：远端跑仓库版 bench-apply.py（stdin 送本机审定版源码，不吃远端旧拷贝）。

    bench-apply 自己先全量预检（不过不发任何请求）：退出码 0=成功、1=bulk 有拒收、
    2=预检/参数失败。控制台只递参数，不发明新的入库逻辑；--drop 的二次确认在页面侧。
    """
    export = (body.get('export') or '').strip()
    es = (body.get('es') or '').strip()
    auth = (body.get('auth') or '').strip()
    insecure = bool(body.get('insecure'))
    drop = bool(body.get('drop'))
    try:
        safe_name(export, 'export tag')
    except ValueError as e:
        return 400, {'error': str(e)}
    if not re.fullmatch(r'https?://[^\s/]+(?:/[^\s]*)?', es or ''):
        return 400, {'error': '--es 必须是 HTTP(S) URL'}
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, 'bench-apply.py'), encoding='utf-8') as f:
            script = f.read()
    except OSError as e:
        return 400, {'error': '读不到本机 bench-apply.py: %s' % e}
    argv = ['--dir', posixpath.join(st.args.root, 'exports', export), '--es', es]
    if insecure:
        argv.append('--insecure')
    if drop:
        argv.append('--drop')
    # 脚本与凭据都走 stdin（JSON 信封）：远端 ps 里只剩一段固定 bootstrap，
    # 看不到口令，也不再依赖 shlex 拼参数。bench-apply.py 从 RACEWAY_AUTH 取凭据。
    payload = json.dumps({'script': script, 'argv': argv, 'auth': auth or None})
    cmd = 'cd %s && python3 -B -c %s' % (shlex.quote(st.args.root),
                                        shlex.quote(APPLY_BOOTSTRAP))
    # 长操作不占 st.lock：入库要跑几分钟，占着它就等于把进度页和"停止"一起锁死，
    # 而那正是最需要止损的时候。st.long_lock 只保证不并发入库。
    with st.long_lock:
        rc, out = ssh_run(st.args.ssh, cmd, input_text=payload, timeout=900)
    return 200, {'result': out, 'rc': rc}


# ============================================================ 扩展功能 API
# 所有新 API 只走 _route_get_core / _post_core —— Go 后端的 serve-rpc 逐请求
# 转发共用同一核心，因此这里新增的路由 Go 侧自动可用，无需改 main.go。

# 可浏览的日志/产物文件：白名单扩展名 + 相对 campaign 根的路径片段，防越界。
_LOG_EXTS = ('.txt', '.md', '.json', '.csv', '.log', '.tsv', '.raw')
_SAFE_REL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')


def safe_camp_rel(rel):
    """campaign 内相对路径：无 ..、无绝对、无反斜杠、扩展名白名单。"""
    if not isinstance(rel, str) or not rel or len(rel) > 200:
        raise ValueError('非法文件路径')
    if '\\' in rel or rel.startswith('/') or '\0' in rel or '..' in rel:
        raise ValueError('非法文件路径')
    if not _SAFE_REL.fullmatch(rel):
        raise ValueError('文件路径含非法字符')
    if not rel.endswith(_LOG_EXTS):
        raise ValueError('只允许查看文本/日志/JSON/CSV 文件')
    return rel


def _ok(reply):
    """把写类 api_* 返回的 dict 包成路由层期待的 (code, payload)。

    写类函数直接返回 remote_operation 的 dict（CLI bench-tools.py 直接消费），
    而 _route_get_core / _post_core 统一按 (code, payload) 取用，故在此适配。
    """
    return 200, reply


def _ok_call(fn, *a, **kw):
    """GET 路由专用：调用写类 api_*（可能抛 SystemExit）并包成 (code, payload)。

    remote_operation 失败时抛 SystemExit，而 do_GET 的 except Exception 不捕获它
    （SystemExit 继承 BaseException），会导致连接直接断开、页面拿到空响应 —— 这里
    先转成 ValueError，让上层按 500 JSON 正常回显原因。
    """
    try:
        return 200, fn(*a, **kw)
    except SystemExit as e:
        raise ValueError(str(e))


def api_track_list(st):
    with st.lock:
        return remote_operation(st.args, 'track-list')


def api_track_read(st, body):
    path = safe_track_rel(body.get('path'))
    with st.lock:
        return remote_operation(st.args, 'track-read', {'path': path})


def safe_track_rel(value):
    """与远端 safe_track_path 同口径的本地校验（路径串先挡一道）。"""
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError('非法赛道路径')
    if '\\' in value or value.startswith('/') or '\0' in value:
        raise ValueError('非法赛道路径')
    parts = value.split('/')
    if any(x in ('', '.', '..') for x in parts):
        raise ValueError('赛道路径不能含空段、. 或 ..')
    if any(not re.fullmatch(r'[A-Za-z0-9._-]+', x) for x in parts):
        raise ValueError('赛道路径只允许字母数字与 ._-')
    return value


def api_track_write(st, body):
    path = safe_track_rel(body.get('path'))
    content = body.get('content')
    if not isinstance(content, str):
        raise ValueError('文件内容必须是文本')
    if len(content.encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('赛道文件不能超过 8MB')
    expect = body.get('expect')
    if expect is not None and (not isinstance(expect, str)
                               or not re.fullmatch(r'[0-9a-f]{64}', expect)):
        raise ValueError('expect 必须是 64 位 sha256')
    with st.lock:
        return remote_operation(st.args, 'track-write',
                                {'path': path, 'content': content, 'expect': expect})


def api_track_delete(st, body):
    path = safe_track_rel(body.get('path'))
    expect = body.get('expect')
    if expect is not None and (not isinstance(expect, str)
                               or not re.fullmatch(r'[0-9a-f]{64}', expect)):
        raise ValueError('expect 必须是 64 位 sha256')
    with st.lock:
        return remote_operation(st.args, 'track-delete',
                                {'path': path, 'expect': expect})


def api_export_gen(st, body):
    tag = (body.get('tag') or '').strip()
    cids = body.get('campaigns')
    if not isinstance(cids, list) or not cids or not all(isinstance(x, str) for x in cids):
        raise ValueError('请选择一个或多个 campaign')
    with st.long_lock:
        return remote_operation(st.args, 'export-gen', {'tag': tag, 'campaigns': cids},
                                timeout=1300)


def api_close(st, body):
    cid = (body.get('cid') or '').strip()
    why = check_cid(cid)
    if why:
        raise ValueError(why)
    dry = body.get('dry_run', True)
    if not isinstance(dry, bool):
        raise ValueError('dry_run 必须是布尔值')
    with st.lock:
        return remote_operation(st.args, 'close', {'cid': cid, 'dry_run': dry})


def api_sidecar(st, cid, fid):
    """实测身份卡：读 results/<fid>-*-sidecar.json（含引擎版本/JDK/settings/堆/核数）。"""
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', fid or ''):
        return 400, {'error': '非法场次 id'}
    base = posixpath.join(st.src.root, cid, 'results')
    listing = st.src.read_abs(posixpath.join(base, '..', 'PLAN.tsv'))  # 探活，仅用目录约定
    sidecars = []
    # 无目录列举接口，按 runner 命名约定逐个探测（每场最多两个尝试相位）
    for ph in ('wq', 'w', 'q', 'm'):
        name = '%s-%s-sidecar.json' % (fid, ph)
        text = st.src.read_abs(posixpath.join(base, name))
        if text:
            try:
                doc = json.loads(text)
            except ValueError:
                doc = {'_parse_error': text[:200]}
            sidecars.append({'phase': ph, 'file': name, 'data': doc})
    attempts = st.src.read_abs(posixpath.join(st.src.root, cid, 'attempts'))
    return 200, {'cid': cid, 'fid': fid, 'sidecars': sidecars,
                 'has_plan': listing is not None, 'attempts_dir_probe': attempts is not None}


def api_race_detail(st, cid, fid):
    """场次详情抽屉：PLAN 行 + RACES 判定 + sidecar 身份 + 逐任务指标 + 产物清单。"""
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', fid or ''):
        return 400, {'error': '非法场次 id'}
    plan = st.src.read_abs(posixpath.join(st.src.root, cid, 'PLAN.tsv')) or ''
    plan_row = None
    for line in plan.splitlines():
        cols = line.split('\t')
        if cols and cols[0] == fid:
            plan_row = cols
            break
    races = st.src.read_abs(posixpath.join(st.src.root, cid, 'RACES.md')) or ''
    race_rows = [ln for ln in races.splitlines() if ln.startswith('| ' + fid + ' ')]
    progress = st.src.read(cid, 'PROGRESS.txt') or ''
    prog_lines = [ln for ln in progress.splitlines() if fid in ln][-40:]

    metrics = []
    for ph in ('wq', 'w', 'q'):
        text = st.src.read_abs(posixpath.join(st.src.root, cid, 'results',
                                             '%s-%s-race.json' % (fid, ph)))
        if not text:
            continue
        try:
            race = json.loads(text)
        except ValueError:
            continue
        for op in (race.get('op_metrics') or []):
            metrics.append({
                'phase': ph, 'task': op.get('operation') or op.get('name') or '?',
                'throughput': (op.get('throughput') or {}).get('mean') if isinstance(op.get('throughput'), dict) else op.get('throughput'),
                'latency': (op.get('latency') or {}).get('mean') if isinstance(op.get('latency'), dict) else op.get('latency'),
                'unit': (op.get('throughput') or {}).get('unit') if isinstance(op.get('throughput'), dict) else None,
            })
    artifacts = []
    for name in ('%s-wq.md' % fid, '%s-wq-race.json' % fid,
                 '%s-wq-sidecar.json' % fid, '%s-expected-tasks.json' % fid):
        if st.src.read_abs(posixpath.join(st.src.root, cid, 'results', name)) is not None:
            artifacts.append(name)
    return 200, {'cid': cid, 'fid': fid, 'plan': plan_row, 'races': race_rows,
                 'progress': prog_lines, 'metrics': metrics, 'artifacts': artifacts}


def api_monitor(st, cid, fid):
    """读某场次的每节点 5 秒时序 CSV（monitors/<fid>-<node>.csv）。"""
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', fid or ''):
        return 400, {'error': '非法场次 id'}
    env_text = st.src.read_abs(posixpath.join(st.src.root, cid, 'monitors', 'ENV-SUMMARY.csv')) or ''
    nodes = []
    for line in env_text.splitlines()[1:]:
        cols = line.split(',')
        if len(cols) >= 3 and cols[0].strip() == fid and cols[2].strip():
            if cols[2] not in nodes:
                nodes.append(cols[2].strip())
    if not nodes:
        # 退化：按 campaign 的 vm 名约定给几个常见节点名探测
        nodes = ['node1', 'node2', 'esrally']
    series = []
    for node in nodes:
        name = '%s-%s.csv' % (fid, node)
        text = st.src.read_abs(posixpath.join(st.src.root, cid, 'monitors', name))
        if not text:
            continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            continue
        header = lines[0].split(',')
        rows = []
        for ln in lines[1:5001]:
            cells = ln.split(',')
            if len(cells) != len(header):
                continue
            row = {}
            for k, v in zip(header, cells):
                try:
                    row[k] = float(v) if v not in ('', '-') else None
                except ValueError:
                    row[k] = v
            rows.append(row)
        series.append({'node': node, 'file': name, 'header': header, 'rows': rows})
    return 200, {'cid': cid, 'fid': fid, 'series': series, 'interval': 5}


def api_logfile(st, cid, rel, n, q):
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    try:
        rel = safe_camp_rel(rel)
    except ValueError as e:
        return 400, {'error': str(e)}
    n = max(1, min(int(n or 200), 5000))
    text = st.src.read(cid, rel)
    if text is None:
        return 404, {'error': '文件不存在或不可读: %s' % rel}
    lines = text.splitlines()
    if q:
        lines = [ln for ln in lines if q.lower() in ln.lower()]
    total = len(lines)
    if total > n:
        lines = lines[-n:]
    return 200, {'cid': cid, 'file': rel, 'total': total, 'lines': lines,
                 'truncated': total > n}


def api_est_suggest(st, track):
    """按历史同赛道实际耗时中位数给出 est 建议（分钟）。

    来源：各 campaign 的 RACES.md 用时列（Xmin / Xs）+ PLAN.tsv 的 est 列作回退；
    只读 campaign 文件，不碰指标库与集群。
    """
    if not isinstance(track, str) or not track:
        raise ValueError('请指定赛道')
    buckets = {}
    for s in Q.campaign_summaries(st.src):
        cid = s.get('cid')
        if not cid:
            continue
        plan = st.src.read_abs(posixpath.join(st.src.root, cid, 'PLAN.tsv')) or ''
        fids = [ln.split('\t')[0] for ln in plan.splitlines()[1:]
                if len(ln.split('\t')) > 2 and ln.split('\t')[2] == track]
        if not fids:
            continue
        races = st.src.read_abs(posixpath.join(st.src.root, cid, 'RACES.md')) or ''
        for ln in races.splitlines():
            if not ln.startswith('| '):
                continue
            cols = [c.strip() for c in ln.strip('|').split('|')]
            if len(cols) < 4 or cols[0] not in fids:
                continue
            if 'VALID' not in cols[1] and 'DEGRADED' not in cols[1]:
                continue
            m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(min|s)', cols[3])
            if not m:
                continue
            val = float(m.group(1)) / 60 if m.group(2) == 's' else float(m.group(1))
            if 0 < val < 24 * 60:
                buckets.setdefault(track, []).append(val)
    vals = sorted(buckets.get(track, []))
    if not vals:
        return 200, {'track': track, 'available': False, 'n': 0, 'suggest': None}
    mid = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    return 200, {'track': track, 'available': True, 'n': len(vals),
                 'suggest': max(1, int(round(mid))), 'min': vals[0], 'max': vals[-1]}


def api_report(st, cid, fmt):
    """报告导出：按 campaign 生成 Markdown / HTML（纯离线读文件，不解析缓存）。"""
    why = check_cid(cid)
    if why:
        return 400, {'error': why}
    fmt = fmt or 'md'
    if fmt not in ('md', 'html'):
        raise ValueError('格式只能是 md 或 html')
    d = Q.campaign_json(Q.build_campaign(st.src, cid))
    # 与 api_queue 同口径：ENV-SUMMARY.csv 的逐场最坏判定 + steal 峰值附到 fields。
    env = parse_env_summary(st.src.read_abs(
        posixpath.join(st.src.root, cid, 'monitors', 'ENV-SUMMARY.csv')) or '')
    for f in d.get('fields', []):
        e = env.get(f.get('fid'))
        if e:
            f['env_verdict'] = e['verdict']
            f['steal_max'] = e['steal_max']
    lines = ['# raceway 跑分报告: %s' % cid, '']
    if d.get('meta'):
        lines += ['## 概况', '']
        for k in ('deadline', 'planned', 'valid', 'failed', 'pending'):
            if k in d['meta']:
                lines.append('- %s: %s' % (k, d['meta'][k]))
        lines.append('')
    lines += ['## 逐场判定', '', '| 场次 | 状态 | 卡点 | ENV | 用时 |',
              '|---|---|---|---|---|']
    for f in d.get('fields', []):
        lines.append('| %s | %s | %s | %s | %s |' % (
            f.get('fid', ''), f.get('status', ''), f.get('precheck', ''),
            f.get('env_verdict', '-'), f.get('elapsed', '-')))
    lines += ['', '## 环境采样（最坏 ENV / steal 峰值）', '',
              '| 场次 | ENV | steal_max |', '|---|---|---|']
    for f in d.get('fields', []):
        lines.append('| %s | %s | %s |' % (f.get('fid', ''),
                     f.get('env_verdict', '-'), f.get('steal_max', '-')))
    lines += ['', '---', '', '由 raceway 控制台生成；口径见 docs/METHODOLOGY.md。']
    md = '\n'.join(lines)
    if fmt == 'html':
        import html as _html
        body = _html.escape(md)
        out = ('<!doctype html><html><head><meta charset="utf-8"><title>raceway %s</title>'
               '<style>body{font:14px/1.6 -apple-system,Segoe UI,sans-serif;max-width:960px;'
               'margin:40px auto;color:#222}pre{white-space:pre-wrap}</style></head>'
               '<body><pre>%s</pre></body></html>') % (_html.escape(cid), body)
        return 200, {'cid': cid, 'format': fmt, 'text': out}
    return 200, {'cid': cid, 'format': fmt, 'text': md}


def _err_body(e):
    """do_GET/serve-rpc 的兜底错误体，保持与旧内联实现一致的 'Type: msg' 形状。"""
    return (500, 'application/json',
            json.dumps({'error': '%s: %s' % (type(e).__name__, e)}, ensure_ascii=False),
            'no-store')


def _route_get_core(st, path, q):
    """GET 路由核心：不碰 socket，只返回 (code, ctype, body, cache)。

    与 do_POST 的 _post_core 一起构成 serve 的纯路由层，供 HTTP Handler 和
    serve-rpc（Go 后端逐请求转发）共用，保证两条路径的接口契约完全一致。
    静态资源与 '/' 由 Go 侧原生服务；这里保留仅为让 Python serve 继续自洽。
    """
    if path == '/':
        # 新版 index.html 优先；缺失时回退旧页面（独立文件，不再内联在 Python 里）
        web_page = os.path.join(_WEB_DIR, 'index.html')
        if not os.path.isfile(web_page):
            web_page = os.path.join(_WEB_DIR, 'legacy.html')
        if os.path.isfile(web_page):
            with open(web_page, encoding='utf-8') as f:
                return 200, 'text/html; charset=utf-8', f.read(), 'no-store'
        return 500, 'text/plain', ('前端文件缺失: web/index.html 与 web/legacy.html 都不存在 —— '
                                   '同步仓库时请把 web/ 目录一并带上'), 'no-store'
    if path in _WEB_STATIC:
        rel, mime = _WEB_STATIC[path]
        local = os.path.join(_WEB_DIR, rel)
        if os.path.isfile(local):
            with open(local, encoding='utf-8') as f:
                cache = 'no-store' if path == '/legacy' else 'public, max-age=300'
                return 200, mime, f.read(), cache
        return 404, 'text/plain', '前端文件缺失: ' + path, 'no-store'
    if path == '/api/bootstrap':
        return 200, 'application/json', json.dumps({
            'cars': [{k: v for k, v in a._asdict().items() if k != 'config'} for a in st.cars],
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
            'csrf_token': st.csrf_token,
            'results_root': st.src.root,
        }, ensure_ascii=False), 'no-store'
    if path == '/api/history':
        return 200, 'application/json', json.dumps(st.history().to_dict(), ensure_ascii=False), 'no-store'
    if path == '/echarts.min.js':
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'echarts.min.js')
        if os.path.isfile(local):
            with open(local, encoding='utf-8') as f:
                return 200, 'application/javascript; charset=utf-8', f.read(), 'public, max-age=86400'
        return 404, 'text/plain', ('echarts.min.js 未随仓库分发（与 bench-compose.py 同目录放置后刷新）'
                                   '—— 趋势图不可用，表格不受影响'), 'no-store'
    if path == '/api/campaigns':
        code, payload = api_campaigns(st)
    elif path == '/api/queue':
        code, payload = api_queue(st, (q.get('cid') or [''])[0])
    elif path == '/api/logtail':
        code, payload = api_logtail(st, (q.get('cid') or [''])[0], (q.get('n') or ['50'])[0])
    elif path == '/api/exports':
        code, payload = api_exports(st)
    elif path == '/api/trend':
        code, payload = api_trend(st, q)
    elif path == '/api/track-files':
        code, payload = _ok_call(api_track_list, st)
    elif path == '/api/track-file':
        try:
            code, payload = _ok_call(api_track_read, st, {'path': (q.get('path') or [''])[0]})
        except ValueError as e:
            return 400, 'application/json', json.dumps({'error': str(e)}, ensure_ascii=False), 'no-store'
    elif path == '/api/sidecar':
        code, payload = api_sidecar(st, (q.get('cid') or [''])[0], (q.get('fid') or [''])[0])
    elif path == '/api/race-detail':
        code, payload = api_race_detail(st, (q.get('cid') or [''])[0], (q.get('fid') or [''])[0])
    elif path == '/api/monitor':
        code, payload = api_monitor(st, (q.get('cid') or [''])[0], (q.get('fid') or [''])[0])
    elif path == '/api/logfile':
        code, payload = api_logfile(st, (q.get('cid') or [''])[0], (q.get('file') or [''])[0],
                                    (q.get('n') or ['200'])[0], (q.get('q') or [''])[0])
    elif path == '/api/est-suggest':
        try:
            code, payload = api_est_suggest(st, (q.get('track') or [''])[0])
        except ValueError as e:
            return 400, 'application/json', json.dumps({'error': str(e)}, ensure_ascii=False), 'no-store'
    elif path == '/api/report':
        try:
            code, payload = api_report(st, (q.get('cid') or [''])[0], (q.get('fmt') or ['md'])[0])
        except ValueError as e:
            return 400, 'application/json', json.dumps({'error': str(e)}, ensure_ascii=False), 'no-store'
    else:
        return 404, 'text/plain', 'not found', 'no-store'
    return code, 'application/json', json.dumps(payload, ensure_ascii=False), 'no-store'


def _post_core(st, path, body):
    """POST 路由核心：body 已由调用方解析为 dict，这里只做分发与错误映射。

    沿用旧 do_POST 的错误语义：SystemExit→400、其它异常→500（transport 层的
    415/坏 JSON 校验仍留在 do_POST，serve-rpc 侧由 Go 保证 Content-Type）。
    """
    try:
        if path == '/api/render':
            code, payload = api_render(st, body)
        elif path == '/api/preview':
            code, payload = api_preview(st, body)
        elif path == '/api/start':
            code, payload = api_start(st, body)
        elif path == '/api/stop':
            code, payload = api_stop(st, body)
        elif path == '/api/compare':
            code, payload = api_compare(st, body)
        elif path == '/api/doctor':
            code, payload = api_doctor(st)
        elif path == '/api/tracks':
            code, payload = api_tracks(st)
        elif path == '/api/apply':
            code, payload = api_apply(st, body)
        elif path == '/api/track-write':
            code, payload = _ok(api_track_write(st, body))
        elif path == '/api/track-delete':
            code, payload = _ok(api_track_delete(st, body))
        elif path == '/api/export-gen':
            code, payload = _ok(api_export_gen(st, body))
        elif path == '/api/close':
            code, payload = _ok(api_close(st, body))
        else:
            code, payload = 404, {'error': 'not found'}
    except SystemExit as e:
        code, payload = 400, {'error': str(e)}
    except Exception as e:  # 远端命令失败等，页面要能看到原因
        code, payload = 500, {'error': '%s: %s' % (type(e).__name__, e)}
    return code, 'application/json', json.dumps(payload, ensure_ascii=False), 'no-store'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, cache='no-store'):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data)

    # 允许的本机回环名：ssh -L 转发后习惯用 localhost 打开，只认 127.0.0.1
    # 会让人看到一个不可行动的 "forbidden host"。仍然是同源守卫（防 DNS rebinding），
    # 只是把三种回环写法都放进来。
    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {'127.0.0.1:%d' % port, 'localhost:%d' % port, '[::1]:%d' % port}

    def _local_request(self, write=False):
        """同源守卫。通过返回 None，否则返回**可行动**的中文原因。"""
        allowed = self._allowed_hosts()
        host = self.headers.get('Host') or ''
        if host not in allowed:
            return ('不认识的 Host: %r。控制台只接受本机回环地址（%s）。'
                    ' 用 ssh -L 转发时请直接打开 http://127.0.0.1:%d。'
                    % (host, ' / '.join(sorted(allowed)), self.server.server_address[1]))
        if write:
            origin = self.headers.get('Origin')
            if origin is not None and origin not in {'http://' + h for h in allowed}:
                return 'Origin %r 与本机不同源，写操作被拒绝' % origin
            site = self.headers.get('Sec-Fetch-Site')
            if site not in (None, 'same-origin', 'none'):
                return 'Sec-Fetch-Site=%r：跨站写操作被拒绝' % site
            token = self.headers.get('X-Raceway-CSRF') or ''
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token)
                    or not secrets.compare_digest(token, self.server.state.csrf_token)):
                return 'CSRF 令牌缺失或不匹配（控制台重启后令牌会变）—— 刷新页面重试'
        return None

    def _guard(self, write=False):
        """守卫不通过时直接回 403 并返回 True。"""
        why = self._local_request(write=write)
        if not why:
            return False
        self._send(403, 'application/json',
                   json.dumps({'error': why}, ensure_ascii=False))
        return True

    def do_GET(self):
        if self._guard():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = self.server.state
        try:
            code, ctype, body, cache = _route_get_core(st, u.path, q)
        except Exception as e:  # 参数/远端失败等，页面要能看到原因而不是断连
            code, ctype, body, cache = _err_body(e)
        self._send(code, ctype, body, cache)

    def do_POST(self):
        st = self.server.state
        if self._guard(write=True):
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
            return self._send(415, 'application/json', '{"error":"application/json required"}')
        try:
            n = integer(self.headers.get('Content-Length') or 0, 'Content-Length', 0, 1048576)
            body = json.loads(self.rfile.read(n) or b'{}')
        except (ValueError, TypeError):
            return self._send(400, 'application/json', '{"error":"bad json or length"}')
        if not isinstance(body, dict):
            return self._send(400, 'application/json', '{"error":"JSON object required"}')
        code, ctype, out, cache = _post_core(st, self.path, body)
        self._send(code, ctype, out, cache)


def cmd_serve(args):
    # 前端都在 web/：新版优先，旧页兜底；两个都没有就起不来，别让人对着 500 页猜。
    if not any(os.path.isfile(os.path.join(_WEB_DIR, f))
               for f in ('index.html', 'legacy.html')):
        raise SystemExit('找不到前端 web/index.html 或 web/legacy.html —— '
                         '同步仓库时请带上 web/ 目录（与 bench-compose.py 同级）。')
    if not os.path.isfile(os.path.join(_WEB_DIR, 'index.html')):
        print('警告：web/index.html 缺失，使用旧页面 web/legacy.html（/ 与 /legacy 均可访问）',
              file=sys.stderr)
    # --root 必须是 raceway 仓库根（要有 run-bench.sh / schedules/）。传成
    # campaigns 目录时车定义与 dry-run 全落到不存在的路径，报错却指向别处，
    # 用户看不出是 root 传错了 —— 起服务前先探一发。
    probe = 'test -f %s' % shlex.quote(posixpath.join(args.root, 'run-bench.sh'))
    if args.ssh:
        rc, _ = ssh_run(args.ssh, probe, timeout=30)
    else:
        rc = 0 if os.path.exists(os.path.join(args.root, 'run-bench.sh')) else 1
    if rc != 0:
        raise SystemExit(
            '--root 应该是 raceway 仓库根（%s 下没有 run-bench.sh）\n'
            '  传成 campaigns 目录会让车定义/dry-run 全部落空；'
            '要编排它的 campaign 也应传仓库根，例如 --root /opt/raceway' % args.root)

    if not args.ssh:
        try:
            args.results_root = remote_operation(args, 'inspect')['results_root']
        except SystemExit as e:
            # 本地 inspect 失败（缺 vm-bench.env / 无 bash 等）时降级：
            # RESULTS_ROOT 按 <root>/campaigns 处理，进度/历史板块读文件不碰集群，
            # 仍然可用；起服务别被环境配置卡死。
            print('警告：本地 inspect 失败（%s）—— RESULTS_ROOT 按 %s 处理'
                  % (e, posixpath.join(args.root, 'campaigns')), file=sys.stderr)
            args.results_root = posixpath.join(args.root, 'campaigns')
    state = ComposeState(args)
    hard = [m for s, m in state.problems if s == 'error']
    if hard:
        print('警告：车定义有未解决问题（渲染时会拦截）：', file=sys.stderr)
        for m in hard:
            print('  ' + m, file=sys.stderr)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.state = state
    url = 'http://127.0.0.1:%d' % args.port
    print('raceway 控制台: %s  （① 启动排程 · ② 运行进度 · ③ 历史与结果 · ④ 趋势 · ⑤ 赛道与入库）' % url)
    print('  远端 %s · raceway 根 %s · 车源 %s' % (args.ssh or '本地', args.root, args.cars_file))
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_serve_rpc(args):
    """Go 后端逐请求转发的 RPC 入口（strangler 模式的 Python 侧）。

    命令行参数与 `serve` 完全一致（由 main() 的默认推导保证语义相同）；
    请求描述从 stdin 读入：{method, path, query, body, csrf_token}。
    复用与 serve 同一套 _route_get_core/_post_core，故两条路径契约一致。
    每次调用重建 ComposeState —— 无常驻状态，Go 侧保持空闲低内存。
    """
    req = json.loads(sys.stdin.read() or '{}')
    method = (req.get('method') or 'GET').upper()
    path = req.get('path') or '/'
    q = req.get('query') or {}
    body = req.get('body') or {}
    st = ComposeState(args)
    if req.get('csrf_token'):
        st.csrf_token = req['csrf_token']
    if method == 'GET':
        try:
            code, ctype, out, cache = _route_get_core(st, path, q)
        except Exception as e:
            code, ctype, out, cache = _err_body(e)
    elif method == 'POST':
        if not isinstance(body, dict):
            code, ctype, out, cache = (400, 'application/json',
                                       '{"error":"JSON object required"}', 'no-store')
        else:
            code, ctype, out, cache = _post_core(st, path, body)
    else:
        code, ctype, out, cache = (405, 'application/json',
                                   '{"error":"method not allowed"}', 'no-store')
    sys.stdout.write(json.dumps(
        {'code': code, 'ctype': ctype, 'body': out, 'cache': cache}, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['cars', 'gen', 'preview', 'start', 'serve', 'serve-rpc'])
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
    ap.add_argument('--root', default=None,
                    help='raceway 仓库根（要有 run-bench.sh）；缺省用本脚本所在目录'
                         '（它本身就是仓库克隆时），否则退回部署约定 %s' % DEFAULT_ROOT)
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

    if args.root is None:
        # 未指定 --root：本脚本所在目录是仓库克隆（有 run-bench.sh）时优先用它 ——
        # 在克隆目录里直接跑 compose 是常见用法，不该被部署约定的 /opt/raceway 卡住
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.isfile(os.path.join(here, 'run-bench.sh')):
            args.root = here
        else:
            args.root = DEFAULT_ROOT

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
                if args.cmd in ('serve', 'serve-rpc'):
                    # serve 是五合一控制台：进度/历史/趋势/入库板块不依赖车定义，
                    # 起服务别被车定义卡死 —— ComposeState 降级为空车表并在页面提示。
                    print('警告：本地车定义 %s 不存在 —— 排程板块不可用'
                          '（进度/历史/趋势/入库不受影响）。'
                          '用 --cars-file 或 RACEWAY_CARS_FILE 指向车定义后重启恢复。'
                          % args.cars_file, file=sys.stderr)
                else:
                    raise SystemExit(
                        '找不到车定义文件: %s\n'
                        '车定义属内部资料，未随公共库公开 —— 用 --cars-file 指定路径，'
                        '或设环境变量 RACEWAY_CARS_FILE 指向它。\n'
                        '远端模式（--ssh）不受影响：默认读远端 %s'
                        % (args.cars_file,
                           posixpath.join(args.root, 'schedules', 'dimsweep-cars.conf')))

    {'cars': cmd_cars, 'gen': cmd_gen, 'preview': cmd_preview,
     'start': cmd_start, 'serve': cmd_serve, 'serve-rpc': cmd_serve_rpc}[args.cmd](args)


if __name__ == '__main__':
    main()
