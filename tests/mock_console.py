#!/usr/bin/env python3
"""raceway 控制台 UI 迭代用 mock 服务：stub 全部 /api + 静态 web/。

用途：不改压测机、不连远端/指标库，就能在浏览器里跑通整个控制台（开发/演示/截图）。
放在 tests/ 是刻意的：README 的同步命令只拷顶层 *.py 与 web/，tests/ 不上压测机。

用法：python3 tests/mock_console.py [端口]   # 缺省 8899，监听 127.0.0.1
数据全是假货：接口名有三方契约把守（tests/test_web_api_contract.py），但**返回字段是
手工对齐的** —— bench-compose.py 改了响应字段名时 mock 不会自动感知，对着 mock 开发完
记得上真机验一遍；形状漂移多了就把真实响应（脱敏后）固化成夹具替换手写数据。
monitor 是随机游走的活数据，方便看曲线交互。仅开发用，勿部署到压测机。
"""
import json, random, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import os

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
ECHARTS = os.path.join(os.path.dirname(WEB), 'echarts.min.js')

random.seed(20261116)

CARS = [
    {'name': 'ez2510-jdk21', 'engine': 'easysearch', 'build': '3010', 'jdk_dir': '/opt/jdks/jdk21', 'label': 'ez2510'},
    {'name': 'ez2510-kona25', 'engine': 'easysearch', 'build': '3010', 'jdk_dir': '/opt/jdks/kona25', 'label': 'kona25'},
    {'name': 'es819', 'engine': 'elasticsearch', 'build': '', 'jdk_dir': '', 'label': 'es819'},
]
GROUPS = [['base', '基座 / 官方语料'], ['vector', '向量检索'], ['ds', '维度扫描（多档位）'], ['write', '写入与更新']]
TRACKS = [
    {'name': 'geonames', 'label': 'geonames 基座', 'est': 38, 'group': 'base'},
    {'name': 'nested', 'label': 'nested 文档', 'est': 40, 'group': 'base'},
    {'name': 'hnsw', 'label': '96 维 200 万向量', 'est': 55, 'group': 'vector', 'dataset': 'hnsw2m'},
    {'name': 'hv1024', 'label': '1024 维向量', 'est': 62, 'group': 'vector', 'dataset': 'hv1024'},
    {'name': 'ds-g1920', 'label': '维度扫描 g1920', 'est': 52, 'group': 'ds', 'dataset': 'jina 系'},
    {'name': 'ds-g768', 'label': '维度扫描 g768', 'est': 48, 'group': 'ds', 'dataset': 'jina 系'},
    {'name': 'ds-gist960', 'label': '维度扫描 gist960', 'est': 58, 'group': 'ds', 'dataset': 'GIST 系'},
    {'name': 'upsert', 'label': 'geonames upsert', 'est': 45, 'group': 'write'},
    {'name': 'codec-default', 'label': 'default 压缩', 'est': 36, 'group': 'base', 'base': 'geonames',
     'variant': True, 'compression': 'default', 'source_reuse': False},
    {'name': 'codec-best_sr', 'label': 'best_compression + sr', 'est': 41, 'group': 'base', 'base': 'geonames',
     'variant': True, 'compression': 'best_compression', 'source_reuse': True, 'engines': 'easysearch'},
]
TOKENS = ['default', 'best', 'zstd']
TOKEN_LABEL = {'default': 'default', 'best': 'best_compression', 'zstd': 'zstd'}
TOKEN_CODEC = {'default': 'default', 'best': 'best_compression', 'zstd': 'zstd'}
FIDS = ['r1-geonames-ez2510-jdk21', 'r1-geonames-ez2510-kona25', 'r1-geonames-es819',
        'r2-geonames-ez2510-jdk21', 'r2-geonames-ez2510-kona25', 'r2-geonames-es819']

PHASES = ['precheck', 'deploy', 'cold-start', 'bulk-index', 'search', 'sample', 'archive']
PROG_LINES = [
    '[00:01:12] campaign jdk3-20261115-093000 开始 · 6 场 · 窗口 12h',
    '[00:01:13] [1/6] r1-geonames-ez2510-jdk21 排程行载入',
    '[00:01:14] 卡点 precheck：机器安静 ✓ steal=0.1% ✓ 集群就绪 ✓ 堆/绑核 ✓',
    '[00:02:05] 部署 easysearch 2.5.0@3010（3 节点）… ibs install',
    '[00:06:40] 冷启动完成，集群 green',
    '[00:07:02] bulk-index：geonames 语料 2.1M docs · 5.4MB/s',
    '[00:18:33] bulk 完成 · 98ms p50',
    '[00:19:00] search 阶段：rally challenge search-default',
    '[00:26:41] race 有效 · 指标落档',
    '[00:27:02] [2/6] r1-geonames-ez2510-kona25 排程行载入',
    '[00:27:30] 卡点 precheck：机器安静 ✓',
]

def queue_payload():
    now = time.time()
    fields = []
    st = ['VALID', 'RUNNING', 'PENDING', 'PENDING', 'PENDING', 'PENDING']
    dur = ['16m 24s', '', '', '', '', '']
    for i, fid in enumerate(FIDS):
        f = {'seq': i + 1, 'fid': fid, 'status': st[i], 'recall': 'recall' if i % 2 == 0 else 'norecall',
             'duration': dur[i], 'detail': ''}
        if st[i] == 'RUNNING':
            f['phase'] = 'search'
            f['elapsed_min'] = 8.4
        if st[i] == 'VALID':
            f['env_verdict'] = 'OK'
        if st[i] == 'RUNNING':
            f['env_verdict'] = 'OK'
        fields.append(f)
    return {'campaign': 'jdk3-20261115-093000', 'ended': False, 'started_at': '09:30:00',
            'rounds': 2, 'tracks': ['geonames'],
            'stats': {'total': 6, 'valid': 1, 'failed': 0, 'skipped': 0, 'pending': 4,
                      'est_remaining_min': 96},
            'fields': fields, 'warnings': []}

MON_STATE = {}

def monitor_payload(fid):
    st = MON_STATE.setdefault(fid, [30.0, 55.0, 120.0, 2.0])
    st[0] = max(3, min(95, st[0] + random.uniform(-6, 6)))
    st[1] = max(10, min(90, st[1] + random.uniform(-8, 8)))
    st[2] = max(2, min(64, st[2] + random.uniform(-4, 4)))
    st[3] = max(0.3, min(30, st[3] + random.uniform(-1.5, 1.5)))
    rows = []
    base = int(time.time()) - 600
    for i in range(60):
        w = (i / 59) * 0.35
        rows.append({'ts': base + i * 10,
                     'cpu_usr': round(st[0] * (1 + w) + random.uniform(-3, 3), 1),
                     'cpu_steal': round(max(0, st[0] * 0.02 + random.uniform(0, .8)), 2),
                     'bench_cpu': round(st[1] * (1 + w * .5) + random.uniform(-4, 4), 1),
                     'load1': round(st[2] * (1 + w * .3) / 20 + random.uniform(-.3, .3), 2),
                     'foreign_cpu': round(max(0, 4 + random.uniform(-2, 4)), 1)})
    return {'interval': 5,
            'series': [{'node': 'node-1', 'header': ['ts', 'cpu_usr', 'cpu_sys', 'cpu_steal', 'load1', 'bench_cpu', 'foreign_cpu'], 'rows': rows},
                       {'node': 'node-2', 'header': ['ts', 'cpu_usr', 'cpu_sys', 'cpu_steal', 'load1', 'bench_cpu', 'foreign_cpu'], 'rows': rows}]}

def est_map_from(est_text, base):
    m = dict(base)
    for p in (est_text or '').split(','):
        p = p.strip()
        if '=' in p and p.split('=')[1].strip().isdigit():
            m[p.split('=')[0].strip()] = int(p.split('=')[1])
    return m

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code, obj):
        self._send(code, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == '/' or p == '/index.html':
            with open(WEB + '/index.html', encoding='utf-8') as f:
                return self._send(200, 'text/html; charset=utf-8', f.read())
        if p.startswith('/web/'):
            try:
                with open(WEB + unquote(p[len('/web'):]), encoding='utf-8') as f:
                    return self._send(200, 'application/javascript; charset=utf-8' if p.endswith('.js') else 'text/css; charset=utf-8', f.read())
            except OSError:
                return self._send(404, 'text/plain', 'missing')
        if p == '/echarts.min.js':
            with open(ECHARTS, encoding='utf-8') as f:
                return self._send(200, 'application/javascript; charset=utf-8', f.read())
        q = parse_qs(u.query)
        if p == '/api/assets':
            return self._json(200, {'available': True, 'pkg_dir': '/opt/raceway/pkg',
                                    'deps': [
                                        {'car': 'jdk25', 'engine': 'easysearch', 'ver': '2.5.0',
                                         'build': '3015', 'pkg': '/opt/raceway/pkg/easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz',
                                         'pkg_stat': {'exists': True, 'size': 734003200, 'mtime': 1762500000},
                                         'jdk_mapped': False, 'jdk_pkg': None},
                                        {'car': 'jdk26', 'engine': 'easysearch', 'ver': '2.5.0',
                                         'build': '3015', 'pkg': '/opt/raceway/pkg/easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz',
                                         'pkg_stat': {'exists': True, 'size': 734003200, 'mtime': 1762500000},
                                         'jdk_pkg': '/opt/raceway/pkg/openjdk-26_linux-x64_bin.tar.gz', 'jdk_mapped': True,
                                         'jdk_stat': {'exists': True, 'size': 209715200, 'mtime': 1750000000}}],
                                    'pkg_files': [
                                        {'name': 'easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz', 'size': 734003200, 'mtime': 1762500000, 'exists': True},
                                        {'name': 'openjdk-26_linux-x64_bin.tar.gz', 'size': 209715200, 'mtime': 1750000000, 'exists': True},
                                        {'name': 'agent-patched-1.18.15.jar', 'size': 1048576, 'mtime': 1740000000, 'exists': True}],
                                    'bundles': [{'key': 'BUNDLE_EZ3015', 'path': '/opt/raceway/pkg/easysearch-2.5.0-3015-linux-amd64-bundle.tar.gz',
                                                 'exists': True, 'size': 734003200, 'mtime': 1762500000}]})
        if p == '/api/cluster-status':
            return self._json(200, {'available': True, 'driver': 'esrally-185',
                                    'engine': ['2.5.0-3015'], 'up': False,
                                    'nodes': [{'name': 'easysearch-0001', 'ip': '10.10.10.224',
                                               'ver': '2.5.0-3015', 'port9200': True},
                                              {'name': 'easysearch-0002', 'ip': '10.10.10.225',
                                               'ver': '2.5.0-3015', 'port9200': True},
                                              {'name': 'easysearch-0003', 'ip': '10.10.10.226',
                                               'ver': '2.5.0-3015', 'port9200': False}]})
        if p == '/api/mutex':
            return self._json(200, {'free': True, 'why': None})
        if p == '/api/bootstrap':
            return self._json(200, {
                'cars': CARS, 'problems': [], 'ds_tiers': [], 'tracks': TRACKS,
                'track_groups': GROUPS, 'base_tracks': ['geonames', 'nested'],
                'codec_tokens': TOKENS, 'token_label': TOKEN_LABEL, 'token_codec': TOKEN_CODEC,
                'ssh': '', 'root': '/opt/raceway（本地 mock）', 'cars_file': 'cars.conf',
                'kibana_url': 'http://127.0.0.1:5601', 'csrf_token': 'mock-csrf',
                'results_root': 'campaigns-local',
                'ext_links': [{'name': 'Kibana 看板', 'url': 'http://127.0.0.1:5601'},
                              {'name': 'LogPilot', 'url': 'http://127.0.0.1:8802'}],
                'apply_defaults': {'es': 'https://127.0.0.1:9237', 'insecure': '1', 'drop': '0'},
                'apply_auth_configured': False, 'console_conf': False})
        if p == '/api/history':
            cells = {}
            for car in ('ez2510-jdk21', 'ez2510-kona25'):
                for tr, r in (('geonames', 1), ('nested', 1), ('upsert', 1)):
                    cells['%s|%s|%d' % (car, tr, r)] = {
                        'per_build': {(car + '@3010') if 'ez' in car else car: {
                            'verdict': 'VALID', 'campaigns': ['jdk3-20261101-083000']}},
                        'builds': [car + '@3010']}
            return self._json(200, {'available': True, 'cells': cells,
                                    'cars': ['ez2510-jdk21', 'ez2510-kona25'], 'tracks': ['geonames', 'nested', 'upsert'],
                                    'summary': {'races': 18, 'rows': 214, 'campaigns': 4, 'tracks': 6, 'cars': 3}})
        if p == '/api/campaigns':
            now = time.time()
            camps = [
                {'cid': 'jdk3-20261115-093000', 'ended': False, 'valid': 1, 'total': 6,
                 'mtime': now - 120, 'last': '[2/6] r1-geonames-ez2510-kona25 · search'},
                {'cid': 'jdk3-20261112-140500', 'ended': True, 'valid': 6, 'total': 6,
                 'mtime': now - 260000, 'last': '✔ 全部完成 6/6 有效'},
                {'cid': 'jdk3-20261108-090100', 'ended': True, 'valid': 4, 'total': 6,
                 'mtime': now - 700000, 'last': '中止：DEADLINE 达成，2 场 DEADLINE-SKIP'},
            ]
            return self._json(200, {'campaigns': camps, 'active': camps[0]['cid'], 'total': 3})
        if p == '/api/queue':
            return self._json(200, queue_payload())
        if p == '/api/logtail':
            return self._json(200, {'lines': '\n'.join(PROG_LINES[-(int((q.get('n') or ['40'])[0])):])})
        if p == '/api/monitor':
            return self._json(200, monitor_payload(q.get('fid', ['x'])[0]))
        if p == '/api/campaign-result':
            rows = []
            for i, fid in enumerate(FIDS):
                verdict = 'VALID' if i == 0 else ('RUNNING' if i == 1 else 'PENDING')
                rows.append({'fid': fid, 'track': 'geonames', 'round': (i // 3) + 1,
                             'verdict': verdict, 'metrics': [
                                 {'phase': 'wq', 'task': 'index-append',
                                  'throughput': 12840 - i * 100, 'tput_unit': 'docs/s',
                                  'lat_p50': None, 'lat_p99': None, 'lat_unit': None},
                                 {'phase': 'wq', 'task': 'knn-search-10-100',
                                  'throughput': 212 - i * 5, 'tput_unit': 'ops/s',
                                  'lat_p50': 46.9, 'lat_p99': 120.0, 'lat_unit': 'ms'}]})
            return self._json(200, {'cid': q.get('cid', [''])[0], 'rows': rows})
        if p == '/api/race-detail':
            fid = q.get('fid', ['x'])[0]
            return self._json(200, {
                'plan': ['r1\tgeonames\tez2510-jdk21\t1\trecall\t38'],
                'races': ['attempt-1  VALID  precheck OK  bulk 412s  search 380s'],
                'metrics': [
                    {'phase': 'index', 'task': 'bulk-index', 'throughput': 12840, 'latency': 98.2, 'unit': 'docs/s'},
                    {'phase': 'search', 'task': 'default', 'throughput': 212.6, 'latency': 46.9, 'unit': 'ops/s'},
                    {'phase': 'search', 'task': 'term', 'throughput': 340.1, 'latency': 28.3, 'unit': 'ops/s'}],
                'artifacts': ['races/%s/race.json' % fid, 'races/%s/rally.log' % fid],
                'progress': PROG_LINES})
        if p == '/api/logfile':
            lines = PROG_LINES * 5
            qq = (q.get('q') or [''])[0]
            if qq:
                lines = [l for l in lines if qq.lower() in l.lower()]
            return self._json(200, {'lines': lines[-int((q.get('n') or ['200'])[0]):], 'total': len(lines)})
        if p == '/api/sidecar':
            return self._json(200, {'sidecars': [
                {'phase': 'search', 'file': 'sidecars/search.json',
                 'data': {'engine': 'easysearch 2.5.0', 'build': '3010', 'jdk': 'kona 25.0.1',
                          'nodes': 3, 'heap': '31g', 'cpu_affinity': '0-7', 'corpus': 'geonames 2.1M'}}]})
        if p == '/api/est-suggest':
            t = (q.get('track') or [''])[0]
            return self._json(200, {'available': t in ('geonames', 'nested', 'upsert'),
                                    'suggest': {'geonames': 38, 'nested': 40, 'upsert': 45}.get(t)})
        if p == '/api/exports':
            return self._json(200, {'exports': [{'tag': 'exp-20261115', 'assertions': 'pass', 'counts': {'bench_metric': 214}}]})
        if p == '/api/report':
            cid = q.get('cid', [''])[0]
            txt = '# raceway 报告 %s\n\n| 场次 | 状态 |\n|---|---|\n' % cid
            for f in queue_payload()['fields']:
                txt += '| %s | %s |\n' % (f['fid'], f['status'])
            return self._json(200, {'cid': cid, 'format': q.get('fmt', ['md'])[0], 'text': txt})
        if p == '/api/track-files':
            return self._json(200, {'files': [
                {'path': 'geonames/index.json', 'size': 2411}, {'path': 'geonames/challenges/default.json', 'size': 822},
                {'path': 'hnsw/index.json', 'size': 1908}, {'path': 'hv1024/index.json', 'size': 2250},
                {'path': 'nested/index.json', 'size': 1304}]})
        if p == '/api/track-file':
            return self._json(200, {'path': q.get('path', [''])[0], 'content': '{\n  "settings": {\n    "index.codec": "default"\n  },\n  "documents": []\n}\n',
                                    'sha256': 'a' * 64})
        if p == '/api/trend':
            fam = (q.get('family') or ['throughput'])[0]
            series = []
            for i, car in enumerate(['ez2510-jdk21@3010', 'ez2510-kona25@3010']):
                pts = []
                base = 200 + i * 12
                for wk in range(6):
                    pts.append({'t': 'W%d' % (wk + 1), 'v': round(base + wk * 6 + random.uniform(-8, 8), 1), 'campaign': 'jdk3-2026110%d-090000' % (wk + 1)})
                series.append({'key': car, 'points': pts})
            return self._json(200, {'available': True, 'n': 12, 'unit': 'ops/s', 'direction': 'higher-better', 'series': series})
        return self._json(404, {'error': 'not found: ' + p})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            body = {}
        p = u.path
        if p in ('/api/render', '/api/preview', '/api/start'):
            cars = body.get('cars') or []
            tracks = (body.get('tracks') or []) + [s.strip() for s in (body.get('custom') or '').split(',') if s.strip()]
            if not cars:
                return self._json(400, {'error': '请至少选择一台车'})
            if not tracks:
                return self._json(400, {'error': '请至少选择一条赛道'})
            # 复刻服务端兼容性拦截：easysearch 专有变体 × es 车 → 400
            car_eng = {c['name']: c['engine'] for c in CARS}
            for tk in tracks:
                info = next((t for t in TRACKS if t['name'] == tk), None)
                if info and info.get('engines'):
                    for cn, eng in car_eng.items():
                        if cn in cars and eng.lower() != info['engines']:
                            return self._json(400, {'error': '矩阵里有引擎与变体不兼容的组合：\n%s（%s）× %s —— 只支持 %s' % (cn, eng, tk, info['engines'])})
            if not body.get('include_covered') and 'geonames' in tracks and 'ez2510-kona25' in cars and +body.get('rounds', 1) >= 1:
                pass  # mock 不真的剔除
            est = est_map_from(body.get('est'), {t['name']: t['est'] for t in TRACKS})
            rows = []
            for tk in tracks:
                for r in range(1, int(body.get('rounds', 1)) + 1):
                    for i in range(len(cars)):
                        rows.append((cars[(i + r - 1) % len(cars)], tk, r))
            total = sum(est.get(tk, 40) for _, tk, _ in rows)
            sched = ['# raceway compose 排程：tag=mock-%d' % (time.time() % 100000), '',
                     '# 每行：<车> <赛道> <轮次> <recall|norecall> <est分钟>', '']
            for car, tk, r in rows:
                sched.append('%s %s %d %s %d' % (car, tk, r, 'norecall' if body.get('recall') == 'norecall' else 'recall', est.get(tk, 40)))
            sched = '\n'.join(sched)
            if p == '/api/render':
                return self._json(200, {'schedule': sched, 'rows': len(rows), 'plan': {},
                                        'what': 'geo', 'est_total_min': total,
                                        'tag': 'mock-' + str(time.time() % 100000), 'digest': 'd' * 64})
            if p == '/api/preview':
                return self._json(200, {'schedule': sched, 'rows': len(rows), 'plan': {},
                                        'what': 'geo', 'est_total_min': total, 'tag': 'mock-t', 'digest': 'd' * 64,
                                        'dryrun': 'DRY-RUN PASS ✓\n预检 6 组全部通过\nPRECHECK OK', 'rc': 0})
            camp = 'jdk3-20261115-%06d' % (random.randrange(999999))
            return self._json(200, {'result': '已起跑 tag=mock\ncampaign: %s' % camp, 'campaign': camp, 'tag': 'mock', 'digest': 'd' * 64})
        if p == '/api/compare':
            return self._json(200, {'rc': 0, 'result': '对比文本…', 'car': 'ez2510 vs es819',
                                    'data': {'a': 'A', 'b': 'B', 'campaigns': ['c1', 'c2'], 'asserts': {'fails': 0, 'rules': []},
                                             'tracks': [{'track': 'geonames',
                                                         'series_a': 'ez', 'series_b': 'es',
                                                         'pool': {'A': {'rounds': 2, 'camps': ['c1']}, 'B': {'rounds': 2, 'camps': ['c2']}},
                                                         'write': [{'task': 'bulk-index', 'metric': 'throughput', 'a_med': 12840, 'b_med': 13500, 'delta_pct': 5.1, 'a_n': 2, 'b_n': 2, 'direction': 'higher-better'}],
                                                         'query': [{'task': 'default', 'a_tput': 212, 'b_tput': 226, 'delta_tput': 6.6, 'a_p50': 46.9, 'b_p50': 44.1, 'delta_p50': -6.0, 'a_p99': 120, 'b_p99': 108, 'delta_p99': -10.0}]}]}})
        if p in ('/api/track-write', '/api/track-delete'):
            return self._json(200, {'ok': True})
        if p == '/api/export-gen':
            return self._json(200, {'log': 'build-race-store …\nbench-export …\nOK exports/mock',
                                    'summary': {'generated_utc': '2026-11-17T00:00:00Z',
                                                'races_scanned': 42, 'segments_scanned': 40,
                                                'bench_run': 24, 'bench_metric': 8214,
                                                'bench_env': 1, 'system_rows': 1102,
                                                'monitors_orphan_cids': 2,
                                                'monitors_orphan_fids': 5}})
        if p == '/api/close':
            return self._json(200, {'result': 'dry-run：无结束行缺失，无需补'})
        if p == '/api/stop':
            return self._json(200, {'result': '已发送停止命令（mock）'})
        if p == '/api/doctor':
            return self._json(200, {'result': '✔ 基础工具\n✔ env 配置\n✔ 拓扑\n✔ 语料', 'rc': 0})
        if p == '/api/apply':
            return self._json(200, {'rc': 0, 'result': '入库完成（mock）'})
        if p == '/api/tracks':
            return self._json(200, {'rc': 0, 'result': 'geonames  ready\nnested   ready\nhv1024   ready'})
        return self._json(404, {'error': 'not found: ' + p})

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    print('mock on http://127.0.0.1:%d' % port)
    srv.serve_forever()
