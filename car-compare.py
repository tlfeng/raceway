#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""car-compare.py —— 两车配对对比（读 rally markdown 报告，支持多 campaign 合池）

用法（无本地 python，在压测机上跑）:
  ssh root@<压测机> 'python3 - <campaigns 目录>' < scripts/car-compare.py < campaign 目录... --a kona25 --b jdk25

说明: A 车（--a）与 B 车（--b）按 campaign×track 分组，给出逐轮实测、中位、差百分比，
      并附每车轮次间极差（dispersion）——本环境 5.5 倍超分，极差是判"差异是否可信"的第一道证据。
"""
import glob
import json
import os
import re
import statistics
import sys

ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(\S.*?)\s*\|\s*([-\d.eE+]+)\s*\|")
FID = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-r(\d+)(?:-try\d+)?-([wq])\.md$")
W_KEYS = ["Median Throughput", "Max Throughput", "50th percentile service time",
          "90th percentile service time", "99th percentile service time", "100th percentile service time"]


DOCS = {"geo": 11396503, "hnsw": 2000000, "nested": 11203029}   # 各赛道语料条数（口径B 用）


def race_duration(path_md):
    """同名 -w-race.json / -wq-race.json 里 index-append 的 duration(ms)；读不到返回 None
    （路径公式按扩展名截断，<fid>-w.md 与 <fid>-wq.md 都落成正确的 race.json 名）"""
    p = path_md[:-3] + "-race.json"
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (ValueError, OSError):
        return None
    for m in (d.get("results") or {}).get("op_metrics") or []:
        if m.get("task") == "index-append" and m.get("duration"):
            return m["duration"] / 1000.0
    return None


def parse_md(path):
    d = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        m = ROW.match(line.strip())
        if m and m.group(2):
            d.setdefault((m.group(1), m.group(2)), float(m.group(3)))
    return d


def load(camps):
    """→ {(car, track): {轮标签: {'w': metrics, 'q': metrics, 'camp': name}}}
       轮标签含 campaign 短名，避免多 campaign 合池时同名轮号互相覆盖。"""
    out = {}
    for camp in camps:
        short = os.path.basename(camp)[-15:]
        for p in sorted(glob.glob(os.path.join(camp, "results", "*.md"))):
            m = FID.match(os.path.basename(p))
            if not m:
                continue
            car, track, rnd, side = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            tm = re.search(r"try(\d+)", os.path.basename(p))
            key = "%s-r%d%s" % (short, rnd, ("-try" + tm.group(1)) if tm else "")
            slot = out.setdefault((car, track), {}).setdefault(key, {"camp": os.path.basename(camp)})
            if side in slot and "-try" not in os.path.basename(p):
                continue
            slot[side] = parse_md(p)
            if side == "w":
                dur = race_duration(p)
                if dur:
                    slot["dur"] = dur
    return out


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def pct(a, b):
    return "  n/a" if not a or not b else f"{(b / a - 1) * 100:+6.1f}%"


def fmt(v, dec=1):
    return "     -" if v is None else (f"{v:,.{dec}f}")


def write_table(data, a, b, track, camps_label):
    ra = data.get((a, track), {})
    rb = data.get((b, track), {})
    if not ra or not rb:
        return
    print(f"\n## {track} 写入（{camps_label}）\n")
    print(f"| 指标 | {a} 逐轮 | {a} 中位 | {b} 逐轮 | {b} 中位 | Δ(B/A) | 轮间极差 A / B |")
    print("|---|---|---|---|---|---|---|")
    for key in W_KEYS:
        va = [ra[r]["w"].get((key, "index-append")) for r in sorted(ra) if "w" in ra[r]]
        vb = [rb[r]["w"].get((key, "index-append")) for r in sorted(rb) if "w" in rb[r]]
        va = [v for v in va if v is not None]
        vb = [v for v in vb if v is not None]
        ma, mb = med(va), med(vb)
        dec = 0 if "Throughput" in key else 1
        spread_a = f"{min(va):,.0f}~{max(va):,.0f}" if va and "Throughput" in key else \
                   (f"{min(va):,.0f}~{max(va):,.0f}" if va else "-")
        spread_b = f"{min(vb):,.0f}~{max(vb):,.0f}" if vb and "Throughput" in key else \
                   (f"{min(vb):,.0f}~{max(vb):,.0f}" if vb else "-")
        print(f"| {key} | {' / '.join(fmt(v, dec) for v in va)} | {fmt(ma, dec)} | "
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


def query_table(data, a, b, track):
    ra, rb = data.get((a, track), {}), data.get((b, track), {})
    tasks = sorted({t for r in ra.values() if "q" in r for (k, t) in r["q"] if k == "Median Throughput"} &
                   {t for r in rb.values() if "q" in r for (k, t) in r["q"] if k == "Median Throughput"})
    if not tasks:
        return
    print(f"\n## {track} 查询 {len(tasks)} 个操作（3 轮中位；Δ 为 {b} 相对 {a}；耗时越低越好）\n")
    print(f"| 操作 | {a} ops/s | {b} ops/s | Δ吞吐 | {a} p50 | {b} p50 | Δp50 | {a} p99 | {b} p99 | Δp99 |")
    print("|---|---|---|---|---|---|---|---|---|---|")

    def m(src, task, metric):
        vs = [src[r]["q"].get((metric, task)) for r in sorted(src) if "q" in src[r]]
        return med(vs)
    for t in tasks:
        ea, eb = m(ra, t, "Median Throughput"), m(rb, t, "Median Throughput")
        e50, b50 = m(ra, t, "50th percentile service time"), m(rb, t, "50th percentile service time")
        e99, b99 = m(ra, t, "99th percentile service time"), m(rb, t, "99th percentile service time")
        d50 = "  n/a" if not e50 or not b50 else f"{(b50 / e50 - 1) * 100:+6.1f}%"
        d99 = "  n/a" if not e99 or not b99 else f"{(b99 / e99 - 1) * 100:+6.1f}%"
        print(f"| {t} | {fmt(ea,1)} | {fmt(eb,1)} | {pct(ea, eb)} | {fmt(e50,1)} | {fmt(b50,1)} | {d50} | "
              f"{fmt(e99,1)} | {fmt(b99,1)} | {d99} |")


def load_thresholds(path):
    """断言文件：每行 CSV —— track,task,metric,cmp,value（cmp: >= | <=；# 注释）。
    例: geo,index-append,Median Throughput,>=,120000
        geo,term,50th percentile service time,<=,8
    断言对象 = B（contender）的逐轮中位。"""
    rules = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) != 5 or parts[3] not in (">=", "<="):
            print(f"  断言行格式错误（应为 track,task,metric,>=|<=,value）: {ln}")
            continue
        rules.append((parts[0], parts[1], parts[2], parts[3], float(parts[4])))
    return rules


def run_asserts(data, b, rules):
    """对 B 车逐规则判中位；返回失败数。吞吐越高越好(>=)，延迟越低越好(<=)。"""
    fails = 0
    print(f"\n## 断言（对象 B={b} 的中位）")
    for track, task, metric, cmp_op, thr in rules:
        rounds = data.get((b, track), {})
        side = "w" if task == "index-append" else "q"
        vals = [rounds[r][side].get((metric, task)) for r in sorted(rounds) if side in rounds[r]]
        m = med(vals)
        ok = m is not None and ((cmp_op == ">=" and m >= thr) or (cmp_op == "<=" and m <= thr))
        shown = f"{m:,.1f}" if m is not None else "无数据"
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  [{mark}] {track}/{task} {metric}: {shown} {cmp_op} {thr:,.0f}（n={len([v for v in vals if v is not None])}）")
    return fails


def main():
    args = [a for a in sys.argv[1:]]
    def take(flag):
        v = args[args.index(flag) + 1] if flag in args else None
        if flag in args:
            del args[args.index(flag):args.index(flag) + 2]
        return v
    a = take("--a") or "kona25"
    b = take("--b") or "jdk25"
    thr_path = take("--assert")
    camps = args
    if not camps:
        print("用法: car-compare.py <campaign 目录...> --a CAR --b CAR [--assert thresholds.conf]")
        return 1
    data = load(camps)
    print(f"对比: A={a}  B={b}  campaign={', '.join(os.path.basename(c) for c in camps)}")
    for track in sorted({t for (_, t) in data}):
        write_table(data, a, b, track, "+".join(os.path.basename(c)[-15:] for c in camps))
        query_table(data, a, b, track)
    # 合池提示：同一车在多 campaign 的轮数
    for (car, track), rounds in sorted(data.items()):
        ws = [r for r in rounds if "w" in rounds[r]]
        print(f"  池: car={car} track={track} 有效轮={len(ws)} 来自 {[rounds[r]['camp'] for r in sorted(ws)]}")
    if thr_path:
        rules = load_thresholds(thr_path)
        if rules:
            fails = run_asserts(data, b, rules)
            print(f"\n断言结果: {'全部通过' if fails == 0 else f'{fails} 条 FAIL'}")
            return 1 if fails else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
