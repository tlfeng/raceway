#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 Kibana Saved Objects 导入包(kibana-objects.ndjson), 模仿 opensearch.org/benchmarks:
  * 每图一个指标族(family), series = arm(实验臂), x 轴 = 场次时间(@timestamp)
  * 面板: 写入吞吐 / 索引耗时 / 查询吞吐 / 查询延迟p90 / 服务时间p99 / 错误率
         / KNN 吞吐 / 向量维度扫描 / 存储 / 段数 / GC
  * 顶部 Markdown = 环境台账(bench-env) + 固定过滤说明; 底部 = 原始层回钻路径
  * 仪表盘级过滤: 顶部 KQL 查询框(默认 valid: true), 用户可直接改表达式

用法:
  python3 make_dashboard.py                 # 生成 kibana-objects.ndjson
  python3 make_dashboard.py --import        # 生成并直接导入 Kibana(overwrite)
  python3 make_dashboard.py --es http://127.0.0.1:9200 --kibana http://127.0.0.1:5601

依赖: 仅 python3 标准库; 面板为 TSVB(Visualize->Metrics), 图表过滤用 Lucene 语法。

产物 kibana-objects.ndjson 是本地生成物, 不入库(.gitignore): 其"环境说明"面板由
bench-env 现场读出渲染, 内嵌真实环境指纹与场次统计, 随每次入库数据变化。
"""
import argparse
import datetime
import json
import os
import uuid
from urllib import request as urlreq

ES = 'http://localhost:9200'
KIBANA = 'http://localhost:5601'

# ---------------------------------------------------------------- ES helpers

def es_get(path):
    with urlreq.urlopen(ES + path) as r:
        return json.loads(r.read())


def es_post(path, body):
    req = urlreq.Request(ES + path, data=json.dumps(body).encode('utf-8'),
                         headers={'Content-Type': 'application/json'})
    with urlreq.urlopen(req) as r:
        return json.loads(r.read())


def flat_fields(index, prefix='', defs=None):
    """mapping -> [(fqn, es_type)]; 跳过 enabled:false 对象"""
    defs = defs or []
    m = es_get('/%s/_mapping' % index)
    props = next(iter(m.values()))['mappings'].get('properties', {})
    def walk(props, prefix):
        for name, spec in props.items():
            fqn = prefix + name
            t = spec.get('type')
            if t:
                defs.append((fqn, t))
            elif spec.get('properties'):
                walk(spec['properties'], fqn + '.')
    walk(props, prefix)
    return defs


def kibana_field_type(es_type):
    return {'integer': 'number', 'long': 'number', 'double': 'number',
            'float': 'number', 'short': 'number', 'byte': 'number',
            'keyword': 'string', 'text': 'string', 'date': 'date',
            'boolean': 'boolean'}.get(es_type, 'unknown')


def index_pattern_obj(name):
    fields = []
    for fqn, t in flat_fields(name):
        numeric = kibana_field_type(t) == 'number'
        fields.append({
            'name': fqn, 'type': kibana_field_type(t), 'esTypes': [t],
            'count': 0, 'scripted': False,
            'searchable': t != 'object',
            'aggregatable': t in ('keyword', 'date', 'boolean') or numeric,
            'readFromDocValues': t in ('keyword', 'date', 'boolean') or numeric,
        })
    time_field = '@timestamp' if any(f[0] == '@timestamp' for f in flat_fields(name)) else None
    return {
        'id': name, 'type': 'index-pattern',
        'attributes': {'title': name, 'timeFieldName': time_field,
                       'fields': json.dumps(fields)},
        'references': [],
    }

# ---------------------------------------------------------------- TSVB panels

# TSVB 面板内的 UUID 必须是固定值（saved object 里成对出现），用同一批常量即可
PANEL = '61ca57f0-469d-11e7-af02-69e470af7417'
SERIES = '61ca57f1-469d-11e7-af02-69e470af7417'
METRIC = '61ca57f2-469d-11e7-af02-69e470af7417'

# index pattern 引用名。Kibana 8 的 saved object 用 references 关联 index-pattern，
# 少了它前端会在请求里丢掉 interval（退化成 auto），面板的桶粒度就随时间跨度漂移。
IP_REF = 'metrics_0_index_pattern'
IP_ID = 'bench-metric'

COLORS = ['rgba(104,188,0,1)', 'rgba(255,128,0,1)', 'rgba(0,161,255,1)',
          'rgba(212,0,255,1)', 'rgba(255,0,0,1)', 'rgba(0,161,128,1)']


def tsvb_series(idx, terms_field, metric_type, metric_field, formatter):
    """一条 TSVB 曲线：按 terms_field 分组，对 metric_field 做聚合"""
    series_id = SERIES
    metric_id = METRIC
    metric = {'id': metric_id, 'type': metric_type, 'field': metric_field}
    return {
        'id': series_id, 'color': COLORS[idx % len(COLORS)], 'split_mode': 'terms',
        'field': metric_field, 'metrics': [metric],
        'separate_axis': 0, 'axis_position': 'right', 'formatter': formatter,
        'chart_type': 'line', 'line_width': 2, 'point_size': 1, 'fill': '0',
        'stacked': 'none', 'terms_field': terms_field, 'terms_order_by': metric_id,
    }


def tsvb_timeseries(title, panel_filter, formatter, terms_field='arm',
                    metric_type='avg', metric_field='value', index_pattern='bench-metric',
                    interval='1d'):
    return {
        'type': 'metrics', 'aggs': [],
        'params': {
            'id': PANEL, 'type': 'timeseries',
            'series': [tsvb_series(0, terms_field, metric_type, metric_field, formatter)],
            'time_field': '@timestamp', 'index_pattern': index_pattern,
            # interval 必须固定成日粒度：interval='auto' 在 now-1y 的跨度下会选 7 天桶，
            # 而压测场次集中在 6 天内 → 所有数据落进唯一一个桶 = 一个数据点，
            # 折线只有一个顶点画不出来（表现为 y 轴 0~0 的空图，ES 侧其实有数据）。
            'interval': interval,
            'axis_position': 'left', 'axis_formatter': 'number',
            'show_legend': 1, 'show_grid': 1, 'legend_position': 'right',
            # filter 必须是 {language, query} 对象：服务端 buildEsQuery 只认带 query 属性的
            # 结构，纯字符串会被 validQueries 过滤掉 —— 面板不报错，但过滤条件静默失效，
            # 于是所有 family 的数据混在一张图里。language=lucene 对应 Options 里的
            # "Filter" 输入框（Kuery 则要改成 KQL 语法）。
            'filter': {'language': 'lucene', 'query': panel_filter},
        },
        'title': title,
    }


MAINT_TASKS = 'index-stats OR node-stats OR force-merge OR refresh-after-index'
KNN_TASKS = 'knn-search-10-100 OR knn-search-100-1000 OR knn-search-100-1000-x32'

# (vis_id, 标题, lucene 过滤, 数字格式, 分组字段, 指标类型, 桶粒度)
# 注意 family 取值来自 bench-export.py 的 RESULT_METRICS 映射：
#   segment_count -> family=disk（不是 segments！曾按 segments 写过滤，整块面板恒为空白）
#   store_size / translog_size / dataset_size -> family=disk
#   young_gc_time -> family=gc, total_time -> family=indexing_time
# 粒度说明：常规面板按天（一个点 = 一天内该臂的均值），向量面板按 2 小时
#   —— ds3 维度扫描整场 campaign 只跨 12 小时，按天只剩 1 个点画不出折线。
PANELS = [
    ('bench-vis-wthr', '写入吞吐 · median docs/s',
     'family:throughput AND unit:"docs/s" AND stat:median', '0.0a', 'arm', 'avg', '1d'),
    ('bench-vis-idx', '索引总耗时 (s)',
     'family:indexing_time AND metric:total_time AND stat:value', '0.0[0]', 'arm', 'avg', '1d'),
    ('bench-vis-qthr', '查询吞吐 · median ops/s',
     'family:throughput AND unit:"ops/s" AND stat:median '
     'AND NOT task:(%s)' % MAINT_TASKS, '0.0a', 'arm', 'avg', '1d'),
    ('bench-vis-qlat', '查询延迟 p90 (s)',
     'family:latency AND stat:p90 '
     'AND NOT task:(index-append OR %s)' % MAINT_TASKS, '0.0[00]', 'arm', 'avg', '1d'),
    ('bench-vis-svc', '查询服务时间 p99 (s)',
     'family:service_time AND metric:service_time AND stat:p99 '
     'AND NOT task:(index-append OR %s)' % MAINT_TASKS, '0.0[00]', 'arm', 'avg', '1d'),
    ('bench-vis-err', '错误率 (0-1)',
     'family:error_rate AND stat:value', '0.000', 'arm', 'avg', '1d'),
    ('bench-vis-knn', 'KNN 检索吞吐 · median ops/s',
     'family:throughput AND unit:"ops/s" AND stat:median AND task:(%s)' % KNN_TASKS,
     '0.0a', 'arm', 'avg', '2h'),
    ('bench-vis-dims', '向量维度扫描 · 查询吞吐 (按维度分线)',
     'family:throughput AND unit:"ops/s" AND stat:median AND dims:>0 '
     'AND NOT task:(%s)' % MAINT_TASKS, '0.0a', 'dims', 'avg', '2h'),
    ('bench-vis-disk', '存储占用 (GB)',
     'family:disk AND metric:store_size AND stat:value', '0.0', 'arm', 'avg', '1d'),
    ('bench-vis-seg', '段数量',
     'family:disk AND metric:segment_count AND stat:value', '0.0a', 'arm', 'avg', '1d'),
    ('bench-vis-gc', 'Young GC 耗时 (s)',
     'family:gc AND metric:young_gc_time AND stat:value', '0.0[00]', 'arm', 'avg', '1d'),
]


def visualization_obj(vis_id, title, panel_filter, formatter,
                      terms_field='arm', metric_type='avg', interval='1d'):
    params = tsvb_timeseries(title, panel_filter, formatter,
                             terms_field=terms_field,
                             metric_type=metric_type,
                             interval=interval)
    params['index_pattern_ref_name'] = IP_REF
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': {
            'title': title,
            'visState': json.dumps(params),
            'uiStateJSON': '{}',
            'description': '',
            # 不写 attributes.version: Kibana 自管, 8.x 迁移会把非 semver 的值喂给 semver 库
            # ("Migration function for version 7.11.0 threw an error: Invalid Version: 1")
            'kibanaSavedObjectMeta': {'searchSourceJSON': '{"filter":[]}'},
        },
        'references': [{'id': IP_ID, 'name': IP_REF, 'type': 'index-pattern'}],
        'migrationVersion': {'visualization': '7.10.0'},
    }

# ---------------------------------------------------------------- markdown 面板

def env_markdown():
    """环境台账（bench-env）+ 口径体检（方案 §6 底部环境说明）"""
    md = ['**Benchmark 结果总览** · 数据源 `bench-run` / `bench-metric`'
          '（raceway bench-export.py 离线入库，源 = rally race.json + sidecar）',
          '',
          '每点 = 一场 race；曲线 = 实验臂(arm)。顶部 KQL 查询框默认 `valid: true`，可改成任意表达式。']
    try:
        r = es_get('/bench-env/_search?size=10')
        for hit in r['hits']['hits']:
            d = hit['_source']
            topo = d.get('topology') or {}
            host = d.get('host') or {}
            md.append('')
            md.append('**环境 %s** — %s，%s 节点 × %s vCPU / %s GB，堆 %s GB，绑核 %s'
                      % (d.get('env_id'), d.get('host_type') or '未知形态',
                         topo.get('node_count'), d.get('vcpu'), d.get('mem_gb'),
                         topo.get('heap_gb'), topo.get('cpu_pinning')))
            md.append('%s · %s · kernel %s'
                      % (d.get('cpu_model') or host.get('cpu_model'),
                         d.get('os') or '-', d.get('kernel') or host.get('kernel') or '-'))
            if d.get('notes'):
                md.append(d['notes'])
    except Exception as e:                                   # noqa: BLE001
        md.append('')
        md.append('(bench-env 读取失败: %s)' % e)

    # 口径体检：valid / 实验臂 / 赛道
    try:
        r = es_post('/bench-run/_search', {
            'size': 0,
            'aggs': {
                'by_valid': {'terms': {'field': 'valid', 'size': 5}},
                'arms': {'terms': {'field': 'arm', 'size': 20}},
                'by_track': {'terms': {'field': 'track', 'size': 20}},
            }})
        aggs = r['aggregations']
        total = r['hits']['total']['value']
        # boolean 字段的 terms 聚合返回的 key 是 0/1（不是 true/false），
        # 用 `is not True` 判身份会把 1 也算成"非 VALID"，把 37 算成 230
        truthy = (True, 1, 'true')
        n_valid = sum(b['doc_count'] for b in aggs['by_valid']['buckets'] if b['key'] in truthy)
        n_invalid = sum(b['doc_count'] for b in aggs['by_valid']['buckets']
                         if b['key'] not in truthy)
        md.append('')
        md.append('**场次体检** — 共 %d 场（VALID %d / 非 VALID %d）' % (total, n_valid, n_invalid))
        md.append('实验臂: ' + ' · '.join('%s=%d' % (b['key'], b['doc_count'])
                                         for b in aggs['arms']['buckets']))
        md.append('赛道: ' + ' · '.join('%s=%d' % (b['key'], b['doc_count'])
                                        for b in aggs['by_track']['buckets']))
    except Exception as e:                                   # noqa: BLE001
        md.append('')
        md.append('(bench-run 体检读取失败: %s)' % e)
    return '\n'.join(md)


def _bucket(aggs, key):
    """terms 聚合取值；boolean 字段的 key 是 0/1，这里按真值语义匹配"""
    truthy = (True, 1, 'true')
    for b in aggs['buckets']:
        if (b['key'] in truthy) if key in truthy else (b['key'] == key):
            return b['doc_count']
    return 0


def markdown_obj(vis_id, title, md):
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': {
            'title': title,
            'visState': json.dumps({'type': 'markdown', 'aggs': [],
                                    'params': {'fontSize': 12, 'markdown': md},
                                    'title': title}),
            'uiStateJSON': '{}', 'description': '',
            'kibanaSavedObjectMeta': {'searchSourceJSON': '{"filter":[]}'},
        },
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }


def section_obj(vis_id, title, text):
    """小节标题（markdown），用来把面板按 写入/查询/向量/资源 分段"""
    md = '**%s** — %s' % (title, text)
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': {
            'title': title,
            'visState': json.dumps({'type': 'markdown', 'aggs': [],
                                    'params': {'fontSize': 14, 'markdown': md},
                                    'title': title}),
            'uiStateJSON': '{}', 'description': '',
            'kibanaSavedObjectMeta': {'searchSourceJSON': '{"filter":[]}'},
        },
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }


USAGE_MD = '''**看图指南**
- 同 track/dims 下比较: 顶部过滤器加 `track:geo` / `dims:1024`
- 只看有效场次: 顶部 KQL 框保持 `valid: true`（默认）；要看失败场次改成 `valid: false`
- 限定实验臂 / campaign: `arm:es819` / `campaign:ds3-20261015-233223`
- 每图一个 family（吞吐/延迟/服务时间/磁盘/GC…），单位已归一（时间 s、大小 GB、错误率 0~1）
- series=arm: jdk25/kona25=easysearch2.5.0-3015+JDK25, ez240=easysearch2.4.0-2975, es819=ES8.19.23
- **错误率说明**: rally 的 error_rate 本就是 0~1 比例，1.0 = 该 task 请求全失败（不要按 % 再除）
- 原始层回钻: campaigns-local/<campaign>/results/<fid>-race.json'''

# ---------------------------------------------------------------- dashboard

SECTIONS = [
    ('bench-sec-write', '① 写入 (write)', '索引阶段吞吐与总耗时，docs/s 口径'),
    ('bench-sec-query', '② 查询 (query)', '检索吞吐与延迟，已剔除 index-stats/node-stats/force-merge 等维护型 task'),
    ('bench-sec-vector', '③ 向量检索 (knn / dims)', 'KNN 任务吞吐；维度扫描按 dims 分线，x 轴为场次时间'),
    ('bench-sec-res', '④ 资源与存储', '赛末存储体积、段数、Young GC 耗时'),
]


def data_window(pad_days=1):
    """仪表盘默认时间窗 = 数据的实际时间范围（各留 1 天余量）。

    为什么不用固定相对窗：压测批次集中在 4 天内，若用 now-90d 这类固定窗，
    曲线会全部挤在最右侧 4% 的位置，完全看不出趋势。窗口跟着数据走，
    打开就是"这一批结果"的完整横轴。

    返回 (timeFrom, timeTo) 字符串；ES 里没数据时退回 now-90d。
    """
    try:
        r = es_post('/bench-run/_search', {
            'size': 0,
            'aggs': {'span': {'stats': {'field': '@timestamp'}},
                     'n': {'value_count': {'field': '@timestamp'}}}})
        st = r['aggregations']['span']
        if not st.get('count'):
            return 'now-90d', 'now'
        lo = datetime.datetime.fromtimestamp(st['min'] / 1000, datetime.timezone.utc)
        hi = datetime.datetime.fromtimestamp(st['max'] / 1000, datetime.timezone.utc)
        lo = lo - datetime.timedelta(days=pad_days)
        hi = hi + datetime.timedelta(days=pad_days)
        return lo.strftime('%Y-%m-%dT%H:%M:%S.000Z'), hi.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    except Exception:                                       # noqa: BLE001
        return 'now-90d', 'now'


def dashboard_obj():
    panels, refs = [], []

    def add(vis_id, x, y, w, h, ptype='visualization'):
        i = str(len(panels) + 1)
        panels.append({'embeddableConfig': {},
                       'gridData': {'x': x, 'y': y, 'w': w, 'h': h, 'i': i},
                       # 面板 version 必须是合法 semver(真实导出里=创建时的 Kibana 版本);
                       # 非 semver 会让 7.11.0 迁移的 semver 比较直接抛 "Invalid Version"
                       'id': vis_id, 'panelIndex': i, 'type': ptype, 'version': '7.10.0'})
        refs.append({'id': vis_id, 'name': i, 'type': ptype})

    add('bench-vis-head', 0, 0, 48, 11)
    add('bench-vis-guide', 0, 11, 48, 8)

    # 面板按段排布：每段 2 列，行高 13；段标题整行高 3
    layout = [
        ('bench-sec-write', ['bench-vis-wthr', 'bench-vis-idx']),
        ('bench-sec-query', ['bench-vis-qthr', 'bench-vis-qlat',
                             'bench-vis-svc', 'bench-vis-err']),
        ('bench-sec-vector', ['bench-vis-knn', 'bench-vis-dims']),
        ('bench-sec-res', ['bench-vis-disk', 'bench-vis-seg', 'bench-vis-gc']),
    ]
    y = 19
    for sec_id, vis_ids in layout:
        add(sec_id, 0, y, 48, 3)
        y += 3
        for n, vis_id in enumerate(vis_ids):
            add(vis_id, 24 * (n % 2), y, 24, 13)
            if n % 2 == 1:
                y += 13
        if len(vis_ids) % 2:
            y += 13

    # 仪表盘级过滤器：用 query_string 过滤类型（query 里带完整 DSL）。
    # 注意不能用 meta.type=custom —— 那种过滤器要求 query 已经是 DSL，
    # 传 {query,language} 会被当成 DSL 直接发给 ES，所有面板一起报
    # "[query] query malformed, no start_object after query name"。
    # 仪表盘级过滤：放在 dashboard 的 query（KQL 表达式）而不是 filter 数组。
    #  - filter 数组要求 meta.type 是 Kibana 原生类型：query_string 是 legacy（UI 标成
    #    Warning），custom 又要求 query 已是 DSL（传 kuery 会被当 DSL 发给 ES，
    #    所有面板一起报 "[query] query malformed"）。
    #  - query 是 dashboard 顶部的 KQL 搜索框，天然可编辑、UI 正常显示。
    scope = 'valid: true'
    time_from, time_to = data_window()
    return {
        'id': 'bench-overview', 'type': 'dashboard',
        'attributes': {
            'title': 'Benchmark 性能总览 · easysearch vs ES',
            'hits': 0,
            # 时间范围必须显式保存：默认是"最近 15 分钟"，而压测数据是历史场次，
            # 不设 timeRestore 的话仪表盘打开就是一片空白。窗口按数据实际范围算。
            'timeRestore': True, 'timeFrom': time_from, 'timeTo': time_to,
            'refreshInterval': {'pause': True, 'value': 0},
            'description': 'rally race 结果可视化（对照 opensearch.org/benchmarks；'
                           '源 = raceway bench-export.py 导出的 bench-run/bench-metric）',
            'panelsJSON': json.dumps(panels),
            'optionsJSON': json.dumps({'hidePanelTitles': False, 'useMargins': True}),
            'kibanaSavedObjectMeta': {
                'searchSourceJSON': json.dumps({
                    'query': {'query': scope, 'language': 'kuery'},
                    'filter': [],
                })},
        },
        'references': refs,
        'migrationVersion': {'dashboard': '7.9.3'},
    }


def main():
    global ES, KIBANA
    ap = argparse.ArgumentParser()
    ap.add_argument('--import', dest='do_import', action='store_true')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(__file__), 'kibana-objects.ndjson'))
    ap.add_argument('--es', default=ES)
    ap.add_argument('--kibana', default=KIBANA)
    args = ap.parse_args()

    ES, KIBANA = args.es, args.kibana

    objs = [index_pattern_obj('bench-metric'),
            index_pattern_obj('bench-run'),
            index_pattern_obj('bench-env'),
            markdown_obj('bench-vis-head', '环境说明', env_markdown()),
            markdown_obj('bench-vis-guide', '看图指南', USAGE_MD)]
    for vis_id, title, text in SECTIONS:
        objs.append(section_obj(vis_id, title, text))
    for vis_id, title, filt, fmt, terms, mtype, interval in PANELS:
        objs.append(visualization_obj(vis_id, title, filt, fmt,
                                       terms_field=terms, metric_type=mtype,
                                       interval=interval))
    objs.append(dashboard_obj())

    ndjson = '\n'.join(json.dumps(o, ensure_ascii=False) for o in objs) + '\n'
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(ndjson)
    print('wrote %s: %d objects' % (args.out, len(objs)))

    if args.do_import:
        boundary = '----zcodebenchboundary'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{os.path.basename(args.out)}"\r\n'
                f'Content-Type: application/json\r\n\r\n').encode() + \
               ndjson.encode() + f'\r\n--{boundary}--\r\n'.encode()
        req = urlreq.Request(
            f'{KIBANA}/api/saved_objects/_import?overwrite=true', data=body, method='POST',
            headers={'kbn-xsrf': 'true',
                     'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urlreq.urlopen(req) as r:
            print(json.dumps(json.loads(r.read()), ensure_ascii=False))


if __name__ == '__main__':
    main()
