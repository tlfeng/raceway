#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""car-compare.py —— 两车配对对比，只统计完整、有效且身份一致的机读测量。

B 侧每条 --assert 至少三个有效样本。不同 build/JDK/处理变量不得按车名合池。
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys

from result_selection import finite, metrics_of, select_measurements

W_KEYS = ["Median Throughput", "Max Throughput", "50th percentile service time",
          "90th percentile service time", "99th percentile service time", "100th percentile service time"]


DOCS = {"geo": 11396503, "hnsw": 2000000, "nested": 11203029}   # 各赛道语料条数（口径B 用）


def load(camps):
    """→ {(car, track_variant): {测量键: {'w': metrics, 'q': metrics, ...}}}。"""
    out, identities = {}, {}
    for camp in dict.fromkeys(os.path.realpath(c) for c in camps):
        if not os.path.isdir(camp):
            raise ValueError("campaign 目录不存在: %s" % camp)
        for row in select_measurements(camp):
            group = (row["car"], row["track"])
            prior = identities.setdefault(group, row["signature"])
            if prior != row["signature"]:
                raise ValueError("%s/%s 的 build、JDK 或实验条件不一致，不能合池" % group)
            slot = {"camp": row["camp"], "series_key": row["series_key"], "w": {}, "q": {},
                    "write_tasks": set(), "query_tasks": set()}
            for phase, race in row["races"].items():
                parsed = metrics_of(race)
                ops = (race.get("results") or {}).get("op_metrics") or []
                writes = {op["task"] for op in ops if (op.get("throughput") or {}).get("unit") == "docs/s"}
                queries = {op["task"] for op in ops if op.get("task") and op["task"] not in writes
                           and not op["task"].startswith(("refresh", "force-merge", "wait-", "delete-", "create-", "check-cluster"))}
                if phase in ("w", "wq"):
                    slot["w"].update({k: v for k, v in parsed.items() if k[1] in writes})
                    slot["write_tasks"].update(writes)
                if phase in ("q", "wq"):
                    slot["q"].update({k: v for k, v in parsed.items() if k[1] in queries})
                    slot["query_tasks"].update(queries)
                for op in (race.get("results") or {}).get("op_metrics") or []:
                    if op.get("task") == "index-append" and finite(op.get("duration")) and op["duration"] > 0:
                        slot["dur"] = op["duration"] / 1000.0
            out.setdefault(group, {})[(camp, row["round"])] = slot
    return out


def med(vals):
    vals = [v for v in vals if finite(v)]
    return statistics.median(vals) if vals else None


def pct(a, b):
    return "  n/a" if not a or not b else f"{(b / a - 1) * 100:+6.1f}%"


def delta_pct(a, b):
    """B 相对 A 的百分比变化（数值版 pct）；任一缺失、非有限值或 A 为 0 → None。"""
    if a is None or b is None or not finite(a) or not finite(b) or a == 0:
        return None
    return (b / a - 1) * 100


def direction_of_key(key):
    """吞吐越高越好；时延/耗时越低越好（与 bench-export 的 direction 口径一致）。"""
    return "higher-better" if "Throughput" in key else "lower-better"


def fmt(v, dec=1):
    return "     -" if v is None else (f"{v:,.{dec}f}")


def write_rows(data, a, b, track):
    """写段指标行（文本表与 --json 共用一份口径）。

    → [{task, metric, a_med, b_med, delta_pct, a_n, b_n, a_rounds, b_rounds, direction}...]，
    a_rounds/b_rounds 是过滤掉缺失后的逐轮原值（文本表的"逐轮/轮间极差"列要用）。
    """
    ra = data.get((a, track), {})
    rb = data.get((b, track), {})
    tasks = sorted({task for slot in ra.values() for task in slot.get("write_tasks", {"index-append"})} |
                   {task for slot in rb.values() for task in slot.get("write_tasks", {"index-append"})})
    rows = []
    for task, key in ((task, key) for task in tasks for key in W_KEYS):
        va = [ra[r]["w"].get((key, task)) for r in sorted(ra) if "w" in ra[r]]
        vb = [rb[r]["w"].get((key, task)) for r in sorted(rb) if "w" in rb[r]]
        va = [v for v in va if v is not None]
        vb = [v for v in vb if v is not None]
        rows.append({"task": task, "metric": key, "a_med": med(va), "b_med": med(vb),
                     "delta_pct": delta_pct(med(va), med(vb)),
                     "a_n": len(va), "b_n": len(vb), "a_rounds": va, "b_rounds": vb,
                     "direction": direction_of_key(key)})
    return rows


def write_table(data, a, b, track, camps_label):
    ra = data.get((a, track), {})
    rb = data.get((b, track), {})
    if not ra or not rb:
        return
    rows = write_rows(data, a, b, track)
    print(f"\n## {track} 写入（{camps_label}）\n")
    print(f"| 任务 / 指标 | {a} 逐轮 | {a} 中位 | {b} 逐轮 | {b} 中位 | Δ(B/A) | 轮间极差 A / B |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        va, vb = row["a_rounds"], row["b_rounds"]
        key, ma, mb = row["metric"], row["a_med"], row["b_med"]
        dec = 0 if "Throughput" in key else 1
        spread_a = f"{min(va):,.0f}~{max(va):,.0f}" if va else "-"
        spread_b = f"{min(vb):,.0f}~{max(vb):,.0f}" if vb else "-"
        print(f"| {row['task']} / {key} | {' / '.join(fmt(v, dec) for v in va)} | {fmt(ma, dec)} | "
              f"{' / '.join(fmt(v, dec) for v in vb)} | {fmt(mb, dec)} | {pct(ma, mb)} | {spread_a} / {spread_b} |")

    if track in DOCS:
        def rate(src):
            return [DOCS[track] / src[r]["dur"] for r in sorted(src) if "dur" in src[r]]
        def secs(src):
            return [src[r]["dur"] for r in sorted(src) if "dur" in src[r]]
        va, vb = rate(ra), rate(rb)
        ma, mb = med(va), med(vb)
        sp = lambda v: f"{min(v):,.0f}~{max(v):,.0f}" if v else "-"
        print(f"| **口径B 全任务速率**（语料÷duration） | {' / '.join(fmt(v) for v in va)} | {fmt(ma)} | "
              f"{' / '.join(fmt(v) for v in vb)} | {fmt(mb)} | {pct(ma, mb)} | {sp(va)} / {sp(vb)} |")
        print(f"| 写入任务总时长 s（暖机占比看这里） | {' / '.join(fmt(v, 1) for v in secs(ra))} | - | "
              f"{' / '.join(fmt(v, 1) for v in secs(rb))} | - | - | - |")


def query_rows(data, a, b, track):
    """查段指标行（文本表与 --json 共用一份口径）。

    → [{task, a_tput, b_tput, delta_tput, a_p50, b_p50, delta_p50, a_p99, b_p99, delta_p99}...]，
    delta 均为 B 相对 A 的百分比（None = 缺一侧测量）。
    """
    ra, rb = data.get((a, track), {}), data.get((b, track), {})
    tasks = sorted(({t for r in ra.values() if "q" in r for (k, t) in r["q"] if k == "Median Throughput"} &
                    {t for r in rb.values() if "q" in r for (k, t) in r["q"] if k == "Median Throughput"})
                   - {"index-append"})
    tasks = [t for t in tasks if not t.startswith(("index-update", "upsert-", "refresh", "force-merge", "wait-", "delete-", "create-"))]
    if not tasks:
        return []

    def m(src, task, metric):
        vs = [src[r]["q"].get((metric, task)) for r in sorted(src) if "q" in src[r]]
        return med(vs)
    rows = []
    for t in tasks:
        ea, eb = m(ra, t, "Median Throughput"), m(rb, t, "Median Throughput")
        e50, b50 = m(ra, t, "50th percentile service time"), m(rb, t, "50th percentile service time")
        e99, b99 = m(ra, t, "99th percentile service time"), m(rb, t, "99th percentile service time")
        rows.append({"task": t,
                     "a_tput": ea, "b_tput": eb, "delta_tput": delta_pct(ea, eb),
                     "a_p50": e50, "b_p50": b50, "delta_p50": delta_pct(e50, b50),
                     "a_p99": e99, "b_p99": b99, "delta_p99": delta_pct(e99, b99)})
    return rows


def query_table(data, a, b, track):
    rows = query_rows(data, a, b, track)
    if not rows:
        return
    print(f"\n## {track} 查询 {len(rows)} 个操作（有效测量中位；时延 ms；Δ 为 {b} 相对 {a}）\n")
    print(f"| 操作 | {a} ops/s | {b} ops/s | Δ吞吐 | {a} p50 | {b} p50 | Δp50 | {a} p99 | {b} p99 | Δp99 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        d50 = "  n/a" if row["delta_p50"] is None else f"{row['delta_p50']:+6.1f}%"
        d99 = "  n/a" if row["delta_p99"] is None else f"{row['delta_p99']:+6.1f}%"
        print(f"| {row['task']} | {fmt(row['a_tput'],1)} | {fmt(row['b_tput'],1)} | {pct(row['a_tput'], row['b_tput'])} | "
              f"{fmt(row['a_p50'],1)} | {fmt(row['b_p50'],1)} | {d50} | "
              f"{fmt(row['a_p99'],1)} | {fmt(row['b_p99'],1)} | {d99} |")


def load_thresholds(path):
    """断言文件：每行 CSV —— track,task,metric,cmp,value（cmp: >= | <=；# 注释）。
    例: geo,index-append,Median Throughput,>=,120000
        geo,term,50th percentile service time,<=,8
    断言对象 = B（contender）的逐轮中位。"""
    rules = []
    with open(path, encoding="utf-8", newline="") as f:
        for number, parts in enumerate(csv.reader(f), 1):
            if not parts or not any(p.strip() for p in parts) or parts[0].lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in parts]
            if len(parts) != 5 or not all(parts) or parts[3] not in (">=", "<="):
                raise ValueError("断言第%d行格式错误（track,task,metric,>=|<=,value）" % number)
            try:
                threshold = float(parts[4])
            except ValueError:
                raise ValueError("断言第%d行阈值不是数字" % number) from None
            if not math.isfinite(threshold):
                raise ValueError("断言第%d行阈值必须有限" % number)
            rules.append((*parts[:4], threshold))
    if not rules:
        raise ValueError("断言文件没有有效规则")
    return rules


def run_asserts(data, b, rules):
    """对 B 车逐规则判中位；返回失败数。吞吐越高越好(>=)，延迟越低越好(<=)。"""
    fails = 0
    print(f"\n## 断言（对象 B={b} 的中位）")
    for track, task, metric, cmp_op, thr in rules:
        rounds = data.get((b, track), {})
        vals = [rounds[r][side].get((metric, task))
                for r in sorted(rounds) for side in ("w", "q") if side in rounds[r]]
        vals = [v for v in vals if finite(v)]
        m = med(vals)
        ok = len(vals) >= 3 and m is not None and ((cmp_op == ">=" and m >= thr) or (cmp_op == "<=" and m <= thr))
        shown = f"{m:,.1f}" if m is not None else "无数据"
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  [{mark}] {track}/{task} {metric}: {shown} {cmp_op} {thr:,.0f}（n={len([v for v in vals if v is not None])}）")
    return fails


def json_payload(data, a, b, tracks, camps, rules):
    """--json 模式的结构化载荷（数字与文本表同源：write_rows / query_rows）。"""
    def d2(v):
        return None if v is None else round(v, 2)
    tracks_out = []
    for track in sorted(tracks):
        pool = {}
        for side in ("A", "B"):
            rounds = data[(side, track)]
            ws = [r for r in rounds if "w" in rounds[r]]
            pool[side] = {"rounds": len(ws), "camps": sorted({rounds[r]["camp"] for r in ws})}
        tracks_out.append({
            "track": track,
            "series_a": next(iter(data[("A", track)].values()))["series_key"],
            "series_b": next(iter(data[("B", track)].values()))["series_key"],
            "write": [{k: (d2(v) if k == "delta_pct" else v) for k, v in row.items()
                       if k not in ("a_rounds", "b_rounds")} for row in write_rows(data, "A", "B", track)],
            "query": [{k: (d2(v) if k.startswith("delta_") else v) for k, v in row.items()}
                      for row in query_rows(data, "A", "B", track)],
            "pool": pool})
    payload = {"a": a, "b": b,
               "campaigns": [os.path.basename(c) for c in camps],
               "tracks": tracks_out}
    if rules is not None:
        payload["asserts"] = {"fails": run_asserts(data, "B", rules), "rules": rules}
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaigns", nargs="*")
    ap.add_argument("--a", default="kona25")
    ap.add_argument("--b", default="jdk25")
    ap.add_argument("--a-campaign", action="append", help="A 侧取数目录，可重复")
    ap.add_argument("--b-campaign", action="append", help="B 侧取数目录，可重复")
    ap.add_argument("--assert", dest="thresholds")
    ap.add_argument("--json", action="store_true",
                    help="输出结构化 JSON（控制台表格用；stdout 只含 JSON，诊断走 stderr）")
    args = ap.parse_args()
    a, b = args.a, args.b
    pool_a, pool_b = args.a_campaign or args.campaigns, args.b_campaign or args.campaigns
    if not pool_a or not pool_b:
        ap.error("两侧均需 campaign 目录")
    if a == b and {os.path.realpath(c) for c in pool_a} == {os.path.realpath(c) for c in pool_b}:
        print("同一车、同一取数池没有可比性", file=sys.stderr)
        return 1
    try:
        rules = load_thresholds(args.thresholds) if args.thresholds else None
        left, right = load(pool_a), load(pool_b)
        data = {("A", t): rows for (car, t), rows in left.items() if car == a}
        data.update({("B", t): rows for (car, t), rows in right.items() if car == b})
    except (OSError, ValueError) as e:
        print("对比失败: %s" % e, file=sys.stderr)
        return 1
    tracks_a = {t for car, t in data if car == "A"}
    tracks_b = {t for car, t in data if car == "B"}
    if not tracks_a or tracks_a != tracks_b:
        print("基线与候选没有相同的完整有效赛道集合", file=sys.stderr)
        return 1
    for track in tracks_a:
        task_sets = [{task for slot in data[(side, track)].values()
                      for phase in ("w", "q") for metric, task in slot[phase]}
                     for side in ("A", "B")]
        if task_sets[0] != task_sets[1]:
            print("%s 两侧任务集合不一致" % track, file=sys.stderr)
            return 1
    camps = list(dict.fromkeys(pool_a + pool_b))
    if args.json:
        payload = json_payload(data, a, b, tracks_a, camps, rules)
        print(json.dumps(payload, ensure_ascii=False))
        if rules is not None:
            return 1 if payload["asserts"]["fails"] else 0
        return 0
    print(f"对比: A={a}  B={b}  campaign={', '.join(os.path.basename(c) for c in camps)}")
    for track in sorted(tracks_a):
        print("  身份: A=%s  B=%s" % (next(iter(data[("A", track)].values()))["series_key"],
                                     next(iter(data[("B", track)].values()))["series_key"]))
        write_table(data, "A", "B", track, "+".join(os.path.basename(c) for c in camps))
        query_table(data, "A", "B", track)
    # 合池提示：同一车在多 campaign 的轮数
    for (car, track), rounds in sorted(data.items()):
        ws = [r for r in rounds if "w" in rounds[r]]
        print(f"  池: car={car} track={track} 有效轮={len(ws)} 来自 {[rounds[r]['camp'] for r in sorted(ws)]}")
    if rules is not None:
        fails = run_asserts(data, "B", rules)
        print(f"\n断言结果: {'全部通过' if fails == 0 else f'{fails} 条 FAIL'}")
        return 1 if fails else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
