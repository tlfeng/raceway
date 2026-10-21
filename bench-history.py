#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-history.py —— 已入库历史覆盖（指标库 bench-run 的只读视图）

为什么需要它
------------
编排台（bench-compose）只会排新活：矩阵勾什么就排什么，**不知道哪些格已经跑过**。
指标库里躺着几百行 bench-run（三十多个 campaign、上百个 臂×赛道×轮次 组合），
用户却要在编排界面里凭记忆避开已做过的组合 —— 结果要么重复跑已经跑过的场次
（白烧几小时机器，还把轮间时序摊薄的前提搞脏），要么漏掉没跑的那一格。

本模块把指标库 bench-run 索引读成一张**覆盖表**：

    一格 = (臂 arm, 赛道 track, 轮次 round)

每格记着：跑过几遍、什么判定、哪个 build、来自哪个 campaign、什么时候。

三个必须守住的口径
------------------
1. **build 隔离**：jdk25@3009 跑过 ≠ jdk25@3015 跑过。跨 build 混线会让结论被
   误读（make_dashboard 就是为此按 arm_build 分线的）。覆盖判定必须带 build，
   否则"已跑过"会把不同引擎版本的数据当成同一格。
2. **一场次两行**：写赛/查赛各一行（phase=w / q）。一场次要算"跑过"，两个相位
   都得判 VALID；只有写赛 VALID 而查赛没跑的，是**半场**，仍需重跑。
   老 campaign 的 write-only 赛道只有一个相位，按 challenge 判定期望相位。
3. **只读**：不写 ES、不碰集群、不碰被测引擎。指标库没跑（压测窗口内这是常态）
   时抛 HistoryUnavailable，由调用方降级 —— 不猜、不伪造、不拿空数据当"没跑过"。

凭据
----
basic auth 的口令走 **stdin**（curl --config -），不出现在任何进程的 argv 里；
查询体（不含机密）走 argv。ssh 模式在服务器上 curl，本机模式直连 urllib。

用法
----
    from bench_history import fetch_coverage, plan_cells
    cov = fetch_coverage(ssh='root@203.0.113.10', auth='admin:xxx')
    cov.cells[('jdk25', 'ds-j1024', 1)].verdict     # 'VALID'
    plan_cells(cov, arms, tracks, rounds)           # 编排矩阵里哪些格该排
"""
import base64
import json
import shlex
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass, field, asdict

INDEX = 'bench-run'
DEFAULT_ES_URL = 'https://127.0.0.1:9237'

# 一次读走的行数上限（不够就再翻页）
PAGE_SIZE = 2000

# bench-run 里要取的字段（只取覆盖判定用得到的，省带宽也省内存）
_SOURCE_FIELDS = ['campaign', 'fid', 'arm', 'arm_build', 'engine', 'engine_build',
                  'build', 'track', 'round', 'status', 'quality', 'phase',
                  '@timestamp', 'caliber_warnings', 'jdk', 'dims', 'pkg',
                  'challenge']

# 判定失败的状态（与 bench-queue 的 STATUS_FAILS 对齐，只取库里真会出现的）
FAIL_STATUSES = (
    'ENGINE-FAIL', 'JDK-SWAP-FAIL', 'JDK-FIX-FAIL', 'JDK-ASSERT-FAIL',
    'COLD-START-FAIL', 'CLUSTER-NOT-READY', 'HEAP-ASSERT-FAIL',
    'CPU-ASSERT-FAIL', 'METRIC-NULL-FAIL', 'WRITE-FAIL', 'QUERY-FAIL',
    'SEG-ASSERT-FAIL',
)
SKIP_STATUSES = ('SKIP', 'DEADLINE-SKIP')

# 判定严重度（越大越坏）；一场次取所有行里最坏的那个
VERDICT_VALID = 'VALID'
VERDICT_DEGRADED = 'DEGRADED'
VERDICT_HALF = 'HALF'      # 只有一个相位跑完
VERDICT_SKIP = 'SKIP'
VERDICT_FAIL = 'FAIL'
VERDICT_UNKNOWN = 'UNKNOWN'


class HistoryUnavailable(Exception):
    """指标库不可用（没起 / 连不上 / 没权限 / 返回不是 JSON）。

    调用方**必须**降级处理：界面要显示"指标库未运行"，而不是把空结果当成
    "什么都没跑过" —— 后者会让编排台把已跑过的场次重排一遍。
    """


# ------------------------------------------------------------------ 查指标库
def es_query(ssh, body, auth=None, es_url=DEFAULT_ES_URL, path=None, timeout=90):
    """查指标库，返回解析后的 JSON。

    ssh 模式：在服务器上 curl，口令经 stdin 的 curl 配置传入（不出现在 argv）。
    本机模式：直连 urllib（自签证书 → 不校验）。
    """
    payload = json.dumps(body, ensure_ascii=False)
    url = '%s/%s' % (es_url.rstrip('/'), path or (INDEX + '/_search'))

    if ssh:
        cmd = ("curl -sk --config - -H 'Content-Type: application/json' -d %s %s"
               % (shlex.quote(payload), shlex.quote(url)))
        stdin = ''
        if auth:
            # curl 配置里的双引号/反斜杠要转义，否则口令里的特殊字符会截断配置
            safe = auth.replace('\\', '\\\\').replace('"', '\\"')
            stdin = 'user = "%s"\n' % safe
        full = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        full += ssh.split()
        full += [cmd]
        try:
            p = subprocess.run(full, input=stdin, capture_output=True, text=True,
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HistoryUnavailable('ssh 查询指标库失败: %s' % e)
        if p.returncode != 0:
            raise HistoryUnavailable('ssh 返回码 %d: %s'
                                     % (p.returncode, (p.stderr or '').strip()[:200]))
        raw = p.stdout
    else:
        req = urllib.request.Request(url, data=payload.encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        if auth:
            tok = base64.b64encode(auth.encode('utf-8')).decode('ascii')
            req.add_header('Authorization', 'Basic ' + tok)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                raw = r.read().decode('utf-8', 'replace')
        except Exception as e:                                    # noqa: BLE001
            raise HistoryUnavailable('连不上指标库 %s: %s' % (url, e))

    try:
        return json.loads(raw)
    except ValueError:
        raise HistoryUnavailable('指标库返回非 JSON（服务没起？）: %s' % raw[:200])


def fetch_rows(ssh=None, auth=None, es_url=DEFAULT_ES_URL, index=INDEX,
               timeout=90, max_rows=20000):
    """读 bench-run 全部行（精简字段）。返回 (rows, total)。

    行数超过单页就用 scroll 继续翻，翻不动就抛 HistoryUnavailable ——
    宁可报错，也不能只拿到前半截就当成"全部历史"。
    """
    body = {'size': PAGE_SIZE, 'query': {'match_all': {}}, '_source': _SOURCE_FIELDS}
    rows = []
    resp = es_query(ssh, body, auth=auth, es_url=es_url,
                    path='%s/_search' % index, timeout=timeout)
    hits = (resp.get('hits') or {})
    total = (hits.get('total') or {}).get('value', len(hits.get('hits') or []))
    for h in hits.get('hits') or []:
        rows.append(h.get('_source') or {})

    scroll_id = resp.get('_scroll_id')
    while len(rows) < total and scroll_id:
        resp = es_query(ssh, {'scroll': '2m', 'scroll_id': scroll_id},
                        auth=auth, es_url=es_url, path='_search/scroll', timeout=timeout)
        hits = (resp.get('hits') or {})
        got = hits.get('hits') or []
        if not got:
            break
        for h in got:
            rows.append(h.get('_source') or {})
        scroll_id = resp.get('_scroll_id')

    if len(rows) < total:
        raise HistoryUnavailable('指标库只读到 %d/%d 行（翻页中断）' % (len(rows), total))
    if len(rows) > max_rows:
        raise HistoryUnavailable('指标库有 %d 行，超过上限 %d' % (len(rows), max_rows))
    return rows, total


# ------------------------------------------------------------------ 覆盖模型
def _expected_phases(row):
    """一场次该有哪些相位。write-only 赛道只有写赛。"""
    if (row.get('challenge') or '').strip() == 'write-only':
        return {'w'}
    return {'w', 'q'}


def _verdict_of(rows):
    """把一场次（若干行）合并成一个判定。"""
    statuses = [(r.get('status') or '') for r in rows]
    if any(s in FAIL_STATUSES for s in statuses):
        return VERDICT_FAIL
    if any(s in SKIP_STATUSES for s in statuses):
        return VERDICT_SKIP
    if not any(s == 'VALID' for s in statuses):
        return VERDICT_UNKNOWN
    phases = set(r.get('phase') or '' for r in rows)
    expected = set()
    for r in rows:
        expected |= _expected_phases(r)
    if expected and not expected.issubset(phases):
        return VERDICT_HALF
    if any((r.get('quality') or '') == 'degraded' for r in rows):
        return VERDICT_DEGRADED
    return VERDICT_VALID


@dataclass
class Cell:
    """一个 (臂, 赛道, 轮次) 格的覆盖情况。

    注意：同一格可能有**多个 build** 的记录 —— ds3 跑的是 3009（有些场次
    DEADLINE-SKIP），ds2 拿 3015 重跑（成功）。所以判定必须**按 build 分开**：
    对 3015 而言这一格是"跑过"，对 3009 而言是"没跑过"。把两个 build 的行
    混在一起取最坏判定，会把后来成功的重跑也一起抹掉。
    """
    arm: str
    track: str
    round: int
    rows: list = field(default_factory=list)

    def add(self, row):
        self.rows.append(row)

    @property
    def key(self):
        return (self.arm, self.track, self.round)

    @property
    def builds(self):
        return sorted({(r.get('arm_build') or r.get('arm') or '') for r in self.rows})

    def rows_for(self, arm_build):
        """某个 build 上的行。没有 build 号的臂按臂名匹配。"""
        return [r for r in self.rows
                if (r.get('arm_build') or r.get('arm') or '') == arm_build]

    def verdict_for(self, arm_build):
        """该 build 上的判定（最坏）。"""
        rows = self.rows_for(arm_build)
        if not rows:
            return VERDICT_UNKNOWN
        return _verdict_of(rows)

    def covered_for(self, arm_build):
        """该 build 上能不能算"已经跑过、结论可用"。"""
        return self.verdict_for(arm_build) in (VERDICT_VALID, VERDICT_DEGRADED)

    @property
    def verdict(self):
        """整格的判定（跨 build 取最坏，仅用于展示，不代表覆盖判定）。"""
        return _verdict_of(self.rows)

    @property
    def covered(self):
        """整格是否至少有一个 build 跑成过。覆盖判定请用 covered_for()。"""
        return self.verdict in (VERDICT_VALID, VERDICT_DEGRADED)

    @property
    def campaigns(self):
        return sorted({(r.get('campaign') or '') for r in self.rows})

    @property
    def phases(self):
        return sorted({(r.get('phase') or '?') for r in self.rows})

    @property
    def last_ts(self):
        ts = [r.get('@timestamp') or '' for r in self.rows if r.get('@timestamp')]
        return max(ts) if ts else ''

    @property
    def warnings(self):
        out = []
        for r in self.rows:
            for w in (r.get('caliber_warnings') or []):
                if w not in out:
                    out.append(w)
        return out

    def per_build(self):
        """每个 build 上的判定 + campaign，供界面展示。"""
        out = {}
        for b in self.builds:
            rows = self.rows_for(b)
            out[b] = {
                'verdict': _verdict_of(rows),
                'runs': len(rows),
                'phases': sorted({(r.get('phase') or '?') for r in rows}),
                'campaigns': sorted({(r.get('campaign') or '') for r in rows}),
                'quality': sorted({(r.get('quality') or '') for r in rows}),
            }
        return out

    def to_dict(self):
        d = asdict(self)
        d.update({
            'verdict': self.verdict,
            'covered': self.covered,
            'builds': self.builds,
            'campaigns': self.campaigns,
            'phases': self.phases,
            'last_ts': self.last_ts,
            'warnings': self.warnings,
            'runs': len(self.rows),
            'per_build': self.per_build(),
        })
        d.pop('rows', None)
        return d


@dataclass
class Coverage:
    """覆盖表。"""
    cells: dict = field(default_factory=dict)       # (arm, track, round) -> Cell
    total_rows: int = 0
    campaigns: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    arms: list = field(default_factory=list)
    available: bool = True
    error: str = ''

    def get(self, arm, track, rnd):
        return self.cells.get((arm, track, rnd))

    def summary(self):
        by_verdict = {}
        for c in self.cells.values():
            by_verdict[c.verdict] = by_verdict.get(c.verdict, 0) + 1
        return {
            'rows': self.total_rows,
            'races': len(self.cells),
            'campaigns': len(self.campaigns),
            'tracks': len(self.tracks),
            'arms': len(self.arms),
            'by_verdict': by_verdict,
        }

    def to_dict(self):
        return {
            'available': self.available,
            'error': self.error,
            'summary': self.summary(),
            'campaigns': self.campaigns,
            'tracks': self.tracks,
            'arms': self.arms,
            'cells': {'|'.join(str(x) for x in k): c.to_dict()
                      for k, c in self.cells.items()},
        }


def build_coverage(rows, total=None):
    """把 bench-run 的行折叠成覆盖表。"""
    cov = Coverage(total_rows=total if total is not None else len(rows))
    camps, tracks, arms = set(), set(), set()
    for r in rows:
        arm = r.get('arm') or ''
        track = r.get('track') or ''
        rnd = r.get('round')
        if rnd is None:
            rnd = 0
        try:
            rnd = int(rnd)
        except (TypeError, ValueError):
            rnd = 0
        if not arm or not track:
            continue
        key = (arm, track, rnd)
        cell = cov.cells.get(key)
        if cell is None:
            cell = Cell(arm=arm, track=track, round=rnd)
            cov.cells[key] = cell
        cell.add(r)
        camps.add(r.get('campaign') or '')
        tracks.add(track)
        arms.add(arm)
    cov.campaigns = sorted(c for c in camps if c)
    cov.tracks = sorted(tracks)
    cov.arms = sorted(arms)
    return cov


def fetch_coverage(ssh=None, auth=None, es_url=DEFAULT_ES_URL, index=INDEX,
                   timeout=90):
    """读指标库 → 覆盖表。指标库不可用时返回 available=False 的空表（不抛）。

    返回空表而不是抛异常，是因为编排台必须在指标库停着时照常工作
    （压测窗口内指标库本来就是停的）—— 但界面要明确显示"指标库未运行"。
    """
    try:
        rows, total = fetch_rows(ssh=ssh, auth=auth, es_url=es_url,
                                  index=index, timeout=timeout)
    except HistoryUnavailable as e:
        cov = Coverage(available=False, error=str(e))
        return cov
    cov = build_coverage(rows, total)
    return cov


# ------------------------------------------------------------------ 编排用
def expected_arm_build(arm, build):
    """该臂在当前 build 下的 arm_build 标识。

    easysearch 臂的 arm_build 是 `arm@build`（如 jdk25@3015）；
    elasticsearch 臂没有 build 号，arm_build 就是臂名本身（如 es819）。
    """
    if not build:
        return arm
    return '%s@%s' % (arm, build)


def plan_cells(coverage, arms, tracks, rounds, build_of=None, include_covered=False):
    """算出编排矩阵里每一格该不该排。

    arms: [{'name':..., 'build':...}, ...] 或 [(name, build), ...]
    build_of: {臂名: build}，缺省视为不区分 build
    include_covered=True 时不做任何剔除（用户明确要重跑）

    返回 {'cells': [...], 'to_run': [...], 'skipped': [...], 'notes': [...]}
    每格：{arm, track, round, covered, same_build, verdict, builds, campaigns,
           reason}
    """
    build_of = build_of or {}
    cells = []
    for arm_spec in arms:
        # 臂的描述有三种形态：dict、Arm 命名元组、(name, build) 二元组。
        # 注意别把命名元组当二元组解包 —— 第二个字段是 label，不是 build。
        if isinstance(arm_spec, dict):
            aname = arm_spec.get('name') or ''
            build = arm_spec.get('build') or ''
        elif hasattr(arm_spec, 'build'):
            aname = getattr(arm_spec, 'name', '')
            build = getattr(arm_spec, 'build', '') or ''
        else:
            aname = arm_spec[0]
            build = arm_spec[1] if len(arm_spec) > 1 else ''
        build = build or build_of.get(aname) or ''
        want = expected_arm_build(aname, build)
        for tk in tracks:
            for rnd in range(1, int(rounds) + 1):
                cell = coverage.get(aname, tk, rnd) if coverage.available else None
                info = {
                    'arm': aname, 'track': tk, 'round': rnd,
                    'covered': False, 'same_build': False,
                    'verdict': '', 'verdict_same_build': '',
                    'builds': [], 'campaigns': [], 'runs': 0,
                    'reason': '',
                }
                if cell is not None:
                    same = want in cell.builds
                    info.update({
                        'verdict': cell.verdict,               # 跨 build 展示用
                        'verdict_same_build': cell.verdict_for(want),
                        'builds': cell.builds,
                        'campaigns': cell.campaigns,
                        'runs': len(cell.rows),
                        'same_build': same,
                        # 覆盖判定按 build 隔离：别的 build 上跑过不算这一格跑过
                        'covered': cell.covered_for(want),
                    })
                if include_covered:
                    info['reason'] = '（含已跑：按要求全部重跑）'
                    info['to_run'] = True
                elif info['covered']:
                    camps = info['campaigns']
                    shown = ','.join(camps[:3])
                    if len(camps) > 3:
                        shown += '…共%d个campaign' % len(camps)
                    info['reason'] = '已跑过（同 build %s，%s）' % (want, shown or '?')
                    info['to_run'] = False
                elif cell is not None and not info['same_build']:
                    per = cell.per_build()
                    detail = '，'.join('%s=%s' % (b, per[b]['verdict'])
                                       for b in info['builds'])
                    if not build:
                        # 臂没带 build 信息 → 无从判断是否同 build。这种情况
                        # 必须说出来，不能当成"已跑过"也不能当成"没跑过"。
                        info['reason'] = ('有记录（%s）但该臂没有 build 信息，'
                                          '无法判定是否同一 build → 需人工确认' % detail)
                    else:
                        info['reason'] = '只有别的 build 跑过（%s），当前 build %s 仍需跑' % (
                            detail, want)
                    info['to_run'] = True
                elif cell is not None:
                    info['reason'] = '有记录但判定 %s，不算跑过，需重跑' % (
                        info['verdict_same_build'] or info['verdict'] or '?')
                    info['to_run'] = True
                else:
                    info['reason'] = '未跑过'
                    info['to_run'] = True
                cells.append(info)
    return {
        'cells': cells,
        'to_run': [c for c in cells if c['to_run']],
        'skipped': [c for c in cells if not c['to_run']],
    }


def filter_rows(plan, rows):
    """按编排计划剔除已覆盖的格。rows 是 (臂, 赛道, 轮次) 序列。"""
    skip = set((c['arm'], c['track'], c['round']) for c in plan['skipped'])
    return [r for r in rows if (r[0], r[1], r[2]) not in skip]


def if_empty(coverage):
    """指标库不可用时的占位覆盖表（界面据此显示"未连接"而不是"全空"）。"""
    return Coverage(available=False, error=coverage.error if coverage else '未配置指标库')
