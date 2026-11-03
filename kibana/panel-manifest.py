#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel-manifest.py —— 从实际导入的 saved objects 反解出《KIBANA 看板面板清单》

为什么要有这个脚本（而不是手写清单）：
  清单是看板的**地面真相** —— 每个图的标题 / saved object id / 过滤串 / 分线字段 /
  聚合 / 数字格式 / 桶粒度，必须与 Kibana 里真正跑的东西一致。
  手写一次就漂一次：改 make_dashboard.py 忘了同步，清单就成了骗人的文档。
  本脚本只读 ndjson，不连 Kibana —— 产物即"这次导入到底建了什么"。

用法：
  python3 panel-manifest.py --in kibana-objects.ndjson --out 看板面板清单.md
"""
import argparse
import json
import os


def load(path):
    objs = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                objs.append(json.loads(line))
    return objs


def panel_row(idx, vis):
    """一行面板描述：从 visState 反解口径"""
    a = vis.get('attributes') or {}
    title = a.get('title') or ''
    try:
        vs = json.loads(a.get('visState') or '{}')
    except ValueError:
        vs = {}
    if vs.get('type') == 'markdown':
        return '| %d | 文本 | %s | `%s` | — | — | — | — | — |' % (idx, title, vis['id'])
    params = vs.get('params') or {}
    series = (params.get('series') or [{}])[0]
    metrics = (series.get('metrics') or [{}])
    m0 = metrics[0]
    agg = '%s(%s)' % (m0.get('type', 'avg'), m0.get('field', 'value'))
    if len(metrics) > 1:
        agg += ' 等 %d 项' % len(metrics)
    flt = ((params.get('filter') or {}).get('query') or '').strip() or '—'
    split = series.get('terms_field') or series.get('split_mode') or '—'
    return '| %d | 时序图 | %s | `%s` | `%s` | `%s` | %s | `%s` | %s |' % (
        idx, title, vis['id'], flt, split, agg, series.get('formatter', '—'),
        params.get('interval', '—'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True, help='kibana-objects.ndjson')
    ap.add_argument('--out', required=True)
    ap.add_argument('--source-note', default=None)
    args = ap.parse_args()

    objs = load(args.src)
    vis = {o['id']: o for o in objs if o.get('type') == 'visualization'}
    dashes = [o for o in objs if o.get('type') == 'dashboard']
    dashes.sort(key=lambda d: (d.get('attributes') or {}).get('title') or '')

    total_panels = sum(len(json.loads((d.get('attributes') or {}).get('panelsJSON') or '[]'))
                       for d in dashes)

    out = []
    out.append('# KIBANA 看板面板清单（%d 个面板逐条 · 机读生成）' % total_panels)
    out.append('')
    out.append('> **本文件是《KIBANA 看板设计规格》的附录**，是看板的**地面真相**：')
    out.append('> 每个看板、每个图的标题 / saved object id / 过滤串 / 分线字段 / 聚合 /')
    out.append('> 数字格式 / 桶粒度，全部从实际导入 Kibana 的 saved objects 里反解出来，不是手写。')
    out.append('> 要改看板内容设计 → 改 `raceway/kibana/make_dashboard.py`，')
    out.append('> 重跑 `--import`，再重新生成本清单（`python3 panel-manifest.py`）。')
    out.append('>')
    out.append('> - 源文件：`%s`' % (args.source_note or args.src))
    out.append('> - 规模：%d 个 saved objects = %d dashboard + %d visualization + %d index-pattern'
               % (len(objs), len(dashes),
                  sum(1 for o in objs if o.get('type') == 'visualization'),
                  sum(1 for o in objs if o.get('type') == 'index-pattern')))
    out.append('')
    out.append('## 汇总')
    out.append('')
    out.append('| # | 看板 ID | 标题 | 面板数 | 时间窗 |')
    out.append('|---|---|---|---|---|')
    for i, d in enumerate(dashes, 1):
        a = d.get('attributes') or {}
        n = len(json.loads(a.get('panelsJSON') or '[]'))
        out.append('| %d | `%s` | %s | %d | %s → %s |'
                   % (i, d['id'], a.get('title'), n,
                      (a.get('timeFrom') or '')[:23], (a.get('timeTo') or '')[:23]))
    out.append('')
    out.append('合计面板 %d 个（含段标题/说明面板）。' % total_panels)
    out.append('')
    out.append('---')
    out.append('')

    for i, d in enumerate(dashes, 1):
        a = d.get('attributes') or {}
        panels = json.loads(a.get('panelsJSON') or '[]')
        refs = {r.get('name'): r.get('id') for r in (d.get('references') or [])
                if isinstance(r, dict) and r.get('name')}
        try:
            src = json.loads((a.get('kibanaSavedObjectMeta') or {}).get('searchSourceJSON') or '{}')
            kql = ((src.get('query') or {}).get('query') or '').strip()
        except ValueError:
            kql = ''
        out.append('## %d. `%s` — %s' % (i, d['id'], a.get('title')))
        out.append('')
        out.append('- 看板级 KQL（顶部查询框默认）：`%s`' % kql)
        out.append('- 时间窗：%s → %s（`timeRestore: true`，随数据范围自动设）'
                   % (a.get('timeFrom'), a.get('timeTo')))
        if a.get('description'):
            out.append('- 说明：%s' % a['description'])
        out.append('- 面板数：%d' % len(panels))
        out.append('')
        out.append('| # | 类型 | 标题 | saved object id | 过滤 (Lucene) | 分线字段 | 聚合 | 格式 | 桶 |')
        out.append('|---|---|---|---|---|---|---|---|---|')
        for n, p in enumerate(panels, 1):
            vid = p.get('id') or refs.get(p.get('panelIndex'))
            v = vis.get(vid) if vid else None
            if not v:
                out.append('| %d | ? | （引用缺失 %s） | `%s` | — | — | — | — | — |'
                           % (n, vid, vid))
                continue
            out.append(panel_row(n, v))
        out.append('')
        out.append('---')
        out.append('')

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print('wrote %s: %d dashboards / %d panels' % (args.out, len(dashes), total_panels))


if __name__ == '__main__':
    main()
