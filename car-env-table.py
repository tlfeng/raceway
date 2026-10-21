#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""car-env-table.py —— 三车×多赛道的"性能 × 环境"联合表（在压测机上跑）

  ssh root@<压测机> 'python3 - <campaign 目录> [--detail]' < scripts/car-env-table.py

数据源（都是编排每场落的）：
  results/<fid>-{w,q}-race.json      → 口径A(rally Median Throughput) 与 口径B(语料÷duration)
  monitors/<fid>-<node>-summary.txt  → 该场采样汇总的 steal/foreign/swapin/verdict
口径A=warmup 之后样本的中位速率，口径B=含 warmup 的全程平均（docs/metrics.rst:140 duration 含 warmup）。
"""
import glob
import json
import os
import re
import statistics
import sys

DOCS = {"track": 11396503, "hnsw2m-2m-s3": 2000000, "nested-s3": 11203029,
        "hv1024-s3": 1000000, "hv512-s3": 1000000}
NAME = {"track": "geo", "hnsw2m-2m-s3": "hnsw", "nested-s3": "nested", "hv1024-s3": "hv1024"}
WARMUP = {"track": 20, "hnsw2m-2m-s3": 40, "nested-s3": 25, "hv1024-s3": 25}


def summary_of(camp_dir, fid):
    """该场三节点环境摘要（取三节点里的最坏值）"""
    agg = {"samples": 0, "steal_avg": 0.0, "steal_max": 0.0,
           "foreign_max": 0.0, "swapin": 0, "verdict": "NO-DATA"}
    files = glob.glob(os.path.join(camp_dir, "monitors", "%s-*-summary.txt" % fid))
    for p in files:
        t = open(p, encoding="utf-8", errors="replace").read()
        g = lambda k, cast=float: (cast(re.search(k + r"=([0-9.]+)", t).group(1))
                                   if re.search(k + r"=([0-9.]+)", t) else 0)
        agg["samples"] += g("samples", int)
        for k, key in (("steal_avg", "steal_avg"), ("steal_max", "steal_max"),
                       ("foreign_max", "foreign_max")):
            agg[key] = max(agg[key], g(k))
        agg["swapin"] += g("swapin", int)
        m = re.search(r"ENV-VERDICT: (\S+)", t)
        if m:
            v = m.group(1)
            order = {"OK": 0, "WARN": 1, "CONTAMINATED": 2, "NO-DATA": 3}
            if order.get(v, 9) >= order.get(agg["verdict"], 9) or agg["verdict"] == "NO-DATA" and v == "OK":
                agg["verdict"] = v
    if not files:
        agg["verdict"] = "无监控"
    return agg


def main():
    args = [a for a in sys.argv[1:]]
    detail = "--detail" in args
    camps = [a for a in args if not a.startswith("--")]
    if not camps:
        print(__doc__)
        return 1
    rows = []
    for camp in camps:
        for p in sorted(glob.glob(os.path.join(camp, "results", "*-w-race.json"))):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if d.get("challenge") != "write-only":
                continue
            om = (d.get("results") or {}).get("op_metrics") or []
            if not om:
                continue
            tr = d.get("track")
            if tr not in DOCS:
                continue
            fid = os.path.basename(p)[:-len("-w-race.json")]
            a = (om[0].get("throughput") or {}).get("median")
            dur = (om[0].get("duration") or 0) / 1000.0
            b = DOCS[tr] / dur if dur else None
            env = summary_of(camp, fid)
            rows.append({"fid": fid, "car": fid.split("-")[0], "track": NAME.get(tr, tr),
                         "round": d["user-tags"].get("round"), "camp": os.path.basename(camp),
                         "a": a, "b": b, "dur": dur, "win": dur - WARMUP.get(tr, 0), "env": env,
                         "p99": (om[0].get("service_time") or {}).get("99_0"),
                         "p100": (om[0].get("service_time") or {}).get("100_0")})
    if not rows:
        print("没有 write-only 的 race.json 可解析")
        return 1

    if detail:
        print("%-20s %-7s %-4s %9s %9s %6s %6s %8s %8s %s" % (
            "场", "车", "轮", "口径A", "口径B", "任务s", "窗口s", "steal峰", "foreign峰", "判定"))
        for r in sorted(rows, key=lambda x: (x["track"], x["camp"], x["fid"])):
            e = r["env"]
            print("%-20s %-7s %-4s %9s %9.0f %6.1f %6.1f %8.2f %8.2f %s" % (
                r["fid"][:20], r["car"], "r%s" % r["round"], ("%.0f" % r["a"]) if r["a"] else "NULL",
                r["b"] or 0, r["dur"], r["win"], e["steal_max"], e["foreign_max"], e["verdict"]))

    print("\n=== 按 车 × 赛道：性能 × 环境 ===")
    print("%-7s %-7s %3s %9s %8s %8s %9s %8s %8s %8s %s" % (
        "车", "赛道", "n", "口径B 中位", "极差%", "A-B偏移%", "bulk p99", "p100", "steal峰", "foreign峰", "判定"))
    grp = {}
    for r in rows:
        grp.setdefault((r["car"], r["track"]), []).append(r)
    for (car, track), v in sorted(grp.items(), key=lambda x: (x[0][1], -statistics.median([y["b"] or 0 for y in x[1]]))):
        bs = [y["b"] for y in v if y["b"]]
        ab = [(y["a"] / y["b"] - 1) * 100 for y in v if y["a"] and y["b"]]
        p99 = [y["p99"] for y in v if y["p99"]]
        p100 = [y["p100"] for y in v if y["p100"]]
        st = max((y["env"]["steal_max"] for y in v), default=0)
        fo = max((y["env"]["foreign_max"] for y in v), default=0)
        verd = " / ".join(sorted({y["env"]["verdict"] for y in v}))
        print("%-7s %-7s %3d %9.0f %8.1f %8.2f %8s %8s %8.2f %8.2f %s" % (
            car, track, len(v), statistics.median(bs) if bs else 0,
            (max(bs) / min(bs) - 1) * 100 if bs and min(bs) else 0,
            statistics.mean(ab) if ab else 0,
            ("%.0f" % statistics.median(p99)) if p99 else "-",
            ("%.0f" % statistics.median(p100)) if p100 else "-",
            st, fo, verd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
