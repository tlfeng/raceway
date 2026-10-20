#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify-dashboard.py —— 校验 Kibana 看板的面板是不是真的出数（Kibana 7.x / 8.x 通用）

为什么不能只看"文档数 > 0"：
  terms 分组拿不到 series 时，前端画出来是空图，但 ES 侧 count 仍然大于 0。
  真正要验的是 **Kibana 自己能不能把这个面板渲染成有数据的曲线**。

怎么验：
  直接调 Kibana 自己的 TSVB 渲染接口 `POST /api/metrics/vis/data`
  （7.17 与 8.1 是同一个路径），对每个 TSVB 面板发一次真实请求：
    - 参数 = 该面板 visState.params（含面板自带的 lucene filter）
    - 再把看板级 KQL（默认 valid: true）并进 filter，还原看板真实口径
    - 时间窗 = 该看板存的 timeFrom/timeTo
  要求返回的 series 里至少有一条曲线的 bucket 非空。

用法:
  python3 verify-dashboard.py --kibana http://127.0.0.1:5601
  python3 verify-dashboard.py --kibana http://127.0.0.1:5601 --dash bench-geo
  python3 verify-dashboard.py --kibana http://127.0.0.1:5601 --json
"""
import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

KIBANA = 'http://127.0.0.1:5601'
VIS_DATA = '/api/metrics/vis/data'


# ---------------------------------------------------------------- HTTP
def kget(path):
    req = urllib.request.Request(KIBANA + path, headers={'kbn-xsrf': 'true'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def kpost(path, body):
    req = urllib.request.Request(KIBANA + path, data=json.dumps(body).encode('utf-8'),
                                 method='POST',
                                 headers={'kbn-xsrf': 'true', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def find_all(so_type, per_page=100):
    """分页拉完某类型的全部 saved object"""
    out, page = [], 1
    while True:
        r = kget('/api/saved_objects/_find?type=%s&per_page=%d&page=%d' % (so_type, per_page, page))
        out.extend(r.get('saved_objects', []))
        total = r.get('total', 0)
        if len(out) >= total or not r.get('saved_objects'):
            return out
        page += 1


# ---------------------------------------------------------------- 时间窗
def to_epoch_ms(value, fallback):
    """看板里的 timeFrom/timeTo：支持 'now-90d' 这类相对写法与 ISO 绝对时间"""
    if not value:
        return fallback
    v = str(value).strip()
    if v.startswith('now'):
        m = re.match(r'^now([+-]\d+)([smhdw])$', v)
        if m:
            n = int(m.group(1))
            unit = {'s': 'seconds', 'm': 'minutes', 'h': 'hours',
                    'd': 'days', 'w': 'weeks'}[m.group(2)]
            return fallback + n * {'seconds': 1000, 'minutes': 60000, 'hours': 3600000,
                                   'days': 86400000, 'weeks': 604800000}[unit]
        return fallback
    try:
        t = v.replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return fallback


# ---------------------------------------------------------------- 面板校验
def panel_data(resp, panel_id):
    """从 TSVB 响应里取出该面板的 series 数据点"""
    res = resp.get(panel_id)
    if res is None and len(resp) == 1:
        res = list(resp.values())[0]
    if not isinstance(res, dict):
        return []
    points = []
    for s in res.get('series', []) or []:
        # TSVB 7.x/8.x 的时序曲线数据在 data 里（bucket 列表），表格在 buckets 里
        for key in ('data', 'buckets'):
            v = s.get(key)
            if isinstance(v, list) and v:
                points.extend(v)
                break
    return points


def main():
    global KIBANA
    ap = argparse.ArgumentParser()
    ap.add_argument('--kibana', default=KIBANA)
    ap.add_argument('--dash', default=None, help='只校验指定看板（id 前缀匹配）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args()
    KIBANA = args.kibana

    version = (kget('/api/status').get('version') or {}).get('number', '?')
    dashboards = find_all('dashboard')
    vis = {v['id']: v for v in find_all('visualization')}

    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    results = []
    for d in dashboards:
        did = d['id']
        if args.dash and not did.startswith(args.dash):
            continue
        attrs = d.get('attributes', {})
        panels = json.loads(attrs.get('panelsJSON') or '[]')
        # ⚠️ Kibana 7.11+ 的 dashboard 迁移会把 panelsJSON 里面板的 `id` 抹掉，
        #    改成 panelIndex + panelRefName，可视化 id 放在 saved object **顶层**的
        #    references 里（references[].name 就是 panelIndex）。
        #    只看 panelsJSON 的面板会一个都关联不上 → 看起来"0 个面板"。
        refs = {}
        for r in (d.get('references') or attrs.get('references') or []):
            if isinstance(r, dict) and r.get('name') and r.get('id'):
                refs[r['name']] = r['id']
        # 看板级 KQL（默认 valid: true）
        kql = ''
        try:
            src = json.loads((attrs.get('kibanaSavedObjectMeta') or {}).get('searchSourceJSON') or '{}')
            kql = ((src.get('query') or {}).get('query') or '').strip()
        except Exception:                                       # noqa: BLE001
            pass
        lo = to_epoch_ms(attrs.get('timeFrom'), now_ms - 90 * 86400000)
        hi = to_epoch_ms(attrs.get('timeTo'), now_ms)

        checked = ok = empty = failed = skipped = 0
        details = []
        for p in panels:
            if p.get('type') != 'visualization':
                continue
            vid = p.get('id') or refs.get(p.get('panelIndex'))
            vo = vis.get(vid) if vid else None
            if not vo:
                continue
            try:
                vs = json.loads(vo.get('attributes', {}).get('visState') or '{}')
            except Exception:                                   # noqa: BLE001
                continue
            if vs.get('type') != 'metrics':                     # markdown / 段标题
                skipped += 1
                continue
            params = json.loads(json.dumps(vs.get('params') or {}))
            # 把看板级 KQL 并进面板 filter，还原看板真实口径
            flt = params.get('filter') or {}
            if isinstance(flt, dict) and kql:
                base = (flt.get('query') or '').strip()
                flt['query'] = '(%s) AND (%s)' % (kql, base) if base else kql
                params['filter'] = flt
            checked += 1
            try:
                resp = kpost(VIS_DATA, {'panels': [params],
                                        'timerange': {'min': lo, 'max': hi}})
                pts = panel_data(resp, params.get('id'))
                if pts:
                    ok += 1
                else:
                    empty += 1
                    details.append({'id': vid, 'title': vo.get('attributes', {}).get('title'),
                                    'filter': (params.get('filter') or {}).get('query')})
            except urllib.error.HTTPError as e:
                failed += 1
                body = e.read().decode('utf-8', 'replace')[:200]
                details.append({'id': vid, 'error': 'HTTP %s %s' % (e.code, body)})
            except Exception as e:                               # noqa: BLE001
                failed += 1
                details.append({'id': vid, 'error': str(e)[:200]})
        results.append({'dashboard': did, 'title': attrs.get('title'),
                        'checked': checked, 'ok': ok, 'empty': empty,
                        'failed': failed, 'skipped': skipped,
                        'details': details})

    if args.json:
        print(json.dumps({'kibana': version, 'results': results},
                         ensure_ascii=False, indent=2))
    else:
        print('Kibana %s · 校验 %d 个看板' % (version, len(results)))
        tot_ok = tot_empty = tot_failed = 0
        for r in results:
            print('  %-14s TSVB 面板 %3d 个：出数 %3d / 空 %2d / 失败 %2d  %s'
                  % (r['dashboard'], r['checked'], r['ok'], r['empty'], r['failed'],
                     r['title'] or ''))
            for det in r['details'][:5]:
                print('       - %s %s' % (det.get('id'),
                                         det.get('error') or det.get('title')))
            tot_ok += r['ok']; tot_empty += r['empty']; tot_failed += r['failed']
        print('  合计：出数 %d / 空 %d / 失败 %d' % (tot_ok, tot_empty, tot_failed))

    bad = sum(r['empty'] + r['failed'] for r in results)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
