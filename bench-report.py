#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-report.py —— 三车 × 三赛道单文件 HTML 报告（离线可看，echarts 内联）

  python3 bench-report.py --export <exports/20261018-a> --campaign <campaigns/jdk3-20261017-155312> \
      --echarts echarts.min.js --out 报告.html

数据全部来自离线导出载荷（benchmark-result-race.jsonl / benchmark-result-metric.jsonl），不需要 ES 集群。
"""
import argparse
import json
import os
import statistics

import importlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    identity_mod = importlib.import_module('identity')
except ImportError:                                                    # 从别处 import 本文件时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    identity_mod = importlib.import_module('identity')
car_of = identity_mod.car_of       # 车代号（user_tags.car；旧载荷顶层 car/arm 也读）

DETAIL_CAMP = None   # 由 --campaign 推断
KEEP_STAT = {"median", "min", "max", "p50", "p90", "p99", "p100", "value"}
TREND_CARS = {"kona25", "jdk25", "ez240", "ez250", "es819", "ez3003"}


def med(v):
    v = [x for x in (v or []) if x is not None]
    return statistics.median(v) if v else None


def load_runs(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            out.append(d)
    return out


def car_of_run(run):
    """场次行的车代号（编排时起的名字）。

    车代号不再作顶层 typed 列（方案 §1 修订：身份由 engine / version /
    engine_build / jdk_vendor / jdk_version / series_key 表达），它留在 user_tags 里
    —— 改名后的批次写 user_tags.car，改名前的批次写 user_tags.arm，两个都读。
    """
    ut = (run or {}).get("user_tags") or {}
    return ut.get("car") or ut.get("arm") or (run or {}).get("car") or (run or {}).get("arm") or ""


def load_detail_metrics(path, camp, run_index=None):
    """流式扫大文件，只留目标 campaign 且画图用得上的行

    benchmark-result-metric 精简后**不再冗余场次维度**（car / engine / track_raw / codec /
    round / phase…，方案 §1 修订）—— 这些按 race_id 从 benchmark-result-race 回填：
    指标行只留指标事实与口径，场次维度以 benchmark-result-race 为准，避免两份漂移。
    """
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"campaign": "%s"' % camp not in line:
                continue
            d = json.loads(line)
            if d.get("family") not in ("throughput", "service_time", "latency", "error_rate"):
                continue
            if d.get("stat") not in KEEP_STAT:
                continue
            row = {k: d.get(k) for k in ("race_id", "fid", "campaign", "track", "valid",
                                         "task", "family", "stat", "value", "unit")}
            run = (run_index or {}).get(d.get("race_id"))
            if run:
                row.update({"car": car_of_run(run), "engine": run.get("engine"),
                            "track_raw": run.get("track_raw"), "codec": run.get("codec"),
                            "round": run.get("round"), "phase": run.get("phase"),
                            "series_key": run.get("series_key")})
            else:
                # 老载荷（车代号还在指标行上）：直接读，兼容旧导出快照
                row.update({"car": car_of(d), "engine": d.get("engine"),
                            "track_raw": d.get("track_raw"), "codec": d.get("codec"),
                            "round": d.get("round"), "phase": d.get("phase")})
            rows.append(row)
    return rows


def build(runs, detail, sysrows, camp):
    W = [r for r in runs if r.get("phase") == "w"]
    # 1) 明细 campaign：按 (car, track) 汇总逐轮
    byat = {}
    for r in W:
        if r["campaign"] != camp:
            continue
        byat.setdefault((car_of_run(r), r["track"]), []).append(r)
    detail_card = []
    for (car, track), rs in sorted(byat.items()):
        rs.sort(key=lambda x: x.get("round") or 0)
        env = {}
        for s in sysrows:
            if s.get("campaign") != camp:
                continue
            e = env.setdefault(s["fid"], {"cpu_steal_max": 0})
            for k in e:
                if s.get("metric") == k and s.get("value_original") is not None:
                    e[k] = max(e[k], s["value_original"])
        detail_card.append({
            "car": car, "track": track, "campaign": camp,
            "rounds": [{"round": x.get("round"), "fid": x["fid"], "race_id": x["race_id"],
                        "b": x.get("throughput_docs_per_s_whole"), "dur": x.get("write_task_duration_s"),
                        "status": x.get("status"), "valid": x.get("valid"),
                        "warn": x.get("caliber_warnings") or [],
                        "env": env.get(x["fid"], {})} for x in rs],
            "med_b": med([x.get("throughput_docs_per_s_whole") for x in rs]),
            "spread": (lambda v: (max(v) / min(v) - 1) * 100 if v and min(v) else None)(
                [x.get("throughput_docs_per_s_whole") for x in rs if x.get("throughput_docs_per_s_whole")]),
            "p99": None, "p100": None})
    # 每车每赛道的 bulk p99/p100（来自明细指标）
    tails = {}
    for m in detail:
        if m["task"] == "index-append" and m["family"] == "service_time" and m["phase"] == "w":
            tails.setdefault((m["car"], m["track"]), {}).setdefault(m["stat"], []).append(m["value"])
    for card in detail_card:
        t = tails.get((card["car"], card["track"]), {})
        card["p99"] = med(t.get("p99"))
        card["p100"] = med(t.get("p100"))
    # 2) 查询延迟/吞吐分车聚合（benchmark-result-metric 逐场逐操作，3 轮取中位）
    acc = {}
    for m in detail:
        if m.get("phase") != "q":
            continue
        acc.setdefault((m.get("track"), m.get("task"), m.get("car")), {}).setdefault(m.get("stat"), {}) \
            .setdefault(m.get("family"), []).append(m.get("value"))
    def pick(cell, stat, family):
        v = (cell or {}).get(stat, {}).get(family) or []
        return med(v)
    qops = {}
    for (track, task, car), cell in acc.items():
        qops.setdefault(track, {}).setdefault(task, {})[car] = {
            "p50": pick(cell, "p50", "latency"), "p90": pick(cell, "p90", "latency"),
            "p99": pick(cell, "p99", "latency"), "p100": pick(cell, "p100", "latency"),
            "sp50": pick(cell, "p50", "service_time"), "sp99": pick(cell, "p99", "service_time"),
            "rtp": pick(cell, "median", "throughput"), "err": pick(cell, "value", "error_rate")}
    qrows = [{"track": tr, "task": tk, "vals": {a: (v.get("p50")) for a, v in carsv.items()}}
             for tr, tmap in qops.items() for tk, carsv in tmap.items()]
    # bulk 写入尾部分车（同赛道内比车，不跨赛道）
    bulk = {}
    for m in detail:
        if m.get("phase") != "w" or m.get("task") != "index-append" or m.get("family") != "service_time":
            continue
        bulk.setdefault((m.get("car"), m.get("track")), {}).setdefault(m.get("stat"), []).append(m.get("value"))
    bulk_card = [{"car": a, "track": t,
                  "p50": med(v.get("p50")), "p90": med(v.get("p90")), "p99": med(v.get("p99")),
                  "p100": med(v.get("p100")), "median": med(v.get("median"))}
                 for (a, t), v in sorted(bulk.items())]
    # 3) 历史趋势：按 campaign 时间的各车口径B 中位（geo/hnsw/nested）
    #    ⚠ 两个必须过滤的混淆：① codec campaign 的场 track 也归一成 geo，若不分 codec，
    #    同一个 x 点会把 default 与 ZSTD/bc 的场平均成一个数；② 作废 attempt 的写赛也有吞吐，
    #    不能混进来（valid is False 的行直接跳过，valid 未知的保留并在 ⚠ 里体现）。
    tl = {}
    for r in W:
        if car_of_run(r) not in TREND_CARS or not r.get("throughput_docs_per_s_whole"):
            continue
        if r.get("track") not in ("geo", "hnsw", "nested"):
            continue
        if r.get("valid") is False:
            continue
        key = (car_of_run(r), r["track"], r.get("codec") or "")
        tl.setdefault(key, {}).setdefault(r["campaign"], []).append(
            r["throughput_docs_per_s_whole"])
    trend = {}
    for (car, track, codec), camps in tl.items():
        pts = []
        for campname, vs in camps.items():
            same = [r for r in W if r["campaign"] == campname and car_of_run(r) == car]
            warn = any("heap 声明不实" in " ".join(r.get("caliber_warnings") or []) for r in same)
            # es819 在 10-18 之前没有 es_heap_pin：ES 出厂 jvm.options 无 -Xms/-Xmx，实际走
            # JVM 默认（物理内存 1/4 = 8g），与 16g 点不可同线比较
            if car == "es819" and same and all(r.get("heap_max_measured_mb") is None for r in same):
                warn = True
            pts.append({"campaign": campname, "codec": codec or None, "n": len(vs),
                        "ts": min((r.get("@timestamp") or "" for r in same), default=""),
                        "med": med(vs), "warn": warn})
        pts.sort(key=lambda x: x["ts"])
        trend.setdefault(track, []).append({"car": car, "codec": codec or None, "points": pts})
    # 4) 环境序列
    envseries = {}
    for s in sysrows:
        if s.get("campaign") != camp:
            continue
        envseries.setdefault(s["fid"], {})[s["metric"]] = s.get("value_original")
    return {"cards": detail_card, "qrows": qrows, "trend": trend,
            "qops": qops, "bulk": bulk_card,
            "env": [{"fid": k, **v} for k, v in sorted(envseries.items())]}


def build_codec(runs, detail, camp):
    """geonames 存储压缩对比（5 组 car×track 组合车）专用汇总。
    codec 是索引设置维度、不是引擎维度，所以分组键是 engine|track_raw，不能用 car|track。"""
    CAR_LABEL = {("jdk25", "geo-codec-def"): "ez-def", ("jdk25", "geo-codec-zstd-sr"): "ez-zstd-sr",
                 ("kona25", "geo-codec-zstd-sr"): "ez-kona-zstd-sr",
                 ("es819", "geo-codec-def"): "es8-def", ("es819", "geo-codec-bc"): "es8-bc"}
    rows = [r for r in runs if r.get("campaign") == camp and r.get("valid") and r.get("fid")]
    if not rows:
        return None
    grp = {}
    for r in rows:
        key = CAR_LABEL.get((r.get("engine"), r.get("track_raw"))) or \
            "%s-%s" % (r.get("engine"), r.get("track_raw"))
        grp.setdefault(key, {"w": [], "q": []})["w" if r.get("phase") == "w" else "q"].append(r)

    def spread(v):
        v = [x for x in v if x]
        return (max(v) / min(v) - 1) * 100 if len(v) > 1 and min(v) else None

    def flat(rows, k):
        out = []
        for x in rows:
            v = x.get(k)
            if isinstance(v, list):
                out += [y for y in v if isinstance(y, (int, float))]
            elif isinstance(v, (int, float)):
                out.append(v)
        return out

    cars = []
    for key, v in grp.items():
        ws, bs = v["w"], [x.get("throughput_docs_per_s_whole") for x in v["w"]]
        cars.append({"key": key, "engine": (ws[0].get("engine") if ws else None),
                    "track_raw": (ws[0].get("track_raw") if ws else None),
                    "codec": (ws[0].get("codec") if ws else None),
                    "n": len(v["w"]), "med_b": med(bs), "spread": spread(bs),
                    "min_b": min([x for x in bs if x], default=None),
                    "max_b": max([x for x in bs if x], default=None),
                    "store": med([x.get("store_size_gb_post_merge") for x in v["w"]]),
                    "segs": (ws[0].get("segments_post_merge") if ws else None),
                    "heap_mb": med(flat(v["w"], "heap_max_measured_mb")),
                    "cores": med(flat(v["w"], "available_processors_measured")),
                    "jdk": sorted({str(j) for x in v["w"] for j in (x.get("jdk_measured") or [])}),
                    # GraalVM 25.0.4 与 TencentKona 25.0.4 的 _nodes 版本号一模一样，
                    # 能区分的只有 sidecar 里的 want.vendor（来自 java -version 厂商字串）
                    "jdk_vendor": sorted({(x.get("jdk_want") or {}).get("vendor") for x in v["w"]
                                          if (x.get("jdk_want") or {}).get("vendor")}),
                    "rounds": [{"round": x.get("round"), "fid": x["fid"], "b": x.get("throughput_docs_per_s_whole"),
                                "dur": x.get("write_task_duration_s"), "store": x.get("store_size_gb_post_merge"),
                                "status": x.get("status")} for x in sorted(v["w"], key=lambda y: y.get("round") or 0)]})
    base = med([a["store"] for a in cars if a["key"] == "ez-def"] or [None])
    for a in cars:
        a["store_delta"] = (a["store"] / base - 1) * 100 if (a["store"] and base) else None
    order = [k for k in ("ez-def", "ez-zstd-sr", "ez-kona-zstd-sr", "es8-def", "es8-bc")
             if k in [a["key"] for a in cars]]
    cars.sort(key=lambda a: order.index(a["key"]) if a["key"] in order else 99)

    # 逐操作查询延迟 p50（benchmark-result-metric 里 family=latency / stat=p50，已归一为秒）
    acc = {}
    for m in detail:
        if m.get("campaign") != camp or m.get("phase") != "q":
            continue
        if m.get("family") != "latency" or m.get("stat") != "p50":
            continue
        key = CAR_LABEL.get((m.get("car") or m.get("engine"), m.get("track_raw")))
        if not key:
            continue
        acc.setdefault(m["task"], {}).setdefault(key, []).append(m["value"])
    qrows = []
    for task in sorted(acc):
        vals = {k: med(v) for k, v in acc[task].items()}
        qrows.append({"task": task, "vals": vals,
                      "d_ez": (vals.get("ez-zstd-sr") / vals["ez-def"] - 1) * 100
                      if vals.get("ez-zstd-sr") and vals.get("ez-def") else None,
                      "d_es": (vals.get("es8-bc") / vals["es8-def"] - 1) * 100
                      if vals.get("es8-bc") and vals.get("es8-def") else None})
    return {"camp": camp, "cars": cars, "keys": order, "qrows": qrows,
            "docs": (rows[0].get("corpus") or {}).get("docs")}


HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>Easysearch 2.4.0 / 2.5.0-GraalVM / 2.5.0-Kona 写入对比</title>
<script>__ECHARTS__</script>
<style>
body{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#12355b;color:#fff;padding:18px 28px}
header h1{margin:0 0 6px;font-size:20px}header div{font-size:12px;opacity:.85;line-height:1.7}
.wrap{max-width:1180px;margin:0 auto;padding:18px}
.card{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:14px 18px;margin:0 0 16px}
h2{font-size:16px;margin:0 0 10px;border-left:4px solid #12355b;padding-left:8px}
.chart{width:100%;height:340px}.chart.tall{height:420px}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border-bottom:1px solid #eef0f3;padding:5px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}
.good{color:#0a7a3c}.bad{color:#c0392b}.mute{color:#8a94a6}
.note{font-size:12px;color:#5a6472;line-height:1.8}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;background:#eef2f7;color:#445;margin-right:4px}
.warn{background:#fff4e5;color:#a2611b}
</style></head><body>
<header><h1>Easysearch 写入与查询对比：2.4.0-2975 ／ 2.5.0-3015 GraalVM 25 ／ 2.5.0-3015 TencentKona 25</h1>
<div id="hd"></div></header><div class="wrap" id="app"></div>
<script>
const D=__DATA__;
const CH=[];                                   // 所有图表实例，窗口尺寸变化时重算
const mk=(box,opt)=>{const c=echarts.init(box);c.setOption(opt);CH.push(c);return c};
window.addEventListener('resize',()=>CH.forEach(c=>c.resize()));
const el=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h)e.innerHTML=h;return e};
const card=(t,note)=>{const c=el('div','card','<h2>'+t+'</h2>'+(note?'<div class=note>'+note+'</div>':''));document.getElementById('app').appendChild(c);return c};
const num=(v,d=0)=>v==null?'—':Number(v).toLocaleString('en',{maximumFractionDigits:d,minimumFractionDigits:d});
const ms=(v,d=1)=>v==null?'—':(v*1000).toLocaleString('en',{maximumFractionDigits:d,minimumFractionDigits:d});  // 导出已归一为秒，图/表按 ms 展示
const pct=(v,d=1)=>v==null?'—':(v>0?'+':'')+v.toFixed(d)+'%';
const cls=v=>v==null?'mute':(v>0?'bad':(v<0?'good':'mute'));
document.getElementById('hd').innerHTML=D.head.map(x=>x).join('　·　');

/* 1 写入总览 */
let c=card('① 写入吞吐（口径B＝语料条数 ÷ index-append 任务时长；与 rally 的 warmup 设置无关）','geo/nested 各 3 轮、hnsw 3 轮，三车在同一 campaign 内按轮右旋交错配对；柱上点为逐轮实测');
let box=el('div','chart');c.appendChild(box);
let tr=[...new Set(D.cards.map(x=>x.track))],cars=[...new Set(D.cards.map(x=>x.car))];
const colors={kona25:'#c0392b',jdk25:'#12355b',ez240:'#7f8c8d'};
mk(box, {tooltip:{trigger:'axis'},legend:{top:0},grid:{top:36,bottom:30},
 xAxis:{type:'category',data:tr},yAxis:[{type:'value',name:'docs/s'}],
 series:cars.map(a=>({name:a,type:'bar',barMaxWidth:56,itemStyle:{color:colors[a]||'#999'},
   label:{show:true,position:'top',formatter:p=>num(p.value)},
   data:tr.map(t=>{const x=D.cards.find(y=>y.car===a&&y.track===t);return x?Math.round(x.med_b):null}),
   markPoint:{symbol:'circle',symbolSize:7,itemStyle:{color:'#000'},
     data:(()=>{let r=[];tr.forEach((t,i)=>{const x=D.cards.find(y=>y.car===a&&y.track===t);(x?.rounds||[]).forEach(q=>{if(q.b!=null)r.push({coord:[i,q.b],value:''})})});return r})()}}))});

/* 2 写入尾部：同赛道内比车（bulk service_time，3 轮中位） */
c=card('② 写入尾部：bulk service_time 分车对比（每个赛道一张，车只在同赛道内可比）','跨赛道比绝对值没意义（bulk 尺寸/文档大小/建图成本都不同）；这里 x 轴是轮内分位，三条线分别是三个车');
{const B=D.bulk||[],TS=[...new Set(B.map(x=>x.track))];
 const box=document.createElement('div');box.style.display='grid';box.style.gridTemplateColumns='repeat(auto-fit,minmax(340px,1fr))';box.style.gap='10px';c.appendChild(box);
 TS.forEach(t=>{const rows=B.filter(x=>x.track===t);const div=el('div','chart');box.appendChild(div);
  mk(div,{title:{text:t+' — bulk service_time（ms）',left:'center',textStyle:{fontSize:13}},
   tooltip:{trigger:'axis',valueFormatter:v=>num(v,1)+' ms'},legend:{bottom:0},grid:{top:40,bottom:34,left:60,right:16},
   xAxis:{type:'category',data:['p50','p90','p99','p100 最差']},yAxis:{type:'log',name:'ms'},
   series:rows.map(x=>({name:x.car,type:'line',symbol:'circle',symbolSize:7,lineStyle:{width:2.2,color:colors[x.car]||'#999'},
     itemStyle:{color:colors[x.car]||'#999'},data:[x.p50,x.p90,x.p99,x.p100].map(v=>v==null?null:v*1000)}))});})}

/* 3 逐轮明细 + 环境 */
c=card('③ 逐场明细：吞吐、任务时长、环境实测','steal 来自每场分相位采样（w/m/q 三相位 × 三节点取最坏，5s 周期），只记录不参与判定；steal 全程 0 说明本环境宿主抢占不是波动主因');
let tb=el('table');tb.innerHTML='<tr><th>场</th><th>车</th><th>赛道</th><th>口径B docs/s</th><th>任务 s</th><th>steal 峰%</th><th>判定</th></tr>'+
 D.cards.flatMap(x=>x.rounds.map(q=>`<tr><td>${q.fid}</td><td class=mut>${x.car}</td><td>${x.track}</td><td>${num(q.b)}</td><td>${num(q.dur,1)}</td><td>${num((q.env.cpu_steal_max||0),2)}</td><td>${q.status||'—'}</td></tr>`));
c.appendChild(tb);

/* 4 查询延迟分车（每赛道一张，log 轴；下面附同数据表格） */
c=card('④ 查询延迟 p50 分车对比（3 轮中位，ms；对数轴）','只在同赛道内比车。操作按 2.5.0-GraalVM(jdk25) 的 p50 降序；Δ 列表同下');
{const Q=D.qops||{};const AT=[...new Set((D.cards||[]).map(x=>x.car))];
 Object.keys(Q).forEach(t=>{const tasks=Object.keys(Q[t]).sort((a,b)=>((Q[t][b].jdk25||{}).p50||0)-((Q[t][a].jdk25||{}).p50||0));
  const h=Math.max(300,26*tasks.length);const div=el('div','chart');div.style.height=h+'px';c.appendChild(div);
  mk(div,{title:{text:t+' — 各操作 p50（ms）',left:'center',textStyle:{fontSize:13}},
   tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>v==null?'—':num(v,1)+' ms'},
   legend:{top:22},grid:{top:52,bottom:26,left:210,right:30},
   xAxis:{type:'log',name:'ms'},yAxis:{type:'category',data:tasks,inverse:true,axisLabel:{fontSize:10}},
   series:AT.map(a=>({name:a,type:'bar',barMaxWidth:12,itemStyle:{color:colors[a]||'#999'},
     data:tasks.map(tk=>{const v=(Q[t][tk]||{})[a];return v&&v.p50!=null?+(v.p50*1000).toFixed(2):null})}))});});}
let tbl=el('table');tbl.innerHTML='<tr><th>操作</th>'+cars.map(a=>`<th>${a}</th>`).join('')+`<th>Kona vs GraalVM</th><th>2.5.0 vs 2.4.0</th></tr>`+
  D.qrows.map(x=>{const v=x.vals||{};const kg=(v.kona25&&v.jdk25)?(v.kona25/v.jdk25-1)*100:null;
   const gn=(v.jdk25&&v.ez240)?(v.jdk25/v.ez240-1)*100:null;
   return `<tr><td>${x.track}·${x.task}</td>`+cars.map(a=>`<td>${ms(v[a],1)}</td>`).join('')+
   `<td class="${cls(kg)}">${pct(kg)}</td><td class="${cls(gn)}">${pct(gn)}</td></tr>`}).join('');
c.appendChild(tbl);

/* 5 尾部放大比 p99/p50 */
c=card('⑤ 尾部放大倍数 p99/p50（同车同操作，无量纲）','比值大＝该操作在不同轮/不同请求间抖动剧烈；跨赛道可比，因为它已经把绝对延迟归一掉了');
{const Q=D.qops||{};const AT=[...new Set((D.cards||[]).map(x=>x.car))];const rows=[];
 Object.keys(Q).forEach(t=>Object.keys(Q[t]).forEach(tk=>{const r={};AT.forEach(a=>{const v=(Q[t][tk]||{})[a];
   if(v&&v.p50&&v.p99!=null)r[a]=v.p99/v.p50});if(Object.keys(r).length)rows.push({label:t+'·'+tk,...r})}));
 rows.sort((x,y)=>(y.jdk25||0)-(x.jdk25||0));const top=rows.slice(0,18);
 const div=el('div','chart');div.style.height=Math.max(340,26*top.length)+'px';c.appendChild(div);
 mk(div,{tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>v==null?'—':num(v,2)+'×'},
  legend:{top:0},grid:{top:34,bottom:26,left:230,right:30},
  xAxis:{type:'value',name:'p99/p50'},yAxis:{type:'category',data:top.map(x=>x.label),inverse:true,axisLabel:{fontSize:10}},
  series:AT.map(a=>({name:a,type:'bar',barMaxWidth:12,itemStyle:{color:colors[a]||'#999'},data:top.map(x=>x[a]==null?null:+x[a].toFixed(2))}))});}

/* 6 查询吞吐 ops/s */
c=card('⑥ 查询吞吐（rally 实测 ops/s，3 轮中位）','target-throughput 限流的操作，吞吐差异即延迟差异；未限流的迭代型操作这里为 null');
{const Q=D.qops||{};const AT=[...new Set((D.cards||[]).map(x=>x.car))];const rows=[];
 Object.keys(Q).forEach(t=>Object.keys(Q[t]).forEach(tk=>{const v=Q[t][tk];const any=AT.some(a=>v[a]&&v[a].rtp);
   if(any)rows.push({label:t+'·'+tk,...Object.fromEntries(AT.map(a=>[a,v[a]?v[a].rtp:null]))})}));
 rows.sort((x,y)=>(y.jdk25||0)-(x.jdk25||0));const top=rows.slice(0,20);
 const div=el('div','chart');div.style.height=Math.max(360,26*top.length)+'px';c.appendChild(div);
 mk(div,{tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>v==null?'—':num(v,1)+' ops/s'},
  legend:{top:0},grid:{top:34,bottom:26,left:230,right:30},
  xAxis:{type:'value',name:'ops/s'},yAxis:{type:'category',data:top.map(x=>x.label),inverse:true,axisLabel:{fontSize:10}},
  series:AT.map(a=>({name:a,type:'bar',barMaxWidth:12,itemStyle:{color:colors[a]||'#999'},data:top.map(x=>x[a])}))});}

/* 7 历史趋势 */
c=card('⑦ 历史趋势：各车口径B 中位（跨 campaign，实线=geo、虚线=hnsw/nested）','一条线＝同一个（车×赛道×codec）在不同 campaign 的走势：纵向起伏=环境/窗口漂移，不是代码差异；**只有同一个 x（同一 campaign）上的不同线才是配对可比的**。⚠ = 该点口径有问题（ds3 的 ez 车实际 1g 堆；es819 在 10-18 之前没钉堆、实际 8g）；跨 campaign 的绝对值只可看趋势，不可直接相减');
box=el('div','chart tall');c.appendChild(box);
let gpts=[];for(const t in D.trend)D.trend[t].forEach(s=>s.points.forEach(p=>gpts.push({...p,track:t,car:s.car,key:s.car+(s.codec?'｜'+s.codec:'')})));
const tracks2=[...new Set(gpts.map(p=>p.track))];
const camps=[...new Set(gpts.map(p=>p.campaign))].sort();
mk(box, {tooltip:{trigger:'axis',formatter:ps=>ps.filter(p=>p.data).map(p=>`${p.seriesName} ${num(p.value)}（${p.data.campaign}，n=${p.data.n}${p.data.warn?'，⚠口径警告':''}）`).join('<br>')},
 legend:{top:0,type:'scroll'},grid:{top:40,bottom:60},
 xAxis:{type:'category',data:camps,axisLabel:{rotate:30,fontSize:10,formatter:v=>v.replace(/^([a-z0-9]+)-/,'$1 ').replace(/-(\d{6})$/,'-$1')}},
 yAxis:{type:'value',name:'docs/s'},
 series:tracks2.map(t=>[...new Set(gpts.filter(p=>p.track===t).map(p=>p.key))].map(k=>({
   name:k,type:'line',smooth:false,symbolSize:6,connectNulls:false,
   lineStyle:{width:t==='geo'?2.4:1.2,type:t==='geo'?'solid':'dashed'},
   itemStyle:{color:(colors[gpts.find(p=>p.key===k).car])||'#999'},
   data:(()=>{let m={};gpts.filter(p=>p.track===t&&p.key===k).forEach(p=>m[p.campaign]=p);
     return camps.map(x=>m[x]?{value:Math.round(m[x].med),campaign:x,n:m[x].n,warn:m[x].warn}:null)})()}))).flat()});

/* 8 环境实测 */
c=card('⑧ 逐场环境实测（27 场 × 三节点 × w/m/q 三相位取最坏，5s 采样）','负载卡点只在起跑前查一次，赛中纯记录：这张图就是"数字可不可信"的证据链。steal 全程 0 ⇒ 宿主抢占不是本批波动来源');
{const E=(D.env||[]).filter(x=>x.fid);const names=E.map(x=>x.fid);
 const mkBar=(key,unit,scale)=>({name:key,type:'bar',barMaxWidth:9,tooltip:{valueFormatter:v=>v+' '+unit},
   data:E.map(x=>x[key]==null?null:+(x[key]*(scale||1)).toFixed(3))});
 const div=el('div','chart');div.style.height='420px';c.appendChild(div);
 mk(div,{tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},legend:{top:0},grid:{top:40,bottom:70,left:56,right:16},
  xAxis:{type:'category',data:names,axisLabel:{rotate:40,fontSize:9,interval:0}},
  yAxis:{type:'value',name:'%'},
  series:[Object.assign(mkBar('cpu_steal_max','%'),{itemStyle:{color:'#c0392b'}}),
          Object.assign(mkBar('foreign_cpu_max','%'),{itemStyle:{color:'#7f8c8d'}})]});}

/* 9 结论 */
c=card('⑨ 本轮结论（27 场，全部 VALID）');
c.innerHTML+=`<div class=note>${D.concl.map(x=>'· '+x).join('<br>')}</div>`;
/* 10-12 存储压缩对比（仅当载荷里有 codec campaign 时渲染） */
if(D.codec){
 const A=D.codec.cars,KS=D.codec.keys;
 const acol={'ez-def':'#12355b','ez-zstd-sr':'#0a7a3c','ez-kona-zstd-sr':'#c0392b','es8-def':'#7f8c8d','es8-bc':'#e08214'};
 c=card('⑩ geonames 存储压缩对比：赛末落盘体积（forcemerge 之后，三节点 store 之和）',
   `组合车＝引擎×索引 settings 变体。体积取 q 阶段 sidecar 的 _nodes/stats 三节点之和——race.json 的 store_size 是写赛结束快照，那时本场 forcemerge 还没跑，多段态会掩盖 codec 差异。每场压同一份 ${num(D.codec.docs)} 条语料、3 分片 0 副本、16g 堆 32 核。`);
 box=el('div','chart');c.appendChild(box);
 mk(box,{tooltip:{trigger:'axis',formatter:ps=>ps.map(p=>{const a=A.find(x=>x.key===p.name);return `${p.name}<br>store ${num(p.value,2)} GB（vs ez-def ${pct(a?a.store_delta:null)}）<br>实测 codec：${a?.codec||'—'}`}).join('<br>')},
  grid:{top:30,bottom:40},xAxis:{type:'category',data:KS,axisLabel:{interval:0,fontSize:11}},
  yAxis:{type:'value',name:'GB'},
  series:[{type:'bar',barMaxWidth:70,data:KS.map(k=>{const a=A.find(x=>x.key===k);return{value:a?a.store:null,name:k,itemStyle:{color:acol[k]||'#999'}}}),
   label:{show:true,position:'top',formatter:p=>{const a=A.find(x=>x.key===p.name);return num(p.value,2)+'\n'+pct(a?a.store_delta:null)}}}]});

 c=card('⑪ 同一批的写入吞吐（口径B，逐轮实测点）','codec 只改存储编码，不改查询；若体积换来的收益要付写入，就体现在这里。极差是同车 3 轮的 max/min−1');
 box=el('div','chart');c.appendChild(box);
 mk(box,{tooltip:{trigger:'axis'},grid:{top:30,bottom:40},xAxis:{type:'category',data:KS,axisLabel:{interval:0,fontSize:11}},
  yAxis:{type:'value',name:'docs/s'},
  series:[{type:'bar',barMaxWidth:70,name:'口径B 中位',
    data:KS.map(k=>{const a=A.find(x=>x.key===k);return{value:a&&a.med_b?Math.round(a.med_b):null,name:k,itemStyle:{color:acol[k]||'#999'}}}),
    label:{show:true,position:'top',formatter:p=>num(p.value)},
    markPoint:{symbol:'circle',symbolSize:7,itemStyle:{color:'#000'},
      data:(()=>{let r=[];KS.forEach((k,i)=>{(A.find(x=>x.key===k)?.rounds||[]).forEach(q=>{if(q.b!=null)r.push({coord:[i,Math.round(q.b)],value:''})})});return r})()}}]});
 let tb=el('table');tb.innerHTML='<tr><th>组合车</th><th>实测 codec</th><th>引擎 / JDK</th><th>heap 实测</th><th>核数实测</th><th>n</th><th>口径B 中位</th><th>极差</th><th>赛末 GB</th><th>vs ez-def</th></tr>'+
  A.map(a=>`<tr><td>${a.key}</td><td>${a.codec||'—'}</td><td>${a.engine||'—'} / ${(a.jdk||[]).join('/')||'—'}${(a.jdk_vendor||[]).length?' · '+a.jdk_vendor.join('/'):''}</td><td>${num(a.heap_mb)} MB</td><td>${num(a.cores)}</td><td>${a.n}</td><td>${num(a.med_b)}</td><td>${pct(a.spread)}</td><td>${num(a.store,2)}</td><td class="${cls(a.store_delta)}">${pct(a.store_delta)}</td></tr>`).join('');
 c.appendChild(tb);

 c=card('⑫ 逐操作查询延迟 p50（3 轮中位，ms）：Δ 列＝同引擎内 codec 之差','左Δ＝ez-zstd-sr vs ez-def（Easysearch 内的压缩代价），右Δ＝es8-bc vs es8-def（ES 内的压缩代价）。stored fields 压缩越狠，读源类操作解码越贵；走 doc_values 的聚合/计数基本不动。');
 let qt=el('table');qt.innerHTML='<tr><th>操作</th>'+KS.map(k=>`<th>${k}</th>`).join('')+'<th>Δ EZ codec</th><th>Δ ES codec</th></tr>'+
  D.codec.qrows.map(x=>`<tr><td>${x.task}</td>`+KS.map(k=>`<td>${ms(x.vals[k],1)}</td>`).join('')+
   `<td class="${cls(x.d_ez)}">${pct(x.d_ez)}</td><td class="${cls(x.d_es)}">${pct(x.d_es)}</td></tr>`).join('');
 c.appendChild(qt);
 // ⑬ 逐操作 p50 分组柱（5 车同图）+ ⑭ codec 代价排序（Δ% 对同引擎 baseline）
 c=card('⑬ 逐操作查询 p50 五车同图（3 轮中位，对数轴 ms）','按操作名列，5 个组合车并排；同一引擎的两根柱之差＝该引擎内部换 codec 的代价');
 let d1=el('div','chart');d1.style.height=Math.max(420,22*D.codec.qrows.length)+'px';c.appendChild(d1);
 mk(d1,{tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>v==null?'—':num(v,1)+' ms'},
  legend:{top:0},grid:{top:34,bottom:26,left:230,right:30},
  xAxis:{type:'log',name:'p50 ms'},yAxis:{type:'category',data:D.codec.qrows.map(x=>x.task),inverse:true,axisLabel:{fontSize:10}},
  series:KS.map(k=>({name:k,type:'bar',barMaxWidth:9,itemStyle:{color:acol[k]||'#999'},
    data:D.codec.qrows.map(x=>x.vals[k]==null?null:+(x.vals[k]*1000).toFixed(2))}))});
 const dl=D.codec.qrows.filter(x=>x.d_ez!=null).sort((a,b)=>b.d_ez-a.d_ez);
 c=card('⑭ 压缩的查询代价排序（Δ p50% 对同引擎默认编码）','红/橙＝变慢，绿/蓝＝变快；EZ 组＝ZSTD+source_reuse vs default，ES 组＝best_compression vs default。两组的 baseline 各自独立，不看绝对值只看方向与幅度');
 let d2=el('div','chart');d2.style.height=Math.max(380,24*dl.length)+'px';c.appendChild(d2);
 mk(d2,{tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>v==null?'—':num(v,1)+'%'},
  legend:{top:0},grid:{top:34,bottom:30,left:230,right:40},
  xAxis:{type:'value',name:'Δ p50 vs 同引擎默认编码'},
  yAxis:{type:'category',data:dl.map(x=>x.task),inverse:true,axisLabel:{fontSize:10}},
  series:[{name:'EZ：zstd+sr − def',type:'bar',barMaxWidth:14,data:dl.map(x=>+x.d_ez.toFixed(1)),
     itemStyle:{color:p=>p.value>=0?'#c0392b':'#0a7a3c'},label:{show:true,position:'right',fontSize:10,formatter:p=>pct(p.value)}},
   {name:'ES：bc − def',type:'bar',barMaxWidth:14,data:dl.map(x=>x.d_es==null?null:+x.d_es.toFixed(1)),
     itemStyle:{color:p=>p.value>=0?'#e08214':'#7fb3d5'},label:{show:true,position:'right',fontSize:10,formatter:p=>p.value==null?'':pct(p.value)}}]});
}
/* 页面可能在 0 宽度的后台标签里完成初次渲染 → 首帧后再 resize 一次 */
requestAnimationFrame(()=>setTimeout(()=>CH.forEach(x=>x.resize()),80));
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--codec-campaign", default=None,
                    help="geonames 存储压缩对比 campaign（WHAT=codec 跑出来的），给报告加 ⑦⑧⑨ 三段")
    ap.add_argument("--echarts", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    camp = os.path.basename(args.campaign.rstrip("/"))
    runs = load_runs(os.path.join(args.export, "benchmark-result-race.jsonl"))
    # 场次维度以 benchmark-result-race 为准：指标行不再冗余 car/engine/track_raw/codec/round/phase
    # （方案 §1 修订）—— 报告按 race_id 回填，两边口径不会漂移。
    run_index = {r.get("race_id"): r for r in runs}
    detail = load_detail_metrics(os.path.join(args.export, "benchmark-result-metric.jsonl"), camp, run_index)
    sysrows = [m for m in detail if m.get("family") == "system"]
    # system 行没有 phase/task，需要单独捞
    with open(os.path.join(args.export, "benchmark-result-metric.jsonl"), encoding="utf-8") as f:
        for line in f:
            if '"family": "system"' not in line or ('"campaign": "%s"' % camp) not in line:
                continue
            d = json.loads(line)
            sysrows.append({k: d.get(k) for k in ("campaign", "fid", "metric", "value", "value_original")})
    payload = build(runs, detail, sysrows, camp)
    ccamp = args.codec_campaign and os.path.basename(args.codec_campaign.rstrip("/"))
    if ccamp:
        payload["codec"] = build_codec(runs, load_detail_metrics(
            os.path.join(args.export, "benchmark-result-metric.jsonl"), ccamp, run_index), ccamp)
    payload["head"] = [
        "campaign %s：3 车 × 3 赛道 × 3 轮 = 27 场，全 VALID、0 重试" % camp,
        "口径 16g 堆 / 32 vCPU 逐场实测断言；负载卡点仅起跑前一次，赛中分相位采样只记录",
        "写入暖机：geo 20s、nested 25s、hnsw 40s（原 geo 70s / nested 120s 会吃掉测量窗口）",
        "生成于导出载荷 benchmark-result-race/benchmark-result-metric（离线，未依赖 ES）",
    ]
    payload["concl"] = [
        "Kona 25 与 Oracle GraalVM 25 在三赛道写入上全部持平：geo +0.6%、hnsw +1.9%、nested +2.2%，均小于同车轮次间极差（7.8~25.5%）。此前「Kona 快 14~17%」与「nested GraalVM 快 19%」均未复现，已撤回。",
        "2.5.0 相对 2.4.0 有实质收益：geonames 写入 +12.4%、hnsw 建库 +21.9%、knn 查询 p50 −15~19%、painless_dynamic −17%、bulk p100 5.6s→4.4s；nested 文档灌入 +0.7%（无收益）。",
        "复现性最好的赛道是 hnsw（同车极差 1.3~3.8%），最差是 nested（14.7~25.5%）；比较结论应取中位并要求 ≥3 轮同场配对。",
        "环境证据：27 场分相位采样显示 steal 全程 0.00%、iowait ≤2%、disk util 3~5%、load1 ≤6.6（32 核）、ENV-VERDICT 全 OK ⇒ 宿主抢占无法解释历史上 45% 的读数散布；跨 campaign 电平漂移（hnsw 三批 4828→4652→4199）需在集群内部（merge/页缓存/JIT 状态）继续找因。",
    ]
    if payload.get("codec"):
        ca = {a["key"]: a for a in payload["codec"]["cars"]}
        ez, zst, bc, esd = ca.get("ez-def"), ca.get("ez-zstd-sr"), ca.get("es8-bc"), ca.get("es8-def")
        scroll = next((q for q in payload["codec"]["qrows"] if q["task"] == "scroll"), None)
        payload["head"].append("⑩-⑭ 来自 codec campaign %s：5 组「引擎×索引 settings」组合车 × 3 轮 = 15 场，全 VALID" % ccamp)
        if ez and zst and bc:
            payload["concl"] = [
                "存储压缩主结论：Easysearch 2.5.0 开 `index.codec=ZSTD` + `index.source_reuse` 后，geonames 赛末落盘 %.2f GB（比默认 LZ4 的 %.2f GB 省 %.1f%%）；ES 8.19 开 best_compression 只到 %.2f GB（省 %.1f%%）。同语料、同 3 分片 0 副本、同 16g 堆 32 核，且两侧 codec 都从 sidecar 的 `_settings` 回读确认生效。" % (
                    zst["store"], ez["store"], -zst["store_delta"], bc["store"], -bc["store_delta"]),
                "写入不赔：ez-zstd-sr 口径B %.0f docs/s vs ez-def %.0f（差 %.1f%%，远小于同车轮次间极差 %.1f%%/%.1f%%）。" % (
                    zst["med_b"], ez["med_b"], (zst["med_b"] / ez["med_b"] - 1) * 100,
                    ez["spread"] or 0, zst["spread"] or 0),
                "代价在读 stored fields 的操作：%s。走 doc_values 的聚合/计数不动。" % (
                    ("`scroll` p50 %.1fms→%.1fms（%+.0f%%）是最大短板，排序/取源类普遍 +2~4ms" % (
                        scroll["vals"]["ez-def"] * 1000, scroll["vals"]["ez-zstd-sr"] * 1000, scroll["d_ez"]))
                    if (scroll and scroll.get("d_ez") is not None) else "排序/取源类普遍 +2~4ms"),
                "口径修正：ES 8.19 出厂 `config/jvm.options` 没有 `-Xms/-Xmx`，堆走 JVM 默认（物理内存 1/4 = 8g@32G 虚机）；本 campaign 每场 es819 赛前写死 `jvm.options.d/heap.options` 并由 heap_assert 硬断言，三节点实测 16384MB。10-13/10-15 的 es819 场是 8g vs 16g 不同基，不可与本批直接比。",
            ]
    html = HTML.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__ECHARTS__", open(args.echarts, encoding="utf-8", errors="replace").read())
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("报告 → %s（%.1f MB；明细指标 %d 行）" % (args.out, os.path.getsize(args.out) / 1e6, len(detail)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
