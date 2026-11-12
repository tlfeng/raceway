#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codec-compare.py —— geonames 存储压缩对比（完整有效测量）。

  ssh root@<压测机> 'python3 - <campaign 目录> [--detail]' < scripts/codec-compare.py

三件事分开看，别混成一个数：
  ① 写入吞吐：口径A=rally Median Throughput（warmup 后样本中位），口径B=语料条数÷duration（全程平均）
  ② 落盘体积：主指标取赛末 sidecar 探针（logs/fp/<fid>-*/nodes-stats.txt）的三节点 store 之和；
     race.json 的 store_size 只是写赛结束时的快照，压缩差异没充分显现（见 post_merge_store）
  ③ 查询延迟：op_metrics[].service_time 分位，统一为 ms，不将开环积压当作引擎耗时

codec 一列读的是 sidecar 里 `GET /geonames/_settings` 的**实测**值，不是赛道文件名烘的期望值
（引擎静默忽略未知设置时，期望 ≠ 生效；ES 就不认 index.source_reuse）。
"""
import argparse
import json
import os
import statistics
import sys

from result_selection import finite, read_json, select_measurements, valid_fids

DOCS = 11396503
CAR_LABEL = {}
CAR_ORDER = []


def j(p):
    try:
        return read_json(p)
    except (ValueError, OSError):
        return None


def median(v):
    v = [x for x in v if finite(x)]
    return statistics.median(v) if v else None


def rng_pct(v):
    v = [x for x in v if finite(x)]
    return (max(v) / min(v) - 1) * 100 if len(v) > 1 and min(v) else 0.0


def measured_codec(camp, fid):
    """sidecar 里实测的 index.codec / index.source_reuse（读不到返回 None）"""
    for ph in ("wq", "q", "w"):
        s = j(os.path.join(camp, "results", "%s-%s-sidecar.json" % (fid, ph)))
        if not s:
            continue
        st = ((s.get("run_params") or {}).get("index_settings_measured") or {}).get("geonames")
        if st:
            c = st.get("index.codec", "default")
            sr = str(st.get("index.source_reuse", "false")).lower()
            return c + ("+source_reuse" if sr == "true" else "")
    return None


def post_merge_store(camp, fid, expected_nodes=None):
    """真·压缩后体积：赛末 sidecar 原始探针 logs/fp/<fid>-<q|wq>/nodes-stats.txt 的三节点
    store 之和（拆分协议落 -q、单挑战协议落 -wq，merge 都发生在探针之前）。
    race.json 的 store_size 是写赛结束时的快照，多段状态下 codec 差异没充分显现
    → 压缩对比必须以赛末（merge 之后）的节点统计为准。"""
    out = {"store_gb": None, "segments": None, "docs": None}
    for ph in ("wq", "q"):
        p = os.path.join(camp, "logs", "fp", "%s-%s" % (fid, ph), "nodes-stats.txt")
        d = j(p)
        if not d or not isinstance(d.get("nodes"), dict) or not d["nodes"]:
            continue
        nodes = list(d["nodes"].values())
        counters = d.get("_nodes") or {}
        if (type(expected_nodes) is not int or expected_nodes <= 0 or len(nodes) != expected_nodes
                or counters.get("total") != expected_nodes or counters.get("successful") != expected_nodes
                or counters.get("failed") != 0):
            continue
        def value(node, family, key):
            result = ((node.get("indices") or {}).get(family) or {}).get(key)
            return result if finite(result) and result >= 0 else None
        stores = [value(n, "store", "size_in_bytes") for n in nodes]
        segments = [value(n, "segments", "count") for n in nodes]
        docs = [value(n, "docs", "count") for n in nodes]
        out["store_gb"] = sum(stores) / 2**30 if all(v is not None for v in stores) else None
        out["segments"] = sum(segments) if all(v is not None for v in segments) else None
        out["docs"] = sum(docs) if all(v is not None for v in docs) else None
        break
    return out


def collect(camp):
    fields = {}
    signatures = {}
    for row in select_measurements(camp):
        variant = row["track"]
        if not (variant in ("geo", "geonames", "gdef", "gzstd", "gbc") or variant.startswith("geo-codec-")):
            continue
        phase = "wq" if "wq" in row["races"] else "w"
        d = row["races"][phase]
        om = (d.get("results") or {}).get("op_metrics") or []
        writes = [m for m in om if m.get("task") == "index-append"]
        if len(writes) != 1:
            continue
        write = writes[0]
        duration = write.get("duration")
        dur = duration / 1000 if finite(duration) and duration > 0 else None
        if dur is None:
            continue
        key = row["series_key"] + "|" + variant
        prior = signatures.setdefault(key, row["signature"])
        if prior != row["signature"]:
            raise ValueError("%s 实测条件不一致，不能合池" % key)
        pm = post_merge_store(camp, row["fid"], int((d.get("user-tags") or {})["nnodes"]))
        res = d.get("results") or {}
        e = fields.setdefault(key, {"w": [], "q": [], "fid0": row["fid"], "signature": row["signature"]})
        e["w"].append({
            "round": row["round"], "a": (write.get("throughput") or {}).get("median"),
            "b": DOCS / dur, "dur": dur, "store": pm["store_gb"],
            "store_race": res.get("store_size"), "segs": pm["segments"],
            "merge": res.get("merge_time", 0) / 1000,
            "merge_throttle": res.get("merge_throttle_time", 0) / 1000,
            "refresh": res.get("refresh_time", 0) / 1000,
            "ygc": res.get("young_gc_time", 0) / 1000,
            "ogc": res.get("old_gc_time", 0) / 1000,
            "err": write.get("error_rate"), "codec": measured_codec(camp, row["fid"]),
        })
        q = row["races"].get("q") or d
        for metric in (q.get("results") or {}).get("op_metrics") or []:
            if metric.get("task", "").startswith(("index", "upsert", "refresh", "force-merge", "wait-")):
                continue
            service = metric.get("service_time") or {}
            factor = {"ms": 1, "s": 1000}.get(service.get("unit", "ms"))
            e["q"].append({"op": metric.get("task"), "round": row["round"],
                           "p50": service["50_0"] * factor if factor and finite(service.get("50_0")) else None,
                           "p99": service["99_0"] * factor if factor and finite(service.get("99_0")) else None,
                           "rtp": (metric.get("throughput") or {}).get("median")})
    return fields


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaigns", nargs="+")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--baseline", help="体积差的精确基线身份（series_key|track_variant）")
    args = ap.parse_args()
    camps, detail = list(dict.fromkeys(args.campaigns)), args.detail
    fields = {}
    try:
        for c in camps:
            for k, v in collect(c).items():
                target = fields.setdefault(k, {"w": [], "q": [], "fid0": v["fid0"], "signature": v["signature"]})
                if target["signature"] != v["signature"]:
                    raise ValueError("%s 跨 campaign 实测条件不一致" % k)
                target["w"] += v["w"]
                target["q"] += v["q"]
    except (OSError, ValueError) as e:
        print("压缩对比失败: %s" % e, file=sys.stderr)
        return 1
    if not fields:
        print("没有可解析的 codec 场次")
        return 1

    print("=== ① 写入 + ② 落盘体积 ===")
    print("%-32s %s %2s %9s %7s %9s %8s %8s %8s %8s %8s %8s %8s" % (
        "组合车", "实测codec", "n", "口径B中位", "极差%", "口径A中位", "A-B%", "store GB(赛末)",
        "vs baseline", "段数", "merge s", "GC y/o s", "err"))
    base_store = None
    order = [k for k in CAR_ORDER if k in fields] + [k for k in sorted(fields) if k not in CAR_ORDER]
    if args.baseline:
        if args.baseline not in fields:
            print("不存在的基线身份: %s" % args.baseline, file=sys.stderr)
            return 1
        base_store = median([x["store"] for x in fields[args.baseline]["w"]])
    for k in order:
        w = fields[k]["w"]
        print("%-32s %-16s %2d %9.0f %7.1f %9s %8s %8s %8s %8s %8s %10s %8s" % (
            CAR_LABEL.get(k, k), w[0]["codec"] or "?", len(w),
            median([x["b"] for x in w]) or 0, rng_pct([x["b"] for x in w]),
            ("%.0f" % median([x["a"] for x in w])) if median([x["a"] for x in w]) else "NULL",
            ("%.1f" % ((median([x["a"] for x in w]) / median([x["b"] for x in w]) - 1) * 100))
            if median([x["a"] for x in w]) and median([x["b"] for x in w]) else "-",
            ("%.2f" % median([x["store"] for x in w])) if median([x["store"] for x in w]) is not None else "-",
            ("%+.1f%%" % ((median([x["store"] for x in w]) / base_store - 1) * 100))
            if base_store and median([x["store"] for x in w]) else "-",
            "/".join(str(x["segs"]) for x in w),
            "%.0f" % (median([x["merge"] for x in w]) or 0),
            "%.1f/%.1f" % (median([x["ygc"] for x in w]) or 0, median([x["ogc"] for x in w]) or 0),
            ",".join(str(x["err"]) for x in w)))

    if detail:
        print("\n=== 逐场 ===")
        print("%-20s %-3s %9s %6s %8s %5s %8s" % ("场", "轮", "口径B", "任务s", "store GB", "段", "merge s"))
        for k in order:
            for x in fields[k]["w"]:
                print("%-20s %-3s %9.0f %6.1f %8s %5s %8.1f" % (
                    CAR_LABEL.get(k, k)[:20], "r%s" % x["round"], x["b"] or 0, x["dur"],
                    ("%.2f" % x["store"]) if x["store"] is not None else "-", x["segs"], x["merge"]))

    ops = sorted({m["op"] for k in order for m in fields[k]["q"] if m["op"]}, key=str)
    if ops:
        print("\n=== ③ 查询延迟中位（ms，三场取中位）===")
        print("%-42s %s" % ("操作", " ".join("%10s" % CAR_LABEL.get(k, k).split(" ")[0][:10] for k in order)))
        for op in ops:
            cells = []
            for k in order:
                v = [m["p50"] for m in fields[k]["q"] if m["op"] == op]
                cells.append("%10s" % (("%.1f" % median(v)) if v and median(v) else "-"))
            print("%-42s %s" % (str(op)[:42], " ".join(cells)))
        print("\n=== ③b 查询吞吐中位（ops/s）===")
        print("%-42s %s" % ("操作", " ".join("%10s" % CAR_LABEL.get(k, k).split(" ")[0][:10] for k in order)))
        for op in ops:
            cells = []
            for k in order:
                v = [m["rtp"] for m in fields[k]["q"] if m["op"] == op]
                cells.append("%10s" % (("%.1f" % median(v)) if v and median(v) else "-"))
            print("%-42s %s" % (str(op)[:42], " ".join(cells)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
