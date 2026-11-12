#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""严格验收 Kibana TSVB 面板：匹配 0 个、引用/状态损坏、错误响应和无有限值均失败。

CLI 才访问 Kibana；verify_dashboard / verify_dashboards 接受模拟请求函数，方便离线测试。
0 是合法值；时间戳、桶数、全 null、NaN/Infinity 不能当作出数证据。
"""
import argparse
import copy
import datetime
import json
import math
import re
import sys
import urllib.request

KIBANA = 'http://127.0.0.1:5601'
VIS_DATA = '/api/metrics/vis/data'


def kget(path):
    req = urllib.request.Request(KIBANA + path, headers={'kbn-xsrf': 'true'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def kpost(path, body):
    req = urllib.request.Request(KIBANA + path, data=json.dumps(body).encode(),
                                 headers={'kbn-xsrf': 'true', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def find_all(so_type, per_page=100):
    out, page = [], 1
    while True:
        result = kget('/api/saved_objects/_find?type=%s&per_page=%d&page=%d' % (so_type, per_page, page))
        batch = result['saved_objects']
        if not isinstance(batch, list):
            raise ValueError('saved_objects 不是数组')
        out.extend(batch)
        total = result['total']
        if len(out) >= total:
            return out
        if not batch:
            raise ValueError('saved_objects 分页提前结束')
        page += 1


def to_epoch_ms(value, now_ms, fallback=None):
    if not value:
        return now_ms if fallback is None else fallback
    if value == 'now':
        return now_ms
    match = re.fullmatch(r'now([+-]\d+)([smhdw])', value)
    if match:
        return now_ms + int(match[1]) * {'s': 1000, 'm': 60000, 'h': 3600000,
                                       'd': 86400000, 'w': 604800000}[match[2]]
    dt = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def panel_data(resp, panel_id):
    """只接受精确 panel id 下的有限 y 值；绝不以别的单一响应作兜底。"""
    if not panel_id or not isinstance(resp, dict) or panel_id not in resp:
        raise ValueError('TSVB 响应 panel id 不匹配: %r' % panel_id)
    result = resp[panel_id]
    if not isinstance(result, dict) or result.get('error') or result.get('errorMessage'):
        raise ValueError('TSVB 面板错误响应')
    series = result.get('series')
    if not isinstance(series, list):
        raise ValueError('TSVB 响应缺少 series 数组')
    points = []
    for item in series:
        if not isinstance(item, dict):
            raise ValueError('TSVB series 结构错误')
        for key in ('data', 'buckets'):
            buckets = item.get(key)
            if buckets is None:
                continue
            if not isinstance(buckets, list):
                raise ValueError('TSVB bucket 不是数组')
            for bucket in buckets:
                if isinstance(bucket, (list, tuple)) and len(bucket) == 2:
                    if finite_number(bucket[1]):
                        points.append(bucket)
                elif isinstance(bucket, dict) and finite_number(bucket.get('value')):
                    points.append(bucket)
            break
    return points


def resolve_panel(panel, refs):
    """直接 id 与引用名两种合法形态；显式引用缺失不静默跳过。"""
    name = panel.get('panelRefName')
    if name is not None:
        if name in refs:
            return refs[name]
        index = panel.get('panelIndex')
        if isinstance(name, str) and index is not None and name == 'panel_' + str(index) and str(index) in refs:
            return refs[str(index)]
        raise ValueError('面板引用缺失: %s' % name)
    if panel.get('id'):
        return panel['id']
    index = panel.get('panelIndex')
    if index in refs:
        return refs[index]
    raise ValueError('面板没有可解析的 visualization 引用')


def _result(dashboard):
    return {'dashboard': dashboard.get('id'), 'title': dashboard.get('attributes', {}).get('title'),
            'checked': 0, 'ok': 0, 'empty': 0, 'failed': 0, 'skipped': 0, 'details': []}


def verify_dashboard(dashboard, visualizations, post, now_ms):
    report = _result(dashboard)

    def fail(identifier, error):
        report['failed'] += 1
        report['details'].append({'id': identifier, 'error': str(error)[:300]})

    try:
        attrs = dashboard['attributes']
        panels = json.loads(attrs['panelsJSON'])
        if not isinstance(panels, list) or not panels:
            raise ValueError('dashboard panelsJSON 为空或不是数组')
        source = json.loads(attrs['kibanaSavedObjectMeta']['searchSourceJSON'])
        if not isinstance(source, dict):
            raise ValueError('dashboard searchSourceJSON 结构损坏')
        query = source.get('query') or {'query': '', 'language': 'kuery'}
        if not isinstance(query, dict) or query.get('language') not in ('kuery', 'lucene'):
            raise ValueError('dashboard query 结构损坏')
        if not isinstance(query.get('query'), str):
            raise ValueError('dashboard query 必须为字符串')
        filters = source.get('filter') or []
        if not isinstance(filters, list):
            raise ValueError('dashboard filters 结构损坏')
        lo = to_epoch_ms(attrs.get('timeFrom'), now_ms, now_ms - 30 * 86400000)
        hi = to_epoch_ms(attrs.get('timeTo'), now_ms)
        if lo >= hi:
            raise ValueError('dashboard 时间窗无效')
        refs = {}
        for ref in dashboard.get('references') or []:
            if ref.get('type') == 'visualization':
                if ref['name'] in refs:
                    raise ValueError('dashboard 引用名重复')
                refs[ref['name']] = ref['id']
    except (KeyError, TypeError, ValueError) as exc:
        fail(dashboard.get('id'), exc)
        return report

    for panel in panels:
        vid = None
        try:
            if not isinstance(panel, dict):
                raise ValueError('panel 不是对象')
            if panel.get('type') != 'visualization':
                raise ValueError('不支持验收的面板类型: %r' % panel.get('type'))
            vid = resolve_panel(panel, refs)
            if vid not in visualizations:
                raise ValueError('visualization 对象缺失: %s' % vid)
            vis = visualizations[vid]
            state = json.loads(vis['attributes']['visState'])
            if not isinstance(state, dict) or not isinstance(state.get('params'), dict):
                raise ValueError('visState 结构损坏')
            if state.get('type') == 'markdown':
                if not isinstance(state['params'].get('markdown'), str):
                    raise ValueError('markdown visState 损坏')
                report['skipped'] += 1
                continue
            if state.get('type') != 'metrics':
                raise ValueError('不支持验收的 visualization 类型')
            params = copy.deepcopy(state['params'])
            if not params.get('id') or not isinstance(params.get('series'), list) or not params['series']:
                raise ValueError('TSVB params 缺少 id/series')
            flt = params.get('filter')
            if not isinstance(flt, dict) or flt.get('language') not in ('lucene', 'kuery') or not isinstance(flt.get('query'), str):
                raise ValueError('TSVB filter 结构损坏')
            # 保留各自语言，不把 KQL 字符串误当 Lucene；运行时与看板一样叠加查询。
            body = {'panels': [params], 'query': [query] if query['query'].strip() else [],
                    'filters': copy.deepcopy(filters), 'timerange': {'min': lo, 'max': hi}}
            report['checked'] += 1
            points = panel_data(post(VIS_DATA, body), params['id'])
            if points:
                report['ok'] += 1
            else:
                report['empty'] += 1
                report['details'].append({'id': vid, 'error': '没有有限数值点（null/NaN/inf 不算出数）'})
        except Exception as exc:  # 包括模拟/真实 HTTP 错误，全部计入失败
            fail(vid or dashboard.get('id'), exc)
    return report


def verify_dashboards(dashboards, visualizations, post, prefix=None, now_ms=None):
    selected = [d for d in dashboards if prefix is None or d.get('id', '').startswith(prefix)]
    if not selected:
        return {'passed': False, 'errors': ['没有匹配的 dashboard'], 'results': []}
    if now_ms is None:
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    vis = {v['id']: v for v in visualizations}
    reports = [verify_dashboard(d, vis, post, now_ms) for d in selected]
    checked = sum(r['checked'] for r in reports)
    errors = [] if checked else ['未验收任何 TSVB 面板']
    return {'passed': not errors and all(not (r['empty'] or r['failed']) for r in reports),
            'errors': errors, 'results': reports}


def main():
    global KIBANA
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--kibana', default=KIBANA)
    ap.add_argument('--dash', help='dashboard id 前缀')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    KIBANA = args.kibana
    try:
        version = kget('/api/status')['version']['number']
        result = verify_dashboards(find_all('dashboard'), find_all('visualization'), kpost, args.dash)
        result['kibana'] = version
    except Exception as exc:
        result = {'passed': False, 'errors': [str(exc)], 'results': []}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('PASS' if result['passed'] else 'FAIL')
        for error in result['errors']:
            print('  ' + error)
        for report in result['results']:
            print('  %s: checked=%d ok=%d empty=%d failed=%d skipped=%d' % (
                report['dashboard'], report['checked'], report['ok'], report['empty'], report['failed'], report['skipped']))
            for detail in report['details']:
                print('    %s: %s' % (detail['id'], detail['error']))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
