#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-queue.py —— campaign 队列观测台（看队列、看进度、看断点续跑状态）

为什么需要它
------------
raceway 的"队列"现在散在四个文件里，没有统一形态：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `PROGRESS.txt`   | 追加文本，逐场时间线（含 write/query 分相位） | runner |
| `RACES.md`       | 判定表，一场一行；**重试会追加多行**（`-try2`/`-try3`） | runner |
| `STATE.txt`      | 排程模式断点续跑状态：`OK/FAIL/SKIP <行号>` | runner |
| `<排程文件>`      | 待跑清单：`<车> <赛道> <轮次> <recall|norecall> <est分钟>` | 人 |

后果是"还剩哪些场次"在非排程模式下**根本不存在**（只在 `run-bench.sh` 的嵌套
for 循环里），起跑后外部无法知道队列全貌；排程模式下也只有"已跑过的"，没有
"待跑的"，除非能读到排程文件本身。

本工具把上面四个文件**推导**成一份队列视图：

- **不改 runner 就能用**（纯读，零侵入）
- runner 将来落盘 `QUEUE.json` 后优先读它（权威源）

它同时负责暴露四类"看不见的问题"：

1. **同一 fid 多条判定且状态矛盾** —— 排程文件在运行期间被改过、重跑后
   判定表没清干净时会出现（实测 ds2 campaign 已出现）
2. **STATE 行号与排程行错位** —— `STATE.txt` 的 key 是排程文件的**行号**，
   排程文件一旦增删行，`OK 8` 就会指向另一场
3. **排程文件在 campaign 运行期间被修改过** —— 文件 mtime 晚于起跑时间
4. **窗底被 runner 重启推后** —— `DEADLINE = now + DS_HOURS - 20min` 是每次
   runner 启动重算的，所以"12h 窗口"约束的是**单次 runner**，不是整个 campaign

用法
----
    python3 bench-queue.py list                                --root <campaigns 目录>
    python3 bench-queue.py status <campaign>                   --root ... [--json] [--watch]
    python3 bench-queue.py plan   <campaign>                   --root ...   # 只看待跑清单
    python3 bench-queue.py html   <campaign> -o q.html          --root ...
    python3 bench-queue.py serve  <campaign> --port 8801        --root ...   # 实时页
    # 远端读（默认 ssh host 走 key）：
    python3 bench-queue.py status ds2-20261021-021151 \
        --root /testing/raceway/campaigns --ssh root@203.0.113.10

只读，不写任何文件、不碰集群、不碰被测引擎。
"""
import argparse
import datetime
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ 状态词表
# 来源：run-bench.sh 写 RACES.md 的全部状态（run_field / run_field_with_retry）
STATUS_VALID = 'VALID'
STATUS_SKIP = 'SKIP'
STATUS_DEADLINE = 'DEADLINE-SKIP'
STATUS_FAILS = (
    'ENGINE-FAIL', 'JDK-SWAP-FAIL', 'JDK-FIX-FAIL', 'JDK-ASSERT-FAIL',
    'COLD-START-FAIL', 'CLUSTER-NOT-READY', 'HEAP-ASSERT-FAIL',
    'CPU-ASSERT-FAIL', 'METRIC-NULL-FAIL', 'WRITE-FAIL', 'QUERY-FAIL',
    'SEG-ASSERT-FAIL',
)
KNOWN_STATUS = (STATUS_VALID, STATUS_SKIP, STATUS_DEADLINE) + STATUS_FAILS

STATUS_PENDING = 'PENDING'      # 还没跑
STATUS_RUNNING = 'RUNNING'      # 正在跑


def is_fail(status):
    """非 VALID 且非"被跳过"的都算失败（用于连续失败告警）。"""
    return status in STATUS_FAILS


# ------------------------------------------------------------------ 数据源
class Source:
    """campaign 目录的读取抽象：本地目录 或 ssh 远端。"""

    def __init__(self, root, ssh=None):
        self.root = root.rstrip('/')
        self.ssh = ssh

    # -- 本地
    def _local_read(self, camp, rel):
        try:
            with open(os.path.join(self.root, camp, rel), 'r',
                      encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return None

    def _local_list(self):
        try:
            return sorted(os.listdir(self.root))
        except OSError:
            return []

    def _local_mtime(self, path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _local_exists(self, path):
        return os.path.exists(path)

    # -- ssh
    def _join(self, *parts):
        """远端是 POSIX 路径：Windows 的 os.path.join 会生成反斜杠，远端认不出来。"""
        j = posixpath.join if self.ssh else os.path.join
        return j(*parts)

    def _isabs(self, path):
        return (posixpath.isabs(path) if self.ssh else os.path.isabs(path))

    def _ssh(self, cmd):
        # ssh 会把 argv 用空格拼成一条远端命令，所以这里的远端命令必须
        # 自己加引号（shlex.quote），否则 `bash -lc "cat x 2>/dev/null"` 会被
        # 拆成 `bash -lc cat x 2>/dev/null` —— -c 只拿到 "cat"。
        full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        full += self.ssh.split()
        full += ['bash', '-lc', shlex.quote(cmd)]
        try:
            out = subprocess.run(full, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.decode('utf-8', 'replace')

    def _remote_read(self, camp, rel):
        p = shlex.quote(self._join(self.root, camp, rel))
        return self._ssh('cat %s 2>/dev/null' % p)

    def _remote_list(self):
        out = self._ssh('ls -1 %s 2>/dev/null' % shlex.quote(self.root))
        return [x for x in (out or '').split('\n') if x.strip()]

    def _remote_mtime(self, path):
        out = self._ssh('stat -c %%Y %s 2>/dev/null' % shlex.quote(path))
        try:
            return int((out or '').strip())
        except ValueError:
            return None

    def _remote_exists(self, path):
        return self._ssh('test -e %s && echo yes' % shlex.quote(path)) == 'yes\n'

    # -- 统一接口
    def read(self, camp, rel):
        if self.ssh:
            return self._remote_read(camp, rel)
        return self._local_read(camp, rel)

    def list_campaigns(self):
        if self.ssh:
            return self._remote_list()
        return self._local_list()

    def mtime(self, path):
        if self.ssh:
            return self._remote_mtime(path)
        return self._local_mtime(path)

    def exists(self, path):
        if self.ssh:
            return self._remote_exists(path)
        return self._local_exists(path)

    def resolve(self, path):
        """把排程文件路径解析成可用路径（相对 campaigns 目录的上一级）。"""
        if self._isabs(path):
            return path
        candidates = [
            self._join(os.path.dirname(self.root), path),   # /testing/raceway/<rel>
            self._join(self.root, path),
        ]
        for c in candidates:
            if self.exists(c):
                return c
        return candidates[0]

    def read_abs(self, path):
        """读一个绝对路径（排程文件不在 campaigns 目录里）。"""
        if self.ssh:
            return self._ssh('cat %s 2>/dev/null' % shlex.quote(path))
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return None

    def tail(self, camp, rel, n=60):
        """读 campaign 里一个文件的最后 n 行（进度页的实时日志尾）。"""
        if self.ssh:
            p = shlex.quote(self._join(self.root, camp, rel))
            return self._ssh('tail -n %d %s 2>/dev/null' % (int(n), p)) or ''
        try:
            with open(os.path.join(self.root, camp, rel), 'r',
                      encoding='utf-8', errors='replace') as f:
                return ''.join(f.readlines()[-int(n):])
        except OSError:
            return ''


# ------------------------------------------------------------------ 模型
@dataclass
class Field:
    seq: int = 0
    fid: str = ''
    car: str = ''
    track: str = ''
    rnd: int = 0
    status: str = STATUS_PENDING
    detail: str = ''
    started: str = ''
    ended: str = ''
    duration: str = ''
    attempts: int = 0
    recall: str = ''
    est: int = 0
    conflicts: list = field(default_factory=list)   # 互相矛盾的判定
    phase: str = ''                             # 运行中相位：write+query / recall
    elapsed_min: float = 0.0                        # 运行中场次已进行分钟数（快照时算）


@dataclass
class Campaign:
    cid: str = ''
    root: str = ''
    started_at: str = ''
    runner_started_at: str = ''
    cars: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    rounds: int = 0
    mode: str = ''                 # schedule | legacy | unknown
    schedule_file: str = ''
    cars_file: str = ''
    deadline: str = ''
    deadlines: list = field(default_factory=list)
    sched_total: int = 0
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    ended: bool = False
    plan_known: bool = False

    # ---- 统计
    @property
    def total(self):
        return len(self.fields)

    @property
    def valid(self):
        return sum(1 for f in self.fields if f.status == STATUS_VALID)

    @property
    def failed(self):
        return sum(1 for f in self.fields if is_fail(f.status))

    @property
    def skipped(self):
        return sum(1 for f in self.fields if f.status in (STATUS_SKIP, STATUS_DEADLINE))

    @property
    def pending(self):
        return sum(1 for f in self.fields if f.status == STATUS_PENDING)

    @property
    def running(self):
        return [f for f in self.fields if f.status == STATUS_RUNNING]

    @property
    def consecutive_fail(self):
        """按 seq 顺序连续的非 VALID 场次数（跳过不算断；碰到未跑的场就停）。"""
        n = 0
        for f in sorted(self.fields, key=lambda x: x.seq):
            if f.status in (STATUS_RUNNING, STATUS_PENDING):
                break
            if is_fail(f.status):
                n += 1
            elif f.status in (STATUS_VALID, STATUS_SKIP, STATUS_DEADLINE):
                n = 0
        return n

    @property
    def last_fails(self):
        """尾部连续的失败场（按 seq 顺序）。"""
        out = []
        for f in sorted(self.fields, key=lambda x: x.seq):
            if f.status in (STATUS_RUNNING, STATUS_PENDING):
                break
            if is_fail(f.status):
                out.append(f)
            else:
                out = []
        return out

    @property
    def est_remaining_min(self):
        """剩余场次的预计耗时（分钟）：有 est 用 est，否则用已完成场的均值。"""
        vals = [_duration_min(f.duration) for f in self.fields]
        vals = [v for v in vals if v]
        avg = (sum(vals) / len(vals)) if vals else None
        total = 0.0
        for f in self.fields:
            if f.status == STATUS_PENDING:
                total += float(f.est) if f.est else (avg or 0.0)
        return total


# ------------------------------------------------------------------ 解析
RE_TS = re.compile(r'^\[([\d\-: ]+)\]')
# tracks 是多词列表（"geo hnsw nested" / "ds-g1920 ds-j2048 ..."），
# 必须用非贪婪 .+? 匹配到 rounds= 之前 —— 用 \S+ 会抓不到多赛道 campaign。
# 第 2 组 configs/cars 是**版本判据**：10-19 之前的旧版 runner 写 cars=，
# 之后的版本写 configs= 并带「轮内右旋配对交错」标记。
RE_HEADER = re.compile(
    r'campaign\s+(\S+)\s+启动:\s*(configs|cars)=\[([^\]]*)\]\s+tracks=(.+?)\s+rounds=(\d+)')
# 旧版 runner 的排程模式起跑头：schedule=24 场, 窗口 12h, deadline ...
RE_SCHED_OLD = re.compile(r'campaign\s+(\S+)\s+启动:\s*schedule=(\d+)\s*场')
RE_SCHED_MODE = re.compile(r'排程模式:\s*(\S+)\s*（车定义\s*(\S+?)\s*）')
RE_DEADLINE = re.compile(r'窗底守卫:\s*(\S+?)h（截止\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
RE_FIELD_START = re.compile(r'^\[([\d\-: ]+)\]\s+=====\s+\[(\d+)\]\s+(\S+)\s+开始\s+=====')
RE_FIELD_DONE = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+完成:\s*(\S+?)（(.*?)）')
RE_FIELD_FAIL = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+(' + '|'.join(KNOWN_STATUS) + r')\b(.*)$')
RE_FIELD_SKIP = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+跳过（(.*?)）')
RE_FIELD_DEADLINE = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+DEADLINE-SKIP（(.*?)）')
RE_RETRY = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+第\s*(\d+)/(\d+)\s*次尝试')
RE_GIVEUP = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+已达最大重试次数')
RE_PHASE = re.compile(r'^\[([\d\-: ]+)\]\s+\[(\d+)\]\s+(\S+)\s+(full race 开始|recall 探针开始)')
# RE_PHASE 第 4 组 → 展示词（进度页的"当前相位"）
PHASE_LABEL = {'full race 开始': 'write+query', 'recall 探针开始': 'recall'}
RE_CAMPAIGN_END = re.compile(r'^\[([\d\-: ]+)\]\s+campaign 结束:\s*(\d+)/(\d+)\s*场有效')


def order_for_round(r, n):
    """复刻 run-bench.sh 的 order_for_round（轮内右旋配对交错）。

    源码：run-bench.sh:199-204
        off = (r-1) % n;  输出 [ (i-off+n) % n for i in range(n) ]
    """
    if n <= 0:
        return []
    off = (r - 1) % n
    return [(i - off + n) % n for i in range(n)]


def base_fid(fid):
    """去掉重试后缀：jdk26-ds-j1024-r1-try2 → jdk26-ds-j1024-r1"""
    return re.sub(r'-try\d+$', '', fid)


def _duration_min(duration):
    """'26min' / '26 分钟' → 26.0；解析不出返回 None。"""
    m = re.match(r'^([\d.]+)\s*(?:min|分钟)?', (duration or '').strip())
    return float(m.group(1)) if m and m.group(1) else None


def split_fid(fid, cars, tracks):
    """把 fid 拆成 (车, 赛道, 轮次, 尝试号)。

    fid 形如 `<CAR_LABEL>-<track>-r<round>`，车名和赛道名都可能带 '-'，
    所以按已知词表做最长前缀/后缀匹配，而不是按 '-' 硬切。
    """
    m = re.search(r'-r(\d+)(-try(\d+))?$', fid)
    rnd = int(m.group(1)) if m else 0
    attempt = int(m.group(3)) if (m and m.group(3)) else 0
    core = re.sub(r'-r\d+(-try\d+)?$', '', fid)

    car = ''
    for a in sorted(cars, key=len, reverse=True):
        if core.startswith(a + '-'):
            car = a
            break
    track = core[len(car) + 1:] if car else core
    for t in sorted(tracks, key=len, reverse=True):
        if core.endswith('-' + t) or core == t:
            track = t
            break
    return car, track, rnd, attempt


def parse_progress(text):
    """从 PROGRESS.txt 抽：起跑头、排程模式、时间线事件。"""
    out = {
        'cid': '', 'cars': [], 'tracks': [], 'rounds': 0,
        'schedule_file': '', 'cars_file': '', 'deadline': '', 'deadlines': [],
        'started_at': '', 'runner_started_at': '',
        'racer': 'unknown', 'sched_total': 0,
        'events': [], 'ended': False, 'end_valid': 0, 'end_total': 0,
    }
    if not text:
        return out
    last_header_pos = -1
    end_pos = -1
    for pos, line in enumerate(text.splitlines()):
        m = RE_HEADER.search(line)
        if m:
            out['cid'] = m.group(1)
            out['racer'] = 'new' if m.group(2) == 'configs' else 'old'
            out['cars'] = [x for x in m.group(3).split() if x]
            out['tracks'] = m.group(4).split()
            out['rounds'] = int(m.group(5))
            ts = RE_TS.match(line)
            if ts:
                # 起跑头每次 runner 重启都会追加一行：第一次是 campaign 真起跑，
                # 最后一次是当前这一轮 —— 两个都要，用途不同。
                if not out['started_at']:
                    out['started_at'] = ts.group(1).strip()
                out['runner_started_at'] = ts.group(1).strip()
            last_header_pos = pos
            continue
        m = RE_SCHED_OLD.search(line)
        if m:
            # 旧版 runner 的排程模式：只记总场数，成分在排程文件里（本地没有）
            out['cid'] = m.group(1)
            out['racer'] = 'old'
            out['sched_total'] = int(m.group(2))
            ts = RE_TS.match(line)
            if ts:
                if not out['started_at']:
                    out['started_at'] = ts.group(1).strip()
                out['runner_started_at'] = ts.group(1).strip()
            last_header_pos = pos
            continue
        m = RE_SCHED_MODE.search(line)
        if m:
            out['schedule_file'] = m.group(1)
            out['cars_file'] = m.group(2)
            continue
        m = RE_DEADLINE.search(line)
        if m:
            # 每次 runner 重启都会重算窗底（DEADLINE = now + DS_HOURS - 20min），
            # 所以这里要**全部**留档：最后一条是当前这一轮的，前面的是历史。
            out['deadlines'].append(m.group(2).strip())
            out['deadline'] = m.group(2).strip()
            continue
        m = RE_CAMPAIGN_END.search(line)
        if m:
            end_pos = pos
            out['end_valid'] = int(m.group(2))
            out['end_total'] = int(m.group(3))
            continue
        for rx, kind in ((RE_FIELD_START, 'start'), (RE_FIELD_DONE, 'done'),
                         (RE_FIELD_FAIL, 'fail'), (RE_FIELD_SKIP, 'skip'),
                         (RE_FIELD_DEADLINE, 'deadline'), (RE_RETRY, 'retry'),
                         (RE_GIVEUP, 'giveup'), (RE_PHASE, 'phase')):
            m = rx.match(line)
            if m:
                out['events'].append((kind, m.groups()))
                break
    # 「campaign 结束」必须出现在**最后一次**起跑头之后才算真结束 ——
    # 否则 runner 被杀后重启，旧的那条结束记录会让人误以为 campaign 已完成。
    out['ended'] = end_pos > last_header_pos >= 0
    return out


def parse_races(text):
    """从 RACES.md 抽：(场次, 状态, 赛前卡点, 用时) 行列表。"""
    rows = []
    if not text:
        return rows
    for line in text.splitlines():
        if not line.startswith('|') or line.startswith('|---') or '场次' in line:
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            continue
        rows.append((cells[0], cells[1], cells[2] if len(cells) > 2 else '',
                     cells[3] if len(cells) > 3 else ''))
    return rows


def parse_state(text):
    """从 STATE.txt 抽：行号 → [状态, ...]（同一行可能有 FAIL 和 OK 并存）。"""
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ('OK', 'FAIL', 'SKIP'):
            out.setdefault(int(parts[1]), []).append(parts[0])
    return out


def parse_schedule(text):
    """解析排程文件：每行 `<车> <赛道> <轮次> <recall|norecall> <est分钟>`。"""
    rows = []
    if not text:
        return rows
    for line in text.splitlines():
        line = re.sub(r'#.*', '', line).strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        car, track, rnd = parts[0], parts[1], parts[2]
        recall = parts[3] if len(parts) > 3 else ''
        est = 0
        if len(parts) > 4:
            try:
                est = int(parts[4])
            except ValueError:
                est = 0
        rows.append({'car': car, 'track': track,
                     'rnd': int(rnd) if rnd.isdigit() else 0,
                     'recall': recall, 'est': est})
    return rows


# ------------------------------------------------------------------ 组装
def build_campaign(src, cid, schedule_override=None):
    """把一个 campaign 目录推导成 Campaign 对象。"""
    prog = parse_progress(src.read(cid, 'PROGRESS.txt'))
    races = parse_races(src.read(cid, 'RACES.md'))
    state = parse_state(src.read(cid, 'STATE.txt'))

    c = Campaign(cid=cid, root=src.root)
    c.cars = prog['cars']
    c.tracks = prog['tracks']
    c.rounds = prog['rounds']
    c.schedule_file = prog['schedule_file']
    c.cars_file = prog['cars_file']
    c.deadline = prog['deadline']
    c.deadlines = prog['deadlines']
    c.started_at = prog['started_at']
    c.runner_started_at = prog['runner_started_at']
    c.ended = prog['ended']

    # ---- 1) 排程清单（待跑队列的来源）
    # 实际执行顺序：时间线里每场的「开始」，按基础 fid 归并。
    # 归并规则是**首次出现去重**，不是折叠连续重复 —— 重跑是同一场的又一次尝试，
    # 会把同一个 fid 再次插回时间线（实测 ds2：jdk26/jdk27 车先 COLD-START-FAIL，
    # 修好 JVMCI 后重跑）。按连续折叠会把这些重跑当成"别处插进来的新场次"，
    # 顺序比对必然对不上，于是把本来正确的排程误判成"推导不可信"。
    observed = []
    seen_obs = set()
    for kind, g in prog['events']:
        if kind != 'start':
            continue
        b = base_fid(g[2])
        if b in seen_obs:
            continue
        seen_obs.add(b)
        observed.append(b)

    plan_rows = []
    sched_path = schedule_override or prog['schedule_file']
    if sched_path:
        resolved = src.resolve(sched_path)
        # 排程文件不在 campaigns 目录里，按解析出的路径读
        plan_rows = parse_schedule(src.read_abs(resolved))
        c.schedule_file = resolved
        c.mode = 'schedule' if plan_rows else 'schedule?'
    if not plan_rows:
        c.mode = 'legacy' if (c.tracks and c.rounds and c.cars) else 'unknown'

    if plan_rows:
        # 排程文件是权威源：清单直接照它来
        c.plan_known = True
        for i, r in enumerate(plan_rows, 1):
            c.fields.append(Field(seq=i, fid='%s-%s-r%d' % (r['car'], r['track'], r['rnd']),
                                  car=r['car'], track=r['track'], rnd=r['rnd'],
                                  recall=r['recall'], est=r['est']))
    elif c.mode == 'legacy':
        # 非排程模式：按 run-bench.sh:1067-1075 的展平规则推导
        #（赛道外层 → 轮次 → 车右旋），但**推导必须自校验**：
        # 对不上实际执行顺序就说明这条规则不适用于这个 campaign
        #（10-19 之前的旧版 runner 规则不同，或排程在运行期间被改过）。
        seq = 0
        plan = []
        for tk in c.tracks:
            for r in range(1, c.rounds + 1):
                for idx in order_for_round(r, len(c.cars)):
                    car = c.cars[idx]
                    seq += 1
                    plan.append(Field(seq=seq, fid='%s-%s-r%d' % (car, tk, r),
                                      car=car, track=tk, rnd=r))
        derived = [f.fid for f in plan]
        n = min(len(observed), len(derived))
        consistent = (not observed) or derived[:n] == observed[:n]
        if consistent:
            c.plan_known = True
            c.fields.extend(plan)
        else:
            c.mode = 'legacy(旧版)'
            c.plan_known = False
            c.warnings.append(
                '推导的队列与实际执行顺序不一致（旧版 runner 的队列规则不同，或排程在运行期间被改过）'
                '—— 待跑清单不可信，这里只显示已跑过的场次')
    if prog['sched_total'] and not c.plan_known:
        c.sched_total = prog['sched_total']
        c.warnings.append(
            '该 campaign 用的是旧版 runner 的排程模式（排程共 %d 场），'
            '排程文件不在这台机器上 —— 待跑清单未知，只显示已跑过的场次'
            % prog['sched_total'])

    # ---- 2) 时间线事件 → 状态
    # 关键约定：
    #   * `-tryN` 是**同一场的第 N 次尝试**，不是新场次 —— 按基础 fid 归并
    #   * 「跳过（STATE 已有 OK N）」/「跳过（RACES.md 已有 VALID…）」是**续跑跳过**，
    #     不是判定结论；把它当成 SKIP 会把已 VALID 的场误判成跳过
    #   * 真正的 SKIP 是赛前静默卡点没过：PROGRESS 里写「SKIP: …」，RACES.md 记 SKIP
    by_base = {}
    for f in c.fields:
        by_base.setdefault(f.fid, f)

    def field_of(fid, seq_hint=0):
        b = base_fid(fid)
        f = by_base.get(b)
        if f is None:
            car, track, rnd, _ = split_fid(b, c.cars, c.tracks)
            # 计划外的场次：用时间线里的序号顶上（runner 自己打的 [n]）
            f = Field(seq=seq_hint, fid=b, car=car, track=track, rnd=rnd)
            by_base[b] = f
            c.fields.append(f)
        return f

    for kind, g in prog['events']:
        ts, seq, fid = g[0], int(g[1]), g[2]
        f = field_of(fid, seq)
        if kind == 'start':
            f.status = STATUS_RUNNING
            f.started = ts.strip()
            f.detail = ''
            f.phase = ''
        elif kind == 'done':
            f.status = g[3]
            f.ended = ts.strip()
            f.duration = g[4]
            f.detail = ''
            f.phase = ''
        elif kind == 'fail':
            f.status = g[3]
            f.ended = ts.strip()
            f.detail = (g[4] or '').strip()
            f.phase = ''
        elif kind == 'phase':
            # 相位事件只对"正在跑"的场有意义（重跑新开始时 start 已清零）
            if f.status == STATUS_RUNNING:
                f.phase = PHASE_LABEL.get(g[3], '')
        elif kind == 'skip':
            txt = (g[3] or '').strip()
            if '已有' in txt:          # 续跑跳过：历史已有结论，别覆盖
                f.detail = f.detail or ('续跑跳过（%s）' % txt)
            else:                      # 赛前卡点没过
                f.status = STATUS_SKIP
                f.ended = ts.strip()
                f.detail = txt
        elif kind == 'deadline':
            f.status = STATUS_DEADLINE
            f.ended = ts.strip()
            f.detail = g[3]
        elif kind == 'retry':
            f.attempts = max(f.attempts, int(g[3]))
        elif kind == 'giveup':
            f.ended = ts.strip()
            if not is_fail(f.status):
                f.status = 'WRITE-FAIL'
                f.detail = '已达最大重试次数'

    # ---- 3) 判定表（RACES.md）归并
    # 同一基础 fid 的多行 = 尝试链（-try2/-try3）+ 重跑；
    # 其中**同一个精确 fid 出现多次且状态不同**才是真矛盾（排程被改后重跑没清表）。
    groups = {}
    for fid, status, precheck, dur in races:
        groups.setdefault(base_fid(fid), []).append((fid, status, precheck, dur))

    for b, rows in groups.items():
        f = field_of(b)
        f.attempts = max(f.attempts, len(rows))
        exact = {}
        for efid, st, _, _ in rows:
            exact.setdefault(efid, []).append(st)
        for efid, sts in exact.items():
            if len(set(sts)) > 1:
                msg = '%s 重复判定：%s' % (efid, ' / '.join(sts))
                f.conflicts.append(msg)
                c.warnings.append('场次 %s 的同一 fid 有 %d 条互相矛盾的判定：%s'
                                  % (efid, len(sts), ' / '.join(sts)))
        last = rows[-1]
        if f.status != STATUS_RUNNING:      # 正在跑的场还没写判定，别覆盖
            f.status = last[1]
        if last[3] and last[3] != '-' and not f.duration:
            f.duration = last[3]
        if last[2] and last[2] != 'precheck=PASS' and not f.detail:
            f.detail = last[2]

    # ---- 4) 尝试次数（-tryN 后缀）
    for f in c.fields:
        m = re.search(r'-try(\d+)$', f.fid)
        if m:
            f.attempts = max(f.attempts, int(m.group(1)))

    # ---- 5) STATE 合并 + 行号错位检测
    for seq, sts in sorted(state.items()):
        f = next((x for x in c.fields if x.seq == seq), None)
        if f is None:
            c.warnings.append('STATE 里有第 %d 行的记录，但排程/计划里没有第 %d 行（行号已错位）'
                              % (seq, seq))
            continue
        if len(set(sts)) > 1:
            c.warnings.append('STATE 第 %d 行同时有 %s 记录（同一行被判过多种结果）'
                              % (seq, '/'.join(sorted(set(sts)))))
        if 'OK' in sts and f.status == STATUS_PENDING:
            f.status = STATUS_VALID

    # ---- 6) 排程文件运行期间被改过？
    if c.schedule_file:
        mt = src.mtime(c.schedule_file)
        started_dt = _parse_ts(c.started_at)
        if mt and started_dt:
            mtd = datetime.datetime.fromtimestamp(mt)
            if mtd > started_dt + datetime.timedelta(seconds=5):
                c.warnings.append(
                    '排程文件 %s 在 campaign 起跑后被修改过（mtime %s > 起跑 %s）——'
                    'STATE 的行号绑定与新排程可能已经错位'
                    % (c.schedule_file, mtd.strftime('%m-%d %H:%M:%S'),
                       started_dt.strftime('%m-%d %H:%M:%S')))

    # ---- 7) 排序 + 连续失败告警 + 窗底预算
    c.fields.sort(key=lambda f: (f.seq or 9999, f.fid))
    cf = c.consecutive_fail
    if cf >= 3:
        c.warnings.append('连续 %d 场失败未恢复（最近：%s）'
                          % (cf, ' / '.join(f.fid for f in c.last_fails)))
    dl = _parse_ts(c.deadline)
    # 窗底漂移：DEADLINE = now + DS_HOURS - 20min 是**每次 runner 启动**重算的，
    # 所以重启几次，"12h 窗口"就被推后几次 —— 它约束的是单次 runner，不是整个 campaign。
    if len(set(c.deadlines)) > 1:
        now = datetime.datetime.now()
        msg = ('窗底被 runner 重启推后了 %d 次（%s）：DS_HOURS 约束的是**单次 runner**，'
               '不是整个 campaign'
               % (len(c.deadlines) - 1, ' → '.join(c.deadlines)))
        first = _parse_ts(c.deadlines[0])
        if first and first < now:
            msg += '；原始窗底 %s 已过，已超出 %.0f 分钟' % (
                c.deadlines[0], (now - first).total_seconds() / 60.0)
        c.warnings.append(msg)
    if dl and c.pending:
        left = (dl - datetime.datetime.now()).total_seconds() / 60.0
        need = c.est_remaining_min
        pending_est = sorted([f for f in c.fields
                              if f.status == STATUS_PENDING and f.est],
                             key=lambda x: x.seq)
        if left <= 0:
            c.warnings.append('已过窗底 %s，剩余 %d 场全部会被 DEADLINE-SKIP'
                              % (c.deadline, c.pending))
        elif need > left:
            # 复刻 runner 的窗底守卫：放不下就跳过、继续后面更便宜的场
            acc, skip = 0.0, 0
            for f in pending_est:
                if acc + f.est > left:
                    skip += 1
                else:
                    acc += f.est
            c.warnings.append(
                '剩余 %d 场按 est 需 %.0f 分钟，但距窗底 %s 只剩 %.0f 分钟 —— '
                '约 %d 场放不下会被 DEADLINE-SKIP'
                % (c.pending, need, c.deadline, left, skip))
    if not c.plan_known and not c.warnings:
        if not (c.tracks and c.rounds):
            c.warnings.append('起跑头里没有赛道/轮次信息（很可能是旧版 runner 产物）'
                              '—— 待跑清单未知，只显示已跑过的场次')
        elif not c.cars:
            c.warnings.append('起跑头里没有车列表 —— 待跑清单未知，只显示已跑过的场次')
        else:
            c.warnings.append('待跑清单未知 —— 只显示已跑过的场次')
    return c


def _parse_ts(s):
    s = (s or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ campaign 摘要
# 进度页/历史页都要一份"全部 campaign 的轻量清单"。逐个 build_campaign 在 ssh
# 模式下是每场 3+ 次远端调用（52 个 campaign 就是上百次 ssh），列表页吃不消 ——
# 这里一次调用/一次扫描拿全部：cid、PROGRESS.txt mtime、RACES 行数、VALID 数、
# 最后一行（判定"是否已结束"用），细节留给点开后的 build_campaign。

def _summary_from_parts(cid, mtime, raw_rows, valid_rows, n_start, n_end, last_line):
    """摘要元组 → dict。RACES 行数含表头+分隔线两行，数据行要减掉。

    「已结束」判定与 build_campaign 同语义：campaign 结束行出现在**最后一次**
    起跑头之后才算真结束 —— 用行数近似（结束行数 ≥ 起跑头行数）。
    runner 被杀后没有结束行，标"运行中"偏安全。
    """
    try:
        mtime = int(mtime or 0)
    except (TypeError, ValueError):
        mtime = 0
    try:
        total = max(0, int(raw_rows or 0) - 2)
    except (TypeError, ValueError):
        total = 0
    try:
        valid = int(valid_rows or 0)
    except (TypeError, ValueError):
        valid = 0
    try:
        n_start = int(n_start or 0)
    except (TypeError, ValueError):
        n_start = 0
    try:
        n_end = int(n_end or 0)
    except (TypeError, ValueError):
        n_end = 0
    last = (last_line or '').strip()
    return {
        'cid': cid,
        'mtime': mtime,
        'total': total,
        'valid': valid,
        'ended': n_end > 0 and n_end >= n_start,
        'last': last,
    }


_RACES_VALID_RE = r'^\\|[^|]*\\|[[:space:]]*VALID[[:space:]]*\\|'


def remote_summaries_script(root):
    """campaign 摘要的远端采集脚本：一次 bash 循环，TSV 输出七列。

    抽成函数是为了可测（bash -n 语法检查 / 本地 bash 直接执行比对）。
    """
    return (
        'cd ' + shlex.quote(root) + ' 2>/dev/null || exit 0\n'
        'for d in */; do\n'
        '  p="${d}PROGRESS.txt"; r="${d}RACES.md"\n'
        '  [ -f "$p" ] || continue\n'
        '  cid="${d%/}"\n'
        '  last=$(grep -v "^[[:space:]]*$" "$p" 2>/dev/null | tail -n 1 | cut -c1-120)\n'
        '  raw=$(grep -c "^|" "$r" 2>/dev/null)\n'
        '  valid=$(grep -cE "' + _RACES_VALID_RE + '" "$r" 2>/dev/null)\n'
        '  ns=$(grep -c "启动:" "$p" 2>/dev/null)\n'
        '  ne=$(grep -c "campaign 结束" "$p" 2>/dev/null)\n'
        '  mt=$(stat -c %Y "$p" 2>/dev/null)\n'
        '  printf \'%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n\' "$cid" "$mt" "$raw" "$valid" "$ns" "$ne" "$last"\n'
        'done\n')


def campaign_summaries(src):
    """全部 campaign 的轻量摘要，按 mtime 新→旧返回。

    ssh 模式下是一次远端 bash 循环（不是逐 campaign 一次 ssh）；本地模式直接读。
    RACES.md 的一致性细节（矛盾判定等）不在这里查 —— 那是 build_campaign 的活。
    """
    if src.ssh:
        out = src._ssh(remote_summaries_script(src.root))
        parts = [(x.split('\t') + [''] * 7)[:7] for x in (out or '').splitlines() if '\t' in x]
    else:
        parts = []
        for cid in src.list_campaigns():
            prog_path = os.path.join(src.root, cid, 'PROGRESS.txt')
            if not os.path.isfile(prog_path):
                continue
            last = ''
            n_start = n_end = 0
            try:
                with open(prog_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if '启动:' in line:
                            n_start += 1
                        elif 'campaign 结束' in line:
                            n_end += 1
                with open(prog_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in reversed(f.readlines()):
                        if line.strip():
                            last = line
                            break
            except OSError:
                pass
            races_path = os.path.join(src.root, cid, 'RACES.md')
            raw = valid = 0
            try:
                with open(races_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.startswith('|'):
                            raw += 1
                            if re.match(r'^\|[^|]*\|\s*VALID\s*\|', line):
                                valid += 1
            except OSError:
                pass
            try:
                mt = os.path.getmtime(prog_path)
            except OSError:
                mt = 0
            parts.append([cid, str(int(mt)), str(raw), str(valid),
                          str(n_start), str(n_end), last.rstrip('\n')])
    sums = [_summary_from_parts(*p) for p in parts]
    sums.sort(key=lambda s: (s['mtime'], s['cid']), reverse=True)
    return sums


def pick_active(summaries):
    """默认该看哪个 campaign：最新的没收尾的；全收尾了就看最新的（当历史用）。"""
    for s in summaries:
        if not s['ended']:
            return s['cid']
    return summaries[0]['cid'] if summaries else None


# ------------------------------------------------------------------ 输出
STATUS_STYLE = {
    STATUS_VALID: 'ok',
    STATUS_RUNNING: 'run',
    STATUS_PENDING: 'pend',
    STATUS_SKIP: 'skip',
    STATUS_DEADLINE: 'skip',
}


def style(status):
    return STATUS_STYLE.get(status, 'bad')


def render_table(c):
    lines = []
    lines.append('  %4s  %-34s %-16s %-8s %-9s %s'
                 % ('seq', '场次', '状态', '用时', 'recall', '说明'))
    lines.append('  ' + '-' * 96)
    for f in c.fields:
        tag = ''
        if f.conflicts:
            tag = '⚠ ' + '；'.join(sorted(set(f.conflicts)))
        if f.detail:
            tag = (tag + '  ' if tag else '') + f.detail
        lines.append('  %4d  %-34s %-16s %-8s %-9s %s'
                     % (f.seq, f.fid, f.status, f.duration or '-', f.recall or '-', tag))
    return '\n'.join(lines)


def render_summary(c):
    out = []
    out.append('campaign %s' % c.cid)
    out.append('  模式 %s   场次 %d   有效 %d   失败 %d   跳过 %d   待跑 %d   计划%s'
               % (c.mode, c.total, c.valid, c.failed, c.skipped, c.pending,
                  '已知' if c.plan_known else '未知（缺排程文件）'))
    if c.sched_total:
        out.append('  排程共 %d 场（排程文件不在本机，具体成分未知）' % c.sched_total)
    if c.started_at:
        extra = ''
        if c.runner_started_at and c.runner_started_at != c.started_at:
            extra = '   本轮 runner 重启于 %s' % c.runner_started_at
        out.append('  起跑 %s%s%s' % (c.started_at, '   已结束' if c.ended else '   运行中',
                                     extra))
    run = c.running
    if run:
        f = run[0]
        extra = ('，相位 %s' % f.phase) if f.phase else ''
        out.append('  进行中：[%d] %s（起 %s%s）' % (f.seq, f.fid, f.started, extra))
    if c.deadline:
        out.append('  窗底守卫截止 %s' % c.deadline)
    if c.pending:
        out.append('  剩余预计 %.0f 分钟（%d 场）' % (c.est_remaining_min, c.pending))
    if c.consecutive_fail:
        out.append('  连续失败 %d 场' % c.consecutive_fail)
    return '\n'.join(out)


def campaign_json(c):
    now = datetime.datetime.now()
    for f in c.fields:
        if f.status == STATUS_RUNNING and f.started:
            st = _parse_ts(f.started)
            if st:
                f.elapsed_min = max(0.0, (now - st).total_seconds() / 60.0)
    return {
        'campaign': c.cid,
        'mode': c.mode,
        'started_at': c.started_at,
        'runner_started_at': c.runner_started_at,
        'ended': c.ended,
        'cars': c.cars,
        'tracks': c.tracks,
        'rounds': c.rounds,
        'schedule_file': c.schedule_file,
        'deadline': c.deadline,
        'deadlines': c.deadlines,
        'plan_known': c.plan_known,
        'stats': {
            'total': c.total, 'valid': c.valid, 'failed': c.failed,
            'skipped': c.skipped, 'pending': c.pending,
            'consecutive_fail': c.consecutive_fail,
            'est_remaining_min': round(c.est_remaining_min, 1),
        },
        'warnings': c.warnings,
        'fields': [asdict(f) for f in c.fields],
    }


HTML_TMPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>raceway 队列 · __CID__</title>
<style>
 :root{color-scheme:dark}
 body{background:#11151c;color:#d8dee9;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:24px}
 h1{font-size:18px;margin:0 0 4px}
 .meta{color:#8b98a9;font-size:12px;margin-bottom:16px}
 .bar{height:8px;background:#1f2630;border-radius:4px;overflow:hidden;margin:12px 0 20px}
 .bar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e)}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
 .card{background:#171c24;border:1px solid #232b36;border-radius:8px;padding:10px 14px;min-width:88px}
 .card b{display:block;font-size:20px}
 .card span{color:#8b98a9;font-size:11px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #1e2530;white-space:nowrap}
 th{color:#8b98a9;font-weight:500;font-size:11px;letter-spacing:.04em}
 tr.now td{background:#16222e}
 .ok{color:#4ade80}.bad{color:#f87171}.run{color:#60a5fa}.skip{color:#fbbf24}.pend{color:#6b7280}
 .warn{background:#2a1f12;border:1px solid #4b3a20;border-left:3px solid #f59e0b;border-radius:6px;padding:8px 12px;margin:6px 0;color:#fcd9a3}
 .note{color:#8b98a9;font-size:12px;margin-top:18px}
</style></head><body>
<h1>raceway 队列 · __CID__</h1>
<div class="meta">模式 __MODE__ · 起跑 __START__ · __UPDATED__</div>
<div class="bar"><i id="bar" style="width:__PCT__%"></i></div>
<div class="cards">__CARDS__</div>
<div id="warns">__WARNS__</div>
<table><thead><tr><th>seq</th><th>场次</th><th>状态</th><th>用时</th><th>recall</th><th>说明</th></tr></thead>
<tbody id="rows">__ROWS__</tbody></table>
<div class="note">本页面由 bench-queue.py 生成；由 <code>bench-queue.py serve</code> 提供时每 5 秒自动刷新。</div>
<script>
async function tick(){
  try{
    const r = await fetch('/api/queue',{cache:'no-store'});
    if(!r.ok) return;
    paint(await r.json());
  }catch(e){/* 静态快照模式：无后端，忽略 */}
}
function paint(d){
  document.querySelector('.meta').textContent =
    '模式 '+d.mode+' · 起跑 '+(d.started_at||'-')+' · 更新 '+new Date().toLocaleString();
  const s=d.stats;
  document.getElementById('bar').style.width=(s.total?100*s.valid/s.total:0)+'%';
  document.getElementById('warns').innerHTML=(d.warnings||[])
    .map(function(w){return '<div class="warn">⚠ '+esc(w)+'</div>'}).join('');
  document.getElementById('rows').innerHTML=(d.fields||[]).map(function(f){
    const cls=f.status==='VALID'?'ok':f.status==='RUNNING'?'run':
      (f.status==='SKIP'||f.status==='DEADLINE-SKIP')?'skip':
      f.status==='PENDING'?'pend':'bad';
    const note=(f.conflicts&&f.conflicts.length)?('⚠ 矛盾判定: '+f.conflicts.join('/')):'';
    return '<tr class="'+(f.status==='RUNNING'?'now':'')+'"><td>'+f.seq+'</td><td>'+esc(f.fid)+'</td>'+
      '<td class="'+cls+'">'+f.status+'</td><td>'+esc(f.duration||'-')+'</td>'+
      '<td>'+esc(f.recall||'-')+'</td><td>'+esc(note||f.detail||'')+'</td></tr>';
  }).join('');
}
function esc(s){var A=String.fromCharCode(38);return String(s==null?'':s).replace(/[&<>]/g,function(c){return A+(c==='&'?'amp;':c==='<'?'lt;':'gt;')})}
tick(); setInterval(tick,5000);
</script></body></html>
"""


def _esc(s):
    """HTML 转义。实体用字符码拼出来，避免源码里出现裸实体被编辑器改写。"""
    a = chr(38)
    return (str(s).replace(a, a + 'amp;').replace(chr(60), a + 'lt;').replace(chr(62), a + 'gt;'))


def render_html(c):
    stats = {'total': c.total, 'valid': c.valid, 'failed': c.failed,
             'skipped': c.skipped, 'pending': c.pending}
    pct = 100 * stats['valid'] / stats['total'] if stats['total'] else 0
    cards = ''.join(
        '<div class="card"><b>%d</b><span>%s</span></div>' % (v, k)
        for k, v in (('总场次', stats['total']), ('有效', stats['valid']),
                     ('失败', stats['failed']), ('跳过', stats['skipped']),
                     ('待跑', stats['pending']), ('连续失败', c.consecutive_fail)))
    warns = ''.join('<div class="warn">⚠ %s</div>' % _esc(w) for w in c.warnings)
    rows = []
    for f in c.fields:
        note = ''
        if f.conflicts:
            note = '⚠ 矛盾判定: ' + '；'.join(sorted(set(f.conflicts)))
        if f.detail:
            note = (note + '  ' if note else '') + f.detail
        rows.append('<tr class="%s"><td>%d</td><td>%s</td><td class="%s">%s</td>'
                    '<td>%s</td><td>%s</td><td>%s</td></tr>'
                    % ('now' if f.status == STATUS_RUNNING else '',
                       f.seq, _esc(f.fid), style(f.status), f.status,
                       _esc(f.duration or '-'), _esc(f.recall or '-'), _esc(note)))
    return (HTML_TMPL.replace('__CID__', _esc(c.cid))
            .replace('__MODE__', _esc(c.mode))
            .replace('__START__', _esc(c.started_at or '-'))
            .replace('__UPDATED__', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            .replace('__PCT__', '%.1f' % pct)
            .replace('__CARDS__', cards)
            .replace('__WARNS__', warns)
            .replace('__ROWS__', ''.join(rows)))


# ------------------------------------------------------------------ 命令
def cmd_list(args, src):
    names = [d for d in src.list_campaigns()
             if src.read(d, 'PROGRESS.txt') or src.read(d, 'RACES.md')]
    print('campaigns 根目录: %s%s' % (src.root, '  (via ssh %s)' % src.ssh if src.ssh else ''))
    print('%-34s %-10s %-16s %s' % ('campaign', '模式', '有效/总数', '最近活动'))
    for cid in sorted(names)[-args.limit:]:
        prog = parse_progress(src.read(cid, 'PROGRESS.txt'))
        races = parse_races(src.read(cid, 'RACES.md'))
        total = len(races)
        valid = sum(1 for r in races if r[1] == STATUS_VALID)
        mode = 'schedule' if prog['schedule_file'] else 'legacy'
        text = src.read(cid, 'PROGRESS.txt') or ''
        lines = [x for x in text.splitlines() if x.strip()]
        last = lines[-1][:46] if lines else ''
        print('%-34s %-10s %-16s %s' % (cid, mode, '%d/%d' % (valid, total), last))
    return 0


def cmd_status(args, src):
    c = build_campaign(src, args.campaign, args.schedule)
    if args.json:
        print(json.dumps(campaign_json(c), ensure_ascii=False, indent=2))
        return 0
    print(render_summary(c))
    if c.warnings:
        print('\n  告警：')
        for w in c.warnings:
            print('   ⚠ %s' % w)
    if not args.plan:
        print()
        print(render_table(c))
    if args.watch:
        try:
            while True:
                time.sleep(args.interval)
                print('\033[2J\033[H', end='')
                c = build_campaign(src, args.campaign, args.schedule)
                print(render_summary(c))
                for w in c.warnings:
                    print('   ⚠ %s' % w)
                print()
                print(render_table(c))
        except KeyboardInterrupt:
            print()
    return 0


def cmd_plan(args, src):
    c = build_campaign(src, args.campaign, args.schedule)
    if not c.plan_known:
        print('待跑清单未知：campaign %s 没有可解析的排程信息' % c.cid)
        print('  （非排程模式且起跑头缺失，或排程文件不在 %s 下）' % src.root)
        for w in c.warnings:
            print('   ⚠ %s' % w)
        return 1
    print('待跑清单（%d 场，其中 %d 场尚未开始）' % (c.total, c.pending))
    for f in c.fields:
        mark = '·' if f.status == STATUS_PENDING else ('✓' if f.status == STATUS_VALID else '!')
        print('  %s %3d  %-32s %-14s %-8s %smin'
              % (mark, f.seq, f.fid, f.status, f.duration or '-', f.est or '?'))
    return 0


def cmd_html(args, src):
    c = build_campaign(src, args.campaign, args.schedule)
    html = render_html(c)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(html)
        print('已写出 %s（%d 场，有效 %d）' % (args.out, c.total, c.valid))
    else:
        sys.stdout.write(html)
    return 0


def cmd_serve(args, src):
    camp = args.campaign

    def snapshot():
        return campaign_json(build_campaign(src, camp, args.schedule))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            raw = body.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith('/api/queue'):
                self._send(json.dumps(snapshot(), ensure_ascii=False),
                           'application/json; charset=utf-8')
            else:
                self._send(render_html(build_campaign(src, camp, args.schedule)),
                           'text/html; charset=utf-8')

    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('队列实时页: http://127.0.0.1:%d/  （campaign=%s，Ctrl-C 退出）' % (args.port, camp))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser(description='raceway campaign 队列观测台（只读）')
    ap.add_argument('cmd', choices=['list', 'status', 'plan', 'html', 'serve'])
    ap.add_argument('campaign', nargs='?', default=None)
    ap.add_argument('--root', default=None, help='campaigns 目录（默认 ./campaigns）')
    ap.add_argument('--ssh', default=None, help='远端主机，如 root@203.0.113.10')
    ap.add_argument('--schedule', default=None, help='显式指定排程文件路径')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--plan', action='store_true', help='status 只显示摘要，不列全场')
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--interval', type=int, default=5)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--port', type=int, default=8801)
    args = ap.parse_args()

    root = args.root or os.path.join(os.getcwd(), 'campaigns')
    src = Source(root, args.ssh)

    if args.cmd == 'list':
        return cmd_list(args, src)
    if not args.campaign:
        ap.error('%s 需要指定 campaign 名' % args.cmd)
    if args.cmd == 'status':
        return cmd_status(args, src)
    if args.cmd == 'plan':
        return cmd_plan(args, src)
    if args.cmd == 'html':
        return cmd_html(args, src)
    if args.cmd == 'serve':
        return cmd_serve(args, src)
    return 1


if __name__ == '__main__':
    sys.exit(main())
