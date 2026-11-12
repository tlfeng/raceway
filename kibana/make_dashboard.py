#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 benchmark 三索引的 Kibana TSVB 看板；仅 main 会访问 ES/Kibana。

测量面板各自 trusted:true；全局查询不隐藏错误率证据。
series_key 是产品身份，comparison_key=series_key+'|'+track_variant 是实验分线键。
默认基座板排除非默认压缩/source_reuse；每个实际变体另有固定 variant 的独立板。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import ssl
from urllib import request as urlreq

ES = 'http://localhost:9200'
KIBANA = 'http://localhost:5601'
ES_AUTH = None
ES_INSECURE = False
KIBANA_VERSION = ''
KIBANA_MAJOR = 0
CODEC_EXCLUDED = ('zstd', 'best_compression')
DASH_SCOPE = 'NOT codec:(zstd or best_compression) AND NOT source_reuse:true'
COMPARISON_FIELD = 'comparison_key'
IP_ID = 'benchmark-result-metric'
IP_REF = 'metrics_0_index_pattern'
PANEL = '61ca57f0-469d-11e7-af02-69e470af7417'
SERIES = '61ca57f1-469d-11e7-af02-69e470af7417'
METRIC = '61ca57f2-469d-11e7-af02-69e470af7417'
COLORS = ['rgba(104,188,0,1)', 'rgba(255,128,0,1)', 'rgba(0,161,255,1)']
DS_TRACK_RE = re.compile(r'^ds-([a-z])(\d+)$')
TRACK_LABEL = {'geonames': 'geonames 文本检索', 'nested': 'nested 嵌套文档',
               'hnsw': 'HNSW 向量检索'}
LINEAGE_LABEL = {'j': 'jina 系', 'g': 'GIST 系'}
LINEAGE_SHORT = LINEAGE_LABEL


def lucene_value(value):
    """精确 keyword 值；不要把 task/variant 中的保留字符当查询语法。"""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def runner_base(track):
    """独立 os-bbq/os-disk/bbqdisk 基座保持原名，不剥量化后缀。"""
    return 'geo' if track == 'geonames' else track


def variant_scope(track, variant=None):
    variant = variant or runner_base(track)
    exact = 'track_variant:%s' % lucene_value(variant)
    return '(%s) AND (%s)' % (DASH_SCOPE, exact) if variant == runner_base(track) else exact


def scoped_query(filt, scope):
    return '(%s) AND (%s)' % (scope, filt) if scope else filt


def _es_headers():
    if not ES_AUTH:
        return {}
    return {'Authorization': 'Basic ' + base64.b64encode(ES_AUTH.encode()).decode('ascii')}


def _es_ssl_ctx():
    if not ES_INSECURE:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def es_get(path):
    req = urlreq.Request(ES + path, headers=_es_headers())
    with urlreq.urlopen(req, context=_es_ssl_ctx(), timeout=120) as r:
        return json.loads(r.read())


def es_post(path, body):
    headers = {'Content-Type': 'application/json', **_es_headers()}
    req = urlreq.Request(ES + path, data=json.dumps(body).encode(), headers=headers)
    with urlreq.urlopen(req, context=_es_ssl_ctx(), timeout=120) as r:
        return json.loads(r.read())


def kibana_get(path):
    with urlreq.urlopen(urlreq.Request(KIBANA + path, headers={'kbn-xsrf': 'true'}), timeout=120) as r:
        return json.loads(r.read())


def kibana_delete(path):
    req = urlreq.Request(KIBANA + path, method='DELETE', headers={'kbn-xsrf': 'true'})
    with urlreq.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def detect_kibana_version():
    global KIBANA_VERSION, KIBANA_MAJOR
    KIBANA_VERSION = kibana_get('/api/status')['version']['number']
    KIBANA_MAJOR = int(KIBANA_VERSION.split('.')[0])
    return KIBANA_VERSION


def vis_attributes(title, vis_state, search_source=None):
    return {'title': title, 'visState': vis_state, 'uiStateJSON': '{}', 'description': '',
            'kibanaSavedObjectMeta': {'searchSourceJSON': search_source or json.dumps({
                'query': {'query': '', 'language': 'kuery'}, 'filter': []})}}


def prune_stale(keep_ids):
    # 先完整收集再删除，避免删除前页对象使后页偏移而漏清。
    stale = []
    for kind in ('visualization', 'dashboard', 'link', 'index-pattern'):
        page = 1
        while True:
            r = kibana_get('/api/saved_objects/_find?type=%s&per_page=200&page=%d' % (kind, page))
            objects = r.get('saved_objects') or []
            stale.extend((kind, o['id']) for o in objects
                         if o['id'].startswith(('bench-', 'benchmark-')) and o['id'] not in keep_ids)
            if not objects or page * 200 >= r.get('total', 0):
                break
            page += 1
    for kind, sid in stale:
        kibana_delete('/api/saved_objects/%s/%s' % (kind, sid))
    return len(stale)


def flat_fields(index):
    mapping = es_get('/%s/_mapping' % index)
    definitions = []

    def walk(props, prefix=''):
        for name, spec in props.items():
            if spec.get('enabled') is False:
                continue
            field = prefix + name
            if spec.get('type'):
                definitions.append((field, spec['type']))
            if spec.get('properties'):
                walk(spec['properties'], field + '.')
    walk(mapping[index]['mappings'].get('properties', {}))
    return definitions


def kibana_field_type(es_type):
    return {'integer': 'number', 'long': 'number', 'double': 'number', 'float': 'number',
            'short': 'number', 'byte': 'number', 'keyword': 'string', 'text': 'string',
            'date': 'date', 'boolean': 'boolean'}.get(es_type, 'unknown')


def index_pattern_obj(name):
    defs = flat_fields(name)
    if name in ('benchmark-result-metric', 'benchmark-result-race'):
        required = {'series_key': 'keyword', 'track_variant': 'keyword',
                    'comparison_key': 'keyword', 'knn_index_type': 'keyword'}
        for field, kind in required.items():
            if dict(defs).get(field) != kind:
                raise ValueError('%s mapping 缺少契约字段或类型错误: %s' % (name, field))
    fields = []
    for fqn, kind in defs:
        numeric = kibana_field_type(kind) == 'number'
        aggregatable = kind in ('keyword', 'date', 'boolean') or numeric
        fields.append({'name': fqn, 'type': kibana_field_type(kind), 'esTypes': [kind],
                       'count': 0, 'scripted': False, 'searchable': kind != 'object',
                       'aggregatable': aggregatable, 'readFromDocValues': aggregatable})
    time_field = '@timestamp' if '@timestamp' in dict(defs) else next(
        (field for field, kind in defs if kind == 'date'), None)
    if time_field is None:
        raise ValueError('%s 没有 date 字段' % name)
    return {'id': name, 'type': 'index-pattern', 'references': [], 'attributes': {
        'title': name, 'timeFieldName': time_field, 'fields': json.dumps(fields)}}


def tsvb_series(idx, terms_field, metric_type, metric_field, formatter):
    metric = {'id': METRIC, 'type': metric_type, 'field': metric_field}
    if metric_type == 'percentile':
        # TSVB 的正确形态：不是 ES DSL 的 percentiles:{percents:[50]}。
        metric['percentiles'] = [{'value': '50', 'mode': 'line'}]
    elif metric_type not in ('max', 'min', 'sum', 'count'):
        raise ValueError('不支持的正式统计口径: %s' % metric_type)
    return {'id': SERIES, 'color': COLORS[idx % len(COLORS)], 'split_mode': 'terms',
            'field': metric_field, 'metrics': [metric], 'separate_axis': 0,
            'axis_position': 'right', 'formatter': formatter, 'chart_type': 'line',
            'line_width': 2, 'point_size': 2, 'fill': '0', 'stacked': 'none',
            'terms_field': terms_field, 'terms_order_by': '_key',
            'terms_direction': 'asc', 'terms_size': 200}


def tsvb_timeseries(title, panel_filter, formatter, terms_field=COMPARISON_FIELD,
                    metric_type='percentile', metric_field='value',
                    index_pattern=IP_ID, interval='1d'):
    return {'type': 'metrics', 'aggs': [], 'title': title, 'params': {
        'id': PANEL, 'type': 'timeseries',
        'series': [tsvb_series(0, terms_field, metric_type, metric_field, formatter)],
        'time_field': 'run_start', 'index_pattern': index_pattern, 'interval': interval,
        'axis_position': 'left', 'axis_formatter': 'number', 'show_legend': 1,
        'show_grid': 1, 'legend_position': 'right', 'drop_last_bucket': 0,
        'filter': {'language': 'lucene', 'query': panel_filter}}}


def visualization_obj(vis_id, title, panel_filter, formatter,
                      terms_field=COMPARISON_FIELD, metric_type='percentile', interval='1d'):
    state = tsvb_timeseries(title, panel_filter, formatter, terms_field, metric_type, interval=interval)
    state['index_pattern_ref_name'] = IP_REF
    return {'id': vis_id, 'type': 'visualization', 'attributes': vis_attributes(title, json.dumps(state)),
            'references': [{'id': IP_ID, 'name': IP_REF, 'type': 'index-pattern'}],
            'migrationVersion': {'visualization': '7.10.0'}}


def markdown_obj(vis_id, title, md):
    state = {'type': 'markdown', 'aggs': [], 'title': title,
             'params': {'fontSize': 12, 'markdown': md}}
    return {'id': vis_id, 'type': 'visualization', 'attributes': vis_attributes(title, json.dumps(state)),
            'references': [], 'migrationVersion': {'visualization': '7.10.0'}}


def section_obj(vis_id, title, text):
    return markdown_obj(vis_id, title, '**%s** — %s' % (title, text))


def task_key(task):
    return 'task-' + task.encode('utf-8').hex()


def is_measurement_task(task):
    return bool(task) and task not in {
        'index-stats', 'node-stats', 'force-merge', 'delete-index', 'create-index',
        'refresh', 'check-cluster-health', 'cluster-health', 'post-ingest-sleep',
    } and not task.startswith(('refresh-', 'wait-', 'check-'))


def group_tasks(tasks):
    # 不同 task 不能求一个平均；未知业务任务动态兜底，不因静态白名单漏排。
    return [(task_key(t), t, [t]) for t in sorted(set(tasks)) if t]


def _count(query):
    return es_post('/%s/_count' % IP_ID, {'query': {'query_string': {'query': query}}})['count']


def panel_has_data(filt, scope=DASH_SCOPE):
    # 查询错误必须终止建板，不能把无效查询当成“有数据”。
    return _count(scoped_query(filt, scope)) > 0


def group_has_metric(track, tasks, family, unit=None, scope=DASH_SCOPE):
    filt = 'track:%s AND trusted:true AND family:%s' % (lucene_value(track), lucene_value(family))
    if unit:
        filt += ' AND unit:%s' % lucene_value(unit)
    if tasks:
        filt += ' AND (%s)' % ' OR '.join('task:%s' % lucene_value(t) for t in tasks)
    return panel_has_data(filt, scope)


def system_metric_exists(track, metric, scope=DASH_SCOPE):
    return panel_has_data('track:%s AND trusted:true AND family:system AND metric:%s' % (
        lucene_value(track), lucene_value(metric)), scope)


# 所有规格一次只量一个 stat/metric，分线字段始终保留 comparison_key。
RESOURCE_SPECS = [
    ('idx', '索引总耗时 (s)', 'indexing_time', 'total_time'),
    ('merge-time', 'Merge 耗时 (s)', 'merge_time', 'merge_time'),
    ('refresh-time', 'Refresh 耗时 (s)', 'refresh_time', 'refresh_time'),
    ('flush-time', 'Flush 耗时 (s)', 'flush_time', 'flush_time'),
    ('throttle', '索引限流时长 (s)', 'indexing_throttle_time', 'indexing_throttle_time'),
    ('merge-count', 'Merge 次数', 'merge', 'merge_count'),
    ('refresh-count', 'Refresh 次数', 'refresh', 'refresh_count'),
    ('flush-count', 'Flush 次数', 'flush', 'flush_count'),
    ('disk', '存储占用 (GB)', 'disk', 'store_size'),
    ('seg', '段数量', 'disk', 'segment_count'),
    ('young-gc-time', 'Young GC 耗时 (s)', 'gc', 'young_gc_time'),
    ('old-gc-time', 'Old GC 耗时 (s)', 'gc', 'old_gc_time'),
    ('young-gc-count', 'Young GC 次数', 'gc', 'young_gc_count'),
    ('old-gc-count', 'Old GC 次数', 'gc', 'old_gc_count'),
]
SYSTEM_METRICS = [
    ('cpu_steal_avg', 'CPU steal · 场内均值 (ratio)'),
    ('cpu_steal_max', 'CPU steal · 场内最大值 (ratio)'),
    ('foreign_cpu_max', '外来进程 CPU · 场内最大值 (ratio)'),
    ('swapin', '换入次数'), ('bench_cpu_avg', '引擎 CPU · 场内均值 (ratio)'),
    ('bench_cpu_max', '引擎 CPU · 场内最大值 (ratio)'),
    ('cpu_usr_avg', '整机 user CPU · 场内均值 (ratio)'),
    ('cpu_sys_avg', '整机 sys CPU · 场内均值 (ratio)'),
    ('disk_rw_mbs_avg', '磁盘读写 · 场内均值 (MB/s)'),
    ('disk_rw_mbs_max', '磁盘读写 · 场内最大值 (MB/s)'),
]


def track_panel_specs(track, groups, interval):
    """规格不提前排除；统一在 build_track_dashboard 按真实 scope 预检。"""
    trusted = 'track:%s AND trusted:true' % lucene_value(track)
    raw = 'track:%s' % lucene_value(track)
    specs = []

    def add(key, title, filt, fmt='0.0[000]', aggregation='percentile'):
        specs.append((key, title, filt, fmt, COMPARISON_FIELD, aggregation, interval))

    for gid, label, tasks in groups:
        tt = '(%s)' % ' OR '.join('task:%s' % lucene_value(t) for t in tasks)
        # 维护 task 也可能失败，保留错误率证据；只是不把它们当业务性能展示。
        add('err-' + gid, '%s · 错误率 (0–1，桶内最坏)' % label,
            '%s AND family:error_rate AND stat:value AND %s' % (raw, tt), '0.000', 'max')
        if not any(is_measurement_task(task) for task in tasks):
            continue
        for unit in ('docs/s', 'ops/s', 'pages/s'):
            add('thr-%s-%s' % (unit.split('/')[0], gid), '%s · 吞吐 p50 (%s)' % (label, unit),
                '%s AND family:throughput AND stat:p50 AND unit:%s AND %s' %
                (trusted, lucene_value(unit), tt), '0.0[00]')
        for family, title in [('service_time', '引擎时延'), ('latency', '端到端时延'),
                              ('queue_time', '发压机积压')]:
            for stat in ('p50', 'p90', 'p99'):
                add('%s-%s-%s' % (family, stat, gid), '%s · %s %s (s)' % (label, title, stat),
                    '%s AND family:%s AND stat:%s AND unit:s AND %s' % (trusted, family, stat, tt))
    for key, title, family, metric in RESOURCE_SPECS:
        add(key, title, '%s AND family:%s AND metric:%s AND stat:value' % (trusted, family, metric))
    for metric, title in SYSTEM_METRICS:
        fld = 'family:system AND metric:%s AND stat:value' % metric
        # 一个场次有多节点/相位摘要，不能把每条摘要冒充一圈；健康图显式画桶内最坏。
        add('system-' + metric, title + ' · 桶内最坏', '%s AND %s' % (trusted, fld),
            aggregation='max')
    return specs


def _dashboard(panels, refs, dash_id, title, time_from, time_to, description, scope=DASH_SCOPE):
    return {'id': dash_id, 'type': 'dashboard', 'references': refs,
            'migrationVersion': {'dashboard': '7.9.3'}, 'attributes': {
                'title': title, 'hits': 0, 'timeRestore': True, 'timeFrom': time_from,
                'timeTo': time_to, 'refreshInterval': {'pause': True, 'value': 0},
                'description': description, 'panelsJSON': json.dumps(panels),
                'optionsJSON': json.dumps({'hidePanelTitles': False, 'useMargins': True}),
                'kibanaSavedObjectMeta': {'searchSourceJSON': json.dumps({
                    'query': {'query': scope, 'language': 'kuery'}, 'filter': []})}}}


def dashboard_objects(dash_id, title, objects, time_from, time_to, scope, description=''):
    panels, refs = [], []
    y = 0
    for n, obj in enumerate(objects):
        i = str(n + 1)
        state = json.loads(obj['attributes']['visState'])
        h = 12 if state['type'] == 'markdown' else 14
        panels.append({'embeddableConfig': {}, 'gridData': {'x': 0, 'y': y, 'w': 48, 'h': h, 'i': i},
                       'id': obj['id'], 'panelIndex': i, 'type': 'visualization', 'version': '7.10.0'})
        refs.append({'id': obj['id'], 'name': i, 'type': 'visualization'})
        y += h
    return objects + [_dashboard(panels, refs, dash_id, title, time_from, time_to, description, scope)]


def track_dashboard_id(track, variant):
    return 'bench-' + track if variant == runner_base(track) else 'bench-%s-variant-%s' % (track, variant)


def build_track_dashboard(track, groups, interval, time_from, time_to, variant=None):
    variant = variant or runner_base(track)
    scope = variant_scope(track, variant)
    dash_id = track_dashboard_id(track, variant)
    objects, dropped = [], []
    for key, title, filt, fmt, terms, metric_type, iv in track_panel_specs(track, groups, interval):
        if not panel_has_data(filt, scope):
            dropped.append((title, '该固定 variant / 测量可信度口径下没有数据'))
            continue
        objects.append(visualization_obj(dash_id + '-' + key, title, filt, fmt, terms, metric_type, iv))
    if dropped:
        md = '**未出图指标**\n\n' + '\n'.join('- %s：%s' % row for row in dropped)
        objects.append(markdown_obj(dash_id + '-nodata', '未出图指标', md))
    title = 'Benchmark · %s · %s' % (track, variant)
    return dashboard_objects(dash_id, title, objects, time_from, time_to, scope,
                             '固定 runner variant；实验分线 comparison_key；跨圈中位，错误率最坏值。'), dropped


def composite_buckets(index, sources, query=None):
    """不依赖 terms top-N；新增赛道、任务、variant 不因桶数上限静默漏排。"""
    after = None
    while True:
        comp = {'size': 500, 'sources': sources}
        if after is not None:
            comp['after'] = after
        body = {'size': 0, 'aggs': {'items': {'composite': comp}}}
        if query is not None:
            body['query'] = query
        result = es_post('/%s/_search' % index, body)['aggregations']['items']
        buckets = result.get('buckets') or []
        yield from buckets
        next_after = result.get('after_key')
        if not buckets or not next_after:
            break
        if next_after == after:
            raise ValueError('composite 分页没有前进')
        after = next_after


def es_track_facts():
    sources = [{key: {'terms': {'field': field, 'missing_bucket': True}}}
               for key, field in [('track', 'track'), ('variant', 'track_variant'),
                                  ('task', 'task'), ('car', COMPARISON_FIELD)]]
    facts = {}
    for bucket in composite_buckets(IP_ID, sources):
        key = bucket['key']
        track, variant, car = key['track'], key['variant'], key['car']
        if not track or not variant or not car:
            raise ValueError('metric 缺少 track / track_variant / comparison_key；请使用新导出契约')
        fact = facts.setdefault(track, {'tasks': set(), 'cars': set(), 'variants': {}, 'metrics': 0})
        v = fact['variants'].setdefault(variant, {'tasks': set(), 'cars': set(), 'metrics': 0})
        for entry in (fact, v):
            if key['task']:
                entry['tasks'].add(key['task'])
            entry['cars'].add(car)
            entry['metrics'] += bucket['doc_count']
    for fact in facts.values():
        for entry in [fact] + list(fact['variants'].values()):
            entry['tasks'] = sorted(entry['tasks'])
            entry['cars'] = sorted(entry['cars'])
    return facts


def es_track_runs(track, variant=None):
    query = {'bool': {'filter': [{'term': {'track': track}}]}}
    if variant is not None:
        query['bool']['filter'].append({'term': {'track_variant': variant}})
    result = es_post('/benchmark-result-race/_count', {'query': query})
    return {'runs': result['count']}


def data_window(track=None, pad_days=1):
    return 'now-30d', 'now'


def data_window_span(lo_ms=None, hi_ms=None, pad_days=1):
    return data_window()


def pick_interval(lo_ms=None, hi_ms=None):
    return '1d' if lo_ms is None or hi_ms is None or hi_ms - lo_ms > 172800000 else '2h'


# 静态快照表的单位与图一致：时延统一 s；percentiles 50 聚合各圈的单场统计。
DS_TABLE_SPECS = [
    ('write', '写入吞吐', 'family:throughput AND unit:"docs/s" AND stat:p50', 'docs/s', 1),
    ('k10', 'k10 吞吐', 'family:throughput AND unit:"ops/s" AND stat:p50 AND task:knn-search-10-100', 'ops/s', 1),
    ('k100', 'k100 吞吐', 'family:throughput AND unit:"ops/s" AND stat:p50 AND task:knn-search-100-1000', 'ops/s', 1),
    ('k32', '32 并发稳态', 'family:throughput AND unit:"ops/s" AND stat:p50 AND task:knn-search-100-1000-x32', 'ops/s', 1),
    ('idx', '索引总耗时', 'family:indexing_time AND metric:total_time AND stat:value', 's', 1),
    ('e2e99', 'k10 端到端时延 p99', 'family:latency AND stat:p99 AND unit:s AND task:knn-search-10-100', 's', 1),
    ('p50', 'k10 引擎时延 p50', 'family:service_time AND stat:p50 AND unit:s AND task:knn-search-10-100', 's', 1),
    ('p99', 'k10 引擎时延 p99', 'family:service_time AND stat:p99 AND unit:s AND task:knn-search-10-100', 's', 1),
    ('disk', '存储占用', 'family:disk AND metric:store_size AND stat:value AND unit:GB', 'GB', 1),
    ('r10', 'Recall@10', 'family:recall AND stat:mean AND task:knn-search-10-100', 'ratio', 1),
    ('r100', 'Recall@100', 'family:recall AND stat:mean AND task:knn-search-100-1000', 'ratio', 1),
]


def ds_lineage_tracks(facts):
    out = {}
    for track in facts:
        match = DS_TRACK_RE.fullmatch(track)
        if match and runner_base(track) in facts[track]['variants']:
            out.setdefault(match[1], []).append((int(match[2]), track))
    return {lin: sorted(tracks) for lin, tracks in sorted(out.items())}


def ds_main_car(cars):
    # 主车选产品身份，跨维度轨迹仍固定每档的默认 runner variant。
    return min(cars, key=lambda c: (-c['tracks'], -c['runs'], c['series_key']))['series_key'] if cars else None


def ds_collect():
    rows, cars = {}, {}
    sources = [{'track': {'terms': {'field': 'track'}}},
               {'variant': {'terms': {'field': 'track_variant'}}},
               {'car': {'terms': {'field': COMPARISON_FIELD}}}]
    after = None
    while True:
        comp = {'size': 500, 'sources': sources}
        if after is not None:
            comp['after'] = after
        aggs = {'n': {'cardinality': {'field': 'race_id'}}}
        for key, _title, filt, _unit, _mul in DS_TABLE_SPECS:
            aggs[key] = {'filter': {'query_string': {'query': filt}},
                         'aggs': {'v': {'percentiles': {'field': 'value', 'percents': [50]}}}}
        result = es_post('/%s/_search' % IP_ID, {'size': 0,
            'query': {'query_string': {'query': scoped_query('track:ds-* AND trusted:true', DASH_SCOPE)}},
            'aggs': {'items': {'composite': comp, 'aggs': aggs}}})['aggregations']['items']
        buckets = result.get('buckets') or []
        for b in buckets:
            track, variant, comparison = (b['key'][k] for k in ('track', 'variant', 'car'))
            if variant != runner_base(track):
                continue
            product, separator, recorded_variant = comparison.rpartition('|')
            if not separator or recorded_variant != variant:
                raise ValueError('comparison_key 与 track_variant 不一致')
            rec = {'runs': b['n']['value']}
            for key, _title, _filt, _unit, mul in DS_TABLE_SPECS:
                value = b[key]['v']['values'].get('50.0')
                rec[key] = value * mul if value is not None else None
            rows[(track, product)] = rec
            car = cars.setdefault(product, {'series_key': product, 'tracks': set(), 'runs': 0})
            car['tracks'].add(track)
            car['runs'] += rec['runs']
        next_after = result.get('after_key')
        if not buckets or not next_after:
            break
        if next_after == after:
            raise ValueError('composite 分页没有前进')
        after = next_after
    for car in cars.values():
        car['tracks'] = len(car['tracks'])
    return rows, list(cars.values())


def _fmt(value, nd=4):
    return '—' if value is None else ('%%.%df' % nd) % value


TIER_NOTE = {512: '截断', 768: '截断', 960: '真实', 1024: '真实',
             1536: 'tile 扩展', 1920: 'tile 扩展', 2048: 'tile 扩展'}


def ds_table_markdown(lin, tracks, rows, car, title=None):
    columns = [('write', '写入 docs/s'), ('k10', 'k10 ops/s'), ('k100', 'k100 ops/s'),
               ('k32', '32c ops/s'), ('p50', 'p50 s'), ('p99', 'p99 s'),
               ('disk', 'GB'), ('r10', 'R@10'), ('r100', 'R@100')]
    md = ['**%s** · 产品 `%s` · 仅默认 runner 基座' % (title or LINEAGE_LABEL.get(lin, lin), car),
          '', '建板时全部入库批次的可信行快照；顶部时间/查询不会更新此静态表。',
          '各列是跨圈中位数，不是算术均值。', '',
          '| 维度/性质 | 场次 | ' + ' | '.join(label for _key, label in columns) + ' | 写当量 | 32c当量 |',
          '|' + '---|' * (len(columns) + 4)]
    for dims, track in tracks:
        rec = rows.get((track, car))
        if rec:
            equivalents = [None if rec.get(key) is None else rec[key] * dims / 1024.0
                           for key in ('write', 'k32')]
            md.append('| %d %s | %d | %s | %s |' % (dims, TIER_NOTE.get(dims, ''), rec['runs'],
                ' | '.join(_fmt(rec.get(key)) for key, _label in columns),
                ' | '.join(_fmt(v) for v in equivalents)))
    md.extend(['', '**当量吞吐** = 实测吞吐 × d/1024；平稳表示维度缩放成本近似线性。',
               '截断 = Matryoshka 截断与重归一；tile 扩展 = 保序维度扩展，不是新真实语料。',
               '**召回护栏**：Recall@10 ≥ 0.95 / Recall@100 ≥ 0.99；召回塌了的性能不作结论。',
               'jina 与 GIST 两系语料分面；跨语料绝对吞吐不可比较。'])
    return '\n'.join(md)


def build_dimsweep_dashboard(lin_tracks, rows, cars, main_car, interval, time_from, time_to):
    objects = []
    for lin, tracks in lin_tracks.items():
        for car in cars:
            product = car['series_key']
            if any((track, product) in rows for _dims, track in tracks):
                tag = hashlib.sha256((lin + product).encode()).hexdigest()[:16]
                objects.append(markdown_obj('bench-dimsweep-table-' + tag,
                    '%s · %s' % (LINEAGE_LABEL.get(lin, lin), product),
                    ds_table_markdown(lin, tracks, rows, product)))
        if main_car:
            # 一条 comparison_key 包含维度档 variant，身份与实验都不混线。
            fixed = '(%s)' % ' OR '.join('(track:%s AND track_variant:%s)' %
                (lucene_value(track), lucene_value(runner_base(track))) for _d, track in tracks)
            for key, title, filt, _unit, _mul in DS_TABLE_SPECS:
                panel_filter = '%s AND series_key:%s AND trusted:true AND %s' % (
                    fixed, lucene_value(main_car), filt)
                if panel_has_data(panel_filter, DASH_SCOPE):
                    objects.append(visualization_obj('bench-dimsweep-%s-%s' % (lin, key),
                        '%s · %s (%s)' % (LINEAGE_LABEL.get(lin, lin), title, _unit), panel_filter, '0.0[0000]',
                        interval=interval))
    return dashboard_objects('bench-dimsweep', 'Benchmark · 向量维度扫描 · 维度曲线', objects,
                             time_from, time_to, DASH_SCOPE,
                             '默认 runner 基座；时序 x 轴为 run_start，各维度实验独立 comparison_key。')


CAR_PANEL_SPECS = [(key, title + ' (' + unit + ')', filt, '0.0[0000]', COMPARISON_FIELD)
                   for key, title, filt, unit, _mul in DS_TABLE_SPECS]


def build_dimsweep_car_dashboard(car_tracks, facts, interval, time_from, time_to):
    objects = []
    for track, dims, lin in car_tracks:
        for key, title, filt, fmt, terms in CAR_PANEL_SPECS:
            panel_filter = 'track:%s AND track_variant:%s AND trusted:true AND %s' % (
                lucene_value(track), lucene_value(runner_base(track)), filt)
            if panel_has_data(panel_filter, DASH_SCOPE):
                objects.append(visualization_obj('bench-dimsweep-car-%s-%s' % (track, key),
                    '%s %d 维 · %s' % (LINEAGE_LABEL.get(lin, lin), dims, title), panel_filter, fmt,
                    terms_field=terms, interval=interval))
        # 错误率证据及健康度仍保留，逐 task/metric 展示且不丢产品/build 隔离。
        for key, title, filt, fmt, terms, aggregation, iv in track_panel_specs(
                track, group_tasks(facts[track]['variants'][runner_base(track)]['tasks']), interval):
            if not key.startswith(('err-', 'system-')):
                continue
            filt = scoped_query(filt, 'track_variant:%s' % lucene_value(runner_base(track)))
            if panel_has_data(filt, DASH_SCOPE):
                objects.append(visualization_obj('bench-dimsweep-car-%s-%s' % (track, key),
                    '%s %d 维 · %s' % (LINEAGE_LABEL.get(lin, lin), dims, title), filt, fmt, terms,
                    aggregation, iv))
    return dashboard_objects('bench-dimsweep-car', 'Benchmark · 向量维度扫描 · 车对比', objects,
                             time_from, time_to, DASH_SCOPE)


USAGE_MD = '''**看图口径**
- 测量面板自身强制 `trusted:true`；错误率不加 trusted，显示每个 task 的桶内最坏值。
- 全局默认只排除 `codec:(zstd or best_compression)` 与 `source_reuse:true`，不排除错误率证据。
- 每板固定真实 runner `track_variant`；非默认 int8/bbq/codec/source_reuse 变体另有独立板。
- `series_key` 是产品身份；`comparison_key=series_key|track_variant` 才是实验分线键。
- 吞吐/时延/GC/资源均跨圈取中位（TSVB percentile 50）；错误率和 system 健康摘要取桶内最坏。
  单场 p50/p99 是其输入统计，不能当跨圈中位。
- x 轴 `run_start`；一个点是该时间桶内相同身份、variant、task、stat 的中位，不宣称每场一个点。
- 所有时延单位为秒 s；跨引擎看 service_time，latency 含开环发压机积压。
- 按引擎筛选写 `series_key:ez@*`，通配符不要加引号。
- 静态维度表是建板时全部批次的快照，不随顶部查询/时间改变。
'''


def env_markdown():
    result = es_get('/benchmark-result-env/_search?size=100')
    rows = ['**环境台账** · 来源 benchmark-result-env；各项为实测/导出快照', '']
    for hit in result['hits']['hits']:
        doc = hit['_source']
        topo, host = doc.get('topology') or {}, doc.get('host') or {}
        rows.append('**环境 `%s`** · %s · %s 节点 × %s vCPU / %s GB · 堆 %s GB · 绑核 %s' % (
            doc.get('env_id'), doc.get('host_type'), topo.get('node_count'), doc.get('vcpu'),
            doc.get('mem_gb'), topo.get('heap_gb'), topo.get('cpu_pinning')))
        rows.append('%s · %s · kernel %s · %s' % (
            doc.get('cpu_model') or host.get('cpu_model'), doc.get('os'),
            doc.get('kernel') or host.get('kernel'), doc.get('notes', '')))
    check = es_post('/benchmark-result-race/_search', {'size': 0, 'track_total_hits': True,
        'aggs': {'trusted': {'terms': {'field': 'trusted', 'size': 2}},
                 'variants': {'filter': {'bool': {'should': [
                     {'terms': {'codec': list(CODEC_EXCLUDED)}}, {'term': {'source_reuse': True}}],
                     'minimum_should_match': 1}}},
                 'missing_key': {'filter': {'bool': {'must_not': {'exists': {'field': COMPARISON_FIELD}}}}}}})
    aggs = check['aggregations']
    rows.extend(['', '**场次体检** — 共 %d 场；可信度 %s；压缩/source_reuse 变体 %d 场；'
                 '缺 comparison_key %d 场' % (check['hits']['total']['value'],
                 ' / '.join('%s=%d' % (b.get('key_as_string', b['key']), b['doc_count'])
                            for b in aggs['trusted']['buckets']),
                 aggs['variants']['doc_count'], aggs['missing_key']['doc_count'])])
    configs = list(composite_buckets('benchmark-result-race', [
        {'car': {'terms': {'field': 'car_code', 'missing_bucket': True}}},
        {'series': {'terms': {'field': 'series_key', 'missing_bucket': True}}},
        {'experiment': {'terms': {'field': COMPARISON_FIELD, 'missing_bucket': True}}}]))
    rows.extend(['', '| 配置代号 | 产品身份 series_key | 实验 comparison_key | 场次 |', '|---|---|---|---|'])
    for bucket in configs:
        key = bucket['key']
        rows.append('| %s | %s | %s | %d |' % (
            *(str(key[name]).replace('|', '&#124;').replace('\n', ' ') for name in ('car', 'series', 'experiment')),
            bucket['doc_count']))
    return '\n'.join(rows)


def nav_markdown(track_info, ds_info=None):
    md = ['**赛道 / 实验导航**', '', '| 赛道 | runner variant | 场次 | 看板路径 |', '|---|---|---|---|']
    for track, info in track_info:
        md.append('| %s | %s | %d | `/app/dashboards#/view/%s` |' % (
            track, info['variant'], info['runs'], info['dashboard']))
    for did, title in ds_info or []:
        md.append('| 维度扫描 | %s | — | `/app/dashboards#/view/%s` |' % (title, did))
    return '\n'.join(md)


def overview_dashboard(track_info):
    objects = [markdown_obj('bench-vis-head', '环境说明', env_markdown()),
               markdown_obj('bench-vis-nav', '赛道导航', nav_markdown(track_info)),
               markdown_obj('bench-vis-guide', '看图指南', USAGE_MD)]
    return dashboard_objects('bench-overview', 'Benchmark 赛道总览', objects, *data_window(), '')


def main():
    global ES, KIBANA, ES_AUTH, ES_INSECURE, KIBANA_VERSION, KIBANA_MAJOR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--import', dest='do_import', action='store_true')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(__file__), 'kibana-objects.ndjson'))
    ap.add_argument('--es', default=ES)
    ap.add_argument('--kibana', default=KIBANA)
    ap.add_argument('--es-auth')
    ap.add_argument('--insecure', action='store_true')
    ap.add_argument('--kibana-version')
    args = ap.parse_args()
    ES, KIBANA, ES_AUTH, ES_INSECURE = args.es, args.kibana, args.es_auth, args.insecure
    if args.kibana_version:
        KIBANA_VERSION = args.kibana_version
        KIBANA_MAJOR = int(KIBANA_VERSION.split('.')[0])
    else:
        detect_kibana_version()
    objects = [index_pattern_obj(index) for index in
               ('benchmark-result-metric', 'benchmark-result-race', 'benchmark-result-env')]
    facts = es_track_facts()
    if not facts:
        raise ValueError('指标库没有可建板的场次')
    track_info = []
    tf, tt = data_window()
    for track, fact in sorted(facts.items()):
        for variant, info in sorted(fact['variants'].items()):
            built, _dropped = build_track_dashboard(track, group_tasks(info['tasks']), '1d', tf, tt, variant)
            objects.extend(built)
            track_info.append((track, {'variant': variant, 'dashboard': track_dashboard_id(track, variant),
                                      'runs': es_track_runs(track, variant)['runs']}))
    # 独立赛道板永不因合并板规则跳过；两台车或单维度也完整可见。
    ds_info = []
    lin_tracks = ds_lineage_tracks(facts)
    if lin_tracks:
        rows, cars = ds_collect()
        objects.extend(build_dimsweep_dashboard(lin_tracks, rows, cars, ds_main_car(cars), '1d', tf, tt))
        ds_info.append(('bench-dimsweep', '维度曲线'))
        car_tracks = [(track, dims, lin) for lin, tracks in lin_tracks.items() for dims, track in tracks
                      if len(facts[track]['variants'][runner_base(track)]['cars']) >= 2]
        if car_tracks:
            objects.extend(build_dimsweep_car_dashboard(car_tracks, facts, '1d', tf, tt))
            ds_info.append(('bench-dimsweep-car', '车对比'))
    overview = overview_dashboard(track_info)
    nav = next(o for o in overview if o['id'] == 'bench-vis-nav')
    nav['attributes'] = markdown_obj('bench-vis-nav', '赛道导航', nav_markdown(track_info, ds_info))['attributes']
    objects.extend(overview)
    ids = [o['id'] for o in objects]
    if len(ids) != len(set(ids)):
        raise ValueError('saved object ID 重复')
    ndjson = ''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in objects)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(ndjson)
    print('wrote %s: %d objects' % (args.out, len(objects)))
    if args.do_import:
        prune_stale(set(ids))
        boundary = '----racewayboundary'
        body = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="kibana-objects.ndjson"\r\n'
                 'Content-Type: application/json\r\n\r\n') % boundary).encode() + ndjson.encode() + \
               ('\r\n--%s--\r\n' % boundary).encode()
        req = urlreq.Request(KIBANA + '/api/saved_objects/_import?overwrite=true', data=body,
                             headers={'kbn-xsrf': 'true', 'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with urlreq.urlopen(req, timeout=120) as r:
            response = json.loads(r.read())
        if not response.get('success') or response.get('errors'):
            raise RuntimeError('Kibana 导入失败: %s' % response)
        print('imported %d objects' % response['successCount'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
