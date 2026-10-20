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
import base64
import datetime
import json
import os
import ssl
import uuid
from urllib import request as urlreq

ES = 'http://localhost:9200'
KIBANA = 'http://localhost:5601'
ES_AUTH = None      # 'user:pass' —— 指标库开了 basic auth 时用
ES_INSECURE = False # 指标库用自签证书时跳过校验

# 当前目标 Kibana 的版本。7.x 与 8.x 的 saved object schema 有实质差异（见 KIBANA_MAJOR
# 处的注释），所以建板前先问一次 Kibana 自己，再按版本生成对象 —— 同一份代码
# 能在 Kibana 7.17 和 8.1.3 上跑出各自合法的对象。
KIBANA_VERSION = ''
KIBANA_MAJOR = 0

# ---------------------------------------------------------------- ES helpers

def _es_headers():
    if not ES_AUTH:
        return {}
    tok = base64.b64encode(ES_AUTH.encode('utf-8')).decode('ascii')
    return {'Authorization': 'Basic ' + tok}


def _es_ssl_ctx():
    if not ES_INSECURE:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def es_get(path):
    req = urlreq.Request(ES + path, headers=_es_headers())
    with urlreq.urlopen(req, context=_es_ssl_ctx()) as r:
        return json.loads(r.read())


def es_post(path, body):
    headers = {'Content-Type': 'application/json'}
    headers.update(_es_headers())
    req = urlreq.Request(ES + path, data=json.dumps(body).encode('utf-8'),
                         headers=headers)
    with urlreq.urlopen(req, context=_es_ssl_ctx()) as r:
        return json.loads(r.read())


def kibana_get(path):
    req = urlreq.Request(KIBANA + path, headers={'kbn-xsrf': 'true'})
    with urlreq.urlopen(req) as r:
        return json.loads(r.read())


def kibana_delete(path):
    req = urlreq.Request(KIBANA + path, method='DELETE', headers={'kbn-xsrf': 'true'})
    with urlreq.urlopen(req) as r:
        return json.loads(r.read())


def detect_kibana_version():
    """问 Kibana 自己是什么版本（/api/status → version.number）。

    7.x 与 8.x 的 saved object schema 有实质差异，不探测就按 8.x 生成会在 7.x 上被
    schema 校验打回：
      * visualization 的 attributes.version 在 7.x 是**必填**的 semver 字符串；
        8.x 把它放宽成可选（历史上还踩过非 semver 值让 8.x 迁移函数抛
        "Invalid Version: 1"）。
      * index-pattern 的 attributes.timeFieldName 在 7.x 同样必填。
    探测不到就退回 8.x（与已验证过的行为保持一致）。
    """
    global KIBANA_VERSION, KIBANA_MAJOR
    try:
        r = kibana_get('/api/status')
        v = (r.get('version') or {}).get('number') or ''
    except Exception:                                       # noqa: BLE001
        v = ''
    if v:
        KIBANA_VERSION = v
        try:
            KIBANA_MAJOR = int(v.split('.')[0])
        except ValueError:
            KIBANA_MAJOR = 0
    return KIBANA_VERSION


def vis_attributes(title, vis_state, search_source=None):
    """visualization 对象的 attributes —— 按目标 Kibana 版本补齐必填字段。

    **不写 attributes.version**：Kibana 7.x 的 .kibana 索引把
    `visualization.version` 映射成 **integer**（7.17 源码
    src/plugins/visualizations/server/saved_objects/visualization.js 的
    mappings.properties.version = {type: 'integer'}），写 "7.17.29" 这种字符串
    会被 ES 的 mapper_parsing_exception 打回整批导入；而 Kibana 7.17 自带的
    导入样例 src/core/public/http/_import_objects.ndjson 里这个字段干脆没有 ——
    它不是必填项。8.x 那边同样不写（历史上写过非 semver 值让 8.x 迁移函数抛
    "Invalid Version: 1"）。

    **searchSourceJSON**：7.x 的样例带 query 字段（{"query":{"query":"","language":...},
    "filter":[]}），8.x 只写 filter 也够。7.x 按样例补齐，8.x 保持原样不动。
    """
    if search_source is None:
        search_source = ('{"query":{"query":"","language":"kuery"},"filter":[]}'
                         if KIBANA_MAJOR == 7 else '{"filter":[]}')
    return {
        'title': title,
        'visState': vis_state,
        'uiStateJSON': '{}',
        'description': '',
        'kibanaSavedObjectMeta': {'searchSourceJSON': search_source},
    }


def prune_stale(keep_ids):
    """删掉本脚本不再生成的旧 saved object。

    Kibana 的导入只覆盖同名对象，**不会**删掉旧文件 —— 改成按赛道分看板后，
    改造前的单板面板（bench-vis-wthr 等）会变成孤儿留在 Kibana 里，
    所以导入前先清掉不在新集合里的 bench-* 对象。
    """
    removed = 0
    for stype in ('visualization', 'dashboard', 'link'):
        try:
            r = kibana_get('/api/saved_objects/_find?type=%s&per_page=200' % stype)
        except Exception:                                   # noqa: BLE001
            continue
        for so in r.get('saved_objects', []):
            sid = so.get('id') or ''
            if sid.startswith('bench-') and sid not in keep_ids:
                try:
                    kibana_delete('/api/saved_objects/%s/%s' % (stype, sid))
                    removed += 1
                except Exception:                           # noqa: BLE001
                    pass
    return removed


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
    defs = flat_fields(name)
    fields = []
    for fqn, t in defs:
        numeric = kibana_field_type(t) == 'number'
        fields.append({
            'name': fqn, 'type': kibana_field_type(t), 'esTypes': [t],
            'count': 0, 'scripted': False,
            'searchable': t != 'object',
            'aggregatable': t in ('keyword', 'date', 'boolean') or numeric,
            'readFromDocValues': t in ('keyword', 'date', 'boolean') or numeric,
        })
    # timeFieldName 在 Kibana 7.x 的 index-pattern schema 里是必填字符串。
    # 优先 @timestamp，其次第一个 date 字段，实在没有就还是给 @timestamp
    # （三张索引都有它，mapping 里读出来的日期字段是兜底）。
    time_field = None
    if any(f[0] == '@timestamp' for f in defs):
        time_field = '@timestamp'
    else:
        dates = [f[0] for f in defs if f[1] == 'date']
        time_field = dates[0] if dates else '@timestamp'
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

# 赛道（esrally track）→ 展示名。未列出的赛道用 track 原名兜底。
TRACK_LABEL = {
    'geo': 'geonames 文本检索',
    'hnsw': 'HNSW 向量检索 · 96 维',
    'nested': 'nested 嵌套文档',
    'ds-j512': '向量维度扫描 · 512 维',
    'ds-j768': '向量维度扫描 · 768 维',
    'ds-g960': '向量维度扫描 · 960 维',
    'ds-j1024': '向量维度扫描 · 1024 维',
    'ds-g1920': '向量维度扫描 · 1920 维',
    'ds-j2048': '向量维度扫描 · 2048 维',
}

# task → workload 分组。
# 为什么要分：同一赛道内不同 task 的吞吐/延迟量纲不同（term 检索 vs country 聚合 vs
# sort vs KNN），混在一张图里是"不同 workload 一起对比"，与"不同赛道不能一起对比"
# 是同一类错误。所以每个 workload 单独出图，组内才是同量纲可比的。
#
# 匹配规则（match_group）：先全表精确匹配，再走前缀匹配。
# 前缀 = pattern 以 '_' 或 '-' 结尾 —— esrally 的 task 命名把两种分隔符混着用
# （randomized-nested-queries-with-inner-hits_default 用下划线，
#   knn-search-100-1000-x32 用连字符），只认一种会漏匹配。
# 精确优先保证 knn-search-100-1000 与 knn-search-100-1000-x32 分属不同 workload。
TASK_GROUPS = [
    ('search', ['term', 'phrase', 'default', 'term_'],
     '检索 · term / phrase'),
    ('agg', ['country_agg_'],
     '聚合 · country agg'),
    ('sort', ['asc_sort_', 'desc_sort_', 'sort_country_code_'],
     '排序 · sort'),
    ('decay', ['decay_geo_gauss_'],
     '衰减打分 · decay_geo_gauss'),
    ('fnscore', ['field_value_', 'expression'],
     '函数打分 · function_score'),
    ('large', ['large_'],
     '大词表 terms · large-terms'),
    ('script', ['painless_'],
     '脚本查询 · painless'),
    ('scroll', ['scroll'],
     'scroll 分页'),
    ('nested-q', ['randomized-nested-queries',
                  'randomized-nested-queries-with-inner-hits_'],
     'nested 检索 · random-nested'),
    ('nested-m', ['match-all', 'nested-date-histo'],
     'nested 匹配 · match-all / date-histo'),
    ('nested-t', ['randomized-term-queries', 'randomized-sorted-term-queries'],
     'nested 词项 · term queries'),
    ('knn-k10', ['knn-search-10-100'],
     'KNN · k=10'),
    ('knn-k100', ['knn-search-100-1000'],
     'KNN · k=100'),
    ('knn-k100c32', ['knn-search-100-1000-x32'],
     'KNN · k=100 · 32 并发'),
]


def match_group(task):
    """task 落在哪个 workload 分组；分不到组返回 None（写入/维护类 task，不出查询图）"""
    for gid, pats, _ in TASK_GROUPS:
        if task in pats:
            return gid
    for gid, pats, _ in TASK_GROUPS:
        for p in pats:
            if (p.endswith('_') or p.endswith('-')) and task.startswith(p):
                return gid
    return None


def group_tasks(tasks):
    """把某赛道的 task 集合按 workload 分组，返回 [(gid, 组名, [task...])]，只保留有数据的组"""
    out = []
    for gid, pats, label in TASK_GROUPS:
        hit = [t for t in tasks if match_group(t) == gid]
        if hit:
            out.append((gid, label, sorted(hit)))
    return out


def _count(q):
    """按 Lucene 查命中数；查询失败返回 None（调用方按"有数据"处理，不因查询失败丢面板）"""
    try:
        r = es_post('/bench-metric/_count', {'query': {'query_string': {'query': q}}})
        return r.get('count', 0)
    except Exception:                                       # noqa: BLE001
        return None


def group_has_metric(track, tasks, family, unit=None):
    """该 workload 组在给定赛道下是否真有这个 family 的数据。

    没有就不出那个面板 —— 例如 scroll 是分页操作，只有 latency 没有 throughput，
    硬出图就是一个恒空的"No results found"面板，比没有更糟。
    """
    tt = '(%s)' % ' OR '.join('task:%s' % x for x in tasks) if tasks else '*'
    # 带 quality:ok —— 否则"该组所有 task 都 degraded"时会白出一个恒空面板
    q = 'track:%s AND quality:ok AND family:%s%s AND %s' % (
        track, family, (' AND unit:"%s"' % unit) if unit else '', tt)
    n = _count(q)
    return True if n is None else bool(n)


def system_metric_exists(track, metric):
    """该赛道是否有这个 system 指标（只有带 monitor 采集的赛道才有）"""
    n = _count('track:%s AND family:system AND metric:(%s)' % (track, metric))
    return True if n is None else bool(n)


# 面板过滤的公共部分。family 取值来自 bench-export.py 的 RESULT_METRICS 映射：
#   segment_count / store_size / translog_size -> family=disk
#   young_gc_time / old_gc_time -> family=gc
#   total_time -> family=indexing_time
# 注意 family 不是按名字归一的：merge_count 属 merge、merge_time 属 merge_time；
#   merge_throttle_time / refresh_time / flush_time 各是独立 family。
# 每个面板都带 track:<赛道> —— 不同赛道的同族指标互不干扰，各自独立成看板。

# 索引阶段分解：每个指标 family 不同，逐个写死
IDX_PHASE_SPECS = [
    ('merge-time', 'Merge 耗时 (s)', 'family:merge_time AND stat:value', '0.0[00]'),
    ('refresh-time', 'Refresh 耗时 (s)', 'family:refresh_time AND stat:value', '0.0[00]'),
    ('flush-time', 'Flush 耗时 (s)', 'family:flush_time AND stat:value', '0.0[00]'),
    ('throttle', '索引限流时长 (s)', 'family:indexing_throttle_time AND stat:value', '0.0[00]'),
]


def track_panel_specs(track, groups, interval):
    """某赛道的面板规格：[(key, 标题, lucene 过滤, 数字格式, 分组字段, 指标类型, 桶粒度)]"""
    # quality:ok 是所有测量类面板的默认闸门 ——
    #   degraded = 该 task 的 error_rate>0（该 task 全部请求失败），它吐出的时延量的是
    #   "失败响应有多快"（实测 p99 只有 0.22s），混进延迟曲线会把引擎显得比实际更快。
    #   15 场 nested 就是这样在 valid:true 过滤下混进曲线的。
    # 唯一不放行 degraded 的是错误率面板 —— 它就是要显示失败场次。
    t = 'track:%s AND quality:ok' % track
    tq = 'track:%s' % track
    specs = [
        ('wthr', '写入吞吐 · p50 docs/s',
         '%s AND family:throughput AND unit:"docs/s" AND stat:p50' % t,
         '0.0a', 'arm', 'avg', interval),
        ('idx', '索引总耗时 (s)',
         '%s AND family:indexing_time AND metric:total_time AND stat:value' % t,
         '0.0[0]', 'arm', 'avg', interval),
    ]
    # 索引阶段分解：merge / refresh / flush / 限流 的耗时，看索引慢在哪一段
    for key, title, f, fmt in IDX_PHASE_SPECS:
        specs.append((key, title, '%s AND %s' % (t, f), fmt, 'arm', 'avg', interval))
    # 索引活动次数：merge/refresh/flush 各多少次，同量纲（次数）→ 一图多线
    specs.append(('idx-events', '索引活动次数 (merge/refresh/flush)',
                  '%s AND family:(merge OR refresh OR flush) AND stat:value '
                  'AND metric:(merge_count OR refresh_count OR flush_count)' % t,
                  '0.0a', 'metric', 'avg', interval))

    for gid, label, tasks in groups:
        tt = '(%s)' % ' OR '.join('task:%s' % x for x in tasks)
        if group_has_metric(track, tasks, 'throughput', 'ops/s'):
            specs.append(('qthr-' + gid, '%s · 吞吐 (ops/s)' % label,
                          '%s AND family:throughput AND unit:"ops/s" AND stat:p50 AND %s' % (t, tt),
                          '0.0a', 'arm', 'avg', interval))
        # ---- 时延三兄弟（2026-10-20 复审重做）----
        # rally 的 latency 有两种口径，取决于该 operation 有没有 target-throughput：
        #   闭环（无 target-throughput）: latency ≡ service_time，量单次请求耗时
        #   开环（有 target-throughput）: latency = 端到端（含发压机积压），
        #     service_time 才是请求的服务端耗时
        # 实测 nested 赛道 5 个 operation 实际饱和（target 20 ops/s×2 clients，
        # achieved 仅 19.86），90 分位 latency 1.477s 而 service_time 只有 36.7ms ——
        # 差的 40 倍全是发压机积压。把两者画在同一张图上，结论会完全跑偏。
        # 所以：引擎时延（service_time）当主曲线，端到端（latency）与积压
        #（queue_time = latency - service_time）各自成图，加工具说明。
        if group_has_metric(track, tasks, 'service_time'):
            specs.append(('qlat-' + gid, '%s · 引擎时延分位 (p50/p90/p99)' % label,
                          '%s AND family:service_time AND stat:(p50 OR p90 OR p99) AND %s' % (t, tt),
                          '0.0[000]', 'stat', 'avg', interval))
        if group_has_metric(track, tasks, 'latency'):
            specs.append(('qe2e-' + gid, '%s · 端到端时延分位 (p50/p90/p99)' % label,
                          '%s AND family:latency AND stat:(p50 OR p90 OR p99) AND %s' % (t, tt),
                          '0.0[000]', 'stat', 'avg', interval))
        if group_has_metric(track, tasks, 'queue_time'):
            specs.append(('qback-' + gid, '%s · 发压机积压 (p50/p90/p99)' % label,
                          '%s AND family:queue_time AND stat:(p50 OR p90 OR p99) AND %s' % (t, tt),
                          '0.0[000]', 'stat', 'avg', interval))

    specs.append(('err', '错误率 (0-1)',
                  '%s AND family:error_rate AND stat:value' % tq,
                  '0.000', 'arm', 'avg', interval))

    # ---- 资源与存储 ----
    specs.append(('disk', '存储占用 (GB)',
                  '%s AND family:disk AND metric:store_size AND stat:value' % t,
                  '0.0', 'arm', 'avg', interval))
    specs.append(('seg', '段数量',
                  '%s AND family:disk AND metric:segment_count AND stat:value' % t,
                  '0.0a', 'arm', 'avg', interval))
    # memory_* 六个分量与 translog_size 实测全为 0（rally 报告未填充 / 赛末已清空），
    # 画出来是六条零线，没有信息量 —— 不出图。
    # GC：耗时与次数各一图（分 young/old 两代，同量纲）
    specs.append(('gc-time', 'GC 耗时 (s · young/old)',
                  '%s AND family:gc AND stat:value '
                  'AND metric:(young_gc_time OR old_gc_time)' % t,
                  '0.0[00]', 'metric', 'avg', interval))
    specs.append(('gc-count', 'GC 次数 (young/old)',
                  '%s AND family:gc AND stat:value '
                  'AND metric:(young_gc_count OR old_gc_count)' % t,
                  '0.0a', 'metric', 'avg', interval))

    # ---- 环境健康度（数据可信度）：只有带 monitor 采集的赛道才有 ----
    for key, title, metric, fmt in [
            ('steal', 'CPU steal (avg/max) · 数据可信度', 'cpu_steal_avg OR cpu_steal_max', '0.000'),
            ('foreign', '外来进程 CPU (max)', 'foreign_cpu_max', '0.000'),
            ('swapin', '换入次数', 'swapin', '0.0a'),
            # 这三项是 load-monitor.sh 每 5 秒就在采、但摘要里一直没吐出来的量：
            # 被测引擎自己的 CPU 占用、整机 CPU 占用、磁盘读写吞吐。
            # 有了它们才能回答"这台机是被打满了还是没吃满""瓶颈在 CPU 还是 IO"。
            ('engcpu', '被测引擎 CPU 占用 (avg/max)', 'bench_cpu_avg OR bench_cpu_max', '0.000'),
            ('syscpu', '整机 CPU 占用 user/sys (avg)', 'cpu_usr_avg OR cpu_sys_avg', '0.000'),
            ('diskio', '磁盘读写吞吐 (MB/s avg/max)', 'disk_rw_mbs_avg OR disk_rw_mbs_max', '0.0')]:
        if group_has_metric(track, [], 'system') and system_metric_exists(track, metric):
            specs.append((key, title,
                          '%s AND family:system AND metric:(%s) AND stat:value' % (t, metric),
                          fmt, 'metric' if ' OR ' in metric else 'arm', 'avg', interval))
    return specs



def visualization_obj(vis_id, title, panel_filter, formatter,
                      terms_field='arm', metric_type='avg', interval='1d'):
    params = tsvb_timeseries(title, panel_filter, formatter,
                             terms_field=terms_field,
                             metric_type=metric_type,
                             interval=interval)
    params['index_pattern_ref_name'] = IP_REF
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': vis_attributes(title, json.dumps(params)),
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
        'attributes': vis_attributes(title, json.dumps(
            {'type': 'markdown', 'aggs': [],
             'params': {'fontSize': 12, 'markdown': md},
             'title': title})),
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }


def section_obj(vis_id, title, text):
    """小节标题（markdown），用来把面板按 写入/查询/向量/资源 分段"""
    md = '**%s** — %s' % (title, text)
    return {
        'id': vis_id, 'type': 'visualization',
        'attributes': vis_attributes(title, json.dumps(
            {'type': 'markdown', 'aggs': [],
             'params': {'fontSize': 14, 'markdown': md},
             'title': title})),
        'references': [],
        'migrationVersion': {'visualization': '7.10.0'},
    }


USAGE_MD = '''**看图指南**
- **每个赛道一个独立看板**（总览页有导航表）。不同赛道的语料、维度、task 集都不同，
  量纲不可比，所以不放在同一张图里对比 —— 对照 opensearch.org/benchmarks 的分页口径。
- 看板内再按 **workload 分图**：同一赛道内不同 task（term 检索 / country 聚合 / sort /
  decay / KNN k 值）的吞吐与延迟量纲不同，混在一张图里同样是误读。
- 每点 = 一场 race；曲线 = 实验臂(arm)。顶部 KQL 查询框默认 `valid: true AND quality: ok`，可改成任意表达式。
- **quality 是"这行数据能不能用"的闸门**：degraded = 该 task 的 error_rate>0（全部请求失败），
  它吐出的时延量的是"失败响应有多快"，混进来会把引擎显得更快。valid 只说明"跑完了"。
- 只看失败场次: 把 KQL 框改成 `valid: false` 或 `quality: degraded`；限定实验臂: `arm:es819`；限定批次: `campaign:ds3-20261015-233223`
- **时延三兄弟，别混用**：
  - **引擎时延** (family=service_time) = 服务端执行时间，**跨引擎比较看这个**
  - **端到端时延** (family=latency) = 含发压机积压；开环 task（设了 target-throughput）
    饱和时会把 36ms 的引擎耗时放大成 1.4s
  - **发压机积压** (family=queue_time) = latency − service_time，看"是引擎慢还是发压机排队"
- 每图一个 family（吞吐/延迟/服务时间/磁盘/GC…），单位已归一（时间 s、大小 GB、错误率 0~1）
- series=arm: jdk25/kona25=easysearch2.5.0-3015+JDK25, ez240=easysearch2.4.0-2975, es819=ES8.19.23
- **错误率说明**: rally 的 error_rate 本就是 0~1 比例，1.0 = 该 task 请求全失败（不要按 % 再除）
- 原始层回钻: campaigns-local/<campaign>/results/<fid>-race.json'''

# ---------------------------------------------------------------- dashboard

SECTIONS = [
    ('write', '① 写入 (write)', '索引吞吐、总耗时与阶段分解（merge / refresh / flush / 限流）'),
    ('query', '② 查询 (query)', '按 workload 分图：吞吐、引擎时延 / 端到端时延 / 发压机积压（p50/p90/p99）'),
    ('res', '③ 资源与存储', '存储体积 / 段数 / GC 耗时与次数（young/old）'),
    ('health', '④ 环境健康度', 'CPU steal / 外来进程 / swap —— 数据可信度'),
]


def _dashboard(panels, refs, dash_id, title, time_from, time_to, description,
               scope='valid: true AND quality: ok'):
    # 默认闸门加 quality:ok ——
    #   valid 只说明"这场跑完了且状态是 VALID"，不说明结果可用。
    #   实测 15 场 nested 有一个 operation error_rate=1.0（全部请求失败），
    #   status 仍是 VALID → valid:true，这些失败 task 的"快速失败"时延
    #   会混进查询延迟曲线。quality 才是"这行数据能不能用"的闸门。
    return {
        'id': dash_id, 'type': 'dashboard',
        'attributes': {
            'title': title,
            'hits': 0,
            # 时间范围必须显式保存：默认是"最近 15 分钟"，而压测数据是历史场次，
            # 不设 timeRestore 的话仪表盘打开就是一片空白。窗口按数据实际范围算。
            'timeRestore': True, 'timeFrom': time_from, 'timeTo': time_to,
            'refreshInterval': {'pause': True, 'value': 0},
            'description': description,
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


def build_track_dashboard(track, groups, interval, time_from, time_to):
    """生成某赛道的独立看板 + 它引用的面板与段标题。

    设计对照 opensearch.org/benchmarks：那边每个 track 一个独立页面
    （HTTP logs / NYC taxis / PMC / Big5 / Vectorsearch-* / Segment Replication …），
    不同 track 不混在一张图里。我们同理：每个 esrally track 一个 dashboard，
    看板内再按 workload 分图。
    """
    objs = []
    panels, refs = [], []

    def add(vis_id, x, y, w, h, ptype='visualization'):
        i = str(len(panels) + 1)
        panels.append({'embeddableConfig': {},
                       'gridData': {'x': x, 'y': y, 'w': w, 'h': h, 'i': i},
                       'id': vis_id, 'panelIndex': i, 'type': ptype, 'version': '7.10.0'})
        refs.append({'id': vis_id, 'name': i, 'type': ptype})

    specs = track_panel_specs(track, groups, interval)
    by_key = {}
    for key, title, filt, fmt, terms, mtype, iv in specs:
        vis_id = 'bench-%s-%s' % (track, key)
        by_key[key] = vis_id
        objs.append(visualization_obj(vis_id, title, filt, fmt,
                                       terms_field=terms, metric_type=mtype,
                                       interval=iv))

    # 段标题
    for skey, stitle, stext in SECTIONS:
        sid = 'bench-%s-sec-%s' % (track, skey)
        objs.append(section_obj(sid, stitle, stext))
        by_key['sec-' + skey] = sid

    # 布局：按段排布，每段 2 列，行高 13；段标题整行高 3。
    # 段内只放实际生成的面板（某指标没数据就不出图，段本身也不会空）。
    def pick(keys):
        return [by_key[k] for k in keys if k in by_key]

    write_ids = pick(['wthr', 'idx', 'merge-time', 'refresh-time',
                      'flush-time', 'throttle', 'idx-events'])
    query_ids = []
    for gid, _label, _tasks in groups:
        for k in ('qthr-' + gid, 'qlat-' + gid, 'qe2e-' + gid, 'qback-' + gid):
            if k in by_key:                 # 该 workload 没有这个指标时面板就不存在
                query_ids.append(by_key[k])
    query_ids += pick(['err'])
    res_ids = pick(['disk', 'seg', 'gc-time', 'gc-count'])
    health_ids = pick(['steal', 'foreign', 'swapin'])

    layout = []
    for sec_key, ids in (('write', write_ids), ('query', query_ids),
                         ('res', res_ids), ('health', health_ids)):
        if ids:
            layout.append((by_key['sec-' + sec_key], ids))
    y = 0
    for sec_id, vis_ids in layout:
        add(sec_id, 0, y, 48, 3)
        y += 3
        for n, vis_id in enumerate(vis_ids):
            add(vis_id, 24 * (n % 2), y, 24, 13)
            if n % 2 == 1:
                y += 13
        if len(vis_ids) % 2:
            y += 13

    label = TRACK_LABEL.get(track, track)
    dash = _dashboard(panels, refs, 'bench-%s' % track,
                       'Benchmark · %s · %s' % (track, label),
                       time_from, time_to,
                       'esrally 赛道 %s 的独立看板（%s）；对照 opensearch.org/benchmarks '
                       '的"每个 track 一个页面"口径，不同赛道不混图' % (track, label))
    objs.append(dash)
    return objs


def es_track_facts():
    """读出每个赛道的：场次数 / 实验臂 / task 集合 / 时间范围。

    全部从 ES 实算，不在代码里硬编码赛道清单 —— 新增一场新赛道的压测后，
    看板脚本跑一次就会自动多出一个独立看板，不用改代码。
    """
    facts = {}
    try:
        r = es_post('/bench-metric/_search', {
            'size': 0,
            'aggs': {'t': {'terms': {'field': 'track', 'size': 200},
                           'aggs': {'k': {'terms': {'field': 'task', 'size': 200}},
                                    'a': {'terms': {'field': 'arm', 'size': 50}},
                                    'span': {'stats': {'field': '@timestamp'}}}}}})
    except Exception:                                       # noqa: BLE001
        return facts
    for b in (r.get('aggregations', {}).get('t', {}).get('buckets') or []):
        span = (b.get('span') or {})
        facts[b['key']] = {
            'metrics': b['doc_count'],
            'tasks': [x['key'] for x in (b.get('k') or {}).get('buckets') or []],
            'arms': [x['key'] for x in (b.get('a') or {}).get('buckets') or []],
            'lo': span.get('min'), 'hi': span.get('max'),
        }
    return facts


def es_track_runs(track):
    """某赛道的场次数（bench-run 一行 = 一场 race）"""
    try:
        r = es_post('/bench-run/_search', {
            'size': 0, 'query': {'term': {'track': track}},
            'aggs': {'n': {'value_count': {'field': 'race_id'}},
                     'arms': {'terms': {'field': 'arm', 'size': 50}},
                     'span': {'stats': {'field': '@timestamp'}}}})
        a = r.get('aggregations', {})
        span = a.get('span') or {}
        return {'runs': (a.get('n') or {}).get('value', 0),
                'arms': [x['key'] for x in (a.get('arms') or {}).get('buckets') or []],
                'lo': span.get('min'), 'hi': span.get('max')}
    except Exception:                                       # noqa: BLE001
        return {'runs': 0, 'arms': [], 'lo': None, 'hi': None}


def pick_interval(lo_ms, hi_ms):
    """桶粒度按该赛道自己的数据跨度定：跨天用天级，半天内用小时级。

    固定 1d 会让只跑了几小时的赛道（向量维度扫描整场 campaign 只跨 12 小时）
    全部数据落进一个桶 = 一个数据点，折线只有一个顶点画不出来。
    """
    if not lo_ms or not hi_ms:
        return '1d'
    span_days = (hi_ms - lo_ms) / 86400000.0
    return '1d' if span_days > 2 else '2h'


def data_window(track=None, pad_days=1):
    """仪表盘默认时间窗 = 数据的实际时间范围（各留 1 天余量）。

    为什么不用固定相对窗：压测批次集中在 4 天内，若用 now-90d 这类固定窗，
    曲线会全部挤在最右侧 4% 的位置，完全看不出趋势。窗口跟着数据走，
    打开就是"这一批结果"的完整横轴。

    传 track 时只算该赛道的窗口 —— 每个赛道是独立看板，时间轴各按自己的批次走。

    返回 (timeFrom, timeTo) 字符串；ES 里没数据时退回 now-90d。
    """
    try:
        query = {'term': {'track': track}} if track else {'match_all': {}}
        r = es_post('/bench-run/_search', {
            'size': 0, 'query': query,
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


def overview_dashboard(track_info):
    """总览页：环境说明 + 看图指南 + 赛道导航（各赛道的独立看板链接）。

    这里刻意**不**放跨赛道的指标对照图 —— 不同赛道的量纲不同，横着比没有意义
    （与 opensearch.org/benchmarks 一致：那边也是每个 track 一个独立页面，
    汇总页只给入口链接）。
    """
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
    add('bench-vis-nav', 0, 11, 48, 9)
    add('bench-vis-guide', 0, 20, 48, 8)

    time_from, time_to = data_window()
    return _dashboard(panels, refs, 'bench-overview',
                      'Benchmark 赛道总览 · easysearch vs ES',
                      time_from, time_to,
                      'rally race 结果可视化总入口（对照 opensearch.org/benchmarks；'
                      '每个 esrally track 一个独立看板，不同赛道不混图；'
                      '源 = raceway bench-export.py 导出的 bench-run/bench-metric）')


def nav_markdown(track_info):
    """赛道导航表：每个 track 一行，列出它自己的独立看板入口

    导航方式说明（Kibana 8.1 实测）：markdown 面板里的 [文字](url) 链接点击后
    **不会**让父页面跳转（链接渲染在面板自己的渲染上下文里），绝对 URL 会被
    安全策略降级成纯文本，而 link 可视化类型在 8.1 已不支持 —— 三条路都试过。
    所以这里给可复制的路径 + 指向 Kibana 列表页的说明：
      ① 直接把路径粘到地址栏；
      ② 左侧栏 ☰ → Analytics → Dashboard（/app/dashboards/list）点标题。
    """
    md = ['**赛道导航** — 每个 esrally track 一个独立看板'
          '（对照 opensearch.org/benchmarks：HTTP logs / NYC taxis / PMC / Big5 / '
          'Vectorsearch-* … 各自独立页面，不同 workload 不混图）', '']
    md.append('| 赛道 | 说明 | 场次 | 实验臂 | workload 分组 | 看板路径 |')
    md.append('|---|---|---|---|---|---|')
    for track, info in track_info:
        label = TRACK_LABEL.get(track, track)
        arms = ', '.join(info['arms']) or '—'
        groups = ' / '.join(g[1] for g in info['groups']) or '—'
        md.append('| `%s` | %s | %d | %s | %s | `/app/dashboards#/view/bench-%s` |'
                  % (track, label, info['runs'], arms, groups, track))
    md.append('')
    md.append('> **进各赛道看板**：把上表路径粘到地址栏，或左侧栏 ☰ → Analytics → '
              'Dashboard（`/app/dashboards/list`）点对应标题。')
    md.append('> 不同赛道的指标不放在同一张图里对比 —— 赛道之间语料、维度、task 集都不同，'
              '量纲不可比。')
    return '\n'.join(md)


def main():
    global ES, KIBANA, ES_AUTH, ES_INSECURE
    ap = argparse.ArgumentParser()
    ap.add_argument('--import', dest='do_import', action='store_true')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(__file__), 'kibana-objects.ndjson'))
    ap.add_argument('--es', default=ES)
    ap.add_argument('--kibana', default=KIBANA)
    ap.add_argument('--es-auth', default=None,
                    help='指标库 basic auth（user:pass）；指标库开了认证时必须给')
    ap.add_argument('--insecure', action='store_true',
                    help='指标库用自签证书时跳过证书校验')
    ap.add_argument('--kibana-version', default=None,
                    help='覆盖自动探测的 Kibana 版本（如 7.17.29）；默认问 Kibana 自己')
    args = ap.parse_args()

    ES, KIBANA = args.es, args.kibana
    ES_AUTH = args.es_auth
    ES_INSECURE = args.insecure

    if args.kibana_version:
        global KIBANA_VERSION, KIBANA_MAJOR
        KIBANA_VERSION = args.kibana_version
        try:
            KIBANA_MAJOR = int(KIBANA_VERSION.split('.')[0])
        except ValueError:
            KIBANA_MAJOR = 0
    else:
        detect_kibana_version()
    print('目标 Kibana: %s (%s)  ES: %s'
          % (KIBANA_VERSION or '未知', KIBANA, ES))

    objs = [index_pattern_obj('bench-metric'),
            index_pattern_obj('bench-run'),
            index_pattern_obj('bench-env')]

    # 赛道从 ES 实算，不硬编码：新增一场新赛道的压测后，跑一次脚本就多一个看板
    facts = es_track_facts()
    track_info = []
    for track in sorted(facts.keys()):
        info = facts[track]
        groups = group_tasks(info['tasks'])
        if not groups:
            # 该赛道没有能归组的查询 task（只有写入）→ 不出查询段，但仍给出写入/资源图
            groups = []
        runs = es_track_runs(track)
        interval = pick_interval(info['lo'], info['hi'])
        tf, tt = data_window(track)
        objs += build_track_dashboard(track, groups, interval, tf, tt)
        track_info.append((track, {'runs': runs['runs'], 'arms': runs['arms'],
                                    'groups': groups}))
        print('  track %-12s runs=%-4d arms=%-28s groups=%s interval=%s'
              % (track, runs['runs'], ','.join(runs['arms']),
                 ','.join(g[0] for g in groups) or '-', interval))

    objs.append(markdown_obj('bench-vis-head', '环境说明', env_markdown()))
    objs.append(markdown_obj('bench-vis-nav', '赛道导航', nav_markdown(track_info)))
    objs.append(markdown_obj('bench-vis-guide', '看图指南', USAGE_MD))
    objs.append(overview_dashboard(track_info))

    ndjson = '\n'.join(json.dumps(o, ensure_ascii=False) for o in objs) + '\n'
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(ndjson)
    print('wrote %s: %d objects (%d dashboards + %d panels)'
          % (args.out, len(objs), len(track_info) + 1, len(objs) - len(track_info) - 4))

    if args.do_import:
        # 先清掉本次不再生成的旧对象（改造前的单板面板），再导入
        keep = set(o['id'] for o in objs)
        removed = prune_stale(keep)
        if removed:
            print('pruned %d stale saved objects' % removed)
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
            res = json.loads(r.read())
            print(json.dumps({'successCount': res.get('successCount'),
                              'success': res.get('success'),
                              'errors': res.get('errors')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
