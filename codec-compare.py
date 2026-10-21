#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codec-compare.py —— geonames 存储压缩对比（5 组合车）汇总（在压测机上跑）

  ssh root@<压测机> 'python3 - <campaign 目录> [--detail]' < scripts/codec-compare.py

三件事分开看，别混成一个数：
  ① 写入吞吐：口径A=rally Median Throughput（warmup 后样本中位），口径B=语料条数÷duration（全程平均）
  ② 落盘体积：race.json 的 store_size 是 `_all` 全集群统计；本轮每场只有一个 geonames 索引，
     所以它就等于该 codec 的实测体积（同语料、同 3 分片、赛后都 forcemerge）——这是压缩对比的主指标
  ③ 查询延迟：q-race.json 的 op_metrics[].latency 分位（codec 改变了 stored fields 的解码成本）

codec 一列读的是 sidecar 里 `GET /geonames/_settings` 的**实测**值，不是赛道文件名烘的期望值
（引擎静默忽略未知设置时，期望 ≠ 生效；ES 就不认 index.source_reuse）。
"""
import glob
import json
import os
import re
import statistics
import sys

DOCS = 11396503
CAR_LABEL = {
    "jdk25-gdef": "ez-def (2.5.0@GraalVM25)",
    "jdk25-gzstd": "ez-zstd-sr (2.5.0@GraalVM25)",
    "kona25-gzstd": "ez-kona-zstd-sr (2.5.0@Kona25)",
    "es819-gdef": "es8-def (8.19.23)",
    "es819-gbc": "es8-bc (8.19.23)",
}
CAR_ORDER = ["jdk25-gdef", "jdk25-gzstd", "kona25-gzstd", "es819-gdef", "es819-gbc"]


def j(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (ValueError, OSError):
        return None


def median(v):
    v = [x for x in v if isinstance(x, (int, float))]
    return statistics.median(v) if v else None


def rng_pct(v):
    v = [x for x in v if isinstance(x, (int, float))]
    return (max(v) / min(v) - 1) * 100 if len(v) > 1 and min(v) else 0.0


def measured_codec(camp, fid):
    """sidecar 里实测的 index.codec / index.source_reuse（读不到返回 None）"""
    for ph in ("w", "q"):
        s = j(os.path.join(camp, "results", "%s-%s-sidecar.json" % (fid, ph)))
        if not s:
            continue
        st = ((s.get("run_params") or {}).get("index_settings_measured") or {}).get("geonames")
        if st:
            c = st.get("index.codec", "default")
            sr = (st.get("index.source_reuse") or "").lower()
            return c + ("+source_reuse" if sr == "true" else "")
    return None


def post_merge_store(camp, fid):
    """真·压缩后体积：q 阶段 sidecar 原始探针 logs/fp/<fid>-q/nodes-stats.txt 的三节点 store 之和。
    race.json 的 store_size 是写赛结束时的快照，那时本编排的 force_merge_to_one 还没跑，
    多段状态下 codec 差异没充分显现 → 压缩对比必须以赛末（merge 之后）的节点统计为准。"""
    out = {"store_gb": None, "segments": None, "docs": None}
    for ph in ("q", "w"):
        p = os.path.join(camp, "logs", "fp", "%s-%s" % (fid, ph), "nodes-stats.txt")
        d = j(p)
        if not d or "nodes" not in d:
            continue
        store = sum((n.get("indices", {}).get("store", {}) or {}).get("size_in_bytes", 0)
                    for n in d["nodes"].values())
        out["store_gb"] = store / 2**30
        out["segments"] = sum((n.get("indices", {}).get("segments", {}) or {}).get("count", 0)
                              for n in d["nodes"].values())
        out["docs"] = sum((n.get("indices", {}).get("docs", {}) or {}).get("count", 0)
                          for n in d["nodes"].values())
        break
    return out


def valid_fids(camp):
    """RACES.md 里状态 VALID 的场次名集合。
    重试场的**写赛**常常是成功的（10-18 的 es819-gbc-r1 两次 QUERY-FAIL 前写赛都落了档），
    只看 results/*.json 会把它们当成独立样本 → n 虚高、且把作废场混进中位数。"""
    p = os.path.join(camp, "RACES.md")
    ok = set()
    if not os.path.isfile(p):
        return None
    for line in open(p, encoding="utf-8", errors="replace"):
        parts = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(parts) >= 2 and parts[0] not in ("场次", "---") and not set(parts[0]) <= set("-"):
            if parts[1] in ("VALID", "ENV-WARN"):
                ok.add(parts[0])
    return ok or None


def collect(camp):
    keep = valid_fids(camp)
    fields = {}
    for p in sorted(glob.glob(os.path.join(camp, "results", "*-g*-w-race.json"))):
        fid = os.path.basename(p)[:-len("-w-race.json")]
        if keep is not None and fid not in keep:
            continue
        key = "-".join(fid.split("-")[:2])          # car + 变体（r 轮号在后头）
        if key not in CAR_LABEL:
            continue
        d = j(p)
        if not d or d.get("challenge") != "write-only":
            continue
        om = (d.get("results") or {}).get("op_metrics") or []
        if not om:
            continue
        res = d.get("results") or {}
        dur = (om[0].get("duration") or 0) / 1000.0
        pm = post_merge_store(camp, fid)
        e = fields.setdefault(key, {"w": [], "q": [], "fid0": fid})
        e["w"].append({
            "round": (d.get("user-tags") or {}).get("round"),
            "a": (om[0].get("throughput") or {}).get("median"),
            "b": DOCS / dur if dur else None,
            "dur": dur,
            "store": pm["store_gb"] if pm["store_gb"] else (res.get("store_size") or 0) / 2**30,
            "store_race": (res.get("store_size") or 0) / 2**30,
            "segs": pm["segments"] if pm["segments"] is not None else res.get("segment_count"),
            "merge": (res.get("merge_time") or 0) / 1000.0,
            "merge_throttle": (res.get("merge_throttle_time") or 0) / 1000.0,
            "refresh": (res.get("refresh_time") or 0) / 1000.0,
            "ygc": (res.get("young_gc_time") or 0) / 1000.0,
            "ogc": (res.get("old_gc_time") or 0) / 1000.0,
            "err": om[0].get("error_rate"),
            "codec": measured_codec(camp, fid),
        })
        qp = os.path.join(camp, "results", "%s-q-race.json" % fid)
        if os.path.isfile(qp):
            qd = j(qp)
            for m in ((qd or {}).get("results") or {}).get("op_metrics") or []:
                e["q"].append({"op": m.get("task"), "round": (qd.get("user-tags") or {}).get("round"),
                               "p50": (m.get("latency") or {}).get("50_0"),
                               "p99": (m.get("latency") or {}).get("99_0"),
                               "rtp": (m.get("throughput") or {}).get("median")})
    return fields


def main():
    args = [a for a in sys.argv[1:]]
    detail = "--detail" in args
    camps = [a for a in args if not a.startswith("--")]
    if not camps:
        print(__doc__)
        return 1
    fields = {}
    for c in camps:
        for k, v in collect(c).items():
            fields.setdefault(k, {"w": [], "q": [], "fid0": v["fid0"]})
            fields[k]["w"] += v["w"]
            fields[k]["q"] += v["q"]
    if not fields:
        print("没有可解析的 codec 场次")
        return 1

    print("=== ① 写入 + ② 落盘体积 ===")
    print("%-32s %s %2s %9s %7s %9s %8s %8s %8s %8s %8s %8s %8s" % (
        "组合车", "实测codec", "n", "口径B中位", "极差%", "口径A中位", "A-B%", "store GB(赛末)",
        "vs ez-def", "段数", "merge s", "GC y/o s", "err"))
    base_store = None
    order = [k for k in CAR_ORDER if k in fields] + [k for k in sorted(fields) if k not in CAR_ORDER]
    if "jdk25-gdef" in fields:
        base_store = median([x["store"] for x in fields["jdk25-gdef"]["w"]])
    for k in order:
        w = fields[k]["w"]
        print("%-32s %-16s %2d %9.0f %7.1f %9s %8s %8.2f %8s %8s %8s %10s %8s" % (
            CAR_LABEL.get(k, k), w[0]["codec"] or "?", len(w),
            median([x["b"] for x in w]) or 0, rng_pct([x["b"] for x in w]),
            ("%.0f" % median([x["a"] for x in w])) if median([x["a"] for x in w]) else "NULL",
            ("%.1f" % ((median([x["a"] for x in w]) / median([x["b"] for x in w]) - 1) * 100))
            if median([x["a"] for x in w]) and median([x["b"] for x in w]) else "-",
            median([x["store"] for x in w]) or 0,
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
                print("%-20s %-3s %9.0f %6.1f %8.2f %5s %8.1f" % (
                    CAR_LABEL.get(k, k)[:20], "r%s" % x["round"], x["b"] or 0, x["dur"], x["store"],
                    x["segs"], x["merge"]))

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
