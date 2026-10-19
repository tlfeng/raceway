#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 Kibana 7.10 Saved Objects 导入包(kibana-objects.ndjson), 模仿 opensearch.org/benchmarks:
  * 每图一个指标族(family), series = arm(实验臂), x 轴 = 场次时间(@timestamp)
  * 面板: 写入吞吐 / 查询吞吐 / 查询延迟p90 / 服务时间p99 / 索引耗时 / 存储 / GC / 段数 / 错误率
  * 顶部 Markdown = 环境台账(bench-env) + 固定过滤说明

用法:
  python3 make_dashboard.py                 # 生成 kibana-objects.ndjson
  python3 make_dashboard.py --import        # 生成并直接导入 Kibana(overwrite)
依赖: 仅 python3 标准库; 面板为 TSVB(Visualize->Metrics), 图表过滤用 Lucene 语法
"""
import argparse
import json
import os
from urllib import request as urlreq

ES = 'http://localhost:9200'
KIBANA = 'http://localhost:5601'

# ---------------------------------------------------------------- ES helpers

def es_get(path):
    with urlreq.urlopen(ES + path) as r:
        return json.loads(r.read())

def flat_fields(index, prefix='', defs=None):
    """mapping -> [(fqn, es_type)]; 跳过 enabled:false 对象"""
    defs = defs or []
    m = es_get(f'/{index}/_mapping')
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

PANEL = '61ca57f0-469d-11e7-af02-69e470af7417'
SERIES = '61ca57f1-469d-11e7-af02-69e470af7417'
METRIC = '61ca57f2-469d-11e7-af02-69e470af7417'

def tsvb_timeseries(title, panel_filter, formatter):
    return {
        'type': 'metrics', 'aggs': [],
        'params': {
            'id': PANEL, 'type': 'timeseries',
            'series': [{
                'id': SERIES, 'color': 'rgba(104,188,0,1)', 'split_mode': 'terms',
                'field': 'value',
                'metrics': [{'id': METRIC, 'type': 'avg', 'field': 'value'}],
                'separate_axis': 0, 'axis_position': 'right', 'formatter': formatter,
                'chart_type': 'line', 'line_width': 2, 'point_size': 1, 'fill': '0',
                'stacked': 'none', 'terms_field': 'arm', 'terms_order_by': METRIC,
            }],
            'time_field': '@timestamp', 'index_pattern': 'bench-metric',
            'interval': 'auto', 'axis_position': 'left', 'axis_formatter': 'number',
            'show_legend': 1, 'show_grid': 1, 'legend_position': 'right',
            'filter': panel_filter,
        },
        'title': title,
    }

MAINT_TASKS = 'index-stats OR node-stats OR force-merge OR refresh-after-index'

# (vis_id, 标题, lucene 过滤, 数字格式)
PANELS = [
    ('bench-vis-wthr', '写入吞吐 · median docs/s',
     f'family:throughput AND unit:"docs/s" AND stat:median', '0.0a'),
    ('bench-vis-qthr', '查询吞吐 · median ops/s',
     f'family:throughput AND unit:"ops/s" AND stat:median '
     f'AND NOT task:({MAINT_TASKS})', '0.0a'),
    ('bench-vis-qlat', '查询延迟 p90 (s)',
     f'family:latency AND stat:p90 '
     f'AND NOT task:(index-append OR {MAINT_TASKS})', '0.0[00]'),
    ('bench-vis-svc', '查询服务时间 p99 (s)',
     f'family:service_time AND metric:service_time AND stat:p99 '
     f'AND NOT task:(index-append OR {MAINT_TASKS})', '0.0[00]'),
    ('bench-vis-idx', '索引总耗时 (s)',
     'family:indexing_time AND metric:total_time AND stat:value', '0.0[0]'),
    ('bench-vis-disk', '存储占用 (GB)',
     'family:disk AND metric:store_size AND stat:value', '0.0'),
    ('bench-vis-gc', 'Young GC 耗时 (s)',
     'family:gc AND metric:young_gc_time AND stat:value', '0.0[00]'),
    ('bench-vis-seg', '段数量',
     'family:segments AND metric:segment_count AND stat:value', '0.0a'),
    ('bench-vis-err', '错误率 (0-1)',
     'family:error_rate AND stat:value', '0.000'),
]

def visualization_obj(vis_id, title, panel_filter, formatter):
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': {
            'title': title,
            'visState': json.dumps(tsvb_timeseries(title, panel_filter, formatter)),
            'uiStateJSON': '{}',
            'description': '',
            'version': 1,
            'kibanaSavedObjectMeta': {'searchSourceJSON': '{"filter":[]}'},
        },
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }

# ---------------------------------------------------------------- markdown 面板

def env_markdown():
    md = ['**Benchmark 结果总览** · 数据源 `bench-run` / `bench-metric`(ingest.py 离线入库, 源=rally race.json + sidecar)',
          '',
          '每点 = 一场 race; 曲线 = 实验臂(arm)。建议固定过滤: `campaign` / `track` / `dims` / `valid:true`']
    try:
        r = es_get('/bench-env/_search?size=10')
        for hit in r['hits']['hits']:
            d = hit['_source']
            md.append('')
            md.append(f"**环境 {d['env_id']}** — {d.get('host_type')}, "
                      f"{(d.get('topology') or {}).get('node_count')} 节点 × "
                      f"{d.get('vcpu')} vCPU / {d.get('mem_gb')} GB, heap "
                      f"{(d.get('topology') or {}).get('heap_gb')} GB, 绑核 "
                      f"{(d.get('topology') or {}).get('cpu_pinning')}")
            md.append(f"{d.get('cpu_model')} · kernel {d.get('kernel')}")
    except Exception as e:
        md.append(f'(bench-env 读取失败: {e})')
    return '\n'.join(md)

def markdown_obj(vis_id, title, md):
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': {
            'title': title,
            'visState': json.dumps({'type': 'markdown', 'aggs': [],
                                    'params': {'fontSize': 12, 'markdown': md},
                                    'title': title}),
            'uiStateJSON': '{}', 'description': '', 'version': 1,
            'kibanaSavedObjectMeta': {'searchSourceJSON': '{"filter":[]}'},
        },
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }

USAGE_MD = '''**看图指南**
- 同 track/dims 下比较: 顶部过滤器加 `track:<name>` `dims:<n>`
- 只看有效场次: 加 `valid:true`
- 每图一个 family(吞吐/延迟/服务时间/磁盘/GC...), 单位已归一(时间 s, 大小 GB)
- series=arm: jdk25/kona25=easysearch2.5.0-3015+JDK25, es819=ES8.19.23, ez240=easysearch2.4.0
- 原始层回钻: campaigns-local/<campaign>/results/<fid>-race.json'''

# ---------------------------------------------------------------- dashboard

def dashboard_obj():
    panels, refs = [], []
    def add(vis_id, x, y, w, h, ptype='visualization'):
        i = str(len(panels) + 1)
        panels.append({'embeddableConfig': {},
                       'gridData': {'x': x, 'y': y, 'w': w, 'h': h, 'i': i},
                       'id': vis_id, 'panelIndex': i, 'type': ptype, 'version': '1'})
        refs.append({'id': vis_id, 'name': i, 'type': ptype})

    add('bench-vis-head', 0, 0, 48, 8)
    add('bench-vis-guide', 0, 8, 48, 5)
    pos = [(0, 13), (24, 13), (0, 26), (24, 26), (0, 39), (24, 39),
           (0, 52), (24, 52), (0, 65)]
    for (vis_id, *_), (x, y) in zip(PANELS, pos):
        add(vis_id, x, y, 24, 13)
    return {
        'id': 'bench-overview', 'type': 'dashboard',
        'attributes': {
            'title': 'Benchmark 性能总览 · easysearch vs ES',
            'hits': 0, 'timeRestore': False,
            'description': 'rally race 结果可视化样例 (对照 opensearch.org/benchmarks)',
            'panelsJSON': json.dumps(panels),
            'optionsJSON': json.dumps({'hidePanelTitles': False, 'useMargins': True}),
            'version': 1,
            'kibanaSavedObjectMeta': {
                'searchSourceJSON': json.dumps(
                    {'query': {'language': 'kuery', 'query': ''}, 'filter': []})},
        },
        'references': refs,
        'migrationVersion': {'dashboard': '7.9.3'},
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--import', dest='do_import', action='store_true')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(__file__), 'kibana-objects.ndjson'))
    args = ap.parse_args()

    objs = [index_pattern_obj('bench-metric'),
            index_pattern_obj('bench-run'),
            index_pattern_obj('bench-env'),
            markdown_obj('bench-vis-head', '环境说明', env_markdown()),
            markdown_obj('bench-vis-guide', '看图指南', USAGE_MD)]
    objs += [visualization_obj(*p) for p in PANELS]
    objs.append(dashboard_obj())

    ndjson = '\n'.join(json.dumps(o, ensure_ascii=False) for o in objs) + '\n'
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(ndjson)
    print(f'wrote {args.out}: {len(objs)} objects')

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
