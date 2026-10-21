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

DEFAULT_ROOT = '/opt/raceway'
DEFAULT_WHAT = 'ds'
# 与 run-bench.sh:117 的 ds) 分支保持同步
DS_TIERS = ['ds-g1920', 'ds-j2048', 'ds-j1024', 'ds-g960', 'ds-j768', 'ds-j512', 'ds-j1536']
DEFAULT_EST = 45

Arm = namedtuple('Arm', 'name label engine jdk_dir config')


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
                        engine=kv.get('ENGINE_NAME', ''), jdk_dir=kv.get('JDK_DIR', ''),
                        config=config))
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

def build_rows(args, arms):
    tracks = [t.strip() for t in args.tracks.split(',') if t.strip()]
    if not tracks:
        raise SystemExit('--tracks 不能为空（如 ds-g1920,ds-j1024）')
    rows = interleave(arms, tracks, args.rounds)
    est_map = parse_est_overrides(args.est)
    return rows, est_map


def compose_all(args):
    """arms 解析 + 校验 + 渲染。返回 (arms, schedule_text, arms_text, rows)。"""
    arms = parse_arms(read_arms_file(args))
    if not arms:
        raise SystemExit('臂定义里没有可用臂: %s' % args.arms_file)
    problems = validate_arms(arms)
    hard = [m for sev, m in problems if sev == 'error']
    if hard:
        raise SystemExit('臂定义校验失败：\n  ' + '\n  '.join(hard))
    rows, est_map = build_rows(args, arms)
    tag = args.tag or ('compose-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    sched = render_schedule(rows, arms, tag, est_map, args.est_default, recall=args.recall)
    # round-trip 自校验：渲染出来的文件按 runner 规则读回去必须等于生成序
    back = [(a, t, r) for a, t, r, _, _ in schedule_rows_from_text(sched)]
    if back != rows:
        raise SystemExit('内部错误：渲染结果与生成序不一致（round-trip 失败）')
    return arms, sched, render_arms(arms, tag), rows, tag


def cmd_arms(args):
    arms = parse_arms(read_arms_file(args))
    for a in arms:
        print('%-8s label=%-10s engine=%-14s jdk_dir=%s' % (a.name, a.label, a.engine, a.jdk_dir or '-'))
    for sev, msg in validate_arms(arms):
        print('[%s] %s' % (sev, msg), file=sys.stderr)


def cmd_gen(args):
    arms, sched, _, rows, _ = compose_all(args)
    sys.stdout.write(sched)


def cmd_preview(args):
    arms, sched, arms_text, rows, tag = compose_all(args)
    upload(args, tag, sched, arms_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours)
    print('== 排程文件（tag=%s，%d 场）==' % (tag, len(rows)))
    sys.stdout.write(sched)
    print('== run-bench.sh --dry-run（rc=%d）==' % rc)
    sys.stdout.write(out)
    if rc != 0:
        raise SystemExit(2)


def cmd_start(args):
    if not args.yes:
        raise SystemExit('起跑需显式确认：加 --yes（先 preview 检查过 dry-run 输出）')
    arms, sched, arms_text, rows, tag = compose_all(args)
    why = remote_mutex_check(args)
    if why:
        raise SystemExit(why)
    upload(args, tag, sched, arms_text)
    rc, out = remote_dry_run(args, tag, args.ds_hours)
    if rc != 0:
        print(out)
        raise SystemExit('dry-run 未通过，拒绝起跑（rc=%d）' % rc)
    camp, log = remote_start(args, tag, args.ds_hours, args.camp_prefix)
    print('已起跑 tag=%s（%d 场）' % (tag, len(rows)))
    print('campaign: %s' % camp)
    print('日志: %s@%s:%s' % (args.ssh, args.root, log))
    print('观测: python3 bench-queue.py status %s --root %s --ssh %s' % (camp, args.root, args.ssh))


# ============================================================ serve（本地 Web 界面）

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>raceway 排程编排台</title>
<style>
 body{font:14px/1.6 -apple-system,'PingFang SC',sans-serif;margin:24px;max-width:1100px;color:#222}
 h2{border-bottom:2px solid #3a7;padding-bottom:4px}
 table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 10px;text-align:left}
 .tracks label{margin-right:12px;white-space:nowrap}
 button{padding:6px 18px;margin-right:10px}
 pre{background:#f6f6f6;border:1px solid #ddd;padding:10px;max-height:340px;overflow:auto}
 .err{color:#b00}.ok{color:#080}
 fieldset{border:1px solid #bbb;margin-bottom:14px}
</style></head><body>
<h1>raceway 排程编排台（compose）</h1>
<p>矩阵勾选 → 生成排程 → dry-run 预览（runner 自己的语义）→ 互斥预检后起跑。
交错由服务端复刻 <code>order_for_round</code>；本页不做任何绕过 precheck 的事。</p>

<fieldset><legend>臂（来自臂定义文件）</legend><div id="arms" class="tracks"></div></fieldset>
<fieldset><legend>赛道（ds-* 档位）</legend><div id="tracks" class="tracks"></div>
 自定义（逗号分隔）<input id="custom" size="30" placeholder="ds-hv1024,..."></fieldset>
<fieldset><legend>轮次与口径</legend>
 轮次 <input id="rounds" type="number" value="1" min="1" max="9" style="width:4em">
 探针 <select id="recall"><option value="recall">recall（带召回探针）</option>
   <option value="norecall">norecall</option></select>
 默认 est <input id="estd" type="number" value="45" min="5" style="width:5em"> 分钟
 DS_HOURS <input id="dsh" type="number" value="12" min="0" style="width:5em">（0=不启用窗底守卫）
 tag <input id="tag" size="22" placeholder="留空自动生成"></fieldset>

<button onclick="doRender()">生成排程</button>
<button onclick="doPreview()">上传 + dry-run 预览</button>
<button onclick="doStart()">互斥预检并起跑</button>
<span id="msg"></span>

<h2>排程</h2><pre id="sched">（未生成）</pre>
<h2>dry-run 输出 / 起跑结果</h2><pre id="out">（空）</pre>

<script>
const DS_TIERS = %s;
let ARMS = [];
async function boot(){
  const r = await fetch('/api/bootstrap'); const j = await r.json();
  ARMS = j.arms;
  document.getElementById('arms').innerHTML = ARMS.map(a =>
    `<label><input type="checkbox" class="armck" value="${a.name}" checked> ${a.name} <small>(${a.engine}${a.jdk_dir? ' + '+a.jdk_dir.split('/').pop():''})</small></label>`).join('');
  document.getElementById('tracks').innerHTML = DS_TIERS.map(t =>
    `<label><input type="checkbox" class="tkck" value="${t}" checked> ${t.replace('ds-','')}</label>`).join('');
}
function matrix(){
  const arms=[...document.querySelectorAll('.armck:checked')].map(e=>e.value);
  const tracks=[...document.querySelectorAll('.tkck:checked')].map(e=>e.value);
  const custom=document.getElementById('custom').value.trim();
  if(custom) custom.split(',').forEach(t=>{t=t.trim(); if(t&&!tracks.includes(t))tracks.push(t);});
  return {arms, tracks, rounds:+document.getElementById('rounds').value,
          recall:document.getElementById('recall').value,
          est_default:+document.getElementById('estd').value,
          ds_hours:+document.getElementById('dsh').value,
          tag:document.getElementById('tag').value.trim()};
}
async function post(path){ const r=await fetch(path,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(matrix())});
  return {status:r.status, j:await r.json()}; }
function show(el,text){document.getElementById(el).textContent=text;}
async function doRender(){ const {status,j}=await post('/api/render');
  show('sched', status==200? j.schedule : '错误: '+j.error); setMsg(status==200,'生成 '+j.rows+' 场');}
async function doPreview(){ setMsg(true,'dry-run 运行中…'); const {status,j}=await post('/api/preview');
  if(status!=200){show('out','错误: '+j.error);setMsg(false,'dry-run 失败');return;}
  show('sched',j.schedule); show('out',j.dryrun); setMsg(j.rc==0, j.rc==0?'dry-run 通过，可起跑':'dry-run 非零，勿起跑');}
async function doStart(){ if(!confirm('确认起跑？会占用压测集群。'))return;
  setMsg(true,'校验并起跑…'); const {status,j}=await post('/api/start');
  if(status!=200){show('out','错误: '+j.error);setMsg(false,'未起跑');return;}
  show('out',j.result); setMsg(true,'已起跑');}
function setMsg(ok,text){const m=document.getElementById('msg');
  m.className=ok?'ok':'err'; m.textContent=' '+text;}
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
    rows = interleave(arms, body['tracks'], max(1, int(body['rounds'])))
    tag = body.get('tag') or ('compose-%s' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    sched = render_schedule(rows, arms, tag, {}, int(body.get('est_default') or DEFAULT_EST),
                            recall=(body.get('recall') != 'norecall'))
    st.last = {'arms': arms, 'schedule': sched, 'tag': tag}
    return 200, {'schedule': sched, 'rows': len(rows)}


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
                'problems': st.problems, 'ds_tiers': DS_TIERS}, ensure_ascii=False))
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
    ap.add_argument('--tracks', default='ds-g1920,ds-j2048,ds-j1024,ds-g960,ds-j768,ds-j512,ds-j1536')
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--recall', dest='recall', action='store_true', default=True)
    ap.add_argument('--norecall', dest='recall', action='store_false')
    ap.add_argument('--est', action='append', default=[], help='赛道=est分钟，可重复')
    ap.add_argument('--est-default', type=int, default=DEFAULT_EST)
    ap.add_argument('--tag', default=None)
    ap.add_argument('--what', default=DEFAULT_WHAT, help='runner 第一参数（排程模式默认 ds）')
    ap.add_argument('--ds-hours', type=int, default=0)
    ap.add_argument('--camp-prefix', default=None)
    ap.add_argument('--root', default=DEFAULT_ROOT, help='远端 raceway 目录')
    ap.add_argument('--ssh', default=None, help='user@host；缺省操作本地')
    ap.add_argument('--port', type=int, default=8802)
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args(argv)

    if args.arms_file is None:
        if args.ssh:
            args.arms_file = posixpath.join(args.root, 'schedules', 'dimsweep-arms.conf')
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            args.arms_file = os.path.join(here, 'schedules', 'dimsweep-arms.conf')

    {'arms': cmd_arms, 'gen': cmd_gen, 'preview': cmd_preview,
     'start': cmd_start, 'serve': cmd_serve}[args.cmd](args)


if __name__ == '__main__':
    main()
